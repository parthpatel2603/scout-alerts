#!/usr/bin/env python3
"""
Scout — autonomous listing monitor.
Runs free on GitHub Actions. Each pass it:
  1. scrapes every ACTIVE for-sale home in the target cities
  2. scrapes recently SOLD homes (real comps -> real medians)
  3. tracks price drops, sold, and off-market changes on everything it's seen
  4. emails new matches + price drops (6h rule / 50-listing rule)
  5. publishes watch.json + market.json so Scout can show live status on the Pack

No paid services: Gmail SMTP + free GitHub Actions + GitHub Pages.
"""
import os, json, smtplib, ssl, statistics, datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from homeharvest import scrape_property

# ---------------- BUYER PROFILE ----------------
PRICE_CAP   = 790_000          # a little over $750K to catch negotiables
BEDS_MIN    = 3
BATHS_MIN   = 2
RADIUS_MI   = 7
SOLD_DAYS   = 60               # window for comps / medians

# city -> [wife mins to Placentia, Parth mins to Monterey Park, fallback $/sqft]
CITIES = {
    "Brea, CA":[10,30,600], "La Habra, CA":[15,28,520], "Diamond Bar, CA":[22,30,560],
    "Rowland Heights, CA":[22,25,520], "Hacienda Heights, CA":[25,22,560],
    "Walnut, CA":[25,28,600], "Chino Hills, CA":[28,38,520], "Pomona, CA":[32,35,470],
    "Glendora, CA":[38,35,520], "Azusa, CA":[40,33,500], "Claremont, CA":[40,42,520],
    "Montclair, CA":[42,45,470],
}
RED_FLAG_WORDS = ["wire","western union","overseas","abroad","no showing",
                  "cash only deposit","moved out of state","zelle deposit"]

STATE_FILE  = "seen.json"
WATCH_FILE  = "watch.json"     # published for Scout
MARKET_FILE = "market.json"    # published for Scout
EMAIL_EVERY_HOURS = 6
BACKLOG_TRIGGER   = 50

SCOUT_URL = (os.environ.get("SCOUT_URL") or "").strip() or \
    "https://parthpatel2603.github.io/scout-alerts/"

# ---------------- STATE ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        s = json.load(open(STATE_FILE))
        s.setdefault("tracked", {})     # url -> {price, first_price, first_seen, last_seen, status, ...}
        s.setdefault("backlog", [])
        s.setdefault("drops", [])
        s.setdefault("last_email", None)
        # migrate from the old flat "seen" list
        for u in s.pop("seen", []) or []:
            s["tracked"].setdefault(u, {"price":0,"first_price":0,
                "first_seen":dt.date.today().isoformat(),"last_seen":dt.date.today().isoformat(),
                "status":"active"})
        return s
    return {"tracked":{}, "backlog":[], "drops":[], "last_email":None}

def save_state(s):
    json.dump(s, open(STATE_FILE,"w"), indent=1)

# ---------------- HELPERS ----------------
def days_on_market(row):
    d = row.get("list_date")
    if not d: return None
    try:
        d = str(d)[:10]
        return (dt.date.today() - dt.date.fromisoformat(d)).days
    except Exception:
        return None

def passes(row):
    price = row.get("list_price") or 0
    beds  = row.get("beds") or 0
    baths = (row.get("full_baths") or 0)+(row.get("half_baths") or 0)*.5
    style = str(row.get("style") or "").upper()
    if price<=0 or price>PRICE_CAP: return False
    if beds<BEDS_MIN or baths<BATHS_MIN: return False
    if any(x in style for x in ["CONDO","TOWNHOME","APARTMENT"]): return False
    return True

