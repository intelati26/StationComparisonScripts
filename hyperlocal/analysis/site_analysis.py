#!/usr/bin/env python3
"""Second-place analysis (run with the venv python: numpy). python site_analysis.py NAME
Per WU station: mean (station temperature - URMA at the place's centre) over the clear, calm night hours, against the small-scale terrain indices."""
import json, os, statistics as st, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c
import terrain_local as TL

name = sys.argv[1]
c.load_env()
center = json.load(open(c.LOG / f"wu_nearby_{name}.json"))["center"]
wu = json.load(open(c.LOG / f"wu_hourly_{name}.json"))
urma = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / f"urma_{name}.json")).items()}
hours = {}
for sid, s in wu.items():
    h = {}
    for k, v in s["hours"].items():
        if v[0] is not None:
            h[datetime.fromisoformat(k.replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0)] = v
    hours[sid] = h
nights = {}
for t in urma:
    nights.setdefault((t - timedelta(hours=13)).date(), []).append(t)
good = []
for d, ts in nights.items():
    if len(ts) < 8:
        continue
    cloud = st.mean(urma[t][1] for t in ts)
    winds = [hours[s][t][2] for s in hours for t in ts if t in hours[s] and hours[s][t][2] is not None]
    if winds and cloud < 20 and st.mean(winds) < 1.0:
        good.append(d)
print(f"centre {center}: {len(nights)} nights with URMA, {len(good)} clear and calm (URMA cloud < 20%, the stations' mean wind < 1 mph)")
cc = [t for d in good for t in nights[d]]
res = []
for sid, s in wu.items():
    x = [hours[sid][t][0] - urma[t][0] for t in cc if t in hours[sid]]
    if len(x) < 40:
        print(f"  {sid}: only {len(x)} hours, left out")
        continue
    ix = TL.indices(s["lat"], s["lon"])
    res.append((sid, s["km"], st.mean(x), len(x), ix))
keys = ["elev_ft", "tpi_250_ft", "tpi_1000_ft", "above_min_500_ft", "rel_250", "rel_500", "upslope_ha", "chan_m"]
print(f"{'station':<13}{'km':>5}{'vs URMA':>9}{'hrs':>5}" + "".join(f"{k.replace('_ft', '')[:12]:>13}" for k in keys))
for sid, km, m, n, ix in sorted(res, key=lambda r: r[2]):
    print(f"{sid:<13}{km:>5.1f}{m:>+8.1f}F{n:>5}" + "".join(f"{(ix[k] if ix[k] is not None else float('nan')):>13.1f}" for k in keys))


def corr(a, b):
    ma, mb = st.mean(a), st.mean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) * st.pstdev(a) * st.pstdev(b))


print(f"\ncorrelation of the night temperature (vs URMA) with each index, {len(res)} stations (the two fixed in advance first):")
for k in ["chan_m", "rel_500"] + [k for k in keys if k not in ("chan_m", "rel_500")]:
    v = [ix[k] for *_, ix in res]
    if all(a is not None for a in v):
        print(f"  {k:<18} {corr([r[2] for r in res], v):+.2f}")
print(f"\nspread between stations: {max(r[2] for r in res) - min(r[2] for r in res):.1f}F (coldest {min(res, key=lambda r: r[2])[0]}, warmest {max(res, key=lambda r: r[2])[0]})")
