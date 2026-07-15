#!/usr/bin/env python3
"""
Pocket Agent — autonomous listing monitor for Parth.
Runs on GitHub Actions. Scrapes for-sale homes across the target cities,
filters to the buyer profile, scores fit + scam-trust, and emails a digest
either every 6 hours OR the moment 50 new listings have piled up — whichever
comes first. No paid services; Gmail SMTP + free GitHub Actions minutes only.
"""
import os, json, smtplib, ssl, datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# HomeHarvest = open-source Realtor.com scraper (MLS-sourced = low fraud risk)
from homeharvest import scrape_property

# ---------------- BUYER PROFILE ----------------
PRICE_CAP   = 790_000          # a touch above $750K to catch negotiables
BEDS_MIN    = 3
BATHS_MIN   = 2
RADIUS_MI   = 7                # ~6-7 mile ring around each city
NO_HOA_PREF = True

# city -> [wife minutes to Placentia, Parth minutes to Monterey Park, median $/sqft]
CITIES = {
    "Brea, CA":[10,30,600], "La Habra, CA":[15,28,520], "Diamond Bar, CA":[22,30,560],
    "Rowland Heights, CA":[22,25,520], "Hacienda Heights, CA":[25,22,560],
    "Walnut, CA":[25,28,600], "Chino Hills, CA":[28,38,520], "Pomona, CA":[32,35,470],
    "Glendora, CA":[38,35,520], "Azusa, CA":[40,33,500], "Claremont, CA":[40,42,520],
    "Montclair, CA":[42,45,470],
}
RED_FLAG_WORDS = ["wire","western union","overseas","abroad","no showing",
                  "cash only deposit","moved out of state","zelle deposit"]

STATE_FILE = "seen.json"
EMAIL_EVERY_HOURS = 6
BACKLOG_TRIGGER   = 50

# ---------------- STATE ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"seen": [], "backlog": [], "last_email": None}

def save_state(s):
    json.dump(s, open(STATE_FILE,"w"), indent=2)

# ---------------- SCORING ----------------
def fit_score(row, city_key):
    w,p,g = 0,0,[]
    def add(ok, wt): 
        nonlocal w,p
        p+=wt; w+= wt if ok else 0
    price = row.get("list_price") or 1e9
    add(price<=750_000, 14)
    add((row.get("beds") or 0)>=BEDS_MIN, 8)
    add(((row.get("full_baths") or 0)+(row.get("half_baths") or 0)*.5)>=BATHS_MIN, 6)
    add((row.get("hoa_fee") or 0)==0, 10)
    yr = row.get("year_built") or 0
    add(yr>=2005, 5)
    wife,parth,_ = CITIES[city_key]
    add(wife<=50, 10); add(parth<=45, 6)
    sf, lot = row.get("sqft") or 0, row.get("lot_sqft") or 0
    add(sf>0 and lot/sf>=4, 6)
    return round(100*w/p) if p else 0

def trust_score(row, median_ppsf):
    t, reasons = 100, []
    price, sf = row.get("list_price") or 0, row.get("sqft") or 0
    ppsf = price/sf if sf else 0
    if ppsf and ppsf < median_ppsf*0.6:
        t-=35; reasons.append(f"⚠ ${int(ppsf)}/sqft vs ~${median_ppsf} area — far-below-market bait")
    else:
        reasons.append("✓ Price/sqft in line with the area")
    # MLS presence is the strongest legitimacy signal
    if not (row.get("mls_id") or row.get("mls")):
        t-=20; reasons.append("⚠ No MLS id — verify the source")
    else:
        reasons.append("✓ MLS-sourced")
    photos = row.get("alt_photos") or ""
    n_photos = len([x for x in str(photos).split(",") if x.strip()])
    if n_photos < 5:
        t-=10; reasons.append(f"⚠ Only {n_photos} photos")
    text = (str(row.get("text") or "")).lower()
    if any(wf in text for wf in RED_FLAG_WORDS):
        t-=30; reasons.append("⚠ Money-first / no-showing language in description")
    return max(0,min(100,t)), reasons

# ---------------- SCRAPE ----------------
def gather():
    found = []
    for city, meta in CITIES.items():
        try:
            df = scrape_property(location=city, listing_type="for_sale",
                                 past_days=3, radius=RADIUS_MI)
        except Exception as e:
            print(f"  scrape failed for {city}: {e}"); continue
        if df is None or df.empty: 
            continue
        for _, r in df.iterrows():
            row = r.to_dict()
            price = row.get("list_price") or 0
            beds  = row.get("beds") or 0
            baths = (row.get("full_baths") or 0)+(row.get("half_baths") or 0)*.5
            style = str(row.get("style") or "").upper()
            if price<=0 or price>PRICE_CAP: continue
            if beds<BEDS_MIN or baths<BATHS_MIN: continue
            if any(x in style for x in ["CONDO","TOWNHOME","APARTMENT"]): continue
            row["_city_key"], row["_median"] = city, meta[2]
            row["_wife"], row["_parth"] = meta[0], meta[1]
            found.append(row)
    return found

