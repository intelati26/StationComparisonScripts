#!/usr/bin/env python3
"""Dryness term test: the equation's min(1,(T-Td)/12) against RH-based forms, on the 287 nights with analysis inputs (URMA T and cloud, HRRR 450 m wind, the station's dewpoint).   python dry_test.py"""
import glob, json, math, os, sqlite3, statistics as st, sys
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
OBS = {h: (a[1] / a[0], a[3] / a[2]) for h, a in acc.items() if a[0] >= 8 and a[2] >= 8}
good = {r["E"] for r in json.load(open(c.LOG / "nbs_nights.json"))}
nights = {}
for t in sorted(URMA):
    if t in OBS and t in W and c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0:
        E = (t - timedelta(hours=13)).date().isoformat()
        if E in good: nights.setdefault(E, []).append(t)
nights = {E: ks for E, ks in nights.items() if len(ks) >= 9}
def sat(tf):
    tc = (tf - 32) / 1.8; return 6.112 * math.exp(17.62 * tc / (243.12 + tc))
def rh(T, D): return min(1.0, sat(D) / sat(T))
def vpd(T, D): return max(0.0, sat(T) - sat(D))
FORMS = {"spread/k (now)": lambda T, D, k: min(1, max(0, T - D) / k),
         "RH: (1-RH)/k": lambda T, D, k: min(1, max(0, 1 - rh(T, D)) / k),
         "vapour deficit es(T)-e(Td), hPa/k": lambda T, D, k: min(1, vpd(T, D) / k),
         "water only: 1 - e(Td)/k": lambda T, D, k: min(1, max(0.05, 1 - sat(D) / k)),
         "spread/12 x water: (1-e(Td)/k)": lambda T, D, k: min(1, max(0, T - D) / 12) * max(0.05, 1 - sat(D) / k),
         "spread/12 / (1+e(Td)/k)": lambda T, D, k: min(1, max(0, T - D) / 12) / (1 + sat(D) / k)}
KS = {"spread/k (now)": [8, 10, 12, 14, 16], "RH: (1-RH)/k": [.3, .4, .5, .6, .8],
      "vapour deficit es(T)-e(Td), hPa/k": [1, 1.5, 2, 3, 4, 6], "water only: 1 - e(Td)/k": [20, 25, 30, 40, 60],
      "spread/12 x water: (1-e(Td)/k)": [20, 25, 30, 40, 60], "spread/12 / (1+e(Td)/k)": [5, 10, 20, 40, 80]}
print(len(nights), "nights, equation on analysis inputs, A fitted per form (8-14)")
for name, f in FORMS.items():
    best = None
    for k in KS[name]:
        for A in (8, 9, 10, 11, 12, 13, 14):
            e = []
            for E, ks in nights.items():
                low = min(URMA[t][0] - A * math.exp(-(W[t] / 30) ** 3) * (1 - 0.8 * URMA[t][1] / 100) * f(URMA[t][0], OBS[t][1], k) for t in ks)
                e.append(low - min(OBS[t][0] for t in ks))
            m = st.mean(abs(x) for x in e)
            if best is None or m < best[0]: best = (m, k, A, st.mean(e), st.pstdev(e))
    print(f"  {name:<36} k={best[1]:<5} A={best[2]:<3} MAE {best[0]:.2f}  mean {best[3]:+.2f}  SD {best[4]:.2f}")
