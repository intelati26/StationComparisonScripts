#!/usr/bin/env python3
"""The wind term of the equation, looked at on its own:  python wind_check.py   (needs log/hrrr_all.json for the HRRR winds; see the scratchpad pull)
 1 the response curve straight from the data (deficit divided by what the other terms predict, by wind bin)
 2 other shapes for the curve, scored leave-one-night-out
 3 which wind, and whether recent wind (the pool eroding) beats the current hour"""
import json, math, os, sys, statistics as st
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

cfg = c.load_config()
obs, mod = c.load_db(cfg)
c.merge_cache(obs, mod)
hr = json.load(open(os.environ["HRRR_JSON"]))["hourly"]
H = {datetime.fromisoformat(t).replace(tzinfo=timezone.utc): i for i, t in enumerate(hr["time"])}
D = 1 + max(0.0, cfg["rim_elevation_ft"] - cfg["valley_elevation_ft"]) / 3000
CW, DS = 0.8, 12.0                           # cloud weight and dewpoint scale as fitted; only the wind term and the maximum are refitted here


def hrrr(name, k):
    i = H.get(k)
    return hr[name][i] if i is not None else None


rows = []
for k in sorted(set(obs) & set(mod)):
    if c.night_factor(k, cfg["lat"], cfg["lon"]) >= 1.0 and len(obs[k]) > 2 and obs[k][2] is not None and hrrr("wind_speed_925hPa", k) is not None:
        prev = lambda name, n: max([hrrr(name, k - timedelta(hours=j)) for j in range(0, n + 1) if hrrr(name, k - timedelta(hours=j)) is not None])
        w = {"URMA 10 m": mod[k][2], "HRRR 10 m": hrrr("wind_speed_10m", k), "HRRR 925 hPa": hrrr("wind_speed_925hPa", k),
             "URMA 10 m, max of 3 h": max([mod[k - timedelta(hours=j)][2] for j in range(0, 3) if (k - timedelta(hours=j)) in mod] or [mod[k][2]]),
             "HRRR 925 hPa, max of 3 h": prev("wind_speed_925hPa", 2), "HRRR 925 hPa, max of 6 h": prev("wind_speed_925hPa", 5)}
        base = D * (1 - CW * mod[k][1] / 100) * min(1.0, max(0.0, mod[k][0] - obs[k][2]) / DS)         # everything but the wind term and the maximum drop
        rows.append(dict(t=k, y=mod[k][0] - obs[k][0], base=base, w=w, night=(k - timedelta(hours=13)).date(), cloud=mod[k][1], spread=mod[k][0] - obs[k][2]))
n = len(rows)
nights = sorted({r["night"] for r in rows})
print(f"{n} night hours, {len(nights)} nights")

# 1: the response curve from the data
good = [r for r in rows if r["cloud"] < 30 and r["spread"] >= 5]
MD = 9.0
print(f"\n1. response curve: deficit / ({MD:.0f} x the other terms), clear hours with spread >= 5 F ({len(good)} hours); 1 = no wind effect, 0 = none of the cooling left")
for name, bins in (("URMA 10 m", [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 12), (12, 15), (15, 40)]), ("HRRR 925 hPa", [(0, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 90)])):
    print(f"  {name} (mph):")
    for lo, hi in bins:
        g = [r["y"] / (MD * r["base"]) for r in good if lo <= r["w"][name] < hi and r["base"] > 0.2]
        if len(g) >= 6:
            mid = (lo + min(hi, 2 * lo + 1)) / 2
            print(f"    {lo:>3}-{hi:<3} n={len(g):>3}  ratio {st.mean(g):4.2f}  (median {st.median(g):4.2f})")

# 2: shapes for the curve
SHAPES = {
    "exp(-(U/U0)^p)  [now]": (lambda u, a, b: math.exp(-(u / a) ** b), [(a, b) for a in (4, 6, 8, 10, 12, 14, 18, 25) for b in (1, 2, 3, 4, 6)]),
    "linear to zero at U0": (lambda u, a, b: max(0.0, 1 - u / a), [(a, 0) for a in (8, 10, 12, 14, 16, 20, 25, 30, 40)]),
    "logistic 1/(1+exp((U-U0)/w))": (lambda u, a, b: 1 / (1 + math.exp(min(50, (u - a) / b))), [(a, b) for a in (6, 8, 10, 12, 15, 20, 25) for b in (1, 2, 3, 5)]),
    "hinge max(0, 1-U/U0)^q": (lambda u, a, b: max(0.0, 1 - u / a) ** b, [(a, b) for a in (10, 14, 18, 25, 35) for b in (0.5, 1, 2, 3)]),
}
def sse(f, params, md, data, name):
    return sum((r["y"] - md * r["base"] * f(r["w"][name], *params)) ** 2 for r in data)
def score(f, grid, name):
    mds = range(5, 16)
    def fit(data):
        return min(((sse(f, p, md, data, name), p, md) for p in grid for md in mds), key=lambda t: t[0])
    ins = fit(rows)
    cv = 0.0
    for d in nights:
        tr = [r for r in rows if r["night"] != d]
        te = [r for r in rows if r["night"] == d]
        _, p, md = fit(tr)
        cv += sse(f, p, md, te, name)
    return math.sqrt(ins[0] / n), math.sqrt(cv / n), ins[1], ins[2]
print("\n2. shapes of the wind curve (URMA 10 m wind; the rest of the equation as fitted; leave-one-night-out):")
for label, (f, grid) in SHAPES.items():
    a, b, p, md = score(f, grid, "URMA 10 m")
    print(f"   {label:<34} in-sample {a:.2f}F | cross-validated {b:.2f}F | params {p}, max {md}")
print("   no wind term at all               ", end="")
a = min(math.sqrt(sum((r["y"] - md * r["base"]) ** 2 for r in rows) / n) for md in range(3, 14)); print(f"in-sample {a:.2f}F")

# 3: which wind
print("\n3. which wind, with the exp(-(U/U0)^p) shape (cross-validated):")
f, grid = SHAPES["exp(-(U/U0)^p)  [now]"]
for name in ("URMA 10 m", "URMA 10 m, max of 3 h", "HRRR 10 m", "HRRR 925 hPa", "HRRR 925 hPa, max of 3 h", "HRRR 925 hPa, max of 6 h"):
    g2 = grid if "925" not in name else [(a, b) for a in (15, 20, 25, 30, 35, 40) for b in (2, 3, 4, 6)]
    a, b, p, md = score(f, g2, name)
    print(f"   {name:<28} in-sample {a:.2f}F | cross-validated {b:.2f}F | U0 {p[0]}, p {p[1]}, max {md}")
