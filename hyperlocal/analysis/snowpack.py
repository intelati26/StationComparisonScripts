#!/usr/bin/env python3
"""Nights inside the late-January 2026 snow pack at the home place (snow 24-25 Jan, daytime highs of 37 F or less until 2 Feb).
URMA at the station point (AWS GRIB) and the home group's Wunderground hourly data for 24 Jan - 2 Feb; clear, calm nights against the autumn baseline.   python snowpack.py"""
import json, math, os, statistics as st, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

c.load_env()
cfg = c.load_config()
f = c.LOG / f"snow_effect_{os.environ.get('HOME_AIRPORT', 'AIRPORT')}.json"
have = json.loads(f.read_text())
nights = [f"2026-01-{d}" for d in range(25, 32)] + ["2026-02-01"]
hours = []
for n in nights:
    day = datetime.fromisoformat(n).replace(tzinfo=timezone.utc)
    t = day + timedelta(hours=12)
    while t < day + timedelta(hours=40):
        if c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0 and t.isoformat() not in have["urma"]:
            hours.append(t)
        t += timedelta(hours=1)
print(len(hours), "URMA hours to fetch", flush=True)
with ThreadPoolExecutor(3) as ex:
    for h, v in ex.map(lambda h: (h, c.grib_urma_hour(cfg, h)), hours):
        if v:
            have["urma"][h.isoformat()] = list(v)
f.write_text(json.dumps(have))

key = urllib.parse.quote(os.environ["WUNDERGROUND_API_KEY"])
nearby = json.load(open(c.LOG / "wu_nearby.json"))["stations"]
stations = [r[1] for r in nearby if r[5] >= 0][:7]
wu = {}
for sid in stations:
    wu[sid] = {}
    for dstr in [f"202601{d:02d}" for d in range(24, 32)] + ["20260201", "20260202", "20260203"]:
        try:
            d = c.get_json(f"https://api.weather.com/v2/pws/history/hourly?stationId={sid}&format=json&units=e&numericPrecision=decimal&date={dstr}&apiKey={key}")
        except Exception:
            d = None
        for o in (d or {}).get("observations", []):
            im = o.get("imperial", {})
            wu[sid][datetime.fromisoformat(o["obsTimeUtc"].replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0).isoformat()] = [im.get("tempAvg"), im.get("windspeedAvg"), im.get("dewptAvg")]
        time.sleep(2.2)
    print(sid, len(wu[sid]), "hours", flush=True)
json.dump(wu, open(c.LOG / "wu_snowpack.json", "w"))

base = json.load(open(c.LOG / "autumn_baseline.json"))      # autumn, clear and calm nights, station minus URMA, per station (kept in log/, not in the repo)
print(f"\n{'night of':<11}{'cloud%':>7}{'URMA wind':>10}" + "".join(f"{s[-3:]:>8}" for s in stations if s in base) + "     (station minus URMA, F; autumn clear-calm baseline in the last row)")
rows = []
for n in nights:
    day = datetime.fromisoformat(n).replace(tzinfo=timezone.utc)
    ts = [day + timedelta(hours=12 + i) for i in range(28) if c.night_factor(day + timedelta(hours=12 + i), cfg["lat"], cfg["lon"]) >= 1.0 and (day + timedelta(hours=12 + i)).isoformat() in have["urma"]]
    if len(ts) < 8:
        continue
    cloud = st.mean(have["urma"][t.isoformat()][1] for t in ts); uw = st.mean(have["urma"][t.isoformat()][2] for t in ts)
    vals = {}
    for s in stations:
        x = [wu[s][t.isoformat()][0] - have["urma"][t.isoformat()][0] for t in ts if t.isoformat() in wu[s] and wu[s][t.isoformat()][0] is not None]
        if len(x) >= 6 and s in base:
            vals[s] = st.mean(x)
    rows.append((n, cloud, uw, vals))
    print(f"{n:<11}{cloud:>7.0f}{uw:>10.1f}" + "".join(f"{vals.get(s, float('nan')):>+8.1f}" for s in stations if s in base))
print(f"{'autumn':<28}" + "".join(f"{base[s]:>+8.1f}" for s in stations if s in base))
print("\nclear (night cloud < 30%) and calm (URMA wind < 8 mph) pack nights:")
good = [r for r in rows if r[1] < 30 and r[2] < 8]
for n, cl, uw, v in good:
    d = {s: v[s] - base[s] for s in v}
    print(f"  {n}: cloud {cl:.0f}%, URMA wind {uw:.1f} mph; change from the autumn baseline (F): " + ", ".join(f"{s[-3:]} {x:+.1f}" for s, x in d.items()) + f"  | mean change {st.mean(d.values()):+.1f}")
if not good:
    print("  none; the clearest nights:", [(r[0], round(r[1]), round(r[2], 1)) for r in sorted(rows, key=lambda r: r[1])[:3]])
