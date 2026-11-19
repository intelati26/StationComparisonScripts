#!/usr/bin/env python3
"""Valley cold-air-pool predictor, v3 (stdlib only).

  coldpool.py forecast   prediction table for the next 24 h; logs tonight's predicted low
  coldpool.py backfill C  goes back night by night through the downloaded cache, C credits at most
  coldpool.py gribfill D  the same from the AWS GRIB files, no credits (slow), back to date D (YYYY-MM-DD)
  coldpool.py track [H]   the last H hours (48) in 6 hour blocks: analysis minus station, modeled drop, wind, cloud
  coldpool.py fit [N]    fits the model's coefficients to the weather_archive.db of StationComparisonScripts

Differences from v2: no invented data (it stops when a source fails), cloud cover and
dewpoint come from the NWS gridpoint data, night is sunset to sunrise for the date, and
everything is logged so the coefficients can be fitted later instead of guessed.
Config: the same environment variables (or .env) as StationComparisonScripts: AMBIENT_API_KEY, AMBIENT_APP_KEY,
AMBIENT_STATION_MAC, WEATHER_LAT, WEATHER_LON, WEATHER_DB_PATH; plus HYPERLOCAL_VALLEY_FT and HYPERLOCAL_RIM_FT.
Coefficients come from log/fit.json when `fit` has been run, else the provisional defaults.
"""
import csv, io, json, math, os, re, sqlite3, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
LOG = HERE / "log"
UA = {"User-Agent": "(hyperlocal-coldpool, hyperlocal-coldpool)", "Accept": "application/json"}


def get_json(url, tries=6):
    """GET and parse; a 429 (the Ambient key allows about one request a second, shared with anything else using it) waits and tries again."""
    import time
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=dict(UA, **{"Accept-Encoding": "gzip"})), timeout=30) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    body = gzip.decompress(body)
                return json.loads(body.decode())
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
        except (TimeoutError, urllib.error.URLError):          # the Ambient API is slow now and then
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def load_env():
    """A .env beside this file or in the current directory (KEY=value lines); real environment variables win."""
    for p in (HERE / ".env", Path(".env")):
        if p.exists():
            for line in p.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_config():
    load_env()
    # the valley floor and the rim: HYPERLOCAL_VALLEY_FT / HYPERLOCAL_RIM_FT if set, else the terrain around the station (python terrain.py -> log/terrain.json)
    terrain = json.loads((LOG / "terrain.json").read_text()) if (LOG / "terrain.json").exists() else {}
    floor = os.environ.get("HYPERLOCAL_VALLEY_FT") or terrain.get("floor_ft")
    rim = os.environ.get("HYPERLOCAL_RIM_FT") or terrain.get("rim_ft")
    missing = [k for k in ("WEATHER_LAT", "WEATHER_LON") if not os.environ.get(k)] + (["HYPERLOCAL_VALLEY_FT (or run terrain.py)"] if floor is None else []) + (["HYPERLOCAL_RIM_FT (or run terrain.py)"] if rim is None else [])
    if missing:
        sys.exit("missing in the environment or .env: " + ", ".join(missing))
    cfg = {"lat": float(os.environ["WEATHER_LAT"]), "lon": float(os.environ["WEATHER_LON"]),
           "valley_elevation_ft": float(floor),
           "rim_elevation_ft": float(rim),
           "db": os.environ.get("WEATHER_DB_PATH", "weather_archive.db"),
           "max_deficit_f": 8.0, "cloud_weight": 0.75, "wind_scale_mph": 5.0}
    fit = LOG / "fit.json"
    if fit.exists():
        cfg.update(json.loads(fit.read_text()))
    return cfg


def station(cfg):
    keys = [os.environ.get(k) for k in ("AMBIENT_API_KEY", "AMBIENT_APP_KEY", "AMBIENT_STATION_MAC")]
    if not all(keys):
        sys.exit("AMBIENT_API_KEY, AMBIENT_APP_KEY and AMBIENT_STATION_MAC must be set")
    api, app, mac = keys
    url = f"https://rt.ambientweather.net/v1/devices/{mac}?apiKey={urllib.parse.quote(api)}&applicationKey={urllib.parse.quote(app)}&limit=1"
    d = get_json(url)
    d = d[0] if isinstance(d, list) else d
    last = d.get("lastData", d)
    return {"time": last["date"], "temp_f": last["tempf"], "wind_mph": last.get("windspeedmph", 0.0), "dew_f": last.get("dewPoint")}


def expand(prop, convert=lambda v: v):
    """NWS gridpoint series ({validTime: 'start/PT3H', value}) -> {hour(UTC): value}."""
    out = {}
    for item in prop["values"]:
        start, dur = item["validTime"].split("/")
        t = datetime.fromisoformat(start.replace("Z", "+00:00"))
        m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?", dur)
        hours = int(m.group(1) or 0) * 24 + int(m.group(2) or 0) or 1
        if item["value"] is None:
            continue
        for h in range(hours):
            out[t + timedelta(hours=h)] = convert(item["value"])
    return out


def nws_hourly(lat, lon, hours=24):
    pt = get_json(f"https://api.weather.gov/points/{lat},{lon}")["properties"]
    grid = get_json(pt["forecastGridData"])["properties"]
    c2f = lambda c: c * 9 / 5 + 32
    temp = expand(grid["temperature"], c2f)
    dew = expand(grid["dewpoint"], c2f)
    sky = expand(grid["skyCover"])
    wind = expand(grid["windSpeed"], lambda k: k * 0.621371)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = []
    for i in range(hours):
        t = now + timedelta(hours=i)
        if t in temp:
            rows.append({"t": t, "temp_f": temp[t], "dew_f": dew.get(t), "cloud": sky.get(t, 50.0), "wind_mph": wind.get(t, 0.0)})
    if not rows:
        sys.exit("NWS returned no gridpoint data")
    return rows, pt.get("gridId", "")


