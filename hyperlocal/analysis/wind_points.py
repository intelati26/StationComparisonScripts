#!/usr/bin/env python3
"""Does the wind aloft at each station's own (one-decimal) point change the equation, against one wind per place (its centre)? Autumn window, the stations of the four non-home places.
   python wind_points.py"""
import json, math, os, statistics as st, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

cfg = c.load_config()
import places
PL = {k: v for k, v in places.load(cfg).items() if k != "home"}
CENTERS = {k: (v["lat"], v["lon"]) for k, v in PL.items()}
D0, D1 = "2026-10-01", "2026-11-19"
cache = {}


def w450(la, lo):
    k = (round(la, 1), round(lo, 1))
    if k not in cache:
        f = c.LOG / f"w450pt_{k[0]}_{k[1]}.json"                      # kept per point, so a rerun does not ask again
        if f.exists():
            cache[k] = {datetime.fromisoformat(a): v for a, v in json.load(open(f)).items()}
            return cache[k]
        import time
        for attempt in range(8):                                       # Open-Meteo's hourly limit: wait and ask again
            try:
                ground, HR = c.hrrr_levels(k[0], k[1], D0, D1)
                break
            except SystemExit:
                print("Open-Meteo busy, waiting 10 min", flush=True)
                time.sleep(600)
        else:
            raise SystemExit("Open-Meteo did not answer")
        cache[k] = {t: w for t, row in HR.items() for w in [c.wind_agl(row, ground, 450.0)] if w is not None}
        json.dump({t.isoformat(): v for t, v in cache[k].items()}, open(f, "w"))
        print("HRRR", k, len(cache[k]), flush=True)
    return cache[k]


def factor(u, td, w):
    return math.exp(-(w / 30.0) ** 3) * (1 - 0.8 * u[1] / 100) * min(1.0, max(0.0, u[0] - td) / 12.0)


def run(pts):
    xs = [p[3] for p in pts]; ys = [p[1] - p[2] for p in pts]
    mx, my = st.mean(xs), st.mean(ys); den = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    n = {}
    for t, u, s_, f in pts:
        n.setdefault((t - timedelta(hours=13)).date(), []).append((u, s_, f))
    n = [v for v in n.values() if len(v) >= 9]
    e = lambda amp, a=0.0: st.mean(abs(min(u - a - amp * f for u, s_, f in v) - min(s_ for u, s_, f in v)) for v in n)
    rmse_h = math.sqrt(st.mean((y - (my - b * mx) - b * x) ** 2 for x, y in zip(xs, ys)))
    return b, e(b, my - b * mx), rmse_h


rows = []
for place, (la0, lo0) in CENTERS.items():
    suf = "_" + PL[place]["tag"]
    S = json.load(open(c.LOG / f"wu_hourly{suf}.json"))
    U = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / f"urma_{PL[place]['tag']}.json")).items()}
    Wc = w450(la0, lo0)
    for sid, s in S.items():
        hrs = {datetime.fromisoformat(k.replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0): v for k, v in s["hours"].items() if v[0] is not None and v[1] is not None}
        Ws = w450(s["lat"], s["lon"])
        res = []
        for W in (Wc, Ws):
            pts = [(t, U[t][0], v[0], factor(U[t], v[1], W[t])) for t, v in hrs.items() if t in U and t in W and c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0]
            res.append(run(pts) if len(pts) >= 150 else None)
        if all(res):
            rows.append((place, sid, (round(s["lat"], 1), round(s["lon"], 1)) != (round(la0, 1), round(lo0, 1)), res))
diff = [r for r in rows if r[2]]
print(f"\n{len(rows)} stations, {len(diff)} of them at a different one-decimal point than the place's centre")
print("                      amplitude b            nightly-low MAE            hourly rmse")
print("place      station    centre   own point    centre   own point         centre   own point")
for p, sid, moved, (a, b) in rows:
    if moved:
        print(f"{p:<10} {sid[-4:]:<8}{a[0]:>7.1f}{b[0]:>10.1f}{a[1]:>11.2f}{b[1]:>10.2f}{a[2]:>16.2f}{b[2]:>9.2f}")
for label, sel in (("all", rows), ("moved only", diff)):
    if sel:
        print(f"{label:<11} mean: b {st.mean(r[3][0][0] for r in sel):.2f} -> {st.mean(r[3][1][0] for r in sel):.2f};  night MAE {st.mean(r[3][0][1] for r in sel):.3f} -> {st.mean(r[3][1][1] for r in sel):.3f};  hourly rmse {st.mean(r[3][0][2] for r in sel):.3f} -> {st.mean(r[3][1][2] for r in sel):.3f}")
