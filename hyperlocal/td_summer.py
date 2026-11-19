#!/usr/bin/env python3
"""URMA dewpoint minus the station's, summer nights (about every 5th night, 5 Jul - 30 Aug 2026):  python td_summer.py"""
import json, os, statistics as st, sys, time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coldpool as c

c.load_env()
cfg = c.load_config()
f = c.LOG / "td_summer.json"
have = json.loads(f.read_text()) if f.exists() else {"td": {}, "obs": {}}
days = [datetime(2026, 7, 5, tzinfo=timezone.utc) + timedelta(days=5 * i) for i in range(11)]
hours = []
for day in days:
    t = day + timedelta(hours=12)
    while t < day + timedelta(hours=40):
        if c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0 and t.isoformat() not in have["td"]:
            hours.append(t)
        t += timedelta(hours=1)
print(len(hours), "URMA dewpoint hours", flush=True)
with ThreadPoolExecutor(3) as ex:
    for h, v in ex.map(lambda h: (h, c.grib_urma_hour(cfg, h, c.GRIB_DPT)), hours):
        if v is not None:
            have["td"][h.isoformat()] = v
f.write_text(json.dumps(have))
for day in days:
    if (day + timedelta(hours=12)).isoformat() in have["obs"]:
        continue
    for k, v in c.ambient_history(day + timedelta(hours=12), day + timedelta(hours=40)).items():
        have["obs"][k.isoformat()] = v
    f.write_text(json.dumps(have))
d = [(k, have["td"][k] - have["obs"][k][2]) for k in have["td"] if k in have["obs"] and have["obs"][k][2] is not None]
print(f"{len(d)} summer night hours: URMA dewpoint minus the station's: mean {st.mean(x for _, x in d):+.1f}F, sd {st.pstdev([x for _, x in d]):.1f}")
bym = {}
for k, x in d:
    bym.setdefault(k[:7], []).append(x)
print("  by month:", {m: f"{st.mean(v):+.1f} (n={len(v)})" for m, v in bym.items()})
