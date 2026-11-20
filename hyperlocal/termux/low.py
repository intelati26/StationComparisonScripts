#!/usr/bin/env python3
"""Tonight's low from Open-Meteo alone (no key, standard library only: runs in Termux).

  python low.py                    the place in .env (WEATHER_LAT, WEATHER_LON; the site's HYPERLOCAL_AMP too), else termux-location (Termux:API), else the last place saved in ~/.lowtemp
  python low.py LAT LON            a place
  python low.py LAT LON --amp 10   this site's amplitude in F (typical site 5; the original station 10)

The equation (hyperlocal/EQUATION.md), for each night hour:
  drop = amp x night x exp(-(U925/30 mph)^3) x (1 - 0.8 x cloud) x min(1, (T - Td)/12)      low = min(T - drop)
with T, Td, cloud and the wind about 450 m above the ground from the HRRR (GFS outside the US). The model's dewpoint runs high (0.6 F in summer, 2.1 F in fall at one station); 1, 3 and 2 F are taken off by season (3 in fall also absorbs the equation's own warm bias with forecast inputs).
Only coordinates rounded to one decimal (about 10 km) are sent unless --decimals says otherwise."""
import argparse, json, math, os, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

def load_env():
    """The same .env as the rest of hyperlocal (WEATHER_LAT, WEATHER_LON, HYPERLOCAL_AMP, HYPERLOCAL_TD_ADJUST): looked for beside this file, in its parent folder, in the current folder, in ~/storage/dcim/weather (a folder you can edit from the phone's file manager) and in the home folder."""
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here, os.path.dirname(here), os.getcwd(), os.path.expanduser("~/storage/dcim/weather"), os.path.expanduser("~")):
        path = os.path.join(folder, ".env")
        if os.path.exists(path):
            for line in open(path).read().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
            return path


load_env()
p = argparse.ArgumentParser()
p.add_argument("lat", nargs="?", type=float)
p.add_argument("lon", nargs="?", type=float)
p.add_argument("--amp", type=float, default=float(os.environ.get("HYPERLOCAL_AMP") or 5.0), help="site amplitude in F: 5 typical, 10 for a cold-prone hollow (default: HYPERLOCAL_AMP from .env, else 5)")
p.add_argument("--td-adjust", type=float, default=float(os.environ["HYPERLOCAL_TD_ADJUST"]) if os.environ.get("HYPERLOCAL_TD_ADJUST") else None, help="F taken off the forecast dewpoint (default: 1 in Jun-Aug, 3 in Sep-Nov, 2 otherwise)")
p.add_argument("--bias", type=float, default=float(os.environ.get("HYPERLOCAL_BIAS") or 0.0), help="F added to the predicted low (default 0: the bias depends on the forecast it is fed: the equation's low ran 1.0 F warm on the NBM bulletin and 1.9 F cold on Open-Meteo's HRRR history; --log keeps each night's prediction so it can be scored; HYPERLOCAL_BIAS in .env)")
p.add_argument("--sd", type=float, default=float(os.environ.get("HYPERLOCAL_SD") or 2.8), help="the standard deviation of the predicted low in F, shown as +- (default 2.8, measured on those nights; 0 hides it; HYPERLOCAL_SD in .env)")
p.add_argument("--decimals", type=int, default=1, help="decimals of the coordinates sent (1 is about 10 km)")
p.add_argument("--table", action="store_true", help="print every night hour")
p.add_argument("--nws", action="store_true", help="take the temperature, dewpoint and cloud from the NWS hourly forecast (US only) instead of the HRRR; the wind aloft is still the HRRR's")
p.add_argument("--hourly", action="store_true", help="a table of the night hours: local time, UTC hour, the forecast, the adjustment and the final temperature")
p.add_argument("--log", nargs="?", const="", default=None, metavar="FILE", help="append this run (the night's hours, the low, the options) as one JSON line to FILE (default: HYPERLOCAL_LOG from .env, else ~/.low_log.jsonl) so the predictions can be scored against the station: score_low_log.py")
p.add_argument("--json", action="store_true", help="print the night as JSON (the hours, the low with the bias and the SD) for a plot: plot_low.py; no coordinates in it")
p.add_argument("--json-file", metavar="FILE", help="also write the result (the --json payload) to FILE, for low_plot.py --from FILE: one request serves the table and the picture")
p.add_argument("--notify", action="store_true", help="for a notification: a title line, then the hourly adjustments")
a = p.parse_args()
SAVE = os.path.expanduser("~/.lowtemp")
lat, lon = a.lat, a.lon
if lat is None and os.environ.get("WEATHER_LAT") and os.environ.get("WEATHER_LON"):
    lat, lon = float(os.environ["WEATHER_LAT"]), float(os.environ["WEATHER_LON"])
