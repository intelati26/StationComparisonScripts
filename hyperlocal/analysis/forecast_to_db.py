#!/usr/bin/env python3
"""Tonight's adjusted forecast into the plotter's database, so plotter.py draws it (dashed, with the +- SD band and a star at the predicted low).
Runs termux/low.py --json (the same place, amplitude, bias and SD as the notification) and writes one model_analysis row per hour: model "coldpool_fc", temp_f = the forecast temperature minus the equation's drop (plus the bias), temp_sd = the SD of the predicted low.
A new run replaces the rows of earlier runs from its first hour on; earlier hours keep the run that was current then, so the evening's forecast stays on the plot against what the station did.
  python analysis/forecast_to_db.py [--db FILE] [low.py options]       the database is WEATHER_DB_PATH from .env unless --db is given; it must already have the plotter's model_analysis table
  python analysis/forecast_to_db.py --from-log [FILE] [--db FILE]       writes every run low.py --log has kept (FILE, else HYPERLOCAL_LOG, else log/low_log.jsonl), oldest first: the previous nights' forecasts for the plotter to hold against what the station did"""
import json, os, sqlite3, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
import coldpool as c

c.load_env(); cfg = c.load_config(); root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
args = sys.argv[1:]; db = None
if "--db" in args:
    i = args.index("--db"); db = args[i + 1]; del args[i:i + 2]
db = db or cfg["db"]
if not os.path.isabs(db): db = os.path.join(root, db)
if not os.path.exists(db): sys.exit(f"no database at {db} (set WEATHER_DB_PATH in .env or give --db)")
def write(con, d):
    rows = []
    for h in d["hours"]:
        t = datetime.fromisoformat(h["utc"].replace("Z", "+00:00")).astimezone(timezone.utc)
        rows.append((t.isoformat(), round(h["final"] + d["bias"], 2), d["sd"] or None, h["cloud"]))
    con.execute("DELETE FROM model_analysis WHERE model='coldpool_fc' AND latitude=? AND longitude=? AND valid_time >= ?", (cfg["lat"], cfg["lon"], rows[0][0]))
    con.executemany("INSERT OR REPLACE INTO model_analysis (model, valid_time, latitude, longitude, temp_f, temp_sd, cloud_cover) VALUES ('coldpool_fc', ?, ?, ?, ?, ?, ?)",
                    [(t, cfg["lat"], cfg["lon"], f, sd, cl) for t, f, sd, cl in rows])
    return rows


con = sqlite3.connect(db)
if not con.execute("SELECT 1 FROM sqlite_master WHERE name='model_analysis'").fetchone(): sys.exit("that database has no model_analysis table (the plotter's)")
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