def sun_times(lat, lon, day):
    """(sunrise, sunset) as UTC datetimes for a date (NOAA approximation, ~±2 min)."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = 0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
    ha = math.degrees(math.acos(math.cos(math.radians(90.833)) / (math.cos(math.radians(lat)) * math.cos(decl)) - math.tan(math.radians(lat)) * math.tan(decl)))
    base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    rise = base + timedelta(minutes=720 - 4 * (lon + ha) - eqt)
    sett = base + timedelta(minutes=720 - 4 * (lon - ha) - eqt)
    return rise, sett


def hours_after_sunset(t, lat, lon):
    """Hours since the sunset that began this night (0 when it is not night)."""
    for d in (t.date() - timedelta(days=1), t.date()):
        sett = sun_times(lat, lon, d)[1]
        nxt = sun_times(lat, lon, d + timedelta(days=1))[0]
        if sett <= t < nxt:
            return (t - sett).total_seconds() / 3600
    return 0.0


def night_factor(t, lat, lon):
    """0 by day, ramping to 1 over 2 h after sunset, holding, ramping back to 0 over 2 h after sunrise."""
    for d in (t.date() - timedelta(days=1), t.date(), t.date() + timedelta(days=1)):
        rise, sett = sun_times(lat, lon, d)
        nxt = sun_times(lat, lon, d + timedelta(days=1))[0]
        if sett <= t < nxt + timedelta(hours=2):
            if t < sett + timedelta(hours=2):
                return (t - sett).total_seconds() / 7200
            if t < nxt:
                return 1.0
            return 1 - (t - nxt).total_seconds() / 7200
    return 0.0


def model(cfg, row, ratio):
    """Predicted valley drop below the NWS temperature. Coefficients are PROVISIONAL until fitted from the logs."""
    depth = max(0.0, cfg["rim_elevation_ft"] - cfg["valley_elevation_ft"])
    eff = row["wind_mph"] * ratio
    if cfg.get("wind_form") == "threshold":
        f_wind = math.exp(-(row["wind_mph"] / cfg["wind_u0"]) ** cfg["wind_p"])        # wind in mph as the analysis (or NWS) gives it, no shelter ratio
    else:
        f_wind = max(0.0, 1 - eff / cfg["wind_scale_mph"])
    f_cloud = 1 - cfg["cloud_weight"] * row["cloud"] / 100
    f_night = night_factor(row["t"], cfg["lat"], cfg["lon"])
    # dry air (a big temperature - dewpoint spread) radiates more: the factor reaches 1 at dep_scale degrees of spread
    ds, dep = cfg.get("dep_scale"), row.get("dep")
    f_dry = min(1.0, max(0.0, dep) / ds) if ds and dep is not None else 1.0
    # the first hours of the night run colder than the later ones (the station cools and decouples early, the grid catches up): 1 + amp * exp(-hours / tau)
    amp = cfg.get("early_amp", 0.0)
    f_early = 1 + amp * math.exp(-max(0.0, hours_after_sunset(row["t"], cfg["lat"], cfg["lon"]) - 2) / cfg.get("early_tau", 3.0)) if amp else 1.0
    drop = cfg["max_deficit_f"] * (1 + depth / 3000) * f_night * f_wind * f_cloud * f_dry * f_early
    return drop, eff


def append(name, header, row):
    LOG.mkdir(exist_ok=True)
    p = LOG / name
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def openmeteo_925(lat, lon):
    """HRRR 925 hPa wind (mph) by hour from Open-Meteo, from the latest runs. Only coordinates rounded to one decimal (about 10 km) are sent."""
    q = {"latitude": round(lat, 1), "longitude": round(lon, 1), "models": "ncep_hrrr_conus", "hourly": "wind_speed_925hPa",
         "wind_speed_unit": "mph", "timezone": "UTC", "forecast_days": 3}
    d = get_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q))["hourly"]
    return {datetime.fromisoformat(t).replace(tzinfo=timezone.utc): v for t, v in zip(d["time"], d["wind_speed_925hPa"]) if v is not None}


# the equation of EQUATION.md: A x night x exp(-(U925/30)^3) x (1 - 0.8 cloud) x min(1, (T - Td)/12), from the 22Z-run test (1.71 F cross-validated)
FORECAST_CFG = {"max_deficit_f": 10, "cloud_weight": 0.8, "dep_scale": 12, "wind_form": "threshold", "wind_u0": 30, "wind_p": 3, "early_amp": 0}


def mental_rule(rows, u925, td_eve, nws_low):
    """The rule of thumb a person can apply to the NWS hourly forecast (clear and calm: about the evening dewpoint; cloudy or breezy: about the forecast low less 2 F)."""
    night = [r for r in rows if r["night"]]
    if not night:
        return None, "no night hours in the forecast"
    cloud = st_mean([r["cloud"] for r in night])
    wind = max(u925.get(r["t"], 0.0) for r in night) if u925 else None
    if wind is not None and cloud < 20 and wind < 20:
        return min(nws_low, td_eve), f"clear ({cloud:.0f}% cloud) and calm (wind aloft up to {wind:.0f} mph): about the evening dewpoint {td_eve:.0f}F"
    if (wind is not None and wind >= 30) or cloud >= 60:
        return nws_low - 1, f"cloudy ({cloud:.0f}%) or breezy (wind aloft up to {wind if wind is None else round(wind)} mph): about the forecast low less 1F"
    return nws_low - 3, f"in between ({cloud:.0f}% cloud, wind aloft up to {wind if wind is None else round(wind)} mph): about the forecast low less 3F"


def cmd_forecast(cfg):
    st = station(cfg)
    rows, grid = nws_hourly(cfg["lat"], cfg["lon"])
    u925 = {}
    try:
        u925 = openmeteo_925(cfg["lat"], cfg["lon"])
    except Exception as e:
        print(f"HRRR 925 hPa wind unavailable ({type(e).__name__}); using the NWS 10 m wind and the URMA-fitted wind term instead")
    fcfg = dict(cfg, **FORECAST_CFG) if u925 else cfg
    ratio = 0.2 if u925 else cfg.get("shelter_ratio", 0.2)
    print(f"station {st['temp_f']:.1f}F {st['wind_mph']:.1f} mph at {st['time']}   NWS grid {grid}   wind term: {'HRRR 925 hPa, latest run' if u925 else 'NWS 10 m (fallback)'}")
    td = st.get("dew_f")
    print(f"dewpoint: the station's {td:.1f}F, held through the night" if td is not None else "dewpoint: the NWS values (the station's is not available)")
    print(f"{'UTC':<6}{'NWS F':>7}{'cloud%':>8}{'NWS wind':>9}{'925 mb':>8}{'drop':>6}{'pred F':>8}")
    pred = []
    for r in rows:
        r["night"] = night_factor(r["t"], cfg["lat"], cfg["lon"]) >= 1.0
        d = td if td is not None else r.get("dew_f")
        r["dep"] = r["temp_f"] - d if d is not None else None
        u = u925.get(r["t"])
        rm = dict(r, wind_mph=u) if u925 and u is not None else r
        drop, _ = model(fcfg, rm, ratio)
        pred.append((r["temp_f"] - drop, r, drop))
        print(f"{r['t']:%H:%M} {r['temp_f']:>7.1f}{r['cloud']:>8.0f}{r['wind_mph']:>9.1f}{(u if u is not None else float('nan')):>8.1f}{drop:>6.1f}{r['temp_f'] - drop:>8.1f}")
    nightp = [p for p in pred if p[1]["night"]] or pred
    low, row, _ = min(nightp, key=lambda x: x[0])
    nws_low = min(p[1]["temp_f"] for p in nightp)
    print(f"\nequation: predicted low {low:.1f}F at {row['t']:%H:%M}Z   (NWS low {nws_low:.1f}F)")
    mental, why = mental_rule(rows, u925, td if td is not None else nws_low, nws_low)
    if mental is not None:
        print(f"mental rule: about {mental:.0f}F   ({why})")
    for r in rows:     # the NWS hourly values, kept to score the forecast winds (and temperatures) against URMA later
        append("nws_forecasts.csv", ["made_utc", "valid_utc", "temp_f", "dew_f", "cloud", "wind_mph", "u925_mph"],
               [datetime.now(timezone.utc).isoformat(timespec="minutes"), r["t"].isoformat(), f"{r['temp_f']:.1f}",
                "" if r["dew_f"] is None else f"{r['dew_f']:.1f}", f"{r['cloud']:.0f}", f"{r['wind_mph']:.1f}", "" if u925.get(r["t"]) is None else f"{u925[r['t']]:.1f}"])
    append("forecasts.csv", ["made_utc", "night_of", "pred_low_f", "mental_low_f", "nws_low_f", "station_dew_f"],
           [datetime.now(timezone.utc).isoformat(timespec="minutes"), rows[0]["t"].date().isoformat(), f"{low:.1f}", "" if mental is None else f"{mental:.1f}", f"{nws_low:.1f}", "" if td is None else f"{td:.1f}"])


def hour_of(text):
    k = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if k.tzinfo is None:
        k = k.replace(tzinfo=timezone.utc)
    return k.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def load_db(cfg):
    """{hour: (temp_f, wind_mph)} for the station and {hour: (temp_f, cloud, wind_mph)} for URMA (RTMA where URMA is missing), from the archive."""
    obs, mod = {}, {}
    if not Path(cfg["db"]).exists():
        return obs, mod
    db = sqlite3.connect(f"file:{Path(cfg['db']).resolve().as_posix()}?mode=ro", uri=True)
    acc = {}
    for ts, t, w, dp in db.execute("SELECT timestamp, temperature, wind_speed, dewpoint FROM station_obs WHERE station=? AND temperature IS NOT NULL",
                               (os.environ.get("HYPERLOCAL_STATION") or sys.exit("set HYPERLOCAL_STATION to the station's name in your database (the station column of station_obs)"),)):
        try:
            a = acc.setdefault(hour_of(ts), [0.0, 0.0, 0, 0.0, 0])
        except ValueError:
            continue
        a[0] += t; a[1] += (w or 0.0); a[2] += 1
        if dp is not None:
            a[3] += dp; a[4] += 1
    obs = {k: (v[0] / v[2], v[1] / v[2], v[3] / v[4] if v[4] else None) for k, v in acc.items()}
    for name in ("rtma", "urma"):                      # urma last, so it wins
        for vt, t, c, w in db.execute("SELECT valid_time, temp_f, cloud_cover, wind_speed FROM model_analysis WHERE model=? AND temp_f IS NOT NULL AND cloud_cover IS NOT NULL", (name,)):
            try:
                mod[hour_of(vt)] = (t, c, w or 0.0)
            except ValueError:
                pass
    return obs, mod


def ambient_history(start, end):
    """Hourly station means from the Ambient API (5-minute records, newest first, 288 a request)."""
    import time
    api, app, mac = (os.environ.get(k) for k in ("AMBIENT_API_KEY", "AMBIENT_APP_KEY", "AMBIENT_STATION_MAC"))
    if not (api and app and mac):
        sys.exit("AMBIENT_API_KEY, AMBIENT_APP_KEY and AMBIENT_STATION_MAC must be set")
    # paged the way StationComparisonScripts' fetch_ambient_ambient does (it is the one that works): endDate as an ISO string, one second before the oldest record seen
    acc, until, last = {}, end.strftime("%Y-%m-%dT%H:%M:%S.000Z"), None
    while until != last:
        last = until
        url = f"https://rt.ambientweather.net/v1/devices/{mac}?apiKey={urllib.parse.quote(api)}&applicationKey={urllib.parse.quote(app)}&endDate={until}&limit=288"
        recs = get_json(url)
        time.sleep(1.1)
        for wait in (3, 6, 12):          # after a few quick pages the API answers an empty one; the same request a few seconds later is answered normally
            if recs:
                break
            time.sleep(wait)
            recs = get_json(url)
        if not recs or not isinstance(recs, list):
            break
        oldest = None
        for r in recs:
            t = datetime.fromtimestamp(r["dateutc"] / 1000, timezone.utc)
            if oldest is None or t < oldest:
                oldest = t
            if start <= t <= end and r.get("tempf") is not None:
                a = acc.setdefault(t.replace(minute=0, second=0, microsecond=0), [0.0, 0.0, 0, 0.0, 0])
                a[0] += r["tempf"]; a[1] += r.get("windspeedmph") or 0.0; a[2] += 1
                if r.get("dewPoint") is not None:
                    a[3] += r["dewPoint"]; a[4] += 1
        if oldest is None or oldest <= start:
            break
        until = (oldest - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {k: (v[0] / v[2], v[1] / v[2], v[3] / v[4] if v[4] else None) for k, v in acc.items()}


def urma_api(cfg, start, end, dataset="urma"):
    """URMA (or RTMA, dataset="rtma") hourly at the point from GribStream: {hour: (temp_f, cloud, wind_mph)}. Costs 4 credits per hour."""
    token = os.environ.get("GRIBSTREAM_TOKEN")
    if not token:
        sys.exit("GRIBSTREAM_TOKEN is not set")
    body = {"fromTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "untilTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "coordinates": [{"lat": cfg["lat"], "lon": cfg["lon"]}],
            "variables": [{"name": "TMP", "level": "2 m above ground", "info": ""}, {"name": "UGRD", "level": "10 m above ground", "info": ""},
                          {"name": "VGRD", "level": "10 m above ground", "info": ""},
                          {"name": "TCDC", "level": "entire atmosphere (considered as a single layer)", "info": ""}]}
    rq = urllib.request.Request(f"https://gribstream.com/api/v2/{dataset}/timeseries", data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + token})
    out = {}
    for r in csv.DictReader(io.StringIO(urllib.request.urlopen(rq, timeout=120).read().decode())):
        try:
            out[hour_of(r["forecasted_time"])] = ((float(r["TMP|2 m above ground|"]) - 273.15) * 9 / 5 + 32,
                                                  float(r["TCDC|entire atmosphere (considered as a single layer)|"]),
                                                  math.hypot(float(r["UGRD|10 m above ground|"]), float(r["VGRD|10 m above ground|"])) * 2.23694)
        except (KeyError, ValueError):
            pass
    return out


def refresh_obs(cache):
    """Cached station hours without a dewpoint (made before it was kept): the Ambient history for their span again, which is free."""
    old = sorted(datetime.fromisoformat(k) for k, v in cache["obs"].items() if len(v) < 3)
    if old:
        print(f"refreshing {len(old)} cached station hours with the dewpoint")
        for k, v in ambient_history(old[0], old[-1] + timedelta(hours=1)).items():
            cache["obs"][k.isoformat()] = v
        LOG.mkdir(exist_ok=True)
        (LOG / "downloaded.json").write_text(json.dumps(cache))


def merge_cache(obs, mod):
    f = LOG / "downloaded.json"
    if f.exists():
        c = json.loads(f.read_text())
        if any(len(v) < 3 for v in c["obs"].values()):
            refresh_obs(c)
        for k, v in c.get("obs", {}).items():
            obs.setdefault(datetime.fromisoformat(k), tuple(v))
        for name in ("mod", "rtma"):
            for k, v in c.get(name, {}).items():
                mod.setdefault(datetime.fromisoformat(k), tuple(v))


def cmd_backfill(cfg, credits):
    """Walk backwards from the oldest night in the cache, one night at a time (Ambient history is free, URMA costs 4 credits per
    night hour), stopping before a night that would take the spending past `credits`."""
    f = LOG / "downloaded.json"
    cache = json.loads(f.read_text()) if f.exists() else {"obs": {}, "mod": {}, "rtma": {}}
    cache.setdefault("rtma", {})
    have = [datetime.fromisoformat(k) for k in list(cache["mod"]) + list(cache["rtma"])]
    if not have:
        sys.exit("nothing cached yet: run `fit 4` first")
    day = (max(have) - timedelta(hours=13)).date() + timedelta(days=1)   # from the newest cached night down: gaps first, then older nights
    spent = 0
    print(f"budget {credits} credits, going back from the night of {day:%a %d %b}")
    while True:
        day -= timedelta(days=1)
        base = datetime(day.year, day.month, day.day, 13, tzinfo=timezone.utc)
        hours = [base + timedelta(hours=i) for i in range(24)]
        latest = datetime.now(timezone.utc) - timedelta(hours=7)          # URMA is not there before that
        hours = [h for h in hours if h <= latest and night_factor(h, cfg["lat"], cfg["lon"]) >= 1.0
                 and h.isoformat() not in cache["mod"] and h.isoformat() not in cache["rtma"]]
        if not hours:
            continue
        cost = 4 * len(hours)
        if spent + cost > credits:
            print(f"stop: the night of {day:%a %d %b} would cost {cost}, {credits - spent} left")
            break
        for k, v in ambient_history(base, base + timedelta(hours=24)).items():
            cache["obs"][k.isoformat()] = v
        got = urma_api(cfg, hours[0], hours[-1] + timedelta(hours=1))
        for k, v in got.items():
            cache["mod"][k.isoformat()] = v
        spent += cost
        LOG.mkdir(exist_ok=True)
        f.write_text(json.dumps(cache))
        print(f"night of {day:%a %d %b}: {len(got)} URMA hours, {cost} credits ({spent} spent)")
    print(f"spent {spent} credits")


GRIB_WANT = (("TMP", "2 m above ground"), ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"),
             ("TCDC", "entire atmosphere (considered as a single layer)"))
GRIB_DPT = (("DPT", "2 m above ground"),)


def grib_urma_hour(cfg, h, want=GRIB_WANT, dataset="urma"):
    """URMA at the point for one hour, straight from NOAA's AWS bucket: the index, then only the four messages (about 6 MB each)
    by byte range, each read with gdallocationinfo. Free but slow. -> (temp_f, cloud, wind_mph) or None."""
    import subprocess, tempfile
    from concurrent.futures import ThreadPoolExecutor
    base = f"https://noaa-{dataset}-pds.s3.amazonaws.com/{dataset}2p5.{h:%Y%m%d}/{dataset}2p5.t{h:%H}z.2dvaranl_ndfd.grb2_wexp"
    try:
        idx = urllib.request.urlopen(base + ".idx", timeout=30).read().decode().splitlines()
    except Exception:
        return None
    rows = [l.split(":") for l in idx if l.count(":") >= 4]
    spans = {}
    for i, r in enumerate(rows):
        for name, level in want:
            if r[3] == name and r[4] == level:
                spans[name] = (int(r[1]), int(rows[i + 1][1]) - 1 if i + 1 < len(rows) else None)
    if len(spans) < len(want):
        return None

    def one(item):
        name, (a, b) = item
        req = urllib.request.Request(base, headers={"Range": f"bytes={a}-{b if b is not None else ''}"})
        with tempfile.NamedTemporaryFile(suffix=".grb2") as f:
            f.write(urllib.request.urlopen(req, timeout=120).read()); f.flush()
            out = subprocess.run(["gdallocationinfo", "-valonly", "-wgs84", f.name, str(cfg["lon"]), str(cfg["lat"])], capture_output=True, text=True)
            return name, float(out.stdout.split()[0])
    try:
        with ThreadPoolExecutor(4) as ex:
            v = dict(ex.map(one, spans.items()))
    except Exception:
        return None
    temp_c = (v["TMP"] - 273.15 if v["TMP"] > 150 else v["TMP"]) if "TMP" in v else None   # gdal gives degrees C for temperature, Kelvin if it did not convert
    if want is GRIB_DPT:
        d = v["DPT"] - 273.15 if v["DPT"] > 150 else v["DPT"]
        return d * 9 / 5 + 32
    return (temp_c * 9 / 5 + 32, v["TCDC"], math.hypot(v["UGRD"], v["VGRD"]) * 2.23694)


def cmd_dptfill(cfg):
    """URMA's own 2 m dewpoint for every night hour the fit uses (cache and archive), from the AWS GRIB files, into log/dpt.json (free, slow)."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    f = LOG / "dpt.json"
    dpt = json.loads(f.read_text()) if f.exists() else {}
    obs, mod = load_db(cfg)
    merge_cache(obs, mod)
    todo = [h for h in sorted(mod) if h.isoformat() not in dpt and night_factor(h, cfg["lat"], cfg["lon"]) >= 1.0]
    print(f"{len(todo)} night hours need the URMA dewpoint ({len(dpt)} done)", flush=True)
    t0 = time.time()

    def one(h):
        time.sleep(0.3)
        return h, grib_urma_hour(cfg, h, GRIB_DPT)
    n = 0
    with ThreadPoolExecutor(3) as ex:
        for h, v in ex.map(one, todo):
            if v is not None:
                dpt[h.isoformat()] = v
            n += 1
            if n % 25 == 0:
                f.write_text(json.dumps(dpt))
                print(f"{n}/{len(todo)}  ({time.time() - t0:.0f} s)", flush=True)
    f.write_text(json.dumps(dpt))
    print(f"done: {len(dpt)} hours with a URMA dewpoint")