if lat is None:
    try:                                                    # Termux:API on the phone
        loc = json.loads(subprocess.run(["termux-location", "-p", "network", "-r", "last"], capture_output=True, text=True, timeout=30).stdout)
        lat, lon = loc["latitude"], loc["longitude"]
    except Exception:
        try:
            lat, lon = map(float, open(SAVE).read().split())
        except Exception:
            sys.exit("no place: give LAT LON, put WEATHER_LAT and WEATHER_LON in .env, or install Termux:API for termux-location")
try:
    open(SAVE, "w").write(f"{round(lat, a.decimals)} {round(lon, a.decimals)}")
except OSError:
    pass


LEVELS = (1000, 975, 950, 925, 900, 850, 800, 700)
TARGET_AGL = 450.0        # the wind used is the wind about 450 m above the ground (300-600 m fit equally well); 925 mb is about that high where the ground is near 300 m


LAST_ERR = [""]


def get(model):
    q = {"latitude": round(lat, a.decimals), "longitude": round(lon, a.decimals), "models": model, "timezone": "auto", "forecast_days": 3,
         "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "hourly": "temperature_2m,dew_point_2m,cloud_cover,wind_speed_10m," + ",".join(f"wind_speed_{l}hPa,geopotential_height_{l}hPa" for l in LEVELS), "daily": "sunrise,sunset"}
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q)
    for wait in (0, 3, 8):                                  # a busy or rate-limited answer (429, 5xx, a timeout) is retried twice after a pause
        time.sleep(wait)
        try:
            return json.loads(urllib.request.urlopen(url, timeout=30).read())
        except Exception as e:
            LAST_ERR[0] = f"{model}: {getattr(e, 'code', '') or type(e).__name__} {getattr(e, 'reason', '') or e}".strip()
            if getattr(e, "code", 0) not in (0, 429, 500, 502, 503, 504):
                break
    raise RuntimeError(LAST_ERR[0])


d = None
for model in ("ncep_hrrr_conus", "gfs_seamless"):
    try:
        d = get(model)
        h = d["hourly"]
        if any(v is not None for v in h["wind_speed_925hPa"][:24]) and any(v is not None for v in h["temperature_2m"][:24]) and d.get("elevation") is not None:
            break
    except Exception:
        d = None
if d is None:
    sys.exit("Open-Meteo did not answer" + (f" ({LAST_ERR[0]})" if LAST_ERR[0] else ""))
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


ground = d["elevation"]


def wind_aloft(i):
    """Wind speed about TARGET_AGL above the ground, interpolated in height between the 10 m wind and the pressure levels that are above the ground (so it works over high terrain, where 925 mb is underground)."""
    pts = [(10.0, h["wind_speed_10m"][i])]
    for l in LEVELS:
        w, z = h[f"wind_speed_{l}hPa"][i], h[f"geopotential_height_{l}hPa"][i]
        if w is not None and z is not None and z - ground > 30:
            pts.append((z - ground, w))
    pts = sorted(p for p in pts if p[1] is not None)
    if len(pts) < 2:
        return None
    for (a0, w0), (a1, w1) in zip(pts, pts[1:]):
        if a0 <= TARGET_AGL <= a1:
            return w0 + (w1 - w0) * (TARGET_AGL - a0) / (a1 - a0)
    return pts[-1][1] if TARGET_AGL > pts[-1][0] else pts[0][1]


def nws_hourly():
    """NWS gridpoint forecast -> {local naive hour: (temp F, dewpoint F, cloud %)}; US only. The grid is found from the rounded coordinates."""
    import re
    ua = {"User-Agent": "(hyperlocal-lowtemp)", "Accept": "application/json"}
    g = lambda url: json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=30).read())
    pt = g(f"https://api.weather.gov/points/{round(lat, a.decimals)},{round(lon, a.decimals)}")["properties"]
    grid = g(pt["forecastGridData"])["properties"]
    off = timedelta(seconds=d.get("utc_offset_seconds") or 0)

    def series(prop, conv):
        out = {}
        for item in grid[prop]["values"]:
            if item["value"] is None:
                continue
            start, dur = item["validTime"].split("/")
            t0 = datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None)
            m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?", dur)
            for k in range(int(m.group(1) or 0) * 24 + int(m.group(2) or 0) or 1):
                out[t0 + timedelta(hours=k) + off] = conv(item["value"])
        return out
    tc, dc, sc = series("temperature", lambda c_: c_ * 9 / 5 + 32), series("dewpoint", lambda c_: c_ * 9 / 5 + 32), series("skyCover", lambda v: v)
    return {t: (tc[t], dc.get(t), sc.get(t)) for t in tc}


adj = a.td_adjust if a.td_adjust is not None else (1.0 if 6 <= now.month <= 8 else 3.0 if 9 <= now.month <= 11 else 2.0)      # summer 1 F (measured bias 0.6), fall 3 F (measured bias 2.1, plus the equation's own warm lag when fed forecasts), else 2 F (not measured)
NWS = {}
if a.nws:
    try:
        NWS = nws_hourly()
    except Exception as e:
        print(f"NWS forecast unavailable ({type(e).__name__}): using the HRRR's", file=sys.stderr)
