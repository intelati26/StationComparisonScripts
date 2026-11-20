#!/usr/bin/env python3
"""Tonight's adjusted forecast into the plotter's database, so plotter.py draws it (dashed, with the +- SD band and a star at the predicted low).
Runs termux/low.py --json (the same place, amplitude, bias and SD as the notification) and writes one model_analysis row per hour: model "coldpool_fc", temp_f = the forecast temperature minus the equation's drop (plus the bias), temp_sd = the SD of the predicted low.
A new run replaces the rows of earlier runs from its first hour on; earlier hours keep the run that was current then, so the evening's forecast stays on the plot against what the station did.
  python forecast_to_db.py [--db FILE] [low.py options]       the database is the collectors' and plotter's own: WEATHER_DB_PATH from .env (a relative path is relative to where you run this) unless --db is given; it must already have the model_analysis table, and only model coldpool_fc / coldpool_bf rows are touched. Needs WEATHER_LAT and WEATHER_LON (.env in the folder you run from, or hyperlocal/.env); HYPERLOCAL_AMP / _BIAS / _SD are optional
  python forecast_to_db.py --backfill N [--db FILE]            the last N days reconstructed in hindsight from Open-Meteo's stitched HRRR hours (model "coldpool_bf", drawn dotted): a first multi-day plot before any forecasts have been kept; hours that already hold a real run are skipped
  python forecast_to_db.py --from-log [FILE] [--db FILE]       writes every run low.py --log has kept (FILE, else HYPERLOCAL_LOG, else log/low_log.jsonl), oldest first: the previous nights' forecasts for the plotter to hold against what the station did"""
import json, os, sqlite3, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone
import coldpool as c

c.load_env(); root = os.path.dirname(os.path.abspath(__file__))
for k in ("WEATHER_LAT", "WEATHER_LON"):
    if not os.environ.get(k): sys.exit(f"{k} is not set (the .env of the collectors or of hyperlocal)")
cfg = {"lat": float(os.environ["WEATHER_LAT"]), "lon": float(os.environ["WEATHER_LON"]), "db": os.environ.get("WEATHER_DB_PATH", "weather_archive.db")}      # only what this script uses: no valley/rim elevations needed
args = sys.argv[1:]; db = None
if "--db" in args:
    i = args.index("--db"); db = args[i + 1]; del args[i:i + 2]
db = os.path.abspath(db or cfg["db"])                    # a relative WEATHER_DB_PATH means relative to where you run this, as for the collectors and plotter.py: run it from the folder you run them from
if not os.path.exists(db): sys.exit(f"no database at {db} (set WEATHER_DB_PATH in .env, run this from the folder the collectors run in, or give --db)")
print(f"database: {db}")
def write(con, d):
    rows = []
    for h in d["hours"]:
        t = datetime.fromisoformat(h["utc"].replace("Z", "+00:00")).astimezone(timezone.utc)
        rows.append((t.isoformat(), round(h["final"] + d["bias"], 2), d["sd"] or None, h["cloud"]))
    con.execute("DELETE FROM model_analysis WHERE model='coldpool_fc' AND latitude=? AND longitude=? AND valid_time >= ?", (cfg["lat"], cfg["lon"], rows[0][0]))
    con.executemany("INSERT OR REPLACE INTO model_analysis (model, valid_time, latitude, longitude, temp_f, temp_sd, cloud_cover) VALUES ('coldpool_fc', ?, ?, ?, ?, ?, ?)",
                    [(t, cfg["lat"], cfg["lon"], f, sd, cl) for t, f, sd, cl in rows])
    return rows