# ---------------- SCORING (mirrors Scout exactly) ----------------
def fit_score(row, city_key):
    got = poss = 0
    def add(state, wt):
        nonlocal got, poss
        poss += wt
        if state=='pass': got += wt
        elif state=='warn': got += wt*0.5
    price = row.get("list_price") or 1e9
    beds  = row.get("beds") or 0
    baths = (row.get("full_baths") or 0)+(row.get("half_baths") or 0)*.5
    hoa   = row.get("hoa_fee") or 0
    yr    = row.get("year_built") or 0
    wife, parth, _ = CITIES[city_key]
    sf, lot = row.get("sqft") or 0, row.get("lot_sqft") or 0
    ratio = lot/sf if sf else 0
    add('pass' if price<=750_000 else ('warn' if price<=790_000 else 'miss'), 14)
    add('pass' if hoa==0 else 'miss', 10)
    add('pass' if beds>=BEDS_MIN else 'miss', 8)
    add('pass' if baths>=BATHS_MIN else 'miss', 6)
    add('pass' if wife<=25 else ('warn' if wife<=50 else 'miss'), 10)
    add('pass' if parth<=45 else 'miss', 6)
    add('pass' if yr>=2005 else ('warn' if yr>=1985 else 'miss'), 5)
    add('pass' if ratio>=4 else ('warn' if ratio>=3 else 'miss'), 5)
    return round(100*got/poss) if poss else 0

def trust_score(row, median_ppsf):
    t, reasons = 100, []
    price, sf = row.get("list_price") or 0, row.get("sqft") or 0
    ppsf = price/sf if sf else 0
    if ppsf and ppsf < median_ppsf*0.6:
        t-=35; reasons.append(f"⚠ ${int(ppsf)}/sqft vs ~${median_ppsf} area — far-below-market bait")
    else:
        reasons.append("✓ Price/sqft in line with the area")
    if not (row.get("mls_id") or row.get("mls")):
        t-=20; reasons.append("⚠ No MLS id — verify the source")
    else:
        reasons.append("✓ MLS-sourced")
    photos = str(row.get("alt_photos") or "")
    n = len([x for x in photos.split(",") if x.strip()])
    if n < 5: t-=10; reasons.append(f"⚠ Only {n} photos")
    text = str(row.get("text") or "").lower()
    if any(w in text for w in RED_FLAG_WORDS):
        t-=30; reasons.append("⚠ Money-first / no-showing language")
    return max(0,min(100,t)), reasons

# ---------------- SCRAPE ----------------
def scrape(city, kind, past=None):
    try:
        kw = dict(location=city, listing_type=kind, radius=RADIUS_MI)
        if past: kw["past_days"] = past
        df = scrape_property(**kw)
        return [] if df is None or df.empty else [r.to_dict() for _,r in df.iterrows()]
    except Exception as e:
        print(f"   {kind} scrape failed for {city}: {e}")
        return []

def gather():
    active, sold = {}, {}
    for city, meta in CITIES.items():
        print(f"  {city}…")
        for row in scrape(city, "for_sale"):
            u = row.get("property_url")
            if not u or not passes(row): continue
            row["_city_key"], row["_median"] = city, meta[2]
            row["_wife"], row["_parth"] = meta[0], meta[1]
            active[u] = row
        for row in scrape(city, "sold", past=SOLD_DAYS):
            u = row.get("property_url")
            if not u: continue
            row["_city_key"] = city
            sold[u] = row
    return active, sold

# ---------------- MARKET MEDIANS (real, from sold) ----------------
def medians(sold):
    out = {}
    for city in CITIES:
        vals = []
        for r in sold.values():
            if r.get("_city_key") != city: continue
            p = r.get("sold_price") or r.get("last_sold_price") or r.get("list_price") or 0
            sf = r.get("sqft") or 0
            if p>0 and sf>300: vals.append(p/sf)
        name = city.replace(", CA","")
        if len(vals) >= 5:
            out[name] = {"ppsf": round(statistics.median(vals)), "n": len(vals)}
        else:
            out[name] = {"ppsf": CITIES[city][2], "n": 0}   # fall back to my estimate
    return out

# ---------------- EMAIL ----------------
def scout_link(row):
    from urllib.parse import urlencode, quote
    city = row["_city_key"].replace(", CA","")
    addr = f'{row.get("street","")}, {row.get("city","")}, CA {row.get("zip_code","")}'.strip(", ")
    p = {"addr":addr,"city":city,"price":int(row.get("list_price") or 0),
         "beds":int(row.get("beds") or 0),"baths":int(row.get("full_baths") or 0),
         "sqft":int(row.get("sqft") or 0),"lot":int(row.get("lot_sqft") or 0),
         "year":int(row.get("year_built") or 0),"hoa":int(row.get("hoa_fee") or 0),
         "url":row.get("property_url") or ""}
    base = SCOUT_URL if SCOUT_URL.endswith("/") else SCOUT_URL+"/"
    # query string, not #fragment: mail clients rewrite links and can drop fragments
    return base + "?" + urlencode(p, quote_via=quote)

