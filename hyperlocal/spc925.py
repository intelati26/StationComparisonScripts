#!/usr/bin/env python3
"""Keep SPC's 925 mb mesoanalysis crop each hour, so tonight's analyses can be scrolled through afterwards.
  python spc925.py [s14 s20 ...]      sectors: s14 Central Plains, s15 Southern Plains, s20 Midwest, s13 Northern Plains, s19 national ...
Run it hourly (cron, or a Termux:Tasker task). Each picture is saved as maps/spc925/<sector>_<YYYYMMDDHH>Z.gif (the hour is taken from the server's Last-Modified time); an unchanged picture is not kept twice.
This is an analysis (the RAP-based mesoanalysis), not a forecast: SPC's HRRR and HREF viewers have no 925 mb wind."""
import hashlib, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps", "spc925")
os.makedirs(out, exist_ok=True)
for sec in (sys.argv[1:] or ["s14"]):
    r = urllib.request.urlopen(f"https://www.spc.noaa.gov/exper/mesoanalysis/{sec}/925mb/925mb.gif", timeout=60)
    body = r.read()
    t = parsedate_to_datetime(r.headers["Last-Modified"]).astimezone(timezone.utc) if r.headers.get("Last-Modified") else datetime.now(timezone.utc)
    t = (t + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)      # the picture is the analysis for the nearest hour (the file is written a little before it)
    path = os.path.join(out, f"{sec}_{t:%Y%m%d%H}Z.gif")
    same = os.path.exists(path) and hashlib.md5(open(path, "rb").read()).hexdigest() == hashlib.md5(body).hexdigest()
    if not same:
        open(path, "wb").write(body)
    print(("kept   " if not same else "already ") + path)
