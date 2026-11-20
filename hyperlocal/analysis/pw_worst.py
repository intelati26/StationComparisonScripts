#!/usr/bin/env python3
"""Does the column's water vapour (Open-Meteo/HRRR total_column_integrated_water_vapour, mm) explain the worst nights of the equation (analysis inputs)?  Pulls the worst 20 nights and 20 ordinary ones -> log/pw_nights.json.   python pw_worst.py"""
import glob, json, math, os, random, sqlite3, statistics as st, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta
import coldpool as c
c.load_env(); cfg = c.load_config()
rows = sorted(json.load(open(c.LOG / "nbs_nights.json")), key=lambda x: -abs(x["ana"]))
random.seed(3); worst = rows[:20]; ctl = random.sample(rows[40:], 20)
URMA = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / "downloaded.json"))["mod"].items()}
nights = {}
for t in sorted(URMA):
    if c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0: nights.setdefault((t - timedelta(hours=13)).date().isoformat(), []).append(t)
F = c.LOG / "pw_nights.json"; PW = json.load(open(F)) if F.exists() else {}
for r in worst + ctl:
    E = r["E"]
    if E in PW or E not in nights: continue
    d0 = datetime.fromisoformat(E); d1 = (d0 + timedelta(days=1)).date().isoformat()
    for attempt in range(6):
        try:
            g, HR = c.hrrr_levels(cfg["lat"], cfg["lon"], E, d1, extra=("total_column_integrated_water_vapour", "dew_point_2m")); break
        except SystemExit: time.sleep(20)
    else: continue
    ks = nights[E]; v = [HR[k]["total_column_integrated_water_vapour"] for k in ks if k in HR and HR[k].get("total_column_integrated_water_vapour") is not None]
    if v: PW[E] = st.mean(v); F.write_text(json.dumps(PW)); print(E, round(PW[E], 1), flush=True)
sat = lambda tf: 6.112 * math.exp(17.62 * ((tf - 32) / 1.8) / (243.12 + (tf - 32) / 1.8))
import sqlite3
acc = {}
for t, dp in sqlite3.connect(c.LOG / "ambient_all.db").execute("SELECT utc, dewpoint FROM ambient WHERE dewpoint IS NOT NULL"):
    h = datetime.fromisoformat(t.replace("Z", "+00:00")).replace(minute=0, second=0); acc.setdefault(h, []).append(dp)
TD = {h: st.mean(v) for h, v in acc.items()}
def corr(a, b):
    ma, mb = st.mean(a), st.mean(b); return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) * st.pstdev(a) * st.pstdev(b))
for lab, grp in (("worst 20", worst), ("ordinary 20", ctl)):
    g = [r for r in grp if r["E"] in PW]
    print(f"\n{lab}: {len(g)} nights, equation error mean {st.mean(r['ana'] for r in g):+.1f}, MAE {st.mean(abs(r['ana']) for r in g):.1f}")
    print(f"  column water mean {st.mean(PW[r['E']] for r in g):.1f} mm; corr(error, column water) = {corr([r['ana'] for r in g],[PW[r['E']] for r in g]):+.2f}")
allr = [r for r in worst + ctl if r["E"] in PW]
tdn = {r["E"]: st.mean(TD[k] for k in nights[r["E"]] if k in TD) for r in allr}
ex = {E: sat(v) for E, v in tdn.items()}
n = len(allr); mx, my = st.mean(ex.values()), st.mean(PW[r["E"]] for r in allr)
b = sum((ex[r["E"]] - mx) * (PW[r["E"]] - my) for r in allr) / sum((ex[r["E"]] - mx) ** 2 for r in allr)
an = {r["E"]: PW[r["E"]] - (my + b * (ex[r["E"]] - mx)) for r in allr}
print(f"\nall {n}: column water explained by the surface dewpoint's vapour pressure: corr {corr(list(ex.values()),[PW[r['E']] for r in allr]):+.2f}")
print(f"  corr(error, column water anomaly given the surface dewpoint) = {corr([r['ana'] for r in allr],[an[r['E']] for r in allr]):+.2f}")
print("  worst, signed error vs anomaly (mm):", ", ".join(f"{r['ana']:+.1f}/{an[r['E']]:+.0f}" for r in worst if r['E'] in an))
