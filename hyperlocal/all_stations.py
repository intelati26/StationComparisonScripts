#!/usr/bin/env python3
"""The equation run over every station (29 Wunderground stations in four places; analysis inputs: URMA temperature and cloud at the place's centre, HRRR 925 hPa wind, the station's own hourly dewpoint).
For each station: its amplitude and offset (y = a + b x the weather factor), and how well the nightly low is predicted by: the forecast (URMA) low alone, the equation with a typical amplitude (5 F), with this station's own amplitude, with
an amplitude predicted from its terrain (position within 500 m, fitted on the other places), and with the amplitude and the offset.   python all_stations.py"""
import json, math, os, re, statistics as st, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coldpool as c

cfg = c.load_config()
obs, mod = c.load_db(cfg)
c.merge_cache(obs, mod)
CENTERS = {"home": (cfg["lat"], cfg["lon"]), "north": (37.5, -94.7), "ozark": (36.9, -93.3), "adrian": (38.4, -94.3)}      # the home place comes from WEATHER_LAT / WEATHER_LON (.env), the others are random places


def wu(path, skip=()):
    out = {}
    for sid, s in json.load(open(path)).items():
        if sid in skip:
            continue
        out[sid] = {datetime.fromisoformat(k.replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0): v for k, v in s["hours"].items() if v[0] is not None and v[1] is not None}
    return out


HOME = json.load(open(c.LOG / "home_stations.json"))          # the home place's Wunderground ids live in log/ (not in the repo)
P = {"home": (wu(c.LOG / "wu_hourly.json", tuple(HOME["skip"])), dict(mod)),
     "north": (wu(c.LOG / "wu_hourly_north.json"), {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / "urma_north.json")).items()}),
     "ozark": (wu(c.LOG / "wu_hourly_ozark.json"), {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / "urma_ozark.json")).items()}),
     "adrian": (wu(c.LOG / "wu_hourly_adrian.json"), {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / "urma_adrian.json")).items()})}
W925 = {}
for p, (la, lo) in CENTERS.items():                        # the HRRR's 925 hPa wind aloft, as the stitched history gives it (one decimal only is sent)
    q = urllib.parse.urlencode({"latitude": round(la, 1), "longitude": round(lo, 1), "models": "ncep_hrrr_conus", "hourly": "wind_speed_925hPa", "wind_speed_unit": "mph", "timezone": "UTC", "start_date": "2026-10-01", "end_date": "2026-11-19"})
    h = json.loads(urllib.request.urlopen("https://historical-forecast-api.open-meteo.com/v1/forecast?" + q, timeout=60).read())["hourly"]
    W925[p] = {datetime.fromisoformat(t).replace(tzinfo=timezone.utc): v for t, v in zip(h["time"], h["wind_speed_925hPa"]) if v is not None}
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
print(len(rows), "stations,", sum(len(r["nights"]) for r in rows), "station-nights")
# amplitude predicted from the terrain (position within 500 m), slope fitted on the other places
IX = {}
for s, cc, ix in json.load(open(c.LOG / "site_indices.json")):
    IX[HOME["home_wu_id"] if s == HOME["home_ambient"] else s] = ix["rel_500"]
for s, cc, ix in json.load(open(c.LOG / "site_indices_north.json")):
    IX[s] = ix["rel_500"]
pat = r'(KMO\w+)\s+([\d.]+)\s+([+-][\d.]+)F\s+(\d+)\s+([\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)'
for fn in ("bs4j1qnkq", "b68sai8jz"):
    for l in open(f"/tmp/claude-1000/-home-mitch-Claude-wxqt-grib/a4e9b04b-0a49-4c19-bf35-c59108a251fa/tasks/{fn}.output").read().splitlines():
        m = re.match(pat, l)
        if m:
            IX.setdefault(m.group(1), float(m.group(10)))
byp = {p: [r for r in rows if r["place"] == p and r["sid"] in IX] for p in P}
gm = st.mean(r["b"] for r in rows)
for r in rows:
    tr = [p for p in P if p != r["place"]]
    num = den = 0.0
    for p in tr:
        v = [IX[x["sid"]] for x in byp[p]]
        if len(v) < 3:
            continue
        m_, s_ = st.mean(v), (st.pstdev(v) or 1); bm = st.mean(x["b"] for x in byp[p])
        for x in byp[p]:
            z = (IX[x["sid"]] - m_) / s_; num += z * (x["b"] - bm); den += z * z
    slope = num / den if den else 0.0
    mine = [IX[x["sid"]] for x in byp[r["place"]]]
    z = (IX[r["sid"]] - st.mean(mine)) / (st.pstdev(mine) or 1) if r["sid"] in IX else 0.0
    r["b_pred"] = max(0.0, gm + slope * z)


def lows(r, amp, offset=0.0):
    e = []
    for d, v in r["nights"].items():
        e.append(min(u - amp * f - offset * 1 for u, s_, f in v) - min(s_ for u, s_, f in v))
    return e


for r in rows:
    r["F"] = lows(r, 0); r["E5"] = lows(r, 5); r["E10"] = lows(r, 10); r["Eb"] = lows(r, r["b"]); r["Ep"] = lows(r, r["b_pred"])
    r["Eba"] = [min(u - (r["a"] + r["b"] * f) for u, s_, f in v) - min(s_ for u, s_, f in v) for v in r["nights"].values()]
mae = lambda e: st.mean(abs(x) for x in e)
print(f"\n{'station':<14}{'place':<10}{'b':>5}{'a':>6}{'b pred':>7} | nightly-low MAE (F): {'forecast':>9}{'A=5':>6}{'A=10':>6}{'own b':>7}{'b from terrain':>16}{'b + offset':>12}")
for r in sorted(rows, key=lambda r: -r["b"]):
    print(f"{r['sid']:<14}{r['place']:<10}{r['b']:>5.1f}{r['a']:>+6.1f}{r['b_pred']:>7.1f} | {'':<21}{mae(r['F']):>6.1f}{mae(r['E5']):>6.1f}{mae(r['E10']):>6.1f}{mae(r['Eb']):>7.1f}{mae(r['Ep']):>16.1f}{mae(r['Eba']):>12.1f}")
print("\nsummary over the stations (mean of each station's MAE; 'stations improved' = better than the forecast low alone):")
for label, key in (("forecast low alone", "F"), ("equation, typical amplitude 5", "E5"), ("equation, amplitude 10 (the original station's)", "E10"), ("equation, the station's own amplitude", "Eb"), ("equation, amplitude predicted from terrain (other places)", "Ep"), ("equation, own amplitude and offset", "Eba")):
    ms = [mae(r[key]) for r in rows]; imp = sum(1 for r in rows if mae(r[key]) < mae(r["F"]) - 0.05)
    print(f"  {label:<62} mean MAE {st.mean(ms):.2f} F (median {st.median(ms):.2f})  | improved {imp} of {len(rows)} stations | mean bias {st.mean(st.mean(r[key]) for r in rows):+.2f}")
print("\nby place, mean MAE (forecast | typical 5 | own b | b from terrain):")
for p in P:
    rs = [r for r in rows if r["place"] == p]
    print(f"  {p:<8} {st.mean(mae(r['F']) for r in rs):.2f} | {st.mean(mae(r['E5']) for r in rs):.2f} | {st.mean(mae(r['Eb']) for r in rs):.2f} | {st.mean(mae(r['Ep']) for r in rs):.2f}   ({len(rs)} stations)")
