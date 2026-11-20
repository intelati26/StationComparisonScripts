#!/usr/bin/env python3
"""Predict the station's own hourly gust from the HRRR (10 m wind, 10 m gust, 450 m wind) and use it as the mixing term.
Leave-one-window-out: the gust fit for each window uses the other windows only.  Uses shear_test.py's caches (nights, HRRR hours, station gusts).   python gust_fit.py"""
import io, contextlib, math, os, runpy, statistics as st, sys
import numpy as np
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path(os.path.join(HERE, "analysis", "shear_test.py"))
nights, SG = g["nights"], g["SG"]
rows = [(gname, t, x, SG[t]) for (gname, d), v in nights.items() for t, u, o, x in v]
print(len(rows), "night hours in", len({r[0] for r in rows}), "windows")
F = lambda x: [1.0, x["w10"], x["gust"], x["w450"]]
y = np.array([r[3] for r in rows]); M = np.array([F(r[2]) for r in rows])
for name, col in (("HRRR 10 m wind", 1), ("HRRR 10 m gust", 2), ("HRRR 450 m wind", 3)):
    print(f"  station gust vs {name}: corr {np.corrcoef(M[:, col], y)[0,1]:+.2f}, mean station {y.mean():.1f} vs {M[:, col].mean():.1f} mph, station max {y.max():.0f}, {name} max {M[:, col].max():.0f}")
print("  station gust, hourly max over night hours: mean %.1f  95th %.1f  max %.0f mph" % (y.mean(), np.percentile(y, 95), y.max()))
wins = sorted({r[0] for r in rows}); pred = {}
for w in wins:
    tr = [i for i, r in enumerate(rows) if r[0] != w]; te = [i for i, r in enumerate(rows) if r[0] == w]
    coef, *_ = np.linalg.lstsq(M[tr], y[tr], rcond=None)
    for i in te: pred[(rows[i][1])] = max(0.0, float(M[i] @ coef))
coef, *_ = np.linalg.lstsq(M, y, rcond=None)
yh = np.array([pred[r[1]] for r in rows])
print(f"  gust fit (all windows): station gust = {coef[0]:+.1f} {coef[1]:+.2f}*U10 {coef[2]:+.2f}*HRRRgust {coef[3]:+.2f}*U450;  out-of-window corr {np.corrcoef(yh, y)[0,1]:+.2f}, RMSE {math.sqrt(np.mean((yh-y)**2)):.1f} mph (HRRR gust alone: RMSE {math.sqrt(np.mean((M[:,2]-y)**2)):.1f}, corr {np.corrcoef(M[:,2], y)[0,1]:+.2f})")
def score(q_of, x0, A):
    out = {}
    for (gname, d), v in nights.items():
        e = []
        for t, u, o, x in v:
            f = math.exp(-(q_of(t, x) / x0) ** 3) * (1 - 0.8 * u[1] / 100) * min(1.0, max(0.0, u[0] - o[1]) / 12.0)
            e.append((u[0] - A * f, o[0]))
        out.setdefault(gname, []).append(min(a for a, b in e) - min(b for a, b in e))
    return out
VARS = {"wind at 450 m (current)": (lambda t, x: x["w450"], range(22, 40, 2)), "HRRR 10 m gust": (lambda t, x: x["gust"], range(12, 40, 3)),
        "predicted station gust (out-of-window fit)": (lambda t, x: pred[t], range(4, 30, 2)), "station's own gust (what we can't forecast)": (lambda t, x: SG[t], range(2, 24, 2))}
print("\nvariant                                       x0    A | pooled MAE | SD   | by window (MAE)")
for name, (q, grid) in VARS.items():
    best = None
    for x0 in grid:
        for A in (8, 9, 10, 11, 12):
            o = score(q, x0, A); e = [v for l in o.values() for v in l]; m = st.mean(abs(v) for v in e)
            if best is None or m < best[0]: best = (m, x0, A, o, e)
    m, x0, A, o, e = best
    print(f"{name:<44}{x0:>4}{A:>5} | {m:>9.2f} | {st.pstdev(e):.2f} | " + " ".join(f"{k} {st.mean(abs(v) for v in l):.2f}" for k, l in sorted(o.items())))
