#!/usr/bin/env python3
"""The 19Z NBS (National Blend of Models text bulletin, 3-hourly) forecast for the nearest airport, from the Iowa Environmental Mesonet's MOS archive: log/nbs_19z.json {run date (the 18Z or 19Z run): [[ftime, tmp F, dpt F, sky %, wind kt, gust kt]]}.
The airport code comes from HOME_AIRPORT in .env (only it and dates are sent to IEM).   python nbs_pull.py 2025-02-01 2026-11-18       resumable by month"""
import csv, io, json, os, sys, time, urllib.error, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, datetime, timedelta
import coldpool as c

c.load_env()
ap = os.environ.get("HOME_AIRPORT") or sys.exit("set HOME_AIRPORT in .env")
st = ("K" + ap) if len(ap) == 3 else ap
f = c.LOG / "nbs_19z.json"
have = json.loads(f.read_text()) if f.exists() else {}
done = set(have.get("_months", []))
d0, d1 = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
month = date(d0.year, d0.month, 1)
fl = lambda v: float(v) if v not in ("", None) else None
while month <= d1:
    nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    key = month.strftime("%Y-%m")
    if key not in done:
        q = urllib.parse.urlencode({"station": st, "model": "NBS", "sts": f"{month}T00:00Z", "ets": f"{nxt}T00:00Z", "format": "csv"})
        body = None
        for i in range(6):
            try:
                body = urllib.request.urlopen("https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py?" + q, timeout=180).read().decode(); break
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise
                print("IEM busy (429), waiting", flush=True); time.sleep(30 * (i + 1))
        if body is None:
            sys.exit("IEM kept answering 429; run again later")
        n = 0
        for r in csv.DictReader(io.StringIO(body)):
            if r["runtime"][11:13] in ("18", "19"):            # the bulletin cycles are 01/07/13/19Z in 2025 and 00/06/12/18Z in November 2026: the run nearest to 19Z either way
                have.setdefault(r["runtime"][:10], []).append([r["ftime"], fl(r["tmp"]), fl(r["dpt"]), fl(r["sky"]), fl(r["wsp"]), fl(r["gst"])]); n += 1
        done.add(key); have["_months"] = sorted(done)
        f.write_text(json.dumps(have)); print(key, n, "rows from the 19Z runs", flush=True)
        time.sleep(15)
    month = nxt
print("done", flush=True)
