#!/usr/bin/env python3
"""Hourly history of nearby Weather Underground personal stations (the ids in log/wu_nearby.json), into log/wu_hourly.json.

  python wu_pull.py 2026-10-01 2026-11-18     only station ids and dates go out (plus the key in WUNDERGROUND_API_KEY); resumes where it stopped
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
for line in (HERE / ".env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
KEY = urllib.parse.quote(os.environ["WUNDERGROUND_API_KEY"])


def get(url):
    for i in range(5):
        try:
            # the Weather Company guide: requests must ask for gzip ("Accept-Encoding: gzip"), the only encoding supported
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept-Encoding": "gzip"}), timeout=30) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    body = gzip.decompress(body)
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code == 204:
                return None
            if e.code in (429, 500, 502, 503) and i < 4:
                time.sleep(5 * (i + 1))
                continue
            raise
        except (TimeoutError, urllib.error.URLError):
            time.sleep(3 * (i + 1))
    return None


def main(d0, d1):
    site = os.environ.get("WU_SITE", "")                  # a second place: WU_SITE=name reads log/wu_nearby_name.json and writes log/wu_hourly_name.json
    nearby = json.loads((HERE / "log" / (f"wu_nearby_{site}.json" if site else "wu_nearby.json")).read_text())["stations"]
    chosen = [r for r in nearby if r[5] >= 0][:10]       # r = [km, id, name, lat, lon, qc]
    f = HERE / "log" / (f"wu_hourly_{site}.json" if site else "wu_hourly.json")
    data = json.loads(f.read_text()) if f.exists() else {}
    day0, day1 = date.fromisoformat(d0), date.fromisoformat(d1)
    for km, sid, name, la, lo, qc in chosen:
        st = data.setdefault(sid, {"km": km, "lat": la, "lon": lo, "elev_ft": None, "hours": {}, "days_done": []})
        if st["elev_ft"] is None:
            cur = get(f"https://api.weather.com/v2/pws/observations/current?stationId={sid}&format=json&units=e&numericPrecision=decimal&apiKey={KEY}")
            if cur and cur.get("observations"):
                st["elev_ft"] = cur["observations"][0]["imperial"].get("elev")
            time.sleep(2.2)
        day = day0
        while day <= day1:
            ds = day.strftime("%Y%m%d")
            if ds not in st["days_done"]:
                d = get(f"https://api.weather.com/v2/pws/history/hourly?stationId={sid}&format=json&units=e&numericPrecision=decimal&date={ds}&apiKey={KEY}")
                for o in (d or {}).get("observations", []):
                    im = o.get("imperial", {})
                    st["hours"][o["obsTimeUtc"]] = [im.get("tempAvg"), im.get("dewptAvg"), im.get("windspeedAvg"), o.get("humidityAvg"), o.get("solarRadiationHigh")]
                st["days_done"].append(ds)
                time.sleep(2.2)
            day += timedelta(days=1)
        f.write_text(json.dumps(data))
        print(f"{sid} ({km} km, elev {st['elev_ft']}): {len(st['hours'])} hours", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
