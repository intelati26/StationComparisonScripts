"""Point-value downloads from web APIs, tried inside a tier before its GRIB download (weather_common.try_model_extraction).

  urma, rtma          GribStream (needs GRIBSTREAM_TOKEN)      4 variables x 1 hour = 4 credits (free plan: 1200 a day)
  hrrr_fNN, rrfs_fNN  Open-Meteo (no key; its newest run for that hour, not a given forecast lead)
  urma, rtma          also the AWS file by byte range, read with GDAL's gdallocationinfo (no key, no eccodes), when GribStream has nothing or its quota is out;
                      grib_points() reads several stations from one download

The tiers themselves are unchanged: each returns the same (temp_f, temp_sd, cloud_cover, wind_speed, precip) tuple
as the GRIB path, or None, and the caller then goes on to Herbie. temp_sd (the 3x3 stencil spread) and precip are not
available from the APIs and come back None. A 429 or any error from a service turns that service off for the rest of
the run, so the GRIB path takes over without retrying a refused call for every hour.

Environment:
  GRIBSTREAM_TOKEN        token for GribStream; without it URMA/RTMA go straight to GRIB
  GRIBSTREAM_DAILY_CREDITS  credits to spend per UTC day (default 1200, the free plan); counted in .gribstream_credits.json, so separate runs add up.
                          URMA costs 4 credits an hour, so the free plan covers 300 hours a day; past that the AWS file is read instead
  WEATHER_API_DOWNLOADS   "off" to disable all API downloads, or a list: "gribstream", "openmeteo", "gribfast" (default: all three)
  WEATHER_ELEVATION_FT    station elevation; sent to Open-Meteo so it does not adjust to its own terrain height

Both services are sent the station's latitude and longitude with every call.
"""
import csv, io, json, math, os, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

_dead = set()      # services switched off for this run
_om_cache = {}     # (model, yyyy-mm-dd, lat, lon) -> {hour: tuple}


def _enabled(service):
    spec = os.environ.get("WEATHER_API_DOWNLOADS", "gribstream,openmeteo,gribfast").strip().lower()
    if spec in ("off", "none", "0", "false", ""):
        return False
    return service in spec and service not in _dead


def _fail(service, why):
    _dead.add(service)
    print(f"    {service}: {why} - using GRIB for the rest of this run")


_CREDITS_PER_HOUR = 4          # one credit per variable-hour, four variables


def _credits_file():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gribstream_credits.json")


def _credits_used():
    """Credits this machine has spent today (UTC), kept in a small file so separate runs add up."""
    try:
        d = json.load(open(_credits_file()))
        return d["used"] if d.get("day") == datetime.now(timezone.utc).strftime("%Y-%m-%d") else 0
    except Exception:
        return 0


def _credits_add(n):
    try:
        json.dump({"day": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "used": _credits_used() + n}, open(_credits_file(), "w"))
    except OSError:
        pass


def gribstream(model_key, target_time, lat, lon):
    """URMA/RTMA at one hour -> (temp_f, None, cloud, wind_mph, None) or None."""
    token = os.environ.get("GRIBSTREAM_TOKEN")
    if not token or not _enabled("gribstream"):
        return None
    budget = int(os.environ.get("GRIBSTREAM_DAILY_CREDITS", "1200"))       # the free plan; set it lower to keep some for other uses
    if _credits_used() + _CREDITS_PER_HOUR > budget:
        _fail("gribstream", f"today's {budget} credits are used")
        return None
    t0 = target_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    body = {"fromTime": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "untilTime": (t0 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "coordinates": [{"lat": lat, "lon": lon}],
            "variables": [{"name": "TMP", "level": "2 m above ground", "info": ""},
                          {"name": "UGRD", "level": "10 m above ground", "info": ""},
                          {"name": "VGRD", "level": "10 m above ground", "info": ""},
                          {"name": "TCDC", "level": "entire atmosphere (considered as a single layer)", "info": ""}]}
    req = urllib.request.Request(f"https://gribstream.com/api/v2/{model_key}/timeseries",
                                 data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + token})
    try:
        text = urllib.request.urlopen(req, timeout=60).read().decode()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            _fail("gribstream", f"HTTP {e.code}")
        return None          # 404 etc.: this hour is not there, GRIB may have it
    except Exception as e:
        _fail("gribstream", str(e))
        return None
    for r in csv.DictReader(io.StringIO(text)):
        try:
            if not r["forecasted_time"].startswith(t0.strftime("%Y-%m-%dT%H")):
                continue
            temp_f = (float(r["TMP|2 m above ground|"]) - 273.15) * 9 / 5 + 32
            wind = math.hypot(float(r["UGRD|10 m above ground|"]), float(r["VGRD|10 m above ground|"])) * 2.23694
            cloud = float(r["TCDC|entire atmosphere (considered as a single layer)|"])
        except (KeyError, ValueError):
            continue
        if math.isnan(temp_f):
            continue
        _credits_add(_CREDITS_PER_HOUR)
        print(f"    {model_key}: from the GribStream API")
        return temp_f, None, cloud, wind, None
    return None