def card(row, drop=None):
    fit = fit_score(row, row["_city_key"])
    trust, reasons = trust_score(row, row["_median"])
    fcol = "#3E8B5A" if fit>=75 else "#C9902F" if fit>=55 else "#C6543B"
    tcol = "#3E8B5A" if trust>=80 else "#C9902F" if trust>=55 else "#C6543B"
    addr = f'{row.get("street","")}, {row.get("city","")} {row.get("zip_code","")}'
    photo = row.get("primary_photo") or ""
    url = row.get("property_url") or "#"
    price = row.get("list_price") or 0
    dom = days_on_market(row)
    rlist = "".join(f"<div style='font-size:12px;color:#555'>{x}</div>" for x in reasons)
    img = f"<img src='{photo}' width='250' style='border-radius:6px;display:block'>" if photo else ""

    banner = ""
    if drop:
        old, cut = drop
        pct = (cut/old*100) if old else 0
        banner = (f"<div style='background:#C6543B;color:#fff;padding:7px 10px;border-radius:6px;"
                  f"font-size:13px;font-weight:700;margin-bottom:8px'>🔻 PRICE DROP — "
                  f"was ${old:,.0f}, cut ${cut:,.0f} ({pct:.1f}%)</div>")
    domtag = ""
    if dom is not None:
        dcol = "#C6543B" if dom>=60 else "#C9902F" if dom>=30 else "#63706A"
        note = " — seller's getting tired" if dom>=60 else ""
        domtag = (f"<span style='font-size:12px;color:{dcol};font-weight:600'>"
                  f"⏱ {dom} days on market{note}</span><br>")

    return f"""
    <table style="border:1px solid #ddd;border-radius:8px;margin:0 0 16px;width:100%;
                  border-collapse:separate;background:#fff"><tr>
      <td style="padding:14px" width="260">{img}</td>
      <td style="padding:14px;vertical-align:top">
        {banner}
        <div style="font-size:20px;font-weight:700;color:#14312B;font-family:monospace">${price:,.0f}</div>
        <div style="font-size:14px;color:#14312B;font-weight:600">{addr}</div>
        <div style="font-size:12px;color:#666;margin:4px 0">
          {int(row.get('beds') or 0)} bd · {(row.get('full_baths') or 0)} ba ·
          {int(row.get('sqft') or 0):,} sqft · lot {int(row.get('lot_sqft') or 0):,} ·
          built {int(row.get('year_built') or 0)} ·
          wife {row['_wife']}m / you {row['_parth']}m
        </div>
        {domtag}
        <span style="background:{fcol};color:#fff;border-radius:3px;padding:2px 8px;font-size:12px">Fit {fit}/100</span>
        <span style="background:{tcol};color:#fff;border-radius:3px;padding:2px 8px;font-size:12px">Trust {trust}/100</span>
        <div style="margin:8px 0 4px">{rlist}</div>
        <a href="{scout_link(row)}" style="display:inline-block;background:#14312B;color:#EBD9B6;
           text-decoration:none;padding:9px 14px;border-radius:8px;font-size:13px;font-weight:700;
           margin:8px 8px 4px 0">🐶 Score in Scout</a>
        <a href="{url}" style="font-size:13px;color:#C9902F;font-weight:600">View listing &amp; all photos →</a>
      </td></tr></table>"""