LABEL = "NWS" if NWS else "HRRR"
rows = []
for i, t in enumerate(T):
    if t < now - timedelta(hours=1) or night(t) <= 0 or any(h[k][i] is None for k in ("temperature_2m", "dew_point_2m", "cloud_cover")) or wind_aloft(i) is None:
        continue
    temp, td, cl, u = h["temperature_2m"][i], h["dew_point_2m"][i] - adj, h["cloud_cover"][i], wind_aloft(i)
    if t in NWS and NWS[t][1] is not None and NWS[t][2] is not None:
        temp, td, cl = NWS[t][0], NWS[t][1] - adj, NWS[t][2]
    drop = a.amp * night(t) * math.exp(-(u / 30.0) ** 3) * (1 - 0.8 * cl / 100) * min(1.0, max(0.0, temp - td) / 12.0)
    rows.append((t, temp, td, cl, u, drop, temp - drop))
if not rows:
    sys.exit("no night hours in the forecast yet")
# the first night only (the hours until the first sunrise after the first of them)
first = rows[0][0]
rows = [r for r in rows if r[0] < first + timedelta(hours=16)]
if a.table:
    print(f"{'hour':<7}{'T':>6}{'Td':>6}{'cloud':>7}{'450 m':>7}{'drop':>6}{'pred':>7}")
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


def hour_label(t):
    h = t.hour % 12 or 12
    return f"{h}{'a' if t.hour < 12 else 'p'}"


OFFSET = timedelta(seconds=d.get("utc_offset_seconds") or 0)


def table_rows():
    """Time | Z | forecast | adjustment | final, one row per night hour."""
    out = []
    for t, temp, td, cl, u, dr, pr in rows:
        z = t - OFFSET
        out.append((hour_label(t), f"{z.hour:02d}Z", f"{temp:.0f}", f"{-dr:+.0f}" if dr >= 0.5 else "0", f"{pr:.0f}"))
    return out


def table_text(sep="|", pad=False):
    head = ("Time", "Z", LABEL, "Adj", "Final")
    body = table_rows()
    if pad:
        w = [max(len(r[i]) for r in [head] + body) for i in range(5)]
        return "\n".join("  ".join(c.rjust(w[i]) for i, c in enumerate(r)) for r in [head] + body)
    return "\n".join(sep.join(r) for r in [head] + body)


LOW = pl[6] + a.bias                                  # the bias-corrected low; the hourly table below stays as the equation gives it
SD = f" +-{a.sd:g}" if a.sd else ""
zt = lambda t: (t - OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ")
PAYLOAD = {"low": round(LOW, 1), "equation_low": round(pl[6], 1), "low_utc": zt(pl[0]), "forecast_low": round(fl[1], 1), "rule": round(rule, 1), "bias": a.bias, "sd": a.sd, "amp": a.amp, "source": LABEL, "td_adjust": adj,
           "utc_offset_seconds": d.get("utc_offset_seconds") or 0, "cloud_mean": round(cloud), "wind_aloft_max": round(umax),
           "hours": [{"utc": zt(t), "forecast": round(temp, 1), "dewpoint": round(td, 1), "cloud": cl, "wind_aloft": round(u, 1), "drop": round(dr, 2), "final": round(pr, 1)} for t, temp, td, cl, u, dr, pr in rows]}
if a.log is not None:
    path = os.path.expanduser(a.log or os.environ.get("HYPERLOCAL_LOG") or "~/.low_log.jsonl")
    with open(path, "a") as lf:
        lf.write(json.dumps({"run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **PAYLOAD}) + "\n")
if a.json_file:
    with open(os.path.expanduser(a.json_file), "w") as jf:
        json.dump(PAYLOAD, jf)
if a.json:
    print(json.dumps(PAYLOAD))
elif a.notify:
    print(f"low {LOW:.0f}F{SD} at {hour_label(pl[0])} (forecast {fl[1]:.0f}F, rule {rule:.0f}F)")
    print(table_text())
    print(f"cloud {cloud:.0f}%, wind aloft up to {umax:.0f} mph")
else:
    if a.hourly:
        print(table_text(pad=True))
    print(f"forecast low {fl[1]:.0f}F at {fl[0]:%a %H}:00 | equation (site amplitude {a.amp:g}) {LOW:.0f}F{SD} at {pl[0]:%a %H}:00 (the equation gives {pl[6]:.0f}F, bias {a.bias:+g}F applied) | mental rule {rule:.0f}F ({why})")
    print(f"night: cloud {cloud:.0f}%, wind aloft (450 m) up to {umax:.0f} mph, dewpoint {td_eve:.0f}F after taking {adj:g} off. ({d.get('generationtime_ms', 0):.0f} ms)")
