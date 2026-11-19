#!/usr/bin/env python3
"""Valley cold-air-pool predictor, v3 (stdlib only).

  coldpool.py forecast   prediction table for the next 24 h; logs tonight's predicted low
  coldpool.py fit        fits the model's coefficients to the weather_archive.db of StationComparisonScripts

Differences from v2: no invented data (it stops when a source fails), cloud cover and
dewpoint come from the NWS gridpoint data, night is sunset to sunrise for the date, and
everything is logged so the coefficients can be fitted later instead of guessed.
Config: the same environment variables (or .env) as StationComparisonScripts: AMBIENT_API_KEY, AMBIENT_APP_KEY,
AMBIENT_STATION_MAC, WEATHER_LAT, WEATHER_LON, WEATHER_DB_PATH; plus HYPERLOCAL_VALLEY_FT and HYPERLOCAL_RIM_FT.
Coefficients come from log/fit.json when `fit` has been run, else the provisional defaults.
"""
import csv, json, math, os, re, sqlite3, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
LOG = HERE / "log"
UA = {"User-Agent": "(hyperlocal-coldpool, set-your-contact-here)", "Accept": "application/json"}


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        return json.loads(r.read().decode())


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
    need = ["WEATHER_LAT", "WEATHER_LON", "HYPERLOCAL_VALLEY_FT", "HYPERLOCAL_RIM_FT"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit("missing in the environment or .env: " + ", ".join(missing))
    cfg = {"lat": float(os.environ["WEATHER_LAT"]), "lon": float(os.environ["WEATHER_LON"]),
           "valley_elevation_ft": float(os.environ["HYPERLOCAL_VALLEY_FT"]),
           "rim_elevation_ft": float(os.environ["HYPERLOCAL_RIM_FT"]),
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
    return {"time": last["date"], "temp_f": last["tempf"], "wind_mph": last.get("windspeedmph", 0.0)}


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
    f_wind = max(0.0, 1 - eff / cfg["wind_scale_mph"])
    f_cloud = 1 - cfg["cloud_weight"] * row["cloud"] / 100
    f_night = night_factor(row["t"], cfg["lat"], cfg["lon"])
    drop = cfg["max_deficit_f"] * (1 + depth / 3000) * f_night * f_wind * f_cloud
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


def cmd_forecast(cfg):
    st = station(cfg)
    rows, grid = nws_hourly(cfg["lat"], cfg["lon"])
    # shelter ratio from the first hours' average, not a single noisy reading
    ref = max(1.0, sum(r["wind_mph"] for r in rows[:3]) / len(rows[:3]))
    ratio = st["wind_mph"] / ref
    print(f"station {st['temp_f']:.1f}F {st['wind_mph']:.1f} mph at {st['time']}   NWS grid {grid}   shelter ratio {ratio:.2f}")
    print(f"{'UTC':<6}{'NWS F':>7}{'cloud%':>8}{'wind':>6}{'eff':>6}{'drop':>6}{'pred F':>8}")
    pred = []
    for r in rows:
        drop, eff = model(cfg, r, ratio)
        pred.append((r["temp_f"] - drop, r, drop))
        print(f"{r['t']:%H:%M} {r['temp_f']:>7.1f}{r['cloud']:>8.0f}{r['wind_mph']:>6.1f}{eff:>6.1f}{drop:>6.1f}{r['temp_f'] - drop:>8.1f}")
    low, row, _ = min(pred, key=lambda x: x[0])
    nws_low = min(r["temp_f"] for r in rows)
    print(f"\npredicted low {low:.1f}F at {row['t']:%H:%M}Z   (NWS low {nws_low:.1f}F)")
    append("forecasts.csv", ["made_utc", "night_of", "pred_low_f", "nws_low_f", "ratio"],
           [datetime.now(timezone.utc).isoformat(timespec="minutes"), rows[0]["t"].date().isoformat(), f"{low:.1f}", f"{nws_low:.1f}", f"{ratio:.2f}"])


def cmd_fit(cfg):
    """Fit max_deficit_f, cloud_weight and wind_scale_mph to the archive's nights: deficit = URMA (the grid's air) minus the station.
    Uses hours at least 2 h past sunset and before sunrise, URMA rows (else RTMA) at the site, station AMBIENT's hourly mean."""
    if not Path(cfg["db"]).exists():
        sys.exit(f"database not found: {cfg['db']} (set WEATHER_DB_PATH)")
    db = sqlite3.connect(cfg["db"])
    station_name = os.environ.get("HYPERLOCAL_STATION", "AMBIENT")
    obs = {}
    for ts, t, w in db.execute("SELECT timestamp, temperature, wind_speed FROM station_obs WHERE station=? AND temperature IS NOT NULL", (station_name,)):
        try:
            k = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if k.tzinfo is None:
            k = k.replace(tzinfo=timezone.utc)
        k = k.replace(minute=0, second=0, microsecond=0)
        o = obs.setdefault(k, [0.0, 0.0, 0])
        o[0] += t; o[1] += (w or 0.0); o[2] += 1
    samples = []
    for model in ("urma", "rtma"):
        for vt, temp, cloud, wind in db.execute("SELECT valid_time, temp_f, cloud_cover, wind_speed FROM model_analysis WHERE model=? AND temp_f IS NOT NULL AND cloud_cover IS NOT NULL", (model,)):
            try:
                k = datetime.fromisoformat(vt.replace("Z", "+00:00"))
            except ValueError:
                continue
            if k.tzinfo is None:
                k = k.replace(tzinfo=timezone.utc)
            k = k.replace(minute=0, second=0, microsecond=0)
            if k in obs and not any(s_[0] == k for s_ in samples):
                n = obs[k][2]
                if night_factor(k, cfg["lat"], cfg["lon"]) >= 1.0:
                    samples.append((k, temp - obs[k][0] / n, cloud, (wind or 0.0) * 2.23694 if wind is not None and wind < 60 else (wind or 0.0), obs[k][1] / n))
    if len(samples) < 50:
        sys.exit(f"only {len(samples)} usable night hours in the archive: collect more first")
    # shelter ratio: station wind over model wind, medians over the nights (model wind is m/s in URMA, converted above when small)
    ratios = sorted(sw / mw for _, _, _, mw, sw in samples if mw > 1.0)
    ratio = ratios[len(ratios) // 2] if ratios else 0.5
    depth = max(0.0, cfg["rim_elevation_ft"] - cfg["valley_elevation_ft"])
    best = None
    for md in [x / 2 for x in range(4, 41)]:
        for cw in [x / 20 for x in range(0, 21)]:
            for ws in [2, 3, 4, 5, 6, 8, 10, 15]:
                err = 0.0
                for _, deficit, cloud, mw, _ in samples:
                    eff = mw * ratio
                    pred = md * (1 + depth / 3000) * max(0.0, 1 - eff / ws) * (1 - cw * cloud / 100)
                    err += (pred - deficit) ** 2
                if best is None or err < best[0]:
                    best = (err, md, cw, ws)
    mean_def = sum(s_[1] for s_ in samples) / len(samples)
    rmse = math.sqrt(best[0] / len(samples))
    base = math.sqrt(sum((s_[1] - mean_def) ** 2 for s_ in samples) / len(samples))
    print(f"{len(samples)} night hours; shelter ratio {ratio:.2f}; mean deficit {mean_def:.2f}F")
    print(f"best: max_deficit_f {best[1]}, cloud_weight {best[2]}, wind_scale_mph {best[3]}  rmse {rmse:.2f}F (spread of the deficit itself {base:.2f}F)")
    LOG.mkdir(exist_ok=True)
    (LOG / "fit.json").write_text(json.dumps({"max_deficit_f": best[1], "cloud_weight": best[2], "wind_scale_mph": best[3], "n": len(samples), "rmse": round(rmse, 2)}, indent=1))
    print("written to log/fit.json")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "forecast"
    cfg = load_config()
    {"forecast": cmd_forecast, "fit": cmd_fit}.get(cmd, lambda c: sys.exit(__doc__))(cfg)
