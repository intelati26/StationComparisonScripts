#!/usr/bin/env python3
"""The home airport's (ASOS) wind and gust as a predictor of the station's own hourly gust, night and day, against the HRRR; and the station/airport ratio by hour and wind. log/<airport>_asos_*.csv from IEM (HYPERLOCAL_AIRPORT in .env).   python gust_airport.py"""
import csv, glob, io, contextlib, math, os, runpy, statistics as st
from datetime import datetime, timezone, timedelta
import numpy as np
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path(os.path.join(HERE, "analysis", "shear_test.py"))
nights, SG, X, cfg, c = g["nights"], g["SG"], g["X"], g["cfg"], g["c"]
K = 1.15078; AP = {}
import places
for f in glob.glob(os.path.join(HERE, "log", f"{places.airport()}_asos_*.csv")):
    for r in csv.DictReader(open(f)):
        try: t = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(minute=0, tzinfo=timezone.utc)
        except ValueError: continue
        s = float(r["sknt"]) * K if r["sknt"] not in ("M", "") else None; gu = float(r["gust"]) * K if r["gust"] not in ("M", "") else None
        if s is None: continue
        a = AP.setdefault(t, [0.0, 0.0]); a[0] = max(a[0], s); a[1] = max(a[1], gu if gu is not None else s)
night_hours = {t for v in nights.values() for t, *_ in v}
def groups():
    out = {"night": [], "day": []}
    for t, x in X.items():
        if t not in SG or t not in AP or x["w450"] is None or x["gust"] is None: continue
        nf = c.night_factor(t, cfg["lat"], cfg["lon"])
        if t.tzinfo is None: continue
        if nf >= 1.0: out["night"].append((t, x))
        elif nf <= 0.0: out["day"].append((t, x))
    return out
G = groups()
win = lambda t: f"{t.year}-{t.month:02d}"
for name, rows in G.items():
    y = np.array([SG[t] for t, x in rows]); ap = np.array([AP[t][1] for t, x in rows]); aw = np.array([AP[t][0] for t, x in rows])
    hg = np.array([x["gust"] for t, x in rows]); w10 = np.array([x["w10"] for t, x in rows])
    print(f"\n{name.upper()}: {len(rows)} hours | station gust mean {y.mean():.1f}, airport peak mean {ap.mean():.1f} (ratio of means {y.mean()/ap.mean():.2f}), HRRR gust mean {hg.mean():.1f}")
    def cv(cols):
        M = np.column_stack([np.ones(len(y))] + cols); mo = np.array([win(t) for t, x in rows]); pr = np.zeros(len(y))
        for m in set(mo):
            tr, te = mo != m, mo == m; co, *_ = np.linalg.lstsq(M[tr], y[tr], rcond=None); pr[te] = M[te] @ co
        co, *_ = np.linalg.lstsq(M, y, rcond=None); return np.corrcoef(pr, y)[0, 1], math.sqrt(np.mean((pr - y) ** 2)), co
    for lab, cols in (("HRRR 10 m wind + gust", [w10, hg]), ("airport peak gust alone", [ap]), ("airport wind + peak gust", [aw, ap]), ("airport + HRRR", [ap, w10, hg])):
        r, e, co = cv(cols); print(f"   {lab:<26} out-of-month corr {r:+.2f}  RMSE {e:.1f} mph   coef {' '.join(f'{v:+.2f}' for v in co)}")
print("\nstation gust / airport peak gust (ratio of sums), by local hour (CST = UTC-6) and by airport wind:")
tot = {}
for name, rows in G.items():
    for t, x in rows:
        h = (t.hour - 6) % 24; a = tot.setdefault(h, [0.0, 0.0, 0]); a[0] += SG[t]; a[1] += AP[t][1]; a[2] += 1
print("  hour: " + " ".join(f"{h:02d}:{a[0]/a[1]:.2f}" for h, a in sorted(tot.items()) if a[2] >= 15))
allr = [(t, x) for rows in G.values() for t, x in rows]
for lo, hi in ((0, 10), (10, 15), (15, 20), (20, 25), (25, 99)):
    for nm in ("night", "day"):
        q = [(SG[t], AP[t][1]) for t, x in G[nm] if lo <= AP[t][1] < hi]
        if len(q) >= 15: print(f"  airport peak {lo:>2}-{hi:<2} mph {nm:<5}: n={len(q):>4} station/airport {sum(a for a, b in q)/sum(b for a, b in q):.2f}  corr {np.corrcoef([a for a, b in q], [b for a, b in q])[0,1]:+.2f}")

print("\nHOURS AROUND SUNSET AND SUNRISE (offset of the hour's centre from the event, hours; ratio = station gust / airport peak gust):")
def near(t, which):
    best = None
    for d in (t.date() - timedelta(days=1), t.date(), t.date() + timedelta(days=1)):
        ev = sun_pair(d)[which]; off = (t + timedelta(minutes=30) - ev).total_seconds() / 3600
        if best is None or abs(off) < abs(best): best = off
    return best
_cache = {}
def sun_pair(d):
    if d not in _cache: _cache[d] = c.sun_times(cfg["lat"], cfg["lon"], d)
    return _cache[d]
for which, nm in ((1, "sunset"), (0, "sunrise")):
    print(f"  {nm}:  offset |   n | station mean | airport mean | HRRR gust | ratio | station/HRRR gust | corr(station, airport)")
    for k in range(-4, 5):
        q = [(t, x) for t, x in X.items() if t in SG and t in AP and x["gust"] is not None and k - 0.5 <= near(t, which) < k + 0.5]
        if len(q) < 30: continue
        s = np.array([SG[t] for t, x in q]); a = np.array([AP[t][1] for t, x in q]); h = np.array([x["gust"] for t, x in q])
        print(f"          {k:>+3} h  | {len(q):>3} |    {s.mean():>5.1f}    |    {a.mean():>5.1f}    |   {h.mean():>5.1f}   | {s.sum()/a.sum():.2f}  |   {s.sum()/h.sum():.2f}   |  {np.corrcoef(s, a)[0,1]:+.2f}")
