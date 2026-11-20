#!/usr/bin/env python3
"""Home station's Ambient history plus URMA (AWS GRIB files, free) for the night hours of chosen windows, added to log/downloaded.json (resumable).
  python season_pull.py 2026-02-01 2026-02-28 [...more START END pairs]"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone, date
import coldpool as c

c.load_env()
cfg = c.load_config()
f = c.LOG / "downloaded.json"
cache = json.loads(f.read_text()) if f.exists() else {"obs": {}, "mod": {}, "rtma": {}}
a = sys.argv[1:]
t0 = time.time()
for d0, d1 in zip(a[::2], a[1::2]):
    day, stop = date.fromisoformat(d0), date.fromisoformat(d1)
    while day <= stop:
        base = datetime(day.year, day.month, day.day, 13, tzinfo=timezone.utc)
        hours = [h for h in (base + timedelta(hours=i) for i in range(24)) if c.night_factor(h, cfg["lat"], cfg["lon"]) >= 1.0]
        need_mod = [h for h in hours if h.isoformat() not in cache["mod"]]
        need_obs = [h for h in hours if h.isoformat() not in cache["obs"]]
        if need_obs and not os.environ.get("SKIP_OBS"):          # SKIP_OBS=1: the station's hours come from log/ambient_all.db instead
            for k, v in c.ambient_history(base, base + timedelta(hours=24)).items():
                cache["obs"][k.isoformat()] = v
        got = 0
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(4) as ex:                          # four hours of the night at once
            for h, r in zip(need_mod, ex.map(lambda h: c.grib_urma_hour(cfg, h), need_mod)):
                if r:
                    cache["mod"][h.isoformat()] = r
                    got += 1
        tmp = f.with_suffix(".tmp"); tmp.write_text(json.dumps(cache)); tmp.replace(f)          # atomic: a stop in the middle cannot leave a half-written file
        print(f"night of {day}: {got}/{len(need_mod)} URMA hours, obs {'yes' if need_obs else 'cached'}  ({time.time() - t0:.0f} s)", flush=True)
        day += timedelta(days=1)
print("done")
