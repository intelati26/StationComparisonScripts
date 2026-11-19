#!/usr/bin/env python3
"""Tonight's low from Open-Meteo alone (no key, standard library only: runs in Termux).

  python low.py                    uses termux-location (Termux:API) or the last place saved in ~/.lowtemp
  python low.py LAT LON            a place
  python low.py LAT LON --amp 10   this site's amplitude in F (typical site 5; the original station 10)

The equation (hyperlocal/EQUATION.md), for each night hour:
  drop = amp x night x exp(-(U925/30 mph)^3) x (1 - 0.8 x cloud) x min(1, (T - Td)/12)      low = min(T - drop)
with T, Td, cloud and the 925 hPa wind from the HRRR (GFS outside the US). The model's dewpoint runs high (0.6 F in summer, 2.1 F in fall at one station); 1, 3 and 2 F are taken off by season (3 in fall also absorbs the equation's own warm bias with forecast inputs).
Only coordinates rounded to one decimal (about 10 km) are sent unless --decimals says otherwise."""
import argparse, json, math, os, subprocess, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

p = argparse.ArgumentParser()
p.add_argument("lat", nargs="?", type=float)
p.add_argument("lon", nargs="?", type=float)
p.add_argument("--amp", type=float, default=5.0, help="site amplitude in F: 5 typical, 10 for a cold-prone hollow")
p.add_argument("--td-adjust", type=float, default=None, help="F taken off the forecast dewpoint (default: 1 in Jun-Aug, 3 in Sep-Nov, 2 otherwise)")
p.add_argument("--decimals", type=int, default=1, help="decimals of the coordinates sent (1 is about 10 km)")
p.add_argument("--table", action="store_true", help="print every night hour")
a = p.parse_args()
SAVE = os.path.expanduser("~/.lowtemp")
lat, lon = a.lat, a.lon
if lat is None:
    try:                                                    # Termux:API on the phone
        loc = json.loads(subprocess.run(["termux-location", "-p", "network", "-r", "last"], capture_output=True, text=True, timeout=30).stdout)
        lat, lon = loc["latitude"], loc["longitude"]
    except Exception:
        try:
            lat, lon = map(float, open(SAVE).read().split())
        except Exception:
            sys.exit("no place: give LAT LON (or install Termux:API for termux-location)")
try:
    open(SAVE, "w").write(f"{round(lat, a.decimals)} {round(lon, a.decimals)}")
except OSError:
    pass


def get(model):
    q = {"latitude": round(lat, a.decimals), "longitude": round(lon, a.decimals), "models": model, "timezone": "auto", "forecast_days": 3,
         "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "hourly": "temperature_2m,dew_point_2m,cloud_cover,wind_speed_925hPa", "daily": "sunrise,sunset"}
    return json.loads(urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q), timeout=30).read())


d = None
for model in ("ncep_hrrr_conus", "gfs_seamless"):
    try:
        d = get(model)
        h = d["hourly"]
        if any(v is not None for v in h["wind_speed_925hPa"][:24]) and any(v is not None for v in h["temperature_2m"][:24]):
            break
    except Exception:
        d = None
if d is None:
    sys.exit("Open-Meteo did not answer")
h = d["hourly"]
T = [datetime.fromisoformat(t) for t in h["time"]]
now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=d.get("utc_offset_seconds") or 0)       # the place's clock, as the forecast's times are
sun = [(datetime.fromisoformat(r), datetime.fromisoformat(s)) for r, s in zip(d["daily"]["sunrise"], d["daily"]["sunset"])]


def night(t):
    for i, (rise, sett) in enumerate(sun):
        if i + 1 >= len(sun):
            break
        nxt = sun[i + 1][0]
        if sett <= t < nxt + timedelta(hours=2):
            if t < sett + timedelta(hours=2):
                return (t - sett).total_seconds() / 7200
            return 1.0 if t < nxt else 1 - (t - nxt).total_seconds() / 7200
    return 0.0


adj = a.td_adjust if a.td_adjust is not None else (1.0 if 6 <= now.month <= 8 else 3.0 if 9 <= now.month <= 11 else 2.0)      # summer 1 F (measured bias 0.6), fall 3 F (measured bias 2.1, plus the equation's own warm lag when fed forecasts), else 2 F (not measured)
rows = []
for i, t in enumerate(T):
    if t < now - timedelta(hours=1) or night(t) <= 0 or any(h[k][i] is None for k in ("temperature_2m", "dew_point_2m", "cloud_cover", "wind_speed_925hPa")):
        continue
    temp, td, cl, u = h["temperature_2m"][i], h["dew_point_2m"][i] - adj, h["cloud_cover"][i], h["wind_speed_925hPa"][i]
    drop = a.amp * night(t) * math.exp(-(u / 30.0) ** 3) * (1 - 0.8 * cl / 100) * min(1.0, max(0.0, temp - td) / 12.0)
    rows.append((t, temp, td, cl, u, drop, temp - drop))
if not rows:
    sys.exit("no night hours in the forecast yet")
# the first night only (the hours until the first sunrise after the first of them)
first = rows[0][0]
rows = [r for r in rows if r[0] < first + timedelta(hours=16)]
if a.table:
    print(f"{'hour':<7}{'T':>6}{'Td':>6}{'cloud':>7}{'925mb':>7}{'drop':>6}{'pred':>7}")
    for t, temp, td, cl, u, drop, pr in rows:
        print(f"{t:%a %H}:00{temp:>6.0f}{td:>6.0f}{cl:>6.0f}%{u:>6.0f}{drop:>6.1f}{pr:>7.0f}")
fl = min(rows, key=lambda r: r[1]); pl = min(rows, key=lambda r: r[6])
cloud = sum(r[3] for r in rows) / len(rows); umax = max(r[4] for r in rows); td_eve = rows[0][2]
if cloud < 20 and umax < 20:
    rule = min(fl[1], td_eve - 4); why = "clear and calm: the evening dewpoint less 4F, or the forecast low if that is lower"
elif umax >= 30 or cloud >= 60:
    rule = fl[1] - 1; why = "cloudy or breezy aloft: the forecast low less 1F"
else:
    rule = fl[1] - 3; why = "in between: the forecast low less 3F"
print(f"forecast low {fl[1]:.0f}F at {fl[0]:%a %H}:00 | equation (site amplitude {a.amp:g}) {pl[6]:.0f}F at {pl[0]:%a %H}:00 | mental rule {rule:.0f}F ({why})")
print(f"night: cloud {cloud:.0f}%, 925 mb wind up to {umax:.0f} mph, dewpoint {td_eve:.0f}F after taking {adj:g} off. ({d.get('generationtime_ms', 0):.0f} ms)")