con = sqlite3.connect(db, timeout=60)                    # a collector may be writing at the same time
if not con.execute("SELECT 1 FROM sqlite_master WHERE name='model_analysis'").fetchone(): sys.exit("that database has no model_analysis table (the plotter's)")
if "--backfill" in args:
    import math, time, urllib.parse, urllib.request
    from datetime import timedelta
    i = args.index("--backfill"); days = int(args[i + 1]); LEV = (1000, 975, 950, 925, 900, 850, 800, 700)
    q = {"latitude": round(cfg["lat"], 1), "longitude": round(cfg["lon"], 1), "models": "ncep_hrrr_conus", "timezone": "UTC", "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "past_days": days + 1, "forecast_days": 1,
         "hourly": "temperature_2m,dew_point_2m,cloud_cover,wind_speed_10m," + ",".join(f"wind_speed_{l}hPa,geopotential_height_{l}hPa" for l in LEV)}
    for attempt in range(4):
        try: om = json.loads(urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q), timeout=90).read()); break
        except Exception as e: om = None; time.sleep(4 * (attempt + 1))
    if om is None: sys.exit("Open-Meteo did not answer")
    H, ground = om["hourly"], om.get("elevation"); amp = float(os.environ.get("HYPERLOCAL_AMP") or 5.0); bias = float(os.environ.get("HYPERLOCAL_BIAS") or 0.0); sd = float(os.environ.get("HYPERLOCAL_SD") or 2.8)
    have = {r[0] for r in con.execute("SELECT valid_time FROM model_analysis WHERE model='coldpool_fc' AND latitude=? AND longitude=?", (cfg["lat"], cfg["lon"]))}
    now = datetime.now(timezone.utc); out = []
    for k, ts in enumerate(H["time"]):
        t = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        nf = c.night_factor(t, cfg["lat"], cfg["lon"])
        if t >= now - timedelta(hours=1) or nf <= 0 or t.isoformat() in have or any(H[v][k] is None for v in ("temperature_2m", "dew_point_2m", "cloud_cover")): continue
        row = {"w10": H["wind_speed_10m"][k]}
        for l in LEV: row[f"w{l}"] = H[f"wind_speed_{l}hPa"][k]; row[f"z{l}"] = H[f"geopotential_height_{l}hPa"][k]
        u = c.wind_agl(row, ground, 450.0)
        if u is None: continue
        adj = 1.0 if 6 <= t.month <= 8 else 3.0 if 9 <= t.month <= 11 else 2.0                      # low.py's seasonal dewpoint adjustment
        T, td, cl = H["temperature_2m"][k], H["dew_point_2m"][k] - adj, H["cloud_cover"][k]
        drop = amp * nf * math.exp(-(u / 30.0) ** 3) * (1 - 0.8 * cl / 100) * min(1.0, max(0.0, T - td) / 12.0)
        out.append((t.isoformat(), round(T - drop + bias, 2), sd or None, cl))
    if not out: sys.exit("nothing to backfill")
    con.execute("DELETE FROM model_analysis WHERE model='coldpool_bf' AND latitude=? AND longitude=? AND valid_time >= ?", (cfg["lat"], cfg["lon"], out[0][0]))
    con.executemany("INSERT OR REPLACE INTO model_analysis (model, valid_time, latitude, longitude, temp_f, temp_sd, cloud_cover) VALUES ('coldpool_bf', ?, ?, ?, ?, ?, ?)", [(t, cfg["lat"], cfg["lon"], f, sd_, cl) for t, f, sd_, cl in out])
    con.commit(); con.close(); print(f"{len(out)} night hours backfilled ({out[0][0][:16]} to {out[-1][0][:16]} UTC), amplitude {amp:g}, bias {bias:+g}, from Open-Meteo's stitched HRRR hours (hindsight, not forecasts as issued) -> {os.path.basename(db)}"); sys.exit()
if "--from-log" in args:
    i = args.index("--from-log"); nxt = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else None
    path = os.path.expanduser(nxt or os.environ.get("HYPERLOCAL_LOG") or os.path.join(root, "log", "low_log.jsonl"))
    runs = sorted((json.loads(l) for l in open(path) if l.strip()), key=lambda d: d["run_utc"])
    for d in runs:
        rows = write(con, d); print(f"run {d['run_utc']}: {len(rows)} hours from {rows[0][0][:16]} UTC, predicted low {d['low']:.0f} F +-{(d['sd'] or 0):g}")
    con.commit(); con.close(); print(f"{len(runs)} runs -> {os.path.basename(db)}"); sys.exit()
r = subprocess.run([sys.executable, os.path.join(root, "termux", "low.py"), "--json"] + args, capture_output=True, text=True)
if r.returncode: sys.exit(r.stderr.strip() or "low.py failed")
d = json.loads(r.stdout)
rows = write(con, d)
con.commit(); con.close()
print(f"{len(rows)} hours written ({rows[0][0][:16]} to {rows[-1][0][:16]} UTC); predicted low {d['low']:.0f} F +-{d['sd']:g} (forecast {d['forecast_low']:.0f} F) -> {os.path.basename(db)}")
