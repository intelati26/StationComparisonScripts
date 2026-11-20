#!/usr/bin/env python3
"""Diagnostics of the fitted equation on the night hours:  python check_equation.py
 1 residuals by hour of night, cloud, wind, dewpoint spread, temperature, date; how much of the error is night-to-night
 2 leave-one-night-out cross-validation against simpler equations
 3 alternative forms of the cloud and dryness terms"""
import json, math, os, sys, statistics as st
from datetime import timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

cfg = c.load_config()
obs, mod = c.load_db(cfg)
c.merge_cache(obs, mod)
fit = json.load(open(c.LOG / "fit.json"))
D = 1 + max(0.0, cfg["rim_elevation_ft"] - cfg["valley_elevation_ft"]) / 3000
rows = []           # t, deficit, cloud, wind, spread (URMA T - station Td), station T, hours after sunset, night key
for k in sorted(set(obs) & set(mod)):
    if c.night_factor(k, cfg["lat"], cfg["lon"]) >= 1.0 and len(obs[k]) > 2 and obs[k][2] is not None:
        rows.append((k, mod[k][0] - obs[k][0], mod[k][1], mod[k][2], mod[k][0] - obs[k][2], obs[k][0], c.hours_after_sunset(k, cfg["lat"], cfg["lon"]), (k - timedelta(hours=13)).date()))
n = len(rows)


def predict(x, p):
    f_w = math.exp(-(x[3] / p["u0"]) ** p["p"]) if p.get("u0") else 1.0
    f_c = (1 - p["cw"] * x[2] / 100) if p.get("cw") is not None else 1.0
    if p.get("ds"):
        g = max(0.0, x[4]) / p["ds"]
        g = min(1.0, g) if p.get("cap", True) else g
    else:
        g = 1.0
    return p["md"] * D * f_w * f_c * g


best = {"md": fit["max_deficit_f"], "cw": fit["cloud_weight"], "ds": fit["dep_scale"], "u0": fit.get("wind_u0") or 10, "p": fit.get("wind_p") or 3}
res = [(x, x[1] - predict(x, best)) for x in rows]            # actual deficit minus modeled drop: positive = the model is too cold-shy
rm = lambda v: math.sqrt(sum(a * a for a in v) / len(v))
mean_def = st.mean(x[1] for x in rows)
print(f"{n} night hours, {len({x[7] for x in rows})} nights. Equation: drop = {best['md']} x {D:.3f} x exp(-(wind/{best['u0']})^{best['p']}) x (1 - {best['cw']} cloud) x min(1, spread/{best['ds']})")
print(f"  rmse {rm([r for _, r in res]):.2f}F | mean residual {st.mean(r for _, r in res):+.2f}F | no model at all (the mean deficit {mean_def:.1f}F): {rm([x[1] - mean_def for x in rows]):.2f}F")


def table(title, keyf, bins):
    print(f"\n{title}   (residual = actual deficit - modeled drop; + means the equation is too small)")
    for lo, hi in bins:
        g = [r for x, r in res if lo <= keyf(x) < hi]
        if len(g) >= 8:
            print(f"  {lo:>6}-{hi:<6} n={len(g):>3}  mean {st.mean(g):+5.2f}F  rmse {rm(g):.2f}F")


table("by hour of night (hours after sunset)", lambda x: x[6], [(2, 4), (4, 6), (6, 8), (8, 10), (10, 13)])
table("by cloud %", lambda x: x[2], [(0, 10), (10, 30), (30, 60), (60, 101)])
table("by URMA wind (mph)", lambda x: x[3], [(0, 3), (3, 6), (6, 9), (9, 12), (12, 40)])
table("by URMA temperature - station dewpoint (F)", lambda x: x[4], [(-5, 6), (6, 9), (9, 12), (12, 16), (16, 60)])
table("by station temperature (F)", lambda x: x[5], [(10, 30), (30, 45), (45, 60), (60, 90)])
import datetime as dt
table("by date (day of year)", lambda x: x[7].timetuple().tm_yday, [(274, 288), (288, 302), (302, 316), (316, 330), (330, 365)])

# how much of the error is night to night
bynight = {}
for x, r in res:
    bynight.setdefault(x[7], []).append(r)
