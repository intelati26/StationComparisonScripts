"""Point-value downloads from web APIs, tried inside a tier before its GRIB download (weather_common.try_model_extraction).

  urma, rtma          GribStream (needs GRIBSTREAM_TOKEN)      4 variables x 1 hour = 4 credits (free plan: 1200 a day)
  hrrr_fNN, rrfs_fNN  Open-Meteo (no key; its newest run for that hour, not a given forecast lead)

The tiers themselves are unchanged: each returns the same (temp_f, temp_sd, cloud_cover, wind_speed, precip) tuple
as the GRIB path, or None, and the caller then goes on to Herbie. temp_sd (the 3x3 stencil spread) and precip are not
available from the APIs and come back None. A 429 or any error from a service turns that service off for the rest of
the run, so the GRIB path takes over without retrying a refused call for every hour.

Environment:
  GRIBSTREAM_TOKEN        token for GribStream; without it URMA/RTMA go straight to GRIB
  WEATHER_API_DOWNLOADS   "off" to disable all API downloads, or a list: "gribstream", "openmeteo" (default: both)
  WEATHER_ELEVATION_FT    station elevation; sent to Open-Meteo so it does not adjust to its own terrain height

Both services are sent the station's latitude and longitude with every call.
"""
import csv, io, json, math, os, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

_dead = set()      # services switched off for this run
_om_cache = {}     # (model, yyyy-mm-dd, lat, lon) -> {hour: tuple}


def _enabled(service):
    spec = os.environ.get("WEATHER_API_DOWNLOADS", "gribstream,openmeteo").strip().lower()
    if spec in ("off", "none", "0", "false", ""):
        return False
    return service in spec and service not in _dead


def _fail(service, why):
    _dead.add(service)
    print(f"    {service}: {why} - using GRIB for the rest of this run")


def gribstream(model_key, target_time, lat, lon):
    """URMA/RTMA at one hour -> (temp_f, None, cloud, wind_mph, None) or None."""
    token = os.environ.get("GRIBSTREAM_TOKEN")
    if not token or not _enabled("gribstream"):
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


def extract(model_key, target_time, lat, lon):
    """The API attempt for a tier; None means use the GRIB download."""
    if model_key in ("urma", "rtma"):
        return gribstream(model_key, target_time, lat, lon)
    if model_key.startswith(("hrrr_f", "rrfs_f")):
        return openmeteo(model_key, target_time, lat, lon)
    return None
