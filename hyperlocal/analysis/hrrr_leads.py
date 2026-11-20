#!/usr/bin/env python3
"""925 hPa wind at the point from one HRRR run per evening, at leads 0-15 h, from NOAA's HRRR archive on AWS (free): log/hrrr925_run22.json
  python hrrr_leads.py 2026-10-01 2026-11-18 [run hour 22]       resumable
Only the index, then the UGRD and VGRD 925 mb messages (about 650 KB each) are requested; the point is read here with gdallocationinfo."""
import json, math, os, subprocess, sys, tempfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

c.load_env()
lat, lon = float(os.environ["WEATHER_LAT"]), float(os.environ["WEATHER_LON"])
d0, d1 = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
RUN = int(sys.argv[3]) if len(sys.argv) > 3 else 22
LEVEL = os.environ.get("HRRR_LEVEL", "925 mb")             # "10 m above ground" reads the surface files (wrfsfcf) instead
FILE = "wrfsfcf" if "above ground" in LEVEL else "wrfprsf"
TAG = "hrrr10" if "above ground" in LEVEL else "hrrr925"
f = c.LOG / f"{TAG}_run{RUN}.json"
have = json.loads(f.read_text()) if f.exists() else {}


def one(args):
    day, lead = args
    key = f"{day}T{RUN:02d}"
    if key in have and str(lead) in have[key]:
        return key, lead, have[key][str(lead)]
    base = f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{day:%Y%m%d}/conus/hrrr.t{RUN:02d}z.{FILE}{lead:02d}.grib2"
    try:
        idx = urllib.request.urlopen(base + ".idx", timeout=30).read().decode().splitlines()
        rows = [l.split(":") for l in idx]
        vals = {}
        for i, r in enumerate(rows):
            if len(r) > 4 and r[3] in ("UGRD", "VGRD") and r[4] == LEVEL:
                a, b = int(r[1]), int(rows[i + 1][1]) - 1
                req = urllib.request.Request(base, headers={"Range": f"bytes={a}-{b}"})
                with tempfile.NamedTemporaryFile(suffix=".grb2") as t:
                    t.write(urllib.request.urlopen(req, timeout=90).read()); t.flush()
                    out = subprocess.run(["gdallocationinfo", "-valonly", "-wgs84", t.name, str(lon), str(lat)], capture_output=True, text=True)
                    vals[r[3]] = float(out.stdout.split()[0])
        if len(vals) == 2:
            return key, lead, math.hypot(vals["UGRD"], vals["VGRD"]) * 2.23694
    except Exception:
        pass
    return key, lead, None


jobs = []
d = d0
while d <= d1:
    jobs += [(d, l) for l in range(0, 16)]
    d += timedelta(days=1)
n = 0
with ThreadPoolExecutor(4) as ex:
    for key, lead, v in ex.map(one, jobs):
        if v is not None:
            have.setdefault(key, {})[str(lead)] = v
        n += 1
        if n % 80 == 0:
            f.write_text(json.dumps(have))
            print(f"{n}/{len(jobs)}", flush=True)
f.write_text(json.dumps(have))
print("done", sum(len(v) for v in have.values()), "values", flush=True)
