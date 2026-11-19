#!/usr/bin/env python3
"""Nights with and without snow reported by the nearest airport (METAR present weather SN), winter 2025-26.   python snow_nights.py AIRPORT   (the airport code, or HOME_AIRPORT in .env)
snow night : snow reported within the 72 h before the evening, and the airport temperature has not been above 40 F since (cover probably still on the ground)
control    : no snow reported in the previous 7 days
writes log/snow_nights_<airport>.json"""
import csv, io, json, os, re, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import coldpool as c

ap = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HOME_AIRPORT") or sys.exit("give the airport code (or set HOME_AIRPORT in .env)")
y1, y2 = ((2025, 11, 20), (2026, 3, 25))
q = urllib.parse.urlencode([("station", ap), ("data", "tmpf"), ("data", "wxcodes"), ("year1", y1[0]), ("month1", y1[1]), ("day1", y1[2]), ("year2", y2[0]), ("month2", y2[1]), ("day2", y2[2]),
                            ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"), ("missing", "empty"), ("trace", "T"), ("direct", "no"), ("report_type", "3"), ("report_type", "4")])
rows = list(csv.DictReader(io.StringIO(urllib.request.urlopen("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + q, timeout=120).read().decode())))
obs = []
for r in rows:
    try:
        t = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        continue
    obs.append((t, float(r["tmpf"]) if r["tmpf"] not in ("", "M") else None, "SN" in (r.get("wxcodes") or "")))
snow_times = [t for t, _, sn in obs if sn]
cfg = c.load_config()                                  # WEATHER_LAT / WEATHER_LON from .env: only used for the sunset time (the airport is within a few km)
nights = []
d = datetime(2025, 12, 1, tzinfo=timezone.utc).date()
while d <= datetime(2026, 3, 15, tzinfo=timezone.utc).date():
    start = datetime(d.year, d.month, d.day, 23, tzinfo=timezone.utc)           # the evening of this date (about sunset + 2 h in winter: 00Z)
    recent = [t for t in snow_times if start - timedelta(hours=72) <= t <= start]
    kind = None
    if recent:
        since = [T for t, T, _ in obs if recent[-1] <= t <= start and T is not None]
        kind = "snow" if since and max(since) <= 40 else "melting"
    elif not [t for t in snow_times if start - timedelta(days=7) <= t <= start]:
        kind = "control"
    nights.append({"night": d.isoformat(), "kind": kind})
    d += timedelta(days=1)
import collections
print(ap, dict(collections.Counter(n["kind"] for n in nights)), "| snow reports:", len(snow_times), "on", len({t.date() for t in snow_times}), "days")
json.dump(nights, open(c.LOG / f"snow_nights_{ap}.json", "w"))
print("snow nights:", [n["night"] for n in nights if n["kind"] == "snow"])