def send_email(new_rows, drop_rows):
    user = os.environ["GMAIL_USER"]; pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("ALERT_TO", user)
    new_rows  = sorted(new_rows,  key=lambda r: fit_score(r, r["_city_key"]), reverse=True)
    drop_rows = sorted(drop_rows, key=lambda d: d[1][1], reverse=True)  # biggest cut first

    parts = []
    if drop_rows:
        parts.append("<h3 style='color:#C6543B;margin:18px 0 8px'>🔻 Price drops — motivated sellers</h3>")
        parts += [card(r, d) for r,d in drop_rows]
    if new_rows:
        parts.append("<h3 style='color:#14312B;margin:18px 0 8px'>🆕 New matches</h3>")
        parts += [card(r) for r in new_rows]

    n = len(new_rows)+len(drop_rows)
    body = f"""<div style="font-family:Arial,sans-serif;max-width:640px;margin:auto">
      <h2 style="color:#14312B">🐶 Scout — {n} update{'s' if n!=1 else ''}</h2>
      <p style="color:#666;font-size:13px">Best-fit first. Price drops mean a seller who's already blinked.
      Reminder: at closing, confirm wire instructions by phone on a known number — that's where real fraud happens.</p>
      {''.join(parts)}
      <p style="color:#999;font-size:11px">Auto-sent by your GitHub Actions monitor.</p></div>"""
    msg = MIMEMultipart("alternative")
    bits = []
    if drop_rows: bits.append(f"{len(drop_rows)} price drop{'s' if len(drop_rows)!=1 else ''}")
    if new_rows:  bits.append(f"{len(new_rows)} new")
    msg["Subject"] = "🐶 Scout — " + " · ".join(bits)
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(body,"html"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465,context=ssl.create_default_context()) as s:
        s.login(user,pw); s.sendmail(user,to.split(","),msg.as_string())
    print(f"Emailed {n} updates to {to}")

# ---------------- MAIN ----------------
def main():
    s = load_state()
    tracked = s["tracked"]
    today = dt.date.today().isoformat()

    print("Scraping active + sold…")
    active, sold = gather()
    print(f"  {len(active)} active matches, {len(sold)} sold in last {SOLD_DAYS}d")

    new_rows, drop_rows = [], []

    # --- active listings: new, price drops, still-alive ---
    for u, row in active.items():
        price = row.get("list_price") or 0
        rec = tracked.get(u)
        if not rec:
            tracked[u] = {"price":price,"first_price":price,"first_seen":today,
                          "last_seen":today,"status":"active","dom":days_on_market(row)}
            new_rows.append(row)
        else:
            old = rec.get("price") or 0
            if old and price < old - 999:          # ignore noise under $1k
                drop_rows.append((row,(old, old-price)))
                rec["dropped"] = today
            rec["price"]=price; rec["last_seen"]=today; rec["status"]="active"
            rec["dom"]=days_on_market(row)
            rec.setdefault("first_price", old or price)

    # --- sold: mark anything we were tracking ---
    for u, row in sold.items():
        rec = tracked.get(u)
        if rec:
            rec["status"]="sold"; rec["last_seen"]=today
            sp = row.get("sold_price") or row.get("last_sold_price") or 0
            if sp: rec["sold_price"]=int(sp)

    # --- vanished from both = pending or delisted ---
    for u, rec in tracked.items():
        if rec.get("status")=="active" and rec.get("last_seen")!=today:
            rec["status"]="off_market"

    # --- publish for Scout (free, via GitHub Pages) ---
    watch = {u:{k:v for k,v in rec.items() if k in
             ("price","first_price","status","sold_price","dom","dropped","first_seen")}
             for u,rec in tracked.items()}
    json.dump({"updated":today,"listings":watch}, open(WATCH_FILE,"w"), indent=1)
    mk = medians(sold)
    json.dump({"updated":today,"window_days":SOLD_DAYS,"cities":mk}, open(MARKET_FILE,"w"), indent=1)
    real = sum(1 for v in mk.values() if v["n"])
    print(f"  medians from real sold data for {real}/{len(mk)} cities")

    # --- queue + email rules ---
    s["backlog"].extend(new_rows)
    s["drops"].extend([[r,d] for r,d in drop_rows])
    now = dt.datetime.utcnow()
    last = dt.datetime.fromisoformat(s["last_email"]) if s["last_email"] else None
    hours = (now-last).total_seconds()/3600 if last else 999
    queued = len(s["backlog"])+len(s["drops"])
    due = queued >= BACKLOG_TRIGGER or hours >= EMAIL_EVERY_HOURS

    if queued and due:
        send_email(s["backlog"], [(r,tuple(d)) for r,d in s["drops"]])
        s["backlog"]=[]; s["drops"]=[]; s["last_email"]=now.isoformat()
    else:
        print(f"Holding: {queued} queued, {hours:.1f}h since last email")

    save_state(s)

if __name__ == "__main__":
    main()
