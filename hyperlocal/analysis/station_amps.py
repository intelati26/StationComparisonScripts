#!/usr/bin/env python3
"""Each Wunderground station's fitted amplitude b and offset a (the first half of all_stations.py), saved to log/station_amps.json.   python station_amps.py"""
"""The equation run over every station (29 Wunderground stations in four places; analysis inputs: URMA temperature and cloud at the place's centre, HRRR 925 hPa wind, the station's own hourly dewpoint).
For each station: its amplitude and offset (y = a + b x the weather factor), and how well the nightly low is predicted by: the forecast (URMA) low alone, the equation with a typical amplitude (5 F), with this station's own amplitude, with
an amplitude predicted from its terrain (position within 500 m, fitted on the other places), and with the amplitude and the offset.   python all_stations.py"""
import json, math, os, re, statistics as st, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

cfg = c.load_config()
obs, mod = c.load_db(cfg)
c.merge_cache(obs, mod)
import places
PL = places.load(cfg)
CENTERS = {k: (v["lat"], v["lon"]) for k, v in PL.items()}      # the home place comes from WEATHER_LAT / WEATHER_LON (.env), the others from log/places.json


def wu(path, skip=()):
    out = {}
    for sid, s in json.load(open(path)).items():
        if sid in skip:
            continue
        out[sid] = {datetime.fromisoformat(k.replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0): v for k, v in s["hours"].items() if v[0] is not None and v[1] is not None}
    return out


HOME = json.load(open(c.LOG / "home_stations.json"))          # the home place's Wunderground ids live in log/ (not in the repo)
P = {"home": (wu(c.LOG / "wu_hourly.json", tuple(HOME["skip"])), dict(mod))}
for k, v in PL.items():
    if k == "home":
        continue
    fw, fu = c.LOG / f"wu_hourly_{v['tag']}.json", c.LOG / f"urma_{v['tag']}.json"
    if fw.exists() and fu.exists():
        P[k] = (wu(fw), {datetime.fromisoformat(a): b for a, b in json.load(open(fu)).items()})
    else:
        CENTERS.pop(k)
W925 = {}
for p, (la, lo) in CENTERS.items():                        # the HRRR's wind about 450 m above its ground at the place's centre (one decimal is sent)
    ground, HR = c.hrrr_levels(la, lo, "2026-10-01", "2026-11-19")
    W925[p] = {t: w for t, row in HR.items() for w in [c.wind_agl(row, ground, 450.0)] if w is not None}
fc = dict(cfg, **c.FORECAST_CFG)


def factor(U, t, td, w):
    return math.exp(-(w / 30.0) ** 3) * (1 - 0.8 * U[1] / 100) * min(1.0, max(0.0, U[0] - td) / 12.0)


rows = []
for place, (S, U) in P.items():
    for sid, hrs in S.items():
        pts = []
        for t, v in hrs.items():
            u, w = U.get(t), W925[place].get(t)
            if u is None or w is None or c.night_factor(t, cfg["lat"], cfg["lon"]) < 1.0:
                continue
            pts.append((t, u[0], v[0], factor(u, t, v[1], w)))          # t, URMA T, station T, weather factor
        if len(pts) < 150:
            continue
        xs = [p[3] for p in pts]; ys = [p[1] - p[2] for p in pts]
        mx, my = st.mean(xs), st.mean(ys); b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs); a = my - b * mx
        nights = {}
        for t, ut, stt, f in pts:
            nights.setdefault((t - timedelta(hours=13)).date(), []).append((ut, stt, f))
        rows.append(dict(place=place, sid=sid, b=b, a=a, nights={d: v for d, v in nights.items() if len(v) >= 9}))
json.dump([dict(place=r["place"], sid=r["sid"], b=r["b"], a=r["a"], nights=len(r["nights"])) for r in rows], open(c.LOG / "station_amps.json", "w"))
print(len(rows), "stations ->", c.LOG / "station_amps.json")
