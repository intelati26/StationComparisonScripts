#!/usr/bin/env python3
"""Everything the Ambient API still holds for the home station (all fields, every 5-minute record, valid ones: a temperature in range), newest first, into log/ambient_all.db (git-ignored). Resumable; a rerun adds what is new.
   python ambient_all.py"""
import json, os, sqlite3, sys, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
import coldpool as c

c.load_env()
api, app, mac = (os.environ.get(k) for k in ("AMBIENT_API_KEY", "AMBIENT_APP_KEY", "AMBIENT_STATION_MAC"))
if not (api and app and mac):
    sys.exit("AMBIENT_API_KEY, AMBIENT_APP_KEY and AMBIENT_STATION_MAC must be set")
db = sqlite3.connect(c.LOG / "ambient_all.db")
db.execute("CREATE TABLE IF NOT EXISTS ambient (utc TEXT PRIMARY KEY, tempf REAL, dewpoint REAL, humidity REAL, windspeedmph REAL, windgustmph REAL, winddir REAL, solarradiation REAL, baromrelin REAL, hourlyrainin REAL, raw TEXT)")
have_new = db.execute("SELECT max(utc) FROM ambient").fetchone()[0]          # a rerun stops once a page holds only records already stored
lo0 = db.execute("SELECT min(utc) FROM ambient").fetchone()[0]                # carry on below what is stored (the newest part was pulled first)
start = datetime.fromisoformat(lo0.replace("Z", "+00:00")) - timedelta(seconds=1) if lo0 else datetime.now(timezone.utc)
until, bad, pages, empties = start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), 0, 0, 0
t0 = time.time()
while True:
    url = f"https://rt.ambientweather.net/v1/devices/{mac}?apiKey={urllib.parse.quote(api)}&applicationKey={urllib.parse.quote(app)}&endDate={until}&limit=288"
    time.sleep(1.2)
    recs = c.get_json(url)
    for wait in (3, 6, 12, 30, 60):                      # an empty page after quick requests is a throttle: wait and repeat; a few real empties in a row mean the end of the history
        if recs:
            break
        time.sleep(wait)
        recs = c.get_json(url)
    if not recs or not isinstance(recs, list):                 # a gap in the station's record: jump a day back and look again, until a month in a row is empty
        empties += 1
        if empties > 30:
            print("no more records (a month of nothing: the end of the history)", flush=True)
            break
        until = (datetime.strptime(until, "%Y-%m-%dT%H:%M:%S.000Z") - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        continue
    empties = 0
    new = 0
    for r in recs:
        t = datetime.fromtimestamp(r["dateutc"] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tf = r.get("tempf")
        if tf is None or not (-60 <= tf <= 140):
            bad += 1
            continue
        cur = db.execute("INSERT OR IGNORE INTO ambient VALUES (?,?,?,?,?,?,?,?,?,?,?)", (t, tf, r.get("dewPoint"), r.get("humidity"), r.get("windspeedmph"), r.get("windgustmph"), r.get("winddir"), r.get("solarradiation"), r.get("baromrelin"), r.get("hourlyrainin"), json.dumps(r)))
        new += cur.rowcount
    db.commit()
    pages += 1
    oldest = min(r["dateutc"] for r in recs)
    until = (datetime.fromtimestamp(oldest / 1000, timezone.utc) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if pages % 10 == 0:
        n, lo = db.execute("SELECT count(*), min(utc) FROM ambient").fetchone()
        print(f"{pages} pages, {n} records, back to {lo}  ({time.time() - t0:.0f} s)", flush=True)
n, lo, hi = db.execute("SELECT count(*), min(utc), max(utc) FROM ambient").fetchone()
print(f"done: {n} records {lo} -> {hi}; {bad} invalid dropped", flush=True)
