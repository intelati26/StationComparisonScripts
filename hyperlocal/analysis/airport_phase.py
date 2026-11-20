#!/usr/bin/env python3
"""Is URMA moving or the station? Through the night, by window: URMA(home cell) - airport, and station - airport (the airport is an open-field reference for how the region cools).
The airport code comes from HOME_AIRPORT (.env); only that code and dates go to the Iowa Environmental Mesonet.   python airport_phase.py"""
import csv, io, math, os, statistics as st, sys, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
import coldpool as c
import winter_tests as w

ap = os.environ.get("HOME_AIRPORT") or sys.exit("set HOME_AIRPORT in .env")
q = urllib.parse.urlencode([("station", ap), ("data", "tmpf"), ("year1", 2026), ("month1", 1), ("day1", 1), ("year2", 2026), ("month2", 11), ("day2", 20), ("tz", "Etc/UTC"),
                            ("format", "onlycomma"), ("latlon", "no"), ("missing", "empty"), ("direct", "no"), ("report_type", "3"), ("report_type", "4")])
rows = list(csv.DictReader(io.StringIO(urllib.request.urlopen("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + q, timeout=120).read().decode())))
AP = {}                                                  # hour -> (distance from the top of the hour in minutes, F): the METAR at HH:53 belongs to the hour HH+1
for r in rows:
    if not r["tmpf"]:
        continue
    t = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    h = (t + timedelta(minutes=30)).replace(minute=0, second=0)
    d = abs((t - h).total_seconds()) / 60
    if h not in AP or d < AP[h][0]:
        AP[h] = (d, float(r["tmpf"]))
print(len(AP), "airport hours")
cfg = w.cfg
out = {g: {p: [[], []] for p in ("early", "deep", "dawn")} for g in ("autumn", "jan", "feb", "mar", "apr", "may", "jul")}
for t, u in sorted(w.URMA.items()):
    g = w.GROUP.get(f"{t.year}-{t.month:02d}"); o = w.OBS.get(t); a = AP.get(t)
    if not g or not o or not a or a[0] > 12 or c.night_factor(t, cfg["lat"], cfg["lon"]) < 1.0:
        continue
    ha = c.hours_after_sunset(t, cfg["lat"], cfg["lon"]); d0 = (t - timedelta(hours=13)).date()
    L = (c.sun_times(cfg["lat"], cfg["lon"], d0 + timedelta(days=1))[0] - c.sun_times(cfg["lat"], cfg["lon"], d0)[1]).total_seconds() / 3600
    ph = "early" if ha < 5 else ("dawn" if L - ha <= 3 else "deep")
    out[g][ph][0].append(u[0] - a[1]); out[g][ph][1].append(o[0] - a[1])
print("mean (F) by part of the night.  URMA-airport | station-airport      (hours)")
print(f"{'window':<8}| " + " | ".join(f"{p:^30}" for p in ("early", "deep", "dawn")))
for g, d in out.items():
    cells = []
    for p in ("early", "deep", "dawn"):
        a, b = d[p]
        cells.append(f"{st.mean(a):>+7.1f} {st.mean(b):>+8.1f}   {st.mean(x - y for x, y in zip(a, b)):>+6.1f}  ({len(a):>3})" if len(a) >= 8 else f"{'-':^30}")
    print(f"{g:<8}| " + " | ".join(cells))
print("(columns in each part: URMA-airport, station-airport, URMA-station)")