def openmeteo(model_key, target_time, lat, lon):
    """hrrr_fNN / rrfs_fNN -> Open-Meteo's value for that hour, or None."""
    if not _enabled("openmeteo"):
        return None
    name = "ncep_hrrr_conus" if model_key.startswith("hrrr") else "ncep_rrfs_conus"
    t0 = target_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    day = t0.strftime("%Y-%m-%d")
    key = (name, day, round(lat, 4), round(lon, 4))
    if key not in _om_cache:
        q = {"latitude": lat, "longitude": lon, "models": name, "timezone": "UTC",
             "hourly": "temperature_2m,cloud_cover,wind_speed_10m",
             "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "start_date": day, "end_date": day}
        ft = os.environ.get("WEATHER_ELEVATION_FT")
        if ft:
            q["elevation"] = round(float(ft) * 0.3048, 1)
        try:
            d = json.loads(urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q), timeout=30).read().decode())
            h = d["hourly"]
            _om_cache[key] = {t: (a, c, w) for t, a, c, w in zip(h["time"], h["temperature_2m"], h["cloud_cover"], h["wind_speed_10m"])}
        except urllib.error.HTTPError as e:
            if e.code in (429, 403):
                _fail("openmeteo", f"HTTP {e.code}")
            _om_cache[key] = {}
        except Exception as e:
            _fail("openmeteo", str(e))
            return None
    row = _om_cache[key].get(t0.strftime("%Y-%m-%dT%H:%M"))
    if not row or row[0] is None:
        return None
    print(f"    {model_key}: from the Open-Meteo API ({name})")
    return row[0], None, row[1], row[2], None


_GRIB_WANT = (("TMP", "2 m above ground"), ("UGRD", "10 m above ground"), ("VGRD", "10 m above ground"),
              ("TCDC", "entire atmosphere (considered as a single layer)"))


_gdal_grib_ok = True      # False once gdallocationinfo has shown it cannot open a GRIB file (a GDAL build without its GRIB driver, as on some Windows installs)
_grid_cache = {}
_grid_lock = __import__("threading").Lock()      # the four reads run in threads: the grid's coordinates are made once


def _pygrib_values(path, points):
    """The value at each point of the first message of a GRIB file, read with pygrib (pip install pygrib: its Windows wheels carry eccodes). The grid's latitudes and longitudes are worked out once
    per grid shape; a point more than about 0.1 degree from any grid point is outside the grid (None)."""
    import numpy as np
    import pygrib
    g = pygrib.open(path)
    try:
        msg = g[1]
        vals = np.asarray(msg.values, dtype=float)
        key = vals.shape
        with _grid_lock:
            if key not in _grid_cache:
                lats, lons = msg.latlons()
                _grid_cache.clear()
                _grid_cache[key] = (np.asarray(lats), np.asarray(lons))
            lats, lons = _grid_cache[key]
        out = []
        for lat, lon in points:
            dlon = (lons - lon + 180.0) % 360.0 - 180.0
            d2 = (lats - lat) ** 2 + (dlon * math.cos(math.radians(lat))) ** 2
            i = int(np.argmin(d2))
            out.append(float(vals.flat[i]) if d2.flat[i] < 0.1 ** 2 and not np.ma.is_masked(vals.flat[i]) else None)
        return out
    finally:
        g.close()


def _gdal_values(path, points):
    """One line a point from gdallocationinfo: a blank line is a point outside the grid. Raises when GDAL cannot read the file at all."""
    import subprocess
    global _gdal_grib_ok
    coords = "".join(f"{lon} {lat}\n" for lat, lon in points)
    run = subprocess.run(["gdallocationinfo", "-valonly", "-wgs84", path], input=coords.encode(), capture_output=True)
    err = run.stderr.decode(errors="replace")
    if "not recognized" in err or "not available in your installation" in err or "gdal_GRIB" in err:
        _gdal_grib_ok = False                           # no GRIB driver in this GDAL: do not try it again this run
        raise RuntimeError("this GDAL has no GRIB driver")
    lines = run.stdout.decode(errors="replace").splitlines()
    vals = [float(x.split()[0]) if x.strip() else None for x in lines]
    if len(vals) != len(points):
        raise ValueError("gdallocationinfo returned the wrong number of values (%d for %d points)%s" % (len(vals), len(points), (": " + err.strip()[:200]) if err.strip() else ""))
    return vals


