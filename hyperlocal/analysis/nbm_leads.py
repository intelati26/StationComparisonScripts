#!/usr/bin/env python3
"""NBM (the National Blend of Models, the base of the NWS point forecast) from the 22Z run each evening, leads 1-16 h, at the point: 2 m temperature, 2 m dewpoint, 10 m wind, cloud cover.
From NOAA's NBM archive on AWS (free): log/nbm_run22.json.   python nbm_leads.py 2026-10-01 2026-11-18
Only the index and the four messages (about 2 MB each) are requested; the point is read here with gdallocationinfo."""
import json, math, os, subprocess, sys, tempfile, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

c.load_env()
lat, lon = float(os.environ["WEATHER_LAT"]), float(os.environ["WEATHER_LON"])
d0, d1 = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
RUN = 22
f = c.LOG / os.environ.get("NBM_OUT", "nbm_run22.json")      # NBM_OUT: another file, so a second pull does not overwrite this one
have = json.loads(f.read_text()) if f.exists() else {}
WANT = (("TMP", "2 m above ground", "T"), ("DPT", "2 m above ground", "D"), ("WIND", "10 m above ground", "W"), ("TCDC", "surface", "C"))


def one(args):
    day, lead = args
    key = f"{day}T{RUN:02d}"
    if key in have and str(lead) in have[key]:
        return key, lead, have[key][str(lead)]
    base = f"https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.{day:%Y%m%d}/{RUN:02d}/core/blend.t{RUN:02d}z.core.f{lead:03d}.co.grib2"
    try:
        rows = [l.split(":") for l in urllib.request.urlopen(base + ".idx", timeout=30).read().decode().splitlines()]
        vals = {}
        for i, r in enumerate(rows):
            for name, level, tag in WANT:
                if len(r) > 5 and r[3] == name and r[4] == level and "std dev" not in r[5] and ":" not in r[6:7] and tag not in vals and "prob" not in r[5] and r[5].endswith("fcst"):
                    a, b = int(r[1]), int(rows[i + 1][1]) - 1
                    req = urllib.request.Request(base, headers={"Range": f"bytes={a}-{b}"})
                    with tempfile.NamedTemporaryFile(suffix=".grb2") as t:
                        t.write(urllib.request.urlopen(req, timeout=90).read()); t.flush()
                        out = subprocess.run(["gdallocationinfo", "-valonly", "-wgs84", t.name, str(lon), str(lat)], capture_output=True, text=True)
                        vals[tag] = float(out.stdout.split()[0])
        if len(vals) == 4:
            tf = lambda v: (v - 273.15 if v > 150 else v) * 9 / 5 + 32
            return key, lead, [tf(vals["T"]), tf(vals["D"]), vals["W"] * 2.23694, vals["C"]]
    except Exception:
        pass
    return key, lead, None


jobs = []
d = d0
while d <= d1:
    jobs += [(d, l) for l in range(1, 17)]
    d += timedelta(days=1)
n = 0
with ThreadPoolExecutor(4) as ex:
    for key, lead, v in ex.map(one, jobs):
        if v is not None:
            have.setdefault(key, {})[str(lead)] = v
        n += 1
        if n % 80 == 0:
            f.write_text(json.dumps(have)); print(f"{n}/{len(jobs)}", flush=True)
f.write_text(json.dumps(have))
print("done", sum(len(v) for v in have.values()), "values", flush=True)
