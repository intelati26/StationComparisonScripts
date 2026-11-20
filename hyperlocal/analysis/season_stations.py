#!/usr/bin/env python3
"""The equation over all stations in each season window (analysis inputs: URMA T and cloud at the place's centre, the HRRR wind 450 m above its ground, the station's own hourly dewpoint), as all_stations.py does for autumn.
Per station and season: amplitude b and offset a (y = a + b x weather factor), the nightly-low MAE of the forecast alone, typical A=5, A=10, the autumn b carried over, and the season's own b.
   python season_stations.py"""
import json, math, os, statistics as st, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

cfg = c.load_config()
obs, mod = c.load_db(cfg)
c.merge_cache(obs, mod)
import places
PL = places.load(cfg)
CENTERS = {k: (v["lat"], v["lon"]) for k, v in PL.items()}
WINDOWS = {"autumn": ("2026-10-01", "2026-11-18"), "feb": ("2026-02-01", "2026-02-28"), "apr": ("2026-04-01", "2026-04-30"), "jul": ("2026-07-01", "2026-07-31")}
HOME = json.load(open(c.LOG / "home_stations.json"))
FILES = {k: ("wu_hourly.json", None) if k == "home" else (f"wu_hourly_{v['tag']}.json", f"urma_{v['tag']}.json") for k, v in PL.items()}


def wu(path, skip=()):
    out = {}
    for sid, s in json.load(open(path)).items():
        if sid in skip:
            continue
        out[sid] = {datetime.fromisoformat(k.replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0): v for k, v in s["hours"].items() if v[0] is not None and v[1] is not None}
    return out


P = {}
for place, (wf, uf) in FILES.items():
    if not os.path.exists(c.LOG / wf):
        continue
    S = wu(c.LOG / wf, tuple(HOME["skip"]) if place == "home" else ())
    if place == "home":
        S["AMBIENT"] = {t: (v[0], v[2]) for t, v in obs.items() if v[2] is not None}      # the home station itself, hourly (temperature, dewpoint)
        U = dict(mod)
    else:
        U = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / uf)).items()}
    P[place] = (S, U)

W = {}                                                              # {window: {place: {hour: wind}}}
for wn, (d0, d1) in WINDOWS.items():
    W[wn] = {}
    for place, (la, lo) in CENTERS.items():
        if place not in P:
            continue
        cache = c.LOG / f"w450_{place}_{wn}.json"
        if cache.exists():
            W[wn][place] = {datetime.fromisoformat(k): v for k, v in json.load(open(cache)).items()}
            continue
        ground, HR = c.hrrr_levels(la, lo, d0, (datetime.fromisoformat(d1) + timedelta(days=1)).date().isoformat())
        W[wn][place] = {t: w for t, row in HR.items() for w in [c.wind_agl(row, ground, 450.0)] if w is not None}
        json.dump({k.isoformat(): v for k, v in W[wn][place].items()}, open(cache, "w"))
        print("HRRR winds", place, wn, len(W[wn][place]), flush=True)


def factor(u, td, w):
    return math.exp(-(w / 30.0) ** 3) * (1 - 0.8 * u[1] / 100) * min(1.0, max(0.0, u[0] - td) / 12.0)


def fit(pts):
    xs = [p[3] for p in pts]; ys = [p[1] - p[2] for p in pts]
    mx, my = st.mean(xs), st.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
    return b, my - b * mx


def nights_of(pts):
    n = {}
    for t, ut, stt, f in pts:
        n.setdefault((t - timedelta(hours=13)).date(), []).append((ut, stt, f))
    return {d: v for d, v in n.items() if len(v) >= 6}


def lows(nights, amp, a=0.0):
    return [min(u - (a + amp * f) for u, s, f in v) - min(s for u, s, f in v) for v in nights.values()]