nm = [st.mean(v) for v in bynight.values() if len(v) >= 6]
within = [r - st.mean(bynight[x[7]]) for x, r in res if len(bynight[x[7]]) >= 6]
tot = rm([r for _, r in res])
print(f"\nerror structure: the night's own mean residual has sd {st.pstdev(nm):.2f}F; what is left inside a night is rmse {rm(within):.2f}F (total {tot:.2f}F)")
print(f"  share of the squared error that is night-to-night: {100 * st.pstdev(nm) ** 2 / (tot ** 2):.0f}%")
lag = [(st.mean(bynight[d]), st.mean(bynight[d + timedelta(days=1)])) for d in bynight if d + timedelta(days=1) in bynight and len(bynight[d]) >= 6 and len(bynight[d + timedelta(days=1)]) >= 6]
if len(lag) > 5:
    a, b = [p[0] for p in lag], [p[1] for p in lag]
    ma, mb = st.mean(a), st.mean(b)
    print(f"  night-to-night autocorrelation of the mean residual (lag 1 night, {len(lag)} pairs): {sum((u - ma) * (v - mb) for u, v in zip(a, b)) / (len(a) * st.pstdev(a) * st.pstdev(b)):+.2f}")

# leave-one-night-out cross-validation of equation variants
GRIDS = {
    "full (threshold wind, cloud, dry)": [dict(md=md, cw=cw, ds=ds, u0=u0, p=p) for md in range(6, 15) for cw in (0.6, 0.8, 1.0) for ds in (8, 12, 18) for u0 in (8, 10, 12) for p in (3, 4)],
    "no dewpoint term": [dict(md=md, cw=cw, u0=u0, p=p) for md in range(3, 12) for cw in (0.6, 0.8, 1.0) for u0 in (8, 10, 12) for p in (3, 4)],
    "no wind term": [dict(md=md, cw=cw, ds=ds) for md in range(6, 15) for cw in (0.6, 0.8, 1.0) for ds in (8, 12, 18)],
    "no cloud term": [dict(md=md, ds=ds, u0=u0, p=p) for md in range(6, 15) for ds in (8, 12, 18) for u0 in (8, 10, 12) for p in (3, 4)],
    "cloud only": [dict(md=md, cw=cw) for md in range(2, 12) for cw in (0.6, 0.8, 1.0)],
}
nights = sorted(bynight)
print("\nleave-one-night-out (fit on the other nights, score the held-out one):")
for name, grid in GRIDS.items():
    err_in, err_cv, cnt = 0.0, 0.0, 0
    sse = lambda p, data: sum((x[1] - predict(x, p)) ** 2 for x in data)
    allbest = min(grid, key=lambda p: sse(p, rows))
    err_in = sse(allbest, rows)
    for d in nights:
        train = [x for x in rows if x[7] != d]
        test = [x for x in rows if x[7] == d]
        if not test:
            continue
        pbest = min(grid, key=lambda p: sse(p, train))
        err_cv += sse(pbest, test)
        cnt += len(test)
    print(f"  {name:<34} in-sample rmse {math.sqrt(err_in / n):.2f}F | cross-validated {math.sqrt(err_cv / cnt):.2f}F")

# other forms of the cloud and dryness terms
print("\nother forms (everything else as fitted, md and the term's scale refitted):")
def refit(label, f):
    b = min(((sum((x[1] - f(x, md, s)) ** 2 for x in rows), md, s) for md in range(4, 17) for s in f.scales), key=lambda t: t[0])
    print(f"  {label:<46} rmse {math.sqrt(b[0] / n):.2f}F  (max {b[1]}, scale {b[2]})")
def mk(fn, scales):
    fn.scales = scales
    return fn
fw = lambda x: math.exp(-(x[3] / best["u0"]) ** best["p"])
refit("cloud linear (as fitted), spread capped at 1", mk(lambda x, md, s: md * D * fw(x) * (1 - best["cw"] * x[2] / 100) * min(1, max(0, x[4]) / 12), [12]))
refit("cloud squared: (1 - c)^2", mk(lambda x, md, s: md * D * fw(x) * (1 - x[2] / 100) ** 2 * min(1, max(0, x[4]) / s), [8, 12, 18]))
refit("cloud as exp(-c/s)", mk(lambda x, md, s: md * D * fw(x) * math.exp(-x[2] / 100 * 2) * min(1, max(0, x[4]) / 12), [12]))
refit("spread not capped", mk(lambda x, md, s: md * D * fw(x) * (1 - best["cw"] * x[2] / 100) * max(0, x[4]) / s, [8, 12, 18, 25, 35]))
refit("spread as sqrt", mk(lambda x, md, s: md * D * fw(x) * (1 - best["cw"] * x[2] / 100) * math.sqrt(min(1.5, max(0, x[4]) / s)), [8, 12, 18]))
refit("additive instead of multiplicative", mk(lambda x, md, s: max(0, md * (0.5 * fw(x) + 0.3 * (1 - best["cw"] * x[2] / 100) + 0.2 * min(1, max(0, x[4]) / s))), [8, 12, 18]))
