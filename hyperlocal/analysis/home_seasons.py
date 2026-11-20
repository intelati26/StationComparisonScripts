#!/usr/bin/env python3
"""The home station by season (Ambient: log/ambient_all.db; URMA from log/downloaded.json; the HRRR wind 450 m up): amplitude, offset, errors, and the wind threshold fitted per season.
   python home_seasons.py"""
import json, math, os, sqlite3, statistics as st, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

c.load_env(); cfg = c.load_config()
WIN = {"autumn": ("2026-10-01", "2026-11-18"), "feb": ("2026-02-01", "2026-02-28"), "apr": ("2026-04-01", "2026-04-30"), "jul": ("2026-07-01", "2026-07-31")}
if "year" in sys.argv:            # every calendar month, February 2025 to November 2026 (the cache holds all the nights; Nov 2026 to the 18th)
    import calendar
    WIN = {}
    for y, m in [(2025, k) for k in range(2, 13)] + [(2026, k) for k in range(1, 12)]:
        last = 18 if (y, m) == (2026, 11) else calendar.monthrange(y, m)[1]
        WIN[f"{y}-{m:02d}"] = (f"{y}-{m:02d}-01", f"{y}-{m:02d}-{last:02d}")
elif "months" in sys.argv:          # the fortnights pulled for the other months (8th to 21st; June 2026 17th to 30th), in date order, then the four seasons above for reference
    extra = {f"2025-{m:02d}": (f"2025-{m:02d}-08", f"2025-{m:02d}-21") for m in range(2, 13)}
    extra.update({f"2026-{m:02d}": (f"2026-{m:02d}-08", f"2026-{m:02d}-21") for m in (1, 3, 5, 8, 9)})
    extra["2026-06"] = ("2026-06-17", "2026-06-30")
    pick = [a for a in sys.argv[1:] if a != "months"]          # names after "months" keep only those fortnights (e.g. 2026-01 2026-03), the four seasons stay
    WIN = {**{k: v for k, v in sorted(extra.items()) if not pick or k in pick}, **WIN}
cache = json.load(open(c.LOG / "downloaded.json"))
URMA = {datetime.fromisoformat(k): v for k, v in {**cache.get("rtma", {}), **cache["mod"]}.items()}
db = sqlite3.connect(c.LOG / "ambient_all.db", timeout=60)
acc = {}
for t, tf, dp, ws in db.execute("SELECT utc, tempf, dewpoint, windspeedmph FROM ambient"):
    h = datetime.fromisoformat(t.replace("Z", "+00:00")).replace(minute=0, second=0)
    a = acc.setdefault(h, [0, 0.0, 0, 0.0])
    a[0] += 1; a[1] += tf
    if dp is not None:
        a[2] += 1; a[3] += dp
OBS = {h: (a[1] / a[0], a[3] / a[2]) for h, a in acc.items() if a[0] >= 8 and a[2] >= 8}          # an hour needs most of its 12 records
print(len(OBS), "valid station hours;", min(OBS), "->", max(OBS), flush=True)


def winds(wn):
    d0, d1 = WIN[wn]
    f = c.LOG / f"w450_home_{wn}.json"
    if f.exists():
        return {datetime.fromisoformat(k): v for k, v in json.load(open(f)).items()}
    for _ in range(8):
        try:
            ground, HR = c.hrrr_levels(cfg["lat"], cfg["lon"], d0, (datetime.fromisoformat(d1) + timedelta(days=1)).date().isoformat())
            break
        except SystemExit:
            print("Open-Meteo busy, waiting 10 min", flush=True); time.sleep(600)
    w = {t: x for t, row in HR.items() for x in [c.wind_agl(row, ground, 450.0)] if x is not None}
    json.dump({k.isoformat(): v for k, v in w.items()}, open(f, "w"))
    return w


def factor(u, td, w, u0=30.0):
    return math.exp(-(w / u0) ** 3) * (1 - 0.8 * u[1] / 100) * min(1.0, max(0.0, u[0] - td) / 12.0)


print(f"{'season':<7}{'nights':>7}{'hours':>7} | {'b':>5}{'offset':>8} | night-low MAE:{'fcst':>6}{'A=10':>6}{'b':>6}{'b+off':>7} | bias(A=10) | wind threshold best (mph) | hourly rmse at 30 / best")
for wn, (d0, d1) in WIN.items():
    lo = datetime.fromisoformat(d0).replace(tzinfo=timezone.utc); hi = datetime.fromisoformat(d1).replace(tzinfo=timezone.utc) + timedelta(days=1, hours=14)
    W = winds(wn)
    rows = [(t, URMA[t], OBS[t], W[t]) for t in sorted(OBS) if lo <= t <= hi and t in URMA and t in W and c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0]
    if len(rows) < 60:
        print(f"{wn:<7} only {len(rows)} usable hours"); continue

    def fitpts(u0):
        pts = [(t, u[0], o[0], factor(u, o[1], w, u0)) for t, u, o, w in rows]
        xs = [p[3] for p in pts]; ys = [p[1] - p[2] for p in pts]; mx, my = st.mean(xs), st.mean(ys); den = sum((x - mx) ** 2 for x in xs)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
        a = my - b * mx
        rmse = math.sqrt(st.mean((y - a - b * x) ** 2 for x, y in zip(xs, ys)))
        return pts, b, a, rmse
    pts, b, a, rm30 = fitpts(30.0)
    best = min(((fitpts(u0)[3], u0) for u0 in range(14, 50, 2)))
    nights = {}
    for t, u, s_, f in pts:
        nights.setdefault((t - timedelta(hours=13)).date(), []).append((u, s_, f))
    nights = [v for v in nights.values() if len(v) >= 6]
    e = lambda amp, off=0.0: [min(u - off - amp * f for u, s_, f in v) - min(s_ for u, s_, f in v) for v in nights]
    mae = lambda x: st.mean(abs(i) for i in x)
    print(f"{wn:<7}{len(nights):>7}{len(pts):>7} | {b:>5.1f}{a:>+8.1f} | {'':<14}{mae(e(0)):>6.2f}{mae(e(10)):>6.2f}{mae(e(b)):>6.2f}{mae(e(b, a)):>7.2f} | {st.mean(e(10)):>+8.2f}   | {best[1]:>8}                  | {rm30:.2f} / {best[0]:.2f}", flush=True)