mae = lambda e: st.mean(abs(x) for x in e) if e else float("nan")
R = {}                                                              # {window: {(place, sid): dict}}
for wn, (d0, d1) in WINDOWS.items():
    lo_t = datetime.fromisoformat(d0).replace(tzinfo=None); hi_t = datetime.fromisoformat(d1).replace(tzinfo=None) + timedelta(days=1, hours=14)
    R[wn] = {}
    for place, (S, U) in P.items():
        for sid, hrs in S.items():
            pts = []
            for t, v in hrs.items():
                if not (lo_t <= t.replace(tzinfo=None) <= hi_t):
                    continue
                u, w = U.get(t), W[wn].get(place, {}).get(t)
                if u is None or w is None or c.night_factor(t, cfg["lat"], cfg["lon"]) < 1.0:
                    continue
                pts.append((t, u[0], v[0], factor(u, v[1], w)))
            if len(pts) < 60:
                continue
            b, a = fit(pts)
            R[wn][(place, sid)] = dict(b=b, a=a, n=nights_of(pts), pts=len(pts))

print(f"{'season':<8}{'stations':>9}{'nights':>8} | amplitude b: {'mean':>5}{'median':>7}{'sd':>5} | offset a mean | nightly-low MAE (F): {'forecast':>9}{'A=5':>6}{'A=10':>6}{'autumn b':>9}{'own b':>7}{'own b+a':>8} | bias A=5")
for wn in WINDOWS:
    rs = R[wn]
    if not rs:
        print(f"{wn:<8}  no data"); continue
    bs = [r["b"] for r in rs.values()]
    Fm, E5, E10, Ea, Eb, Eba, B5 = [], [], [], [], [], [], []
    for key, r in rs.items():
        if len(r["n"]) < 5:
            continue
        Fm.append(mae(lows(r["n"], 0))); E5.append(mae(lows(r["n"], 5))); E10.append(mae(lows(r["n"], 10)))
        Eb.append(mae(lows(r["n"], r["b"]))); Eba.append(mae(lows(r["n"], r["b"], r["a"]))); B5.append(st.mean(lows(r["n"], 5)))
        ra = R["autumn"].get(key)
        if ra and wn != "autumn":
            Ea.append(mae(lows(r["n"], ra["b"])))
    nn = sum(len(r["n"]) for r in rs.values())
    print(f"{wn:<8}{len(rs):>9}{nn:>8} | {'':<13}{st.mean(bs):>5.1f}{st.median(bs):>7.1f}{st.pstdev(bs):>5.1f} | {st.mean(r['a'] for r in rs.values()):>+8.1f}      | {'':<21}{st.mean(Fm):>6.2f}{st.mean(E5):>6.2f}{st.mean(E10):>6.2f}{(st.mean(Ea) if Ea else float('nan')):>9.2f}{st.mean(Eb):>7.2f}{st.mean(Eba):>8.2f} | {st.mean(B5):>+.2f}")
print("\nper-station amplitude b by season (same stations; the autumn value, then each season):")
print("  place      sid          autumn   feb   apr   jul")
rows = []
for key in R["autumn"]:
    rows.append((key, [R[w].get(key, {}).get("b") for w in ("autumn", "feb", "apr", "jul")]))
for (place, sid), v in sorted(rows, key=lambda x: -x[1][0]):
    print(f"  {place:<10} {('home-station' if sid == 'AMBIENT' else sid[-4:]):<12}" + "".join(f"{x:>6.1f}" if x is not None else "     -" for x in v))
for w in ("feb", "apr", "jul"):
    pr = [(r["autumn"], r[w]) for _, r in [(k, dict(zip(("autumn", "feb", "apr", "jul"), v))) for k, v in rows] if r["autumn"] is not None and r[w] is not None]
    if len(pr) >= 4:
        xs, ys = zip(*pr)
        mx, my = st.mean(xs), st.mean(ys)
        cor = sum((x - mx) * (y - my) for x, y in pr) / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
        print(f"  {w}: {len(pr)} stations in both, correlation of b with autumn b {cor:+.2f}, mean ratio {st.mean(ys) / st.mean(xs):.2f}")
