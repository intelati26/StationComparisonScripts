#!/usr/bin/env python3
"""Control for the dusk/dawn gust step: do other OPEN airports (IEM ASOS) lose their gusts to the home airport's at sunset the way the sheltered station does?  Ratio of peak gust (max of gust and wind, mph) by hour from sunset / sunrise at the home place.  log/<id>_asos_*.csv.   python analysis/open_control.py"""
import csv, glob, os, sys
from datetime import datetime, timezone, timedelta
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c
cfg = c.load_config(); HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); K = 1.15078
def load(prefix):
    d = {}
    for f in glob.glob(os.path.join(HERE, "log", f"{prefix}_asos_*.csv")):
        for r in csv.DictReader(open(f)):
            try: t = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(minute=0, tzinfo=timezone.utc)
            except ValueError: continue
            if r["sknt"] in ("M", ""): continue
            s = float(r["sknt"]) * K; g = float(r["gust"]) * K if r["gust"] not in ("M", "") else s
            d[t] = max(d.get(t, 0.0), s, g)
    return d
import places
AP = places.airport(); HOMEAP = load(AP); ST = {}
import sqlite3
for t, g in sqlite3.connect(os.path.join(HERE, "log", "ambient_all.db")).execute("SELECT utc, windgustmph FROM ambient WHERE windgustmph IS NOT NULL"):
    h = datetime.fromisoformat(t.replace("Z", "+00:00")).replace(minute=0, second=0); ST[h] = max(ST.get(h, 0), g)
SERIES = {"your station": ST}
for p in places.other_airports():
    d = load(p)
    if len(d) > 2000: SERIES[p.upper() + " (open airport)"] = d
_s = {}
def sun(d):
    if d not in _s: _s[d] = c.sun_times(cfg["lat"], cfg["lon"], d)
    return _s[d]
def off(t, which):
    return min(((t + timedelta(minutes=30) - sun(d)[which]).total_seconds() / 3600 for d in (t.date() - timedelta(days=1), t.date(), t.date() + timedelta(days=1))), key=abs)
for which, nm in ((1, "SUNSET"), (0, "SUNRISE")):
    print(f"\n{nm}: ratio of each series' peak gust to the home airport's, by hour from the event (n per bin ~ days)")
    print(f"{'':<22}" + "".join(f"{k:>+7d}h" for k in range(-4, 5)))
    for name, d in SERIES.items():
        row = []
        for k in range(-4, 5):
            q = [(d[t], HOMEAP[t]) for t in d if t in HOMEAP and k - 0.5 <= off(t, which) < k + 0.5]
            row.append(f"{sum(a for a, b in q)/sum(b for a, b in q):>8.2f}" if len(q) >= 30 else f"{'-':>8}")
        print(f"{name:<22}" + "".join(row))