def _have_pygrib():
    try:
        import pygrib  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def grib_points(model_key, target_time, points):
    """URMA/RTMA straight from NOAA's AWS bucket without eccodes or Herbie: the .idx, then only the four messages (about 6 MB each) by byte range,
    each read at ALL the points at once with gdallocationinfo (needs GDAL's command line tools). points = [(lat, lon), ...]
    -> [(temp_f, None, cloud, wind_mph, None), ...] in the same order, or None. Several stations cost one download, not one each."""
    import shutil, subprocess, tempfile
    from concurrent.futures import ThreadPoolExecutor
    if not _enabled("gribfast") or model_key not in ("urma", "rtma"):
        return None
    if not ((_gdal_grib_ok and shutil.which("gdallocationinfo")) or _have_pygrib()):
        return None                                  # neither GDAL's command line tools (with a GRIB driver) nor pygrib
    t0 = target_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    base = f"https://noaa-{model_key}-pds.s3.amazonaws.com/{model_key}2p5.{t0:%Y%m%d}/{model_key}2p5.t{t0:%H}z.2dvaranl_ndfd.grb2_wexp"
    try:
        idx = urllib.request.urlopen(base + ".idx", timeout=30).read().decode().splitlines()
    except Exception:
        return None                                  # that hour is not published (yet)
    rows = [l.split(":") for l in idx if l.count(":") >= 4]
    spans = {}
    for i, r in enumerate(rows):
        for name, level in _GRIB_WANT:
            if r[3] == name and r[4] == level:
                spans[name] = (int(r[1]), int(rows[i + 1][1]) - 1 if i + 1 < len(rows) else None)
    if len(spans) < len(_GRIB_WANT):
        return None
    coords = "".join(f"{lon} {lat}\n" for lat, lon in points)

    def one(item):
        name, (a, b) = item
        req = urllib.request.Request(base, headers={"Range": f"bytes={a}-{b if b is not None else ''}"})
        # The file is closed before it is opened again (Windows will not let a second program open a temp file that is still open).
        import os as _os
        fd, path = tempfile.mkstemp(suffix=".grb2")
        try:
            with _os.fdopen(fd, "wb") as f:
                f.write(urllib.request.urlopen(req, timeout=120).read())
            vals = None
            if _gdal_grib_ok and shutil.which("gdallocationinfo"):
                try:
                    vals = _gdal_values(path, points)
                except RuntimeError:
                    vals = None                      # no GRIB driver: pygrib below
            if vals is None:
                if not _have_pygrib():
                    raise RuntimeError("GDAL has no GRIB driver and pygrib is not installed (pip install pygrib)")
                vals = _pygrib_values(path, points)
        finally:
            try:
                _os.remove(path)
            except OSError:
                pass
        return name, vals
    try:
        with ThreadPoolExecutor(4) as ex:
            v = dict(ex.map(one, spans.items()))
    except Exception as e:
        print(f"    {model_key}: fast GRIB read failed ({e})")
        return None
    result = []
    for i in range(len(points)):
        if any(v[k][i] is None for k in v):             # the point is outside the grid this model covers
            print(f"    {model_key}: the point ({points[i][0]}, {points[i][1]}) is outside the grid (CONUS only)")
            result.append(None)
            continue
        tc = v["TMP"][i]
        tc = tc - 273.15 if tc > 150 else tc         # GDAL gives degrees C for temperature, Kelvin if it did not convert
        result.append((tc * 9 / 5 + 32, None, v["TCDC"][i], math.hypot(v["UGRD"][i], v["VGRD"][i]) * 2.23694, None))
    return result


def gribfast(model_key, target_time, lat, lon):
    r = grib_points(model_key, target_time, [(lat, lon)])
    if r and r[0] is not None:
        print(f"    {model_key}: from the AWS file, byte ranges (no eccodes)")
        return r[0]
    return None


def extract(model_key, target_time, lat, lon):
    """The API attempt for a tier; None means use the GRIB download."""
    if model_key in ("urma", "rtma"):
        return gribstream(model_key, target_time, lat, lon) or gribfast(model_key, target_time, lat, lon)
    if model_key.startswith(("hrrr_f", "rrfs_f")):
        return openmeteo(model_key, target_time, lat, lon)
    return None
