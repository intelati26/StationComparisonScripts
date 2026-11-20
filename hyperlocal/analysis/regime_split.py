#!/usr/bin/env python3
"""Split the 287 NBS-forecast nights by the night's own regime (cloud, wind aloft, dryness, from URMA and the HRRR 450 m wind) and show where the error grows.  Needs log/nbs_nights.json (nbs_score.py).   python regime_split.py"""
import glob, json, os, sqlite3, statistics as st, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta
import coldpool as c
cfg = c.load_config()
URMA = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / "downloaded.json"))["mod"].items()}
W = {}
for f in glob.glob(str(c.LOG / "w450_home_*.json")):
    W.update({datetime.fromisoformat(k): v for k, v in json.load(open(f)).items()})
acc = {}
for t, tf, dp in sqlite3.connect(c.LOG / "ambient_all.db").execute("SELECT utc, tempf, dewpoint FROM ambient"):
    h = datetime.fromisoformat(t.replace("Z", "+00:00")).replace(minute=0, second=0)
    a = acc.setdefault(h, [0, 0.0, 0, 0.0]); a[0] += 1; a[1] += tf
    if dp is not None: a[2] += 1; a[3] += dp
TD = {h: a[3] / a[2] for h, a in acc.items() if a[0] >= 8 and a[2] >= 8}
rows = {r["E"]: r for r in json.load(open(c.LOG / "nbs_nights.json"))}
nights = {}
for t in sorted(URMA):
    if t in W and c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0:
        nights.setdefault((t - timedelta(hours=13)).date().isoformat(), []).append(t)
for E, r in rows.items():
    ks = nights.get(E)
    if not ks: continue
    r["cloud"] = st.mean(URMA[k][1] for k in ks); r["wind"] = max(W[k] for k in ks)
    r["spread"] = st.mean(URMA[k][0] - TD[k] for k in ks if k in TD) if any(k in TD for k in ks) else None
R = [r for r in rows.values() if r.get("spread") is not None]
print(len(R), "nights; URMA row length", len(URMA[next(iter(URMA))]))
def show(name, f, cuts):
    print(name)
    for lo, hi in cuts:
        g = [x for x in R if lo <= f(x) < hi]
        if len(g) >= 6:
            for key, lab in (("fall", "forecast"), ("ana", "analysis")):
                e = [x[key] for x in g]
                print(f"   {lo:>5}..{hi:<5} n={len(g):>3} {lab:<8} mean {st.mean(e):+5.1f}  SD {st.pstdev(e):4.1f}  MAE {st.mean(abs(v) for v in e):4.1f}")
show("night-mean cloud cover (%)", lambda x: x["cloud"], [(0, 15), (15, 50), (50, 85), (85, 101)])
show("strongest wind at 450 m (mph)", lambda x: x["wind"], [(0, 10), (10, 20), (20, 30), (30, 99)])
show("mean T-Td spread, URMA T minus the station dewpoint (F)", lambda x: x["spread"], [(0, 3), (3, 6), (6, 10), (10, 99)])
clear = [x for x in R if x["cloud"] < 30 and x["wind"] < 15]; mid = [x for x in R if not (x["cloud"] < 30 and x["wind"] < 15)]
for lab, g in (("clear and calm (cloud<30%, wind<15 mph)", clear), ("everything else", mid)):
    for key in ("fall", "ana"):
        e = [x[key] for x in g]; print(f"{lab:<42} n={len(g):>3} {key:<4} mean {st.mean(e):+5.1f} SD {st.pstdev(e):4.1f} MAE {st.mean(abs(v) for v in e):4.1f}")
