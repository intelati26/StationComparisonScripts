#!/usr/bin/env python3
"""Open-Meteo's HRRR history (stitched, analysis-like) at the place, month by month: log/om_home_YYYY-MM.json {hour(UTC): [T2m F, dewpoint 2m F, cloud %, wind at 450 m mph, 10 m wind mph]}.
This is the stream termux/low.py runs on, so it is what the tool would have had in hindsight.   python om_history.py [FIRST LAST]  (months, default 2025-02 2026-11)   resumable"""
import json, os, sys, time, calendar
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
import coldpool as c

c.load_env(); cfg = c.load_config()
first, last = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("2025-02", "2026-11")
y, m = map(int, first.split("-")); ye, me = map(int, last.split("-"))
while (y, m) <= (ye, me):
    f = c.LOG / f"om_home_{y}-{m:02d}.json"
    if not f.exists():
        d1 = calendar.monthrange(y, m)[1]
        if (y, m) == (2026, 11):
            d1 = 20
        for attempt in range(8):
            try:
                ground, HR = c.hrrr_levels(cfg["lat"], cfg["lon"], f"{y}-{m:02d}-01", f"{y}-{m:02d}-{d1:02d}", extra=("temperature_2m", "dew_point_2m", "cloud_cover")); break
            except SystemExit:
                print("Open-Meteo busy, waiting 10 min", flush=True); time.sleep(600)
        else:
            sys.exit("Open-Meteo kept failing")
        out = {}
        for t, r in HR.items():
            w = c.wind_agl(r, ground, 450.0)
            if None not in (r.get("temperature_2m"), r.get("dew_point_2m"), r.get("cloud_cover"), w):
                out[t.isoformat()] = [r["temperature_2m"], r["dew_point_2m"], r["cloud_cover"], w, r["w10"]]
        f.write_text(json.dumps(out)); print(f"{y}-{m:02d}", len(out), "hours", flush=True)
    m += 1
    if m > 12:
        y, m = y + 1, 1
print("done", flush=True)