# ---------------- EMAIL ----------------
def card(row):
    fit = fit_score(row, row["_city_key"])
    trust, reasons = trust_score(row, row["_median"])
    tcolor = "#3E6B4F" if trust>=80 else "#A9772F" if trust>=55 else "#B24A34"
    fcolor = "#3E6B4F" if fit>=75 else "#A9772F" if fit>=55 else "#B24A34"
    addr = f'{row.get("street","")}, {row.get("city","")} {row.get("zip_code","")}'
    photo = row.get("primary_photo") or ""
    url = row.get("property_url") or "#"
    price = row.get("list_price") or 0
    rlist = "".join(f"<div style='font-size:12px;color:#555'>{x}</div>" for x in reasons)
    img = f"<img src='{photo}' width='260' style='border-radius:6px;display:block'>" if photo else ""
    return f"""
    <table style="border:1px solid #ddd;border-radius:8px;margin:0 0 16px;width:100%;
                  border-collapse:separate;background:#fff"><tr>
      <td style="padding:14px" width="270">{img}</td>
      <td style="padding:14px;vertical-align:top">
        <div style="font-size:20px;font-weight:700;color:#14312B;font-family:monospace">${price:,.0f}</div>
        <div style="font-size:14px;color:#14312B;font-weight:600">{addr}</div>
        <div style="font-size:12px;color:#666;margin:4px 0">
          {int(row.get('beds') or 0)} bd · {(row.get('full_baths') or 0)} ba ·
          {int(row.get('sqft') or 0):,} sqft · lot {int(row.get('lot_sqft') or 0):,} ·
          built {int(row.get('year_built') or 0)} ·
          wife {row['_wife']}m / you {row['_parth']}m
        </div>
        <span style="background:{fcolor};color:#fff;border-radius:3px;padding:2px 8px;font-size:12px">Fit {fit}/100</span>
        <span style="background:{tcolor};color:#fff;border-radius:3px;padding:2px 8px;font-size:12px">Trust {trust}/100</span>
        <div style="margin:8px 0 4px">{rlist}</div>
        <a href="{url}" style="font-size:13px;color:#A9772F;font-weight:600">View listing & all photos →</a>
      </td></tr></table>"""

def send_email(rows):
    user = os.environ["GMAIL_USER"]; pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("ALERT_TO", user)
    rows = sorted(rows, key=lambda r: fit_score(r, r["_city_key"]), reverse=True)
    body = f"""<div style="font-family:Arial,sans-serif;max-width:640px;margin:auto">
      <h2 style="color:#14312B">🏡 Pocket Agent — {len(rows)} new matches</h2>
      <p style="color:#666;font-size:13px">Sorted best-fit first. Prices, beds/baths and commute
      minutes are shown per home. Reminder: at closing, always confirm wire instructions
      by phone on a known number — that's where real fraud happens.</p>
      {''.join(card(r) for r in rows)}
      <p style="color:#999;font-size:11px">Auto-sent by your GitHub Actions monitor.</p></div>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏡 {len(rows)} new homes matching your profile"
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(body,"html"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465,context=ssl.create_default_context()) as srv:
        srv.login(user,pw); srv.sendmail(user,to.split(","),msg.as_string())
    print(f"Emailed {len(rows)} listings to {to}")

# ---------------- MAIN ----------------
def main():
    s = load_state()
    seen = set(s["seen"])
    print("Scraping...")
    fresh = [r for r in gather() if r.get("property_url") not in seen]
    print(f"  {len(fresh)} genuinely new listings")

    # add to backlog (store the minimal fields we need to rebuild cards)
    s["backlog"].extend(fresh)
    for r in fresh:
        seen.add(r.get("property_url"))
    s["seen"] = list(seen)

    # decide whether to email now
    now = dt.datetime.utcnow()
    last = dt.datetime.fromisoformat(s["last_email"]) if s["last_email"] else None
    hours = (now-last).total_seconds()/3600 if last else 999
    due = (len(s["backlog"]) >= BACKLOG_TRIGGER) or (hours >= EMAIL_EVERY_HOURS)

    if s["backlog"] and due:
        send_email(s["backlog"])
        s["backlog"] = []
        s["last_email"] = now.isoformat()
    else:
        print(f"Holding: backlog={len(s['backlog'])}, {hours:.1f}h since last email")

    save_state(s)

if __name__ == "__main__":
    main()
