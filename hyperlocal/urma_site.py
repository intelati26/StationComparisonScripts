#!/usr/bin/env python3
"""URMA (temperature F, cloud %, wind mph) at one point for the night hours of 1 Oct - 18 Nov, from the AWS GRIB files (free): log/urma_<name>.json, resumable.
  python urma_site.py NAME LAT LON
"""
import json, sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import coldpool as c

name, lat, lon = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
c.load_env()
cfg = {"lat": lat, "lon": lon}
f = c.LOG / f"urma_{name}.json"
have = json.loads(f.read_text()) if f.exists() else {}
t = datetime(2026, 10, 1, tzinfo=timezone.utc)
hours = []
while t < datetime(2026, 11, 19, 14, tzinfo=timezone.utc):
    if c.night_factor(t, lat, lon) >= 1.0 and t.isoformat() not in have:
        hours.append(t)
    t += timedelta(hours=1)
print(f"{len(hours)} night hours to fetch ({len(have)} done)", flush=True)


def one(h):
    time.sleep(0.2)
    return h, c.grib_urma_hour(cfg, h)


n = 0
with ThreadPoolExecutor(3) as ex:
    for h, v in ex.map(one, hours):
        if v:
            have[h.isoformat()] = list(v)
        n += 1
        if n % 40 == 0:
            f.write_text(json.dumps(have))
            print(f"{n}/{len(hours)}", flush=True)
f.write_text(json.dumps(have))
print("done", len(have), flush=True)