def cmd_track(cfg, hours=48):
    """How the analysis and the station have compared lately, in 6 hour blocks (05, 11, 17, 23Z): the analysis minus the station, the station's wind,
    the cloud, and what the fitted model said the drop would be. URMA where it is out (7 h), RTMA for the newest hours. Free: AWS GRIB files, kept in log/track.json."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    now = datetime.now(timezone.utc)
    end = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours)
    f = LOG / "track.json"
    cache = json.loads(f.read_text()) if f.exists() else {}
    obs = ambient_history(start, end + timedelta(hours=1))
    todo = []
    for i in range(hours + 1):
        h = start + timedelta(hours=i)
        ds = "urma" if h <= now - timedelta(hours=7) else "rtma"
        key = h.isoformat()
        if key not in cache or (cache[key][0] != "urma" and ds == "urma"):
            todo.append((h, ds))
    print(f"{len(todo)} hours to fetch", flush=True)

    def one(a):
        h, ds = a
        time.sleep(0.3)
        return h, ds, grib_urma_hour(cfg, h, GRIB_WANT, ds)
    with ThreadPoolExecutor(3) as ex:
        for h, ds, v in ex.map(one, todo):
            if v:
                cache[h.isoformat()] = [ds] + list(v)
    LOG.mkdir(exist_ok=True)
    f.write_text(json.dumps(cache))
    ratio = cfg.get("shelter_ratio", 0.2)
    blocks = {}
    for i in range(hours + 1):
        h = start + timedelta(hours=i)
        a, o = cache.get(h.isoformat()), obs.get(h)
        if not a or not o:
            continue
        b0 = h - timedelta(hours=(h.hour - 5) % 6)                 # the block starts at 05, 11, 17 or 23Z
        dep = a[1] - o[2] if o[2] is not None else None
        drop = model(cfg, {"t": h, "cloud": a[2], "wind_mph": a[3], "dep": dep}, ratio)[0]
        blocks.setdefault(b0, []).append((a[1] - o[0], o[1], a[2], drop, a[0], o[0]))
    print(f"\n{'block (UTC)':<14}{'hrs':>4}{'analysis-station':>18}{'modeled drop':>14}{'station wind':>14}{'cloud':>7}{'station T':>11}  source")
    for b0, xs in sorted(blocks.items()):
        if len(xs) < 3:
            continue
        src = "RTMA" if any(x[4] != "urma" for x in xs) else "URMA"
        print(f"{b0:%a %d %H}Z-{(b0 + timedelta(hours=6)):%H}Z{len(xs):>4}{st_mean([x[0] for x in xs]):>+17.1f}F{st_mean([x[3] for x in xs]):>13.1f}F{st_mean([x[1] for x in xs]):>12.1f} mph{st_mean([x[2] for x in xs]):>6.0f}%{st_mean([x[5] for x in xs]):>10.1f}F  {src}")


def cmd_gribfill(cfg, until):
    """Back from the oldest cached night to `until` (YYYY-MM-DD), a night at a time, URMA from the AWS GRIB files (no credits). Cached after each night."""
    import time
    f = LOG / "downloaded.json"
    cache = json.loads(f.read_text()) if f.exists() else {"obs": {}, "mod": {}, "rtma": {}}
    cache.setdefault("rtma", {})
    have = [datetime.fromisoformat(k) for k in list(cache["mod"]) + list(cache["rtma"])]
    if not have:
        sys.exit("nothing cached yet: run `fit 4` first")
    dbobs, dbmod = load_db(cfg)            # hours the archive already has are not fetched again
    stop = datetime.strptime(until, "%Y-%m-%d").date()
    day = (min(have) - timedelta(hours=13)).date() + timedelta(days=1)
    t0 = time.time()
    print(f"URMA from the AWS GRIB files, back to {stop:%d %b}, starting at the night of {day:%a %d %b}", flush=True)
    while True:
        day -= timedelta(days=1)
        if day < stop:
            break
        base = datetime(day.year, day.month, day.day, 13, tzinfo=timezone.utc)
        hours = [base + timedelta(hours=i) for i in range(24)]
        hours = [h for h in hours if night_factor(h, cfg["lat"], cfg["lon"]) >= 1.0]
        need_mod = [h for h in hours if h.isoformat() not in cache["mod"] and h not in dbmod]
        need_obs = [h for h in hours if h.isoformat() not in cache["obs"] and h not in dbobs]
        if not need_mod and not need_obs:
            continue
        if need_obs:
            for k, v in ambient_history(base, base + timedelta(hours=24)).items():
                cache["obs"][k.isoformat()] = v
        hours = need_mod
        got = 0
        for h in hours:
            r = grib_urma_hour(cfg, h)
            if r:
                cache["mod"][h.isoformat()] = r
                got += 1
            time.sleep(0.5)
        f.write_text(json.dumps(cache))
        print(f"night of {day:%a %d %b}: {got}/{len(hours)} URMA hours   ({time.time() - t0:.0f} s)", flush=True)
    print("done")


def cmd_fit(cfg, days=0):
    """Fit max_deficit_f, cloud_weight and wind_scale_mph: deficit = URMA (the grid's air) minus the station, on night hours
    (2 h past sunset to sunrise). Hours come from the archive and, with `fit N`, from the last N days downloaded (Ambient history
    plus URMA from GribStream, 4 credits per hour), the archive winning where both have an hour. Then a night-by-night report."""
    import io  # noqa
    obs, mod = load_db(cfg)
    print(f"archive: {len(obs)} station hours, {len(mod)} model hours")
    if not days:
        merge_cache(obs, mod)
    if days:
        end = (datetime.now(timezone.utc) - timedelta(hours=7)).replace(minute=0, second=0, microsecond=0)   # URMA lands about 6 h late
        start = end - timedelta(days=days)
        cache_file = LOG / "downloaded.json"
        cache = json.loads(cache_file.read_text()) if cache_file.exists() else {"obs": {}, "mod": {}}
        cache.setdefault("rtma", {})
        for k, v in ambient_history(start, end).items():
            cache["obs"][k.isoformat()] = v
        # URMA only for the night hours (about 11 of 24) that are not already cached: 4 credits an hour
        need = [start + timedelta(hours=i) for i in range(int((end - start).total_seconds() // 3600))]
        need = [h for h in need if night_factor(h, cfg["lat"], cfg["lon"]) >= 1.0 and h.isoformat() not in cache["mod"]]
        runs, cur = [], []
        for h in need:
            if cur and h - cur[-1] != timedelta(hours=1):
                runs.append(cur); cur = []
            cur.append(h)
        if cur:
            runs.append(cur)
        print(f"downloading URMA for {len(need)} night hours in {len(runs)} runs ({len(need) * 4} credits)")
        for run in runs:
            for k, v in urma_api(cfg, run[0], run[-1] + timedelta(hours=1)).items():
                cache["mod"][k.isoformat()] = v
        # night hours URMA does not have yet (it lands about 6 h late): RTMA, about 1 h late; replaced by URMA on a later run
        now_end = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        for k, v in ambient_history(end, now_end).items():
            cache["obs"][k.isoformat()] = v
        gap = [start + timedelta(hours=i) for i in range(int((now_end - start).total_seconds() // 3600))]
        gap = [h for h in gap if night_factor(h, cfg["lat"], cfg["lon"]) >= 1.0 and h.isoformat() not in cache["mod"] and h.isoformat() not in cache["rtma"]]
        if gap:
            print(f"RTMA for {len(gap)} night hours URMA does not have yet ({len(gap) * 4} credits)")
            run = [gap[0]]
            for h in gap[1:] + [None]:
                if h is None or h - run[-1] != timedelta(hours=1):
                    for k, v in urma_api(cfg, run[0], run[-1] + timedelta(hours=1), "rtma").items():
                        cache["rtma"][k.isoformat()] = v
                    run = [h]
                else:
                    run.append(h)
        LOG.mkdir(exist_ok=True)
        cache_file.write_text(json.dumps(cache))
        if any(len(v) < 3 for v in cache["obs"].values()):
            refresh_obs(cache)
        for k, v in cache["obs"].items():
            obs.setdefault(datetime.fromisoformat(k), tuple(v))
        for k, v in cache["mod"].items():
            mod.setdefault(datetime.fromisoformat(k), tuple(v))
        for k, v in cache["rtma"].items():
            mod.setdefault(datetime.fromisoformat(k), tuple(v))
    dptf = LOG / "dpt.json"
    dpt = {datetime.fromisoformat(k): v for k, v in json.loads(dptf.read_text()).items()} if dptf.exists() else {}
    samples = []
    for k in sorted(set(obs) & set(mod)):
        if night_factor(k, cfg["lat"], cfg["lon"]) >= 1.0:
            dew = obs[k][2] if len(obs[k]) > 2 else None
            samples.append((k, mod[k][0] - obs[k][0], mod[k][1], mod[k][2], obs[k][1], obs[k][0], mod[k][0], dew, dpt.get(k)))
    print(f"{len(samples)} usable night hours")
    if len(samples) < 40:
        sys.exit("too few: collect more (fit N downloads the last N days)")
    depth = max(0.0, cfg["rim_elevation_ft"] - cfg["valley_elevation_ft"])
    data = [x for x in samples if x[7] is not None and x[8] is not None]       # hours with both dewpoints, so the variants see the same hours
    if len(data) < 40:
        data = [x for x in samples if x[7] is not None] or samples
    n = len(data)
    mean_def = sum(x[1] for x in data) / n
    base = math.sqrt(sum((x[1] - mean_def) ** 2 for x in data) / n)
    print(f"{n} hours; mean deficit {mean_def:.2f}F, spread of the deficit itself {base:.2f}F")

    def grid(td_index):
        best = {None: None}
        scales = (None, 3, 5, 8, 12, 18, 25, 35) if td_index is not None and all(x[td_index] is not None for x in data) else (None,)
        for ds in scales:
            for md in range(4, 21):
                for cw in [x / 10 for x in range(0, 11)]:
                    for ws in (2, 3, 4, 6, 10, 15):
                        for ratio in (0.1, 0.2, 0.3, 0.5, 1.0):
                            err = 0.0
                            for x in data:
                                g = min(1.0, max(0.0, x[6] - x[td_index]) / ds) if ds else 1.0
                                err += (md * (1 + depth / 3000) * max(0.0, 1 - x[3] * ratio / ws) * (1 - cw * x[2] / 100) * g - x[1]) ** 2
                            if best.get(ds) is None or err < best[ds][0]:
                                best[ds] = (err, md, cw, ws, ratio)
        return best

    results = {}
    for name, idx in (("station dewpoint", 7), ("URMA dewpoint", 8)):
        b = grid(idx)
        none = b[None]
        ds, val = min(((k, v) for k, v in b.items() if k), key=lambda kv: kv[1][0]) if len(b) > 1 else (None, none)
        results[name] = (b, ds, val)
        print(f"{name}: with it rmse {math.sqrt(val[0] / n):.2f}F (scale {ds}F) {val[1:]}   | without {math.sqrt(none[0] / n):.2f}F {none[1:]}")
    r0 = math.sqrt(results["URMA dewpoint"][0][None][0] / n)
    # the variant with the lowest error, if it beats "no dewpoint" by 0.1F; the station's dewpoint can be read live in the evening, URMA's cannot
    cand = [(math.sqrt(v[0] / n), nm, ds_, v) for nm, (bb, ds_, v) in results.items() if len(bb) > 1]
    rbest, nm, ds, val = min(cand, key=lambda c_: c_[0])
    b = results[nm][0]
    td_index = 8 if nm == "URMA dewpoint" else 7
    use = ds if rbest < r0 - 0.1 else None
    _, md, cw, ws, ratio = b[use] if use is None else (None, *val[1:])
    print(f"kept: {'dewpoint term (' + ('URMA' if td_index == 8 else 'station') + ', scale ' + str(use) + 'F)' if use else 'no dewpoint term'}")
    # stage 2: the time-of-night shape (wind scale and shelter ratio kept from stage 1); judged on all hours and on the clear, calm nights
    hs = {x[0]: hours_after_sunset(x[0], cfg["lat"], cfg["lon"]) for x in data}
    nightkey = lambda x: (x[0] - timedelta(hours=13)).date()
    bynight = {}
    for x in data:
        bynight.setdefault(nightkey(x), []).append(x)
    calm = {d_ for d_, xs_ in bynight.items() if len(xs_) >= 8 and st_mean([y[2] for y in xs_]) < 20 and st_mean([y[4] for y in xs_]) < 1}

    def sse(md_, cw_, ds_, amp_, tau_, sel=None):
        e = 0.0
        for x in data:
            if sel is not None and nightkey(x) not in sel:
                continue
            g = min(1.0, max(0.0, x[6] - x[td_index]) / ds_) if ds_ and use else 1.0
            fe = 1 + amp_ * math.exp(-max(0.0, hs[x[0]] - 2) / tau_) if amp_ else 1.0
            e += (md_ * (1 + depth / 3000) * max(0.0, 1 - x[3] * ratio / ws) * (1 - cw_ * x[2] / 100) * g * fe - x[1]) ** 2
        return e
    base_err = sse(md, cw, use, 0, 3)
    best2 = (base_err, md, cw, 0, 3)
    for md_ in range(4, 21):
        for cw_ in (0.4, 0.6, 0.8, 1.0):
            for amp_ in (0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.5):
                for tau_ in (2, 3, 4, 6, 8):
                    if amp_ == 0 and tau_ != 3:
                        continue
                    e = sse(md_, cw_, use, amp_, tau_)
                    if e < best2[0]:
                        best2 = (e, md_, cw_, amp_, tau_)
    ncalm = sum(len(bynight[d_]) for d_ in calm)
    print(f"time-of-night shape: all hours rmse {math.sqrt(base_err / n):.2f}F -> {math.sqrt(best2[0] / n):.2f}F   (amp {best2[3]}, tau {best2[4]} h, max drop {best2[1]}, cloud weight {best2[2]})")
    print(f"  the {len(calm)} clear+calm nights ({ncalm} hours): rmse {math.sqrt(sse(md, cw, use, 0, 3, calm) / ncalm):.2f}F -> {math.sqrt(sse(best2[1], best2[2], use, best2[3], best2[4], calm) / ncalm):.2f}F")
    early = 0.0
    if math.sqrt(best2[0] / n) < math.sqrt(base_err / n) - 0.05:
        _, md, cw, early_amp, early_tau = best2
    else:
        early_amp, early_tau = 0, 3
    # a threshold-shaped wind factor exp(-(U/U0)^p) in place of the straight line (COLPEX: the valley floor decouples below about 5 m/s, mixing above it)
    wind_form, wu0, wp = "linear", None, None
    final_sse = sse(md, cw, use, early_amp, early_tau)
    bt = None
    for u0 in (8, 10, 12, 14, 16):
        for p in (2, 3, 4, 6):
            for md_ in range(4, 21):
                for cw_ in (0.6, 0.8, 1.0):
                    e = 0.0
                    for x in data:
                        g = min(1.0, max(0.0, x[6] - x[td_index]) / use) if use else 1.0
                        e += (md_ * (1 + depth / 3000) * math.exp(-(x[3] / u0) ** p) * (1 - cw_ * x[2] / 100) * g - x[1]) ** 2
                    if bt is None or e < bt[0]:
                        bt = (e, u0, p, md_, cw_)
    print(f"wind factor: linear rmse {math.sqrt(final_sse / n):.2f}F | threshold exp(-(U/{bt[1]})^{bt[2]}) rmse {math.sqrt(bt[0] / n):.2f}F (max drop {bt[3]}, cloud weight {bt[4]})")
    if math.sqrt(bt[0] / n) < math.sqrt(final_sse / n) - 0.03:
        wind_form, wu0, wp, md, cw, final_sse, early_amp = "threshold", bt[1], bt[2], bt[3], bt[4], bt[0], 0
        print("  kept: the threshold form")
    fitted = dict(cfg, max_deficit_f=md, cloud_weight=cw, wind_scale_mph=ws, dep_scale=use, early_amp=early_amp, early_tau=early_tau,
                  wind_form=wind_form, wind_u0=wu0, wind_p=wp)
    LOG.mkdir(exist_ok=True)
    (LOG / "fit.json").write_text(json.dumps({"max_deficit_f": md, "cloud_weight": cw, "wind_scale_mph": ws, "shelter_ratio": ratio, "dep_scale": use,
                                              "dewpoint": "urma" if td_index == 8 else "station", "early_amp": early_amp, "early_tau": early_tau,
                                              "wind_form": wind_form, "wind_u0": wu0, "wind_p": wp,
                                              "n": n, "rmse": round(math.sqrt(final_sse / n), 2)}, indent=1))
    nights = {}
    for x in data:
        nights.setdefault((x[0] - timedelta(hours=13)).date(), []).append(x)
    print(f"\n{'night of':<11}{'hrs':>4}{'station min':>13}{'URMA min':>10}{'deficit':>9}{'modeled':>9}{'err':>6}{'wind':>6}{'cloud':>6}{'Td eve':>8}{'min-Td':>8}")
    for day, xs in sorted(nights.items()):
        if len(xs) < 6:
            continue
        pred = [model(fitted, {"t": x[0], "cloud": x[2], "wind_mph": x[3], "dep": x[6] - x[td_index] if x[td_index] is not None else None}, ratio)[0] for x in xs]
        dd = [x[1] for x in xs]
        tdeve = xs[0][td_index]
        tail = f"{tdeve:>7.1f}F{min(x[5] for x in xs) - tdeve:>+7.1f}F" if tdeve is not None else ""
        print(f"{day.strftime('%a %d %b'):<11}{len(xs):>4}{min(x[5] for x in xs):>12.1f}F{min(x[6] for x in xs):>9.1f}F{st_mean(dd):>8.1f}F{st_mean(pred):>8.1f}F{st_mean([p - d for p, d in zip(pred, dd)]):>+6.1f}{st_mean([x[4] for x in xs]):>6.1f}{st_mean([x[2] for x in xs]):>6.0f}{tail}")
    print("\nwritten to log/fit.json")


def st_mean(v):
    return sum(v) / len(v)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "forecast"
    cfg = load_config()
    if cmd == "track":
        cmd_track(cfg, int(sys.argv[2]) if len(sys.argv) > 2 else 48)
    elif cmd == "dptfill":
        cmd_dptfill(cfg)
    elif cmd == "gribfill":
        cmd_gribfill(cfg, sys.argv[2])
    elif cmd == "backfill":
        cmd_backfill(cfg, int(sys.argv[2]))
    elif cmd == "fit":
        cmd_fit(cfg, int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    else:
        {"forecast": cmd_forecast}.get(cmd, lambda c: sys.exit(__doc__))(cfg)
