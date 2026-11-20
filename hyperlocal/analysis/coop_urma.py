#!/usr/bin/env python3
"""URMA 2 m temperature at every COOP station in log/coop_stations.json (made by the COOP check), for the night hours of chosen windows. One download of the field per hour, all stations read from it at once (gdallocationinfo).
Resumable: log/coop_urma.json {hour: [F per station]}.   python coop_urma.py START END [START END ...]"""
import json, os, subprocess, sys, tempfile, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, date
import coldpool as c

c.load_env(); cfg = c.load_config()
ST = json.load(open(c.LOG / "coop_stations.json"))
coords = "\n".join(f"{s[4]} {s[3]}" for s in ST) + "\n"
f = c.LOG / "coop_urma.json"
have = json.load(open(f)) if f.exists() else {}


def one(h):
    base = f"https://noaa-urma-pds.s3.amazonaws.com/urma2p5.{h:%Y%m%d}/urma2p5.t{h:%H}z.2dvaranl_ndfd.grb2_wexp"
    try:
        idx = urllib.request.urlopen(base + ".idx", timeout=30).read().decode().splitlines()
        rows = [l.split(":") for l in idx if l.count(":") >= 4]
        for i, r in enumerate(rows):
            if r[3] == "TMP" and r[4] == "2 m above ground":
                a, b = int(r[1]), (int(rows[i + 1][1]) - 1 if i + 1 < len(rows) else None)
                break
        else:
            return h, None
        req = urllib.request.Request(base, headers={"Range": f"bytes={a}-{b if b is not None else ''}"})
        with tempfile.NamedTemporaryFile(suffix=".grb2") as t:
            t.write(urllib.request.urlopen(req, timeout=120).read()); t.flush()
            out = subprocess.run(["gdallocationinfo", "-valonly", "-wgs84", t.name], input=coords, capture_output=True, text=True).stdout.split()
        v = [float(x) for x in out]
        if len(v) != len(ST):
            return h, None
        return h, [(x - 273.15 if x > 150 else x) * 9 / 5 + 32 for x in v]
    except Exception:
        return h, None


a = sys.argv[1:]
hours = []
for d0, d1 in zip(a[::2], a[1::2]):
    day = date.fromisoformat(d0)
    while day <= date.fromisoformat(d1):
        base = datetime(day.year, day.month, day.day, 13, tzinfo=timezone.utc)
        hours += [h for h in (base + timedelta(hours=i) for i in range(24)) if c.night_factor(h, cfg["lat"], cfg["lon"]) >= 1.0 and h.isoformat() not in have]
        day += timedelta(days=1)
print(len(hours), "hours to fetch for", len(ST), "stations", flush=True)
n = 0
with ThreadPoolExecutor(3) as ex:
    for h, v in ex.map(one, hours):
        if v:
            have[h.isoformat()] = v
        n += 1
        if n % 25 == 0:
            f.write_text(json.dumps(have)); print(f"{n}/{len(hours)}", flush=True)
f.write_text(json.dumps(have)); print("done", len(have), flush=True)
