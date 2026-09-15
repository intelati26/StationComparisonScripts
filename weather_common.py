#!/usr/bin/env python3
"""
weather_common.py — Shared library for NWS station fetching (KSGF, KBBG),
Ambient Weather API fetching (G6964), and NOAA analysis-model fetching
(URMA/RTMA/HRRR/RRFS, with local spatial standard deviation uncertainty
envelopes), all backed by a single SQLite archive.

This module has no CLI or __main__ — it's imported by four entrypoint
scripts, split by how often each data source can actually be re-checked
AND by dependency weight:

  collect_stations.py  — NWS + Ambient station observations.
                          Safe to run every few minutes. No herbie/
                          cfgrib/eccodes dependency.
  collect_models.py    — Fast-turnaround model tiers: HRRR (~45min
                          latency) and, optionally, RRFS. Safe to run
                          every ~45min-1h. Requires herbie + cfgrib +
                          eccodes (for GRIB decoding).
  collect_urma.py       — Slow gold-standard tiers: URMA (~6h latency)
                          and RTMA (~1h) as an interim upgrade. Also owns
                          the deep reverse-backfill (--backfill-reverse)
                          and the anomaly-detection repair pass, since
                          both are about eventually reaching gold-standard
                          data rather than fast turnaround. Meant to run
                          every few hours. Requires herbie + cfgrib +
                          eccodes.
  collect_gladstone.py  — ECCODES-FREE FALLBACK for the model_analysis
                          table. Scrapes a point-analysis CSV (temp +
                          wind only) from weather.gladstonefamily.net
                          instead of decoding GRIB grids directly. Much
                          coarser than real URMA/RTMA/HRRR extraction,
                          but needs nothing beyond requests + the stdlib.
                          Use this in place of collect_models.py/
                          collect_urma.py wherever eccodes can't be
                          installed (e.g. no root/sudo, no conda). Writes
                          model="gladstone" rows into the same
                          model_analysis table, so coverage_calendar.py
                          and anything else querying that table works
                          unchanged.

ECCODES-FREE FLOW:
  If eccodes isn't available in your environment, herbie's GRIB
  extraction (used by collect_models.py and collect_urma.py) simply
  can't run there -- there's no way around that short of installing it.
  The only thing in this whole pipeline that hard-requires it is GRIB
  decoding. Everything else -- station fetching AND the Gladstone
  point-analysis fallback -- uses only requests/csv/stdlib. So the
  full eccodes-free pipeline is just:
      collect_stations.py   (station_obs)
      collect_gladstone.py  (model_analysis, model="gladstone")
  herbie itself is imported lazily (inside make_herbie(), not at module
  level) specifically so importing this module -- and therefore running
  collect_stations.py/collect_gladstone.py -- never requires herbie to
  be installed at all, let alone working. collect_models.py/
  collect_urma.py will raise a clear RuntimeError if you try to run them
  without it.

MODEL CASCADE (per target hour, "rounded up" from observation time):
  Tier 1 — URMA anl:    Best quality, ~6h latency. 2.5km grid.
  Tier 2 — RTMA anl:    Good quality, ~1h latency. 2.5km grid.
  Tier 3 — RRFS (if enabled): experimental, see note below.
  Tier 4 — HRRR f00:    Analysis hour, ~45min latency. 3km grid.
  Tier 5 — HRRR f01+:   Forecast from prior cycle, valid at target hour.

Each fetch function takes a `tiers` argument (a set of any of "urma",
"rtma", "rrfs", "hrrr") to restrict which parts of the cascade it will
attempt — this is how the three entrypoint scripts stay in their own
lane instead of all racing to fetch the same rows. `tiers=None` (the
default) means "attempt the full cascade", matching the old single-script
behavior.

NOTE ON RRFS (August 2026):
  The RRFS prototype data feed on AWS stopped updating on August 11, 2026
  when the pre-implementation parallel phase began. RRFS v1 is not
  operational until October 6, 2026. During this gap, HRRR is the primary
  forecast model — it is still running and available on AWS.
  Any existing RRFS rows in the DB from before Aug 11 are kept and will
  be upgraded to URMA/RTMA when those become available. Fetching no
  longer attempts RRFS by default — pass --enable-rrfs (in
  collect_models.py) to retry it once the feed and Herbie support are
  confirmed working again.

CONFIGURATION (required, via environment or a local .env file — never
hardcoded in this script):
  AMBIENT_API_KEY, AMBIENT_APP_KEY, AMBIENT_STATION_MAC
  WEATHER_LAT, WEATHER_LON
  WEATHER_DB_PATH (optional, defaults to weather_archive.db)

SELF-HEALING UPGRADE PATH:
  On subsequent runs (without --force), lower-tier rows are retried and
  upgraded when a better source becomes available, within whichever
  tiers that script was called with. Once urma is stored, the row is
  never touched again (gold standard). Upgrade deltas are printed so you
  can see how much the value shifted.

CONCURRENT-WRITER NOTE:
  Since these three scripts are meant to run on independent, overlapping
  cron schedules against the same SQLite file, init_db() enables a busy
  timeout so a writer waits instead of failing outright if another
  script is mid-transaction. Still, keep cron intervals loose enough
  that runs don't regularly overlap for long.
"""

import os
import sys
import csv
import io
import time
import sqlite3
import argparse
from pathlib import Path
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
# NOTE: herbie is intentionally NOT imported here. It (and its cfgrib/
# eccodes dependency) is only needed by the GRIB-extraction code path
# (make_herbie / try_model_extraction, used by fetch_models,
# fetch_models_reverse, backfill_anomalies). Importing it lazily there
# means collect_stations.py and collect_gladstone.py work in
# environments where herbie/cfgrib/eccodes aren't installed at all --
# see the module docstring for the eccodes-free flow.

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_db_env = os.environ.get("WEATHER_DB_PATH", "weather_archive.db")
# A relative WEATHER_DB_PATH (including the bare default) resolves
# against THIS FILE'S directory, not the shell's current working
# directory. Without this, running any of collect_stations.py/
# collect_urma.py/collect_models.py from a different cwd than where
# weather_common.py lives silently opens/creates an empty database at
# that cwd instead -- sqlite3.connect() creates a fresh file rather than
# erroring on a path that doesn't exist yet, so this fails silently
# rather than loudly. Same bug, same fix already applied to
# tempAnalysis.py/plotter.py/bymonth.py/bymonth_viz.py/climate_normals.py
# (which don't import this module at all, precisely to avoid the
# AMBIENT_* env requirement below) -- this was the one place it was
# missed, since the three collect_*.py scripts import DB_PATH from here
# rather than computing their own.
DB_PATH = (_db_env if os.path.isabs(_db_env)
          else os.path.join(_SCRIPT_DIR, _db_env))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "TermuxWeatherPipeline/1.0 "
    "(contact@example.com)"})

# ─── Secrets & location — loaded from environment / .env, never hardcoded ───
# Required:
#   AMBIENT_API_KEY, AMBIENT_APP_KEY, AMBIENT_STATION_MAC
#   WEATHER_LAT, WEATHER_LON
# Optional:
#   WEATHER_DB_PATH (defaults to weather_archive.db)
#
# Put these in a local .env file (never committed) or export them in your
# shell profile. Example .env:
#   AMBIENT_API_KEY=...
#   AMBIENT_APP_KEY=...
#   AMBIENT_STATION_MAC=...
#   WEATHER_LAT=...
#   WEATHER_LON=...

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional — fall back to real environment variables

def _require_env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"Missing required environment variable: {name}\n"
            f"Set it in your shell or in a local .env file (see comment "
            f"above AMBIENT_API_KEY in this script)."
        )
    return val

AMBIENT_API_KEY = _require_env("AMBIENT_API_KEY")
AMBIENT_APP_KEY = _require_env("AMBIENT_APP_KEY")
G6964_MAC = _require_env("AMBIENT_STATION_MAC")

# Stencil offset in degrees (~2.5 km at mid-latitudes, matching URMA/RTMA 2.5km
# grid)
STENCIL_OFFSET_DEG = 0.025

# Rolling sanity check threshold (only active with --force)
ROLLING_DELTA_THRESHOLD_F = 10.0

# Anomaly detection threshold (post-hoc backfill pass)
ANOMALY_THRESHOLD_F = 20.0

# Latency thresholds (seconds) — how old must a target hour be before we try
# each tier
URMA_LATENCY_S = 21600       # 6 hours — URMA publishes ~6h after valid time
RTMA_LATENCY_S = 3600        # 1 hour — RTMA publishes ~30-45min, give margin
HRRR_F00_LATENCY_S = 2700    # 45 min — HRRR f00 available ~30-40min, give 
# margin

# Maximum HRRR forecast lead hours to walk back (tier 4)
HRRR_MAX_LEAD = 5

# Maximum RRFS forecast lead hours to walk back (only used if --enable-rrfs)
RRFS_MAX_LEAD = 4

# Model tier hierarchy — higher = better quality
MODEL_TIER = {
    "urma": 7,
    "rtma": 6,
    "rrfs_f00": 5,
    "rrfs_f01": 4,
    "rrfs_f02": 3,
    "rrfs_f03": 2,
    "rrfs_f04": 1,
    "hrrr_f00": 0,
    "hrrr_f01": -1,
    "hrrr_f02": -2,
    "hrrr_f03": -3,
    "hrrr_f04": -4,
    "hrrr_f05": -5,
}

# Safety valve: max pages to prevent infinite loops
AMBIENT_MAX_PAGES = 10000

# Reverse backfill: stop after this many consecutive failures (likely past
# archive boundary)
REVERSE_BACKFILL_MAX_CONSECUTIVE_FAILURES = 50
REVERSE_BACKFILL_WARN_THRESHOLD = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS station_obs (
    station TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    temperature REAL,
    dewpoint REAL,
    relative_humidity REAL,
    feels_like REAL,
    temp_indoor REAL,
    humidity_indoor REAL,
    wind_speed REAL,
    wind_gust REAL,
    max_daily_gust REAL,
    wind_dir REAL,
    wind_gust_dir REAL,
    wind_speed_avg2m REAL,
    wind_dir_avg2m REAL,
    wind_speed_avg10m REAL,
    wind_dir_avg10m REAL,
    pressure_rel REAL,
    pressure_abs REAL,
    rain_hourly REAL,
    rain_daily REAL,
    rain_24h REAL,
    rain_weekly REAL,
    rain_monthly REAL,
    rain_yearly REAL,
    rain_event REAL,
    rain_total REAL,
    uv_index REAL,
    solar_radiation REAL,
    co2 REAL,
    pm25 REAL,
    pm25_24h REAL,
    lightning_day INTEGER,
    lightning_hour INTEGER,
    lightning_distance REAL,
    last_rain TEXT,
    PRIMARY KEY (station, timestamp)
);

CREATE TABLE IF NOT EXISTS model_analysis (
    model TEXT NOT NULL,
    valid_time TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    temp_f REAL,
    temp_sd REAL,
    cloud_cover REAL,
    wind_speed REAL,
    precip REAL,
    PRIMARY KEY (model, valid_time, latitude, longitude)
);
"""


def backup_and_compress_db(conn, output_path=None):
    """Snapshot the live DB via SQLite's own backup API (safe to run while
    other cron jobs are writing -- unlike a raw file copy, this won't grab
    a half-written page), then gzip the snapshot. Pure stdlib (sqlite3 +
    gzip) -- no native extension, no per-platform binary, unlike the
    ZIPVFS/sqlite-zstd approaches. Real-world ratios seen on this schema
    are large (~13x on a 90MB file) since it's mostly REAL/INTEGER columns
    with many NULLs and repeated station codes/timestamp prefixes.

    This compresses a BACKUP COPY for cold storage/archival, not the live
    queryable DB -- collect_*.py keep reading/writing the uncompressed
    original as normal.
    """
    import gzip as _gzip
    import shutil as _shutil
    import os as _os
    from datetime import datetime as _dt, timezone as _tz

    if output_path is None:
        stamp = _dt.now(_tz.utc).strftime("%Y%m%d_%H%M%S")
        output_path = f"{DB_PATH}.{stamp}.gz"

    tmp_snapshot = f"{DB_PATH}.snapshot_tmp"
    if _os.path.exists(tmp_snapshot):
        _os.remove(tmp_snapshot)

    print(f"Snapshotting {DB_PATH} via sqlite3 backup API...")
    dest_conn = sqlite3.connect(tmp_snapshot)
    try:
        conn.backup(dest_conn)
    finally:
        dest_conn.close()

    size_before = _os.path.getsize(tmp_snapshot)
    print(f"  Snapshot: {size_before / 1e6:.1f} MB. Compressing...")

    with open(tmp_snapshot, "rb") as f_in:
        with _gzip.open(output_path, "wb") as f_out:
            _shutil.copyfileobj(f_in, f_out)

    _os.remove(tmp_snapshot)
    size_after = _os.path.getsize(output_path)
    ratio = (size_before / size_after) if size_after else 0
    print(f"  Done: {output_path}")
    print(f"  {size_before / 1e6:.1f} MB -> {size_after / 1e6:.1f} MB "
          f"({ratio:.1f}x)")
    return output_path


def vacuum_db(conn):
    """Reclaim space left behind by INSERT OR REPLACE overwrites (CSV
    imports, --force re-pulls, anomaly repairs). This is NOT real
    compression -- SQLite's stdlib build has no page-compression codec --
    it just rebuilds the file without the freed pages. Manual/opt-in only:
    VACUUM needs an exclusive lock and rewrites the entire file, which
    would collide with collect_stations.py's frequent cron schedule if run
    automatically on every invocation.
    """
    import os as _os
    size_before = (_os.path.getsize(DB_PATH)
                   if _os.path.exists(DB_PATH) else 0)
    print(f"Running VACUUM on {DB_PATH} ({size_before / 1e6:.1f} MB)...")
    conn.execute("VACUUM;")
    size_after = (_os.path.getsize(DB_PATH)
                  if _os.path.exists(DB_PATH) else 0)
    print(f"  Done. {size_after / 1e6:.1f} MB "
          f"({'-' if size_after <= size_before else '+'}"
          f"{abs(size_before - size_after) / 1e6:.1f} MB)")


def init_db(conn):
    # Multiple entrypoint scripts may run against this DB on overlapping
    # cron schedules — wait for locks instead of raising "database is
    # locked" outright.
    conn.execute("PRAGMA busy_timeout = 30000;")

    # WAL mode lets one writer and multiple readers proceed concurrently
    # without blocking each other -- default rollback-journal mode blocks
    # readers while a writer's transaction is open. This matters here
    # specifically because collect_stations.py/collect_urma.py run on
    # overlapping cron schedules while plotter.py/tempAnalysis.py may hold
    # a long read open against a much larger file. This is a per-file
    # setting stored in the database itself, so it only needs to actually
    # change mode once -- safe/cheap to call on every connection.
    conn.execute("PRAGMA journal_mode = WAL;")

    conn.executescript(SCHEMA)
    cursor = conn.cursor()

    # Both tables' PRIMARY KEYs lead with station/model, which makes them
    # efficient for "WHERE station = ? AND timestamp >= ?"-style queries
    # but USELESS for a date-only filter with no station/model specified
    # (e.g. plotter.py's "all stations in this window" queries) -- SQLite
    # can't use a leading-column index when the leading column isn't in
    # the WHERE clause, so those fall back to a full table scan. These
    # indexes cover that access pattern directly. IF NOT EXISTS makes this
    # safe/idempotent to run on every connection, matching the migration
    # pattern used elsewhere in this function.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_station_obs_timestamp "
        "ON station_obs(timestamp);"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_analysis_valid_time "
        "ON model_analysis(valid_time);"
    )

    # --- model_analysis migrations ---
    cursor.execute("PRAGMA table_info(model_analysis);")
    model_columns = [col[1] for col in cursor.fetchall()]

    new_model_cols = {
        "temp_sd": "REAL",
        "cloud_cover": "REAL",
        "wind_speed": "REAL",
        "precip": "REAL",
    }
    for col_name, col_type in new_model_cols.items():
        if col_name not in model_columns:
            print(f"Migrating schema: Adding '{col_name}' column to "
                  f"model_analysis...")
            cursor.execute(f"ALTER TABLE model_analysis ADD COLUMN {col_name} "
                           f"{col_type};")

    # --- station_obs migrations (expanded Ambient Weather fields) ---
    cursor.execute("PRAGMA table_info(station_obs);")
    station_columns = [col[1] for col in cursor.fetchall()]

    new_station_cols = {
        "feels_like": "REAL",
        "temp_indoor": "REAL",
        "humidity_indoor": "REAL",
        "wind_speed": "REAL",
        "wind_gust": "REAL",
        "max_daily_gust": "REAL",
        "wind_dir": "REAL",
        "wind_gust_dir": "REAL",
        "wind_speed_avg2m": "REAL",
        "wind_dir_avg2m": "REAL",
        "wind_speed_avg10m": "REAL",
        "wind_dir_avg10m": "REAL",
        "pressure_rel": "REAL",
        "pressure_abs": "REAL",
        "rain_hourly": "REAL",
        "rain_daily": "REAL",
        "rain_24h": "REAL",
        "rain_weekly": "REAL",
        "rain_monthly": "REAL",
        "rain_yearly": "REAL",
        "rain_event": "REAL",
        "rain_total": "REAL",
        "uv_index": "REAL",
        "solar_radiation": "REAL",
        "co2": "REAL",
        "pm25": "REAL",
        "pm25_24h": "REAL",
        "lightning_day": "INTEGER",
        "lightning_hour": "INTEGER",
        "lightning_distance": "REAL",
        "last_rain": "TEXT",
        # --- Added for CSV-imported sensors not present via the API ---
        "indoor_feels_like": "REAL",
        "indoor_dew_point": "REAL",
        "porch_temp": "REAL",
        "porch_humidity": "REAL",
        "porch_feels_like": "REAL",
        "porch_dew_point": "REAL",
        "pm25_indoor": "REAL",
        "pm25_indoor_24h": "REAL",
        "battery_outdoor": "INTEGER",
        "battery_indoor": "INTEGER",
        "battery_porch": "INTEGER",
        "battery_pm25_outdoor": "INTEGER",
        "battery_pm25_indoor": "INTEGER",
    }
    for col_name, col_type in new_station_cols.items():
        if col_name not in station_columns:
            print(f"Migrating schema: Adding '{col_name}' column to "
                  f"station_obs...")
            cursor.execute(f"ALTER TABLE station_obs ADD COLUMN {col_name} "
                           f"{col_type};")

    conn.commit()


def env_lat_lon():
    """
    Return (lat, lon) as floats from WEATHER_LAT/WEATHER_LON, or
    (None, None) if unset. Entrypoint scripts should let --lat/--lon
    override this, and error out if neither is available.
    """
    lat = os.environ.get("WEATHER_LAT")
    lon = os.environ.get("WEATHER_LON")
    return (float(lat) if lat else None, float(lon) if lon else None)


def parse_date_arg(date_str):
    """Parse a date string flexibly. Returns a UTC-aware datetime."""
    dt = pd.to_datetime(date_str)
    if dt.tzinfo is None:
        dt = dt.tz_localize("UTC")
    else:
        dt = dt.tz_convert("UTC")
    return dt


# ---------------------------------------------------------------------------
# MODEL HERBIE OBJECT CREATION
# ---------------------------------------------------------------------------

def make_herbie(target_str, model, fxx=0):
    try:
        from herbie import Herbie
    except ImportError as e:
        raise RuntimeError(
            "herbie (and its cfgrib/eccodes dependency) is required for "
            "URMA/RTMA/HRRR/RRFS fetching but is not available in this "
            "environment. Use collect_stations.py + collect_gladstone.py "
            "instead, which have no GRIB/eccodes dependency -- see the "
            "eccodes-free flow note in this module's docstring."
        ) from e

    if model in ("urma", "rtma", "rtma_ru"):
        return Herbie(
            target_str,
            model=model,
            product="anl",
            fxx=0,
            priority=["aws", "nomads"],
        )
    elif model == "hrrr":
        return Herbie(
            target_str,
            model="hrrr",
            product="sfc",
            fxx=fxx,
            priority=["aws", "nomads"],
        )
    else:
        return Herbie(
            target_str,
            model=model,
            fxx=fxx,
            priority=["aws", "nomads"],
        )


# ---------------------------------------------------------------------------
# GRIB FIELD EXTRACTION
# ---------------------------------------------------------------------------

def extract_grib_fields(H, lat, lon, model_name, target_str, fxx=0):
    if H.grib is None:
        return None, None, None, None, None

    points = pd.DataFrame({"latitude": [lat], "longitude": [lon]})
    temp_f = temp_sd = cloud_cover = wind_speed = precip = None
    full_file_downloaded = False

    def _xarray_with_fallback(H_obj, query):
        nonlocal full_file_downloaded
        try:
            return H_obj.xarray(query)
        except (ValueError, Exception) as e:
            err_str = str(e).lower()
            if "no index file was found" in err_str:
                if not full_file_downloaded:
                    print(f"    📦 No .idx file on server. Downloading full "
                          f"GRIB...")
                    H_obj.download()
                    full_file_downloaded = True
                H_new = make_herbie(target_str, model_name, fxx=fxx)
                if H_new.grib is None:
                    local_path = H_obj.get_localFilePath("")
                    if local_path.exists():
                        print(f"    📄 Reading local file: {local_path}")
                        H_new = make_herbie(target_str, model_name,
                                            fxx=fxx)
                        H_new.grib = str(local_path)
                        H_new.grib_source = "local"
                    else:
                        raise FileNotFoundError(
                            f"Local GRIB not found after download: "
                            f"{local_path}")
                try:
                    del H_new.index_as_dataframe
                except AttributeError:
                    pass
                return H_new.xarray(query)
            raise

    # 1. Temperature & Stencil Spatial SD
    for query in ["TMP:2 m above ground", "TMP:2 m"]:
        try:
            ds = _xarray_with_fallback(H, query)
            if isinstance(ds, list):
                ds = ds[0]
            if ds is None or "t2m" not in ds.data_vars:
                continue

            lon_sample = float(ds.longitude.values.min())
            uses_0_360 = lon_sample >= 0

            picked = ds.herbie.pick_points(points, method="nearest")
            picked_lat = float(picked["latitude"].values.flatten()[0])
            picked_lon_raw = float(picked["longitude"].values.flatten()[0])
            picked_lon_display = (picked_lon_raw - 360
                                  if picked_lon_raw > 180
                                  else picked_lon_raw)
            dist_deg = np.sqrt((picked_lat - lat) ** 2
                               + (picked_lon_display - lon) ** 2)
            if dist_deg > 0.5:
                print(f"    🚨 WARNING: Nearest grid point "
                      f"({picked_lat:.4f}, {picked_lon_display:.4f}) "
                      f"is {dist_deg:.2f}° from target ({lat}, {lon})!")
            else:
                print(f"    ✅ Grid point ({picked_lat:.4f}, "
                      f"{picked_lon_display:.4f}) "
                      f"is {dist_deg:.3f}° from target — looks correct.")

            temp_k = float(picked["t2m"].values.flatten()[0])
            temp_f = (temp_k - 273.15) * 9 / 5 + 32

            offsets_latlon = [
                (lat + STENCIL_OFFSET_DEG, lon + STENCIL_OFFSET_DEG),
                (lat + STENCIL_OFFSET_DEG, lon),
                (lat + STENCIL_OFFSET_DEG, lon - STENCIL_OFFSET_DEG),
                (lat,                     lon + STENCIL_OFFSET_DEG),
                (lat,                     lon),
                (lat,                     lon - STENCIL_OFFSET_DEG),
                (lat - STENCIL_OFFSET_DEG, lon + STENCIL_OFFSET_DEG),
                (lat - STENCIL_OFFSET_DEG, lon),
                (lat - STENCIL_OFFSET_DEG, lon - STENCIL_OFFSET_DEG),
            ]
            stencil_lons = [lo % 360 if uses_0_360 else lo
                            for _, lo in offsets_latlon]
            stencil_lats = [la for la, _ in offsets_latlon]
            stencil_points = pd.DataFrame({"latitude": stencil_lats,
                                           "longitude": stencil_lons})
            stencil_picked = ds.herbie.pick_points(stencil_points,
                                                   method="nearest")
            stencil_vals_k = stencil_picked["t2m"].values.flatten()

            temp_sd_k = float(np.std(stencil_vals_k))
            temp_sd = temp_sd_k * 9 / 5

            print(f"    📍 t2m: {temp_k:.2f}K = {temp_f:.1f}°F | SD: "
                  f"{temp_sd:.2f}°F | "
                  f"lon_convention: "
                  f"{'0-360' if uses_0_360 else '-180..180'}")
            break
        except Exception as e:
            print(f"    xarray error for '{query}': {e}")
            continue

    # 2. Total Cloud Cover
    for query in ["TCDC:entire atmosphere", "tcdc", "TCDC"]:
        try:
            ds = _xarray_with_fallback(H, query)
            if isinstance(ds, list):
                ds = ds[0]
            if ds is not None:
                picked = ds.herbie.pick_points(points, method="nearest")
                key = next((k for k in ["tcdc", "TCDC", "cc"]
                            if k in picked), None)
                if key:
                    cloud_cover = float(picked[key].values.flatten()[0])
                    break
        except Exception:
            continue

    # 3. Wind Speed (mph)
    for u_query, v_query in [("UGRD:10 m above ground",
                              "VGRD:10 m above ground"),
                             ("UGRD:10 m", "VGRD:10 m")]:
        try:
            ds_u = _xarray_with_fallback(H, u_query)
            ds_v = _xarray_with_fallback(H, v_query)
            if isinstance(ds_u, list):
                ds_u = ds_u[0]
            if isinstance(ds_v, list):
                ds_v = ds_v[0]
            if ds_u is not None and ds_v is not None:
                p_u = ds_u.herbie.pick_points(points, method="nearest")
                p_v = ds_v.herbie.pick_points(points, method="nearest")
                uk = next((k for k in ["u10", "UGRD", "u"]
                           if k in p_u), None)
                vk = next((k for k in ["v10", "VGRD", "v"]
                           if k in p_v), None)
                if uk and vk:
                    u_val = float(p_u[uk].values.flatten()[0])
                    v_val = float(p_v[vk].values.flatten()[0])
                    wind_speed = np.sqrt(u_val ** 2 + v_val ** 2) * 2.23694
                    break
        except Exception:
            continue

    # 4. Precipitation (inches)
    for query in ["APCP:surface", "tp", "APCP"]:
        try:
            ds = _xarray_with_fallback(H, query)
            if isinstance(ds, list):
                ds = ds[0]
            if ds is not None:
                picked = ds.herbie.pick_points(points, method="nearest")
                pk = next((k for k in ["tp", "APCP", "p"]
                           if k in picked), None)
                if pk:
                    p_kg = float(picked[pk].values.flatten()[0])
                    precip = p_kg / 25.4
                    break
        except Exception:
            continue

    return temp_f, temp_sd, cloud_cover, wind_speed, precip


# ---------------------------------------------------------------------------
# MODEL CASCADE RESOLUTION
# ---------------------------------------------------------------------------

ALL_TIERS = {"urma", "rtma", "rrfs", "hrrr"}


def resolve_model_cascade(target_time, enable_rrfs=False, tiers=None,
                          hrrr_max_lead=HRRR_MAX_LEAD):
    """
    Build the ordered (model_key, fxx) cascade to try for target_time,
    best quality first.

    tiers: a subset of {"urma", "rtma", "rrfs", "hrrr"} restricting which
    parts of the cascade are considered. None (default) = all tiers, i.e.
    the original single-script behavior. Use this to keep a fast-running
    script (collect_models.py) from wasting requests on tiers it has no
    chance of getting yet (urma/rtma), and a slow gold-standard sweep
    (collect_urma.py) from falling back to hrrr when it should just wait.
    """
    if tiers is None:
        tiers = ALL_TIERS

    age_s = (datetime.now(timezone.utc) - target_time).total_seconds()
    cascade = []
    is_future = age_s < 0

    if not is_future:
        if "urma" in tiers and age_s >= URMA_LATENCY_S:
            cascade.append(("urma", 0))
        if "rtma" in tiers and age_s >= RTMA_LATENCY_S:
            cascade.append(("rtma", 0))

        # RRFS — off by default even when "rrfs" is in tiers; also needs
        # enable_rrfs=True. The RRFS AWS feed was paused during the
        # Aug 2026 pre-implementation phase (see module docstring) and
        # Herbie support was unverified as of this writing. Pass
        # --enable-rrfs once both are confirmed working; until then this
        # tier is skipped entirely and HRRR remains the forecast fallback.
        if "rrfs" in tiers and enable_rrfs:
            if age_s >= HRRR_F00_LATENCY_S:
                cascade.append(("rrfs_f00", 0))
            for lead in range(1, RRFS_MAX_LEAD + 1):
                cycle_time = target_time - timedelta(hours=lead)
                cycle_age = (datetime.now(timezone.utc)
                             - cycle_time).total_seconds()
                if cycle_age >= HRRR_F00_LATENCY_S:
                    cascade.append((f"rrfs_f{lead:02d}", lead))

        if "hrrr" in tiers and age_s >= HRRR_F00_LATENCY_S:
            cascade.append(("hrrr_f00", 0))

    if "hrrr" in tiers:
        for lead in range(1, hrrr_max_lead + 1):
            cycle_time = target_time - timedelta(hours=lead)
            cycle_age = (datetime.now(timezone.utc)
                         - cycle_time).total_seconds()
            if cycle_age >= HRRR_F00_LATENCY_S:
                cascade.append((f"hrrr_f{lead:02d}", lead))

    return cascade


def _cleanup_grib_cache(H):
    """Best-effort delete of the local GRIB2 file(s) Herbie downloaded for
    this H object, so long-running collection no longer leaves every
    fetched message sitting in Herbie's cache directory forever.

    KNOWN LIMITATION: extract_grib_fields() issues several separate
    .xarray(query) calls (temp, cloud cover, wind u/v, precip), and Herbie
    may download a distinct on-disk "subset" file per unique search string
    rather than reusing a single file. This function only knows about
    H.grib -- the path tracked on the object we were handed -- not any
    per-query subset files Herbie may have created internally, nor the
    H_new objects created in extract_grib_fields()'s idx-fallback path.
    Verify actual disk usage after a real run; if subset files are still
    accumulating, cleanup will need to target Herbie's save directory for
    this cycle/model directly instead of relying on H.grib alone.

    Silently ignores missing files / permission errors -- other cron jobs
    may be reading or have already cleaned the same path concurrently.
    """
    if H is None or H.grib is None:
        return
    for suffix in ("", ".idx"):
        try:
            path = Path(str(H.grib) + suffix)
            if path.exists():
                path.unlink()
        except OSError:
            pass


def try_model_extraction(target_time, model_key, fxx, lat, lon):
    if (model_key.startswith("hrrr_f") or model_key.startswith("rrfs_f")) \
            and fxx > 0:
        cycle_time = target_time - timedelta(hours=fxx)
        target_str_for_herbie = cycle_time.strftime("%Y-%m-%d %H:%M")
    else:
        target_str_for_herbie = target_time.strftime("%Y-%m-%d %H:%M")

    if model_key.startswith("hrrr"):
        base_model = "hrrr"
    elif model_key.startswith("rrfs"):
        # Experimental — untested against Herbie as of this writing.
        base_model = "rrfs"
    else:
        base_model = model_key

    H = make_herbie(target_str_for_herbie, base_model, fxx=fxx)

    if H.grib is None:
        print(f"    {model_key}: file not found on any source "
              f"({target_str_for_herbie} F{fxx:02d})")
        return None, None, None, None, None

    result = extract_grib_fields(H, lat, lon, base_model,
                                 target_str_for_herbie, fxx=fxx)
    _cleanup_grib_cache(H)
    return result


# ---------------------------------------------------------------------------
# STATION DATA INGESTION
# ---------------------------------------------------------------------------

def fetch_nws_station(conn, station, start_time, end_time, force=False):
    mode = "FORCE (overwrite)" if force else "SKIP EXISTING"
    print(f"Fetching station observations for {station} [{mode}]...")
    print(f"  Range: {start_time.strftime('%Y-%m-%d %H:%M')} -> "
          f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")

    if not force:
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp FROM station_obs WHERE station = ?",
                       (station,))
        existing = {row[0] for row in cursor.fetchall()}
    else:
        existing = set()

    url = f"https://api.weather.gov/stations/{station}/observations"
    try:
        r = SESSION.get(url, params={"limit": 500}, timeout=15)
        if r.status_code != 200:
            print(f"  Warning: Status {r.status_code} for {station}")
            return
        features = r.json().get("features", [])

        new_count = 0
        overwritten_count = 0
        skipped_count = 0

        with conn:
            for f in features:
                props = f.get("properties", {})
                ts_str = props.get("timestamp")
                if not ts_str:
                    continue
                ts = pd.to_datetime(ts_str)
                ts = (ts.tz_localize("UTC") if ts.tzinfo is None
                      else ts.tz_convert("UTC"))
                iso_ts = ts.isoformat()

                if ts < start_time or ts > end_time:
                    continue

                if not force and iso_ts in existing:
                    skipped_count += 1
                    continue

                temp_c = props.get("temperature", {}).get("value")
                temp_f = temp_c * 9 / 5 + 32 if temp_c is not None else None
                # BUG FIX: dewpoint from the NWS API is Celsius, exactly
                # like temperature -- this was previously stored raw and
                # unconverted, meaning KSGF/KBBG's dewpoint column held
                # Celsius values while G6964's (Ambient-sourced) dewpoint
                # is correctly Fahrenheit. Same column, silently mixed
                # units depending on station.
                dp_c = props.get("dewpoint", {}).get("value")
                dp_f = dp_c * 9 / 5 + 32 if dp_c is not None else None
                rh = props.get("relativeHumidity", {}).get("value")
                # NWS API reports this in mm (wmoUnit:mm), matching the
                # unit convention used for temperature/dewpoint -- convert
                # to inches to match rain_hourly's existing convention
                # (G6964's Ambient-sourced values are already inches).
                precip_mm = props.get("precipitationLastHour", {}).get("value")
                rain_hourly = precip_mm / 25.4 if precip_mm is not None else None

                is_existing = iso_ts in existing
                conn.execute(
                    """INSERT OR REPLACE INTO station_obs
                       (station, timestamp, temperature, dewpoint,
                        relative_humidity, rain_hourly)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (station, iso_ts, temp_f, dp_f, rh, rain_hourly),
                )

                if force and is_existing:
                    overwritten_count += 1
                else:
                    new_count += 1
                existing.add(iso_ts)

        print(f"  {station}: {new_count} new, {overwritten_count} "
              f"overwritten, {skipped_count} skipped (already in DB)")
    except Exception as e:
        print(f"  Error fetching {station}: {e}")


# ---------------------------------------------------------------------------
# IEM MESONET ASOS HISTORICAL BACKFILL
# ---------------------------------------------------------------------------
# Iowa Environmental Mesonet's ASOS request service
# (mesonet.agron.iastate.edu) holds long-run historical station
# observations -- often years to decades further back than the live NWS
# API's rolling window. Single plain HTTP GET, no auth, no pagination.
#
# Adds wind_speed for NWS stations (KSGF/KBBG), which fetch_nws_station()
# above does NOT populate -- this is a deliberate enrichment beyond the
# live-API path, made possible because IEM's ASOS export includes it and
# station_obs already has the column (used for G6964 today).

IEM_ASOS_BASE_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# Conservative early floor for --full-history: IEM's archive naturally
# truncates to whatever a given station actually has, so this just needs
# to be "earlier than any real ASOS station could have data," not exact.
IEM_FULL_HISTORY_START = datetime(1928, 1, 1, tzinfo=timezone.utc)


def _download_with_progress(session, url, params, label="Downloading",
                            timeout=300):
    """Stream a GET response, printing a curl-style progress line as bytes
    arrive, and return (status_code, full_text). Used for the IEM full-
    history pulls, which can run for minutes with a single blocking
    request otherwise giving zero feedback.

    IEM's CGI-generated CSV typically has no Content-Length header (the
    server doesn't know the size upfront), so this falls back to a plain
    byte counter in that case -- same as curl's own behavior when the
    remote doesn't advertise a size.
    """
    with session.get(url, params=params, timeout=timeout, stream=True) as r:
        if r.status_code != 200:
            return r.status_code, ""

        total = r.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None

        chunks = []
        downloaded = 0
        last_print = 0.0

        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            downloaded += len(chunk)
            now = time.time()
            if now - last_print > 0.15:
                _print_progress(label, downloaded, total)
                last_print = now

        _print_progress(label, downloaded, total, final=True)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return r.status_code, text


def _print_progress(label, downloaded, total, final=False):
    mb = downloaded / 1e6
    if total:
        pct = min(100.0, downloaded / total * 100)
        bar_width = 28
        filled = int(bar_width * downloaded / total)
        bar = "#" * filled + "-" * (bar_width - filled)
        line = f"\r  {label}: [{bar}] {pct:5.1f}%  {mb:7.1f} MB"
    else:
        line = f"\r  {label}: {mb:7.1f} MB downloaded"
    print(line, end=("\n" if final else ""), flush=True)


def fetch_iem_asos(conn, station, start_time, end_time, force=False):
    mode = "FORCE (overwrite)" if force else "SKIP EXISTING"
    print(f"Fetching IEM ASOS history for {station} [{mode}]...")
    print(f"  Range: {start_time.strftime('%Y-%m-%d %H:%M')} -> "
          f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")

    if not force:
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp FROM station_obs WHERE station = ?",
                       (station,))
        existing = {row[0] for row in cursor.fetchall()}
    else:
        existing = set()

    params = {
        "station": station,
        "data": "all",
        "year1": start_time.year, "month1": start_time.month, "day1": start_time.day,
        "year2": end_time.year, "month2": end_time.month, "day2": end_time.day,
        "tz": "Etc/UTC",
        "format": "comma",   # WITH header row -- parse by column name, not
                             # position, so this doesn't silently break if
                             # IEM ever changes the "all" field order/set.
        "latlon": "no",
        "elev": "no",
        "missing": "M",
        "trace": "T",
        "direct": "no",
    }

    try:
        # report_type must be sent as a repeated key (3=routine, 4=special);
        # requests supports this via a list value under one params dict key.
        status_code, text = _download_with_progress(
            SESSION, IEM_ASOS_BASE_URL,
            {**params, "report_type": ["3", "4"]},
            label=f"{station} history",
        )
        if status_code != 200:
            print(f"  Warning: Status {status_code} for {station}")
            return

        # IEM prefixes comment/metadata lines with '#' above the real header
        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        if not lines:
            print(f"  No data returned for {station} in this range.")
            return

        reader = csv.DictReader(lines)

        new_count = 0
        overwritten_count = 0
        skipped_count = 0
        bad_ts_count = 0

        with conn:
            for row in reader:
                ts_str = row.get("valid")
                if not ts_str:
                    continue
                try:
                    ts = pd.to_datetime(ts_str)
                    ts = (ts.tz_localize("UTC") if ts.tzinfo is None
                          else ts.tz_convert("UTC"))
                except (ValueError, TypeError):
                    bad_ts_count += 1
                    continue
                iso_ts = ts.isoformat()

                if ts < start_time or ts > end_time:
                    continue

                if not force and iso_ts in existing:
                    skipped_count += 1
                    continue

                def _val(field):
                    raw = row.get(field, "").strip()
                    if raw == "" or raw == "M":
                        return None
                    try:
                        return float(raw)
                    except (ValueError, TypeError):
                        return None

                temp_f = _val("tmpf")
                dewpoint_f = _val("dwpf")
                relative_humidity = _val("relh")
                wind_knots = _val("sknt")
                wind_speed = (wind_knots * 1.15078
                             if wind_knots is not None else None)
                # p01i: 1-hour precip, already inches per IEM's convention
                # (unlike NWS's live API, which reports mm) -- no
                # conversion needed.
                rain_hourly = _val("p01i")

                is_existing = iso_ts in existing
                conn.execute(
                    """INSERT OR REPLACE INTO station_obs
                       (station, timestamp, temperature, dewpoint,
                        relative_humidity, wind_speed, rain_hourly)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (station, iso_ts, temp_f, dewpoint_f,
                     relative_humidity, wind_speed, rain_hourly),
                )

                if force and is_existing:
                    overwritten_count += 1
                else:
                    new_count += 1
                existing.add(iso_ts)

        print(f"  {station}: {new_count} new, {overwritten_count} "
              f"overwritten, {skipped_count} skipped (already in DB)"
              + (f", {bad_ts_count} bad timestamps" if bad_ts_count else ""))
    except Exception as e:
        print(f"  Error fetching IEM ASOS history for {station}: {e}")


# ---------------------------------------------------------------------------
# IEM MESONET ASOS HIGH-FREQUENCY ("1-MINUTE") BACKFILL
# ---------------------------------------------------------------------------
# A DIFFERENT endpoint from fetch_iem_asos() above -- NOT a report_type
# value on asos.py. Sourced from NCEI's one-minute ASOS archive; per IEM's
# own docs this is "mostly undocumented" and parsed on a "best-guess
# effort" basis, delayed ~24-36h, with coverage starting ~2000 and varying
# by station.
#
# Confirmed empirically (not from written docs, since the help page
# wasn't fetchable and this format has no authoritative reference):
#   - Uses 3-letter FAA codes (SGF, BBG), not 4-letter ICAO (KSGF, KBBG) --
#     mapped here so results still land under the same station identity
#     as the METAR-sourced rows from fetch_iem_asos().
#   - Header: station,station_name,valid(UTC),<requested vars...>
#   - No relh field available at all -- relative_humidity is left NULL
#     for these rows rather than back-computed from temp/dewpoint, since
#     that's a derived-value decision for a dedicated derived_fields.py
#     module, not something to embed silently in a fetch function.
#   - sample=5min does NOT mean "resample to 5-minute intervals" --
#     empirically produced 1 row/hour instead of the expected ~12,
#     suggesting it filters to a fixed offset rather than aggregating.
#     Deliberately NOT used here; the endpoint's natural raw cadence
#     (~14 rows/hour observed for KSGF) is taken as-is instead, since we
#     control the semantics of what we store rather than trusting an
#     unverified server-side parameter.
# Given these open unknowns, treat this as provisional -- worth spot-
# checking actual imported rows against what the source returns,
# especially for a station/period not yet tested against real data.

IEM_1MIN_BASE_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"


def _icao_to_faa(station):
    """KSGF -> SGF, KBBG -> BBG. Standard US convention: 4-letter ICAO
    code for CONUS airports is 'K' + the 3-letter FAA identifier."""
    if len(station) == 4 and station.startswith("K"):
        return station[1:]
    return station


def fetch_iem_asos_1min(conn, station, start_time, end_time, force=False):
    mode = "FORCE (overwrite)" if force else "SKIP EXISTING"
    faa_code = _icao_to_faa(station)
    print(f"Fetching IEM ASOS 1-minute history for {station} "
          f"(FAA code: {faa_code}) [{mode}]...")
    print(f"  Range: {start_time.strftime('%Y-%m-%d %H:%M')} -> "
          f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")

    if not force:
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp FROM station_obs WHERE station = ?",
                       (station,))
        existing = {row[0] for row in cursor.fetchall()}
    else:
        existing = set()

    params = {
        "station": faa_code,
        # precip added here is UNVERIFIED for this specific endpoint --
        # unlike p01i on the hourly ASOS fetch above (confirmed against
        # real sample data), this is inferred from an old reference
        # showing the underlying 1-minute table has a 'precip' column,
        # not confirmed against an actual response from THIS endpoint.
        # Worth a real spot-check before trusting it; if it's not a valid
        # var name here, IEM should just omit it from the response rather
        # than error, but that's also unverified.
        "vars": ["tmpf", "dwpf", "drct", "sknt", "precip"],
        "sts": start_time.strftime("%Y-%m-%dT%H:%M") + "Z",
        "ets": end_time.strftime("%Y-%m-%dT%H:%M") + "Z",
        "tz": "UTC",
        "what": "download",
        # NOTE: no 'sample' param -- see module comment above.
    }

    try:
        status_code, text = _download_with_progress(
            SESSION, IEM_1MIN_BASE_URL, params,
            label=f"{station} 1-minute history",
        )
        if status_code != 200:
            print(f"  Warning: Status {status_code} for {station}")
            return

        lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
        if not lines:
            print(f"  No data returned for {station} in this range.")
            return

        reader = csv.DictReader(lines)

        new_count = 0
        overwritten_count = 0
        skipped_count = 0
        bad_ts_count = 0

        with conn:
            for row in reader:
                ts_str = row.get("valid(UTC)")
                if not ts_str:
                    continue
                try:
                    ts = pd.to_datetime(ts_str)
                    ts = (ts.tz_localize("UTC") if ts.tzinfo is None
                          else ts.tz_convert("UTC"))
                except (ValueError, TypeError):
                    bad_ts_count += 1
                    continue
                iso_ts = ts.isoformat()

                if ts < start_time or ts > end_time:
                    continue

                if not force and iso_ts in existing:
                    skipped_count += 1
                    continue

                def _val(field):
                    raw = (row.get(field) or "").strip()
                    if raw == "" or raw.upper() in ("M", "NONE", "NULL"):
                        return None
                    try:
                        return float(raw)
                    except (ValueError, TypeError):
                        return None

                temp_f = _val("tmpf")
                dewpoint_f = _val("dwpf")
                wind_dir = _val("drct")
                wind_knots = _val("sknt")
                wind_speed = (wind_knots * 1.15078
                             if wind_knots is not None else None)
                rain_hourly = _val("precip")  # see UNVERIFIED note above

                is_existing = iso_ts in existing
                conn.execute(
                    """INSERT OR REPLACE INTO station_obs
                       (station, timestamp, temperature, dewpoint,
                        wind_dir, wind_speed, rain_hourly)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (station, iso_ts, temp_f, dewpoint_f,
                     wind_dir, wind_speed, rain_hourly),
                )

                if force and is_existing:
                    overwritten_count += 1
                else:
                    new_count += 1
                existing.add(iso_ts)

        print(f"  {station}: {new_count} new, {overwritten_count} "
              f"overwritten, {skipped_count} skipped (already in DB)"
              + (f", {bad_ts_count} bad timestamps" if bad_ts_count else ""))
    except Exception as e:
        print(f"  Error fetching IEM ASOS 1-minute history for {station}: {e}")


# ---------------------------------------------------------------------------
# AMBIENT WEATHER FULL FIELD SUPPORT
# ---------------------------------------------------------------------------

AMBIENT_FIELD_MAP = {
    "tempf":              "temperature",
    "humidity":           "relative_humidity",
    "dewPoint":           "dewpoint",
    "feelsLike":          "feels_like",
    "tempinf":            "temp_indoor",
    "humidityin":         "humidity_indoor",
    "windspeedmph":       "wind_speed",
    "windgustmph":        "wind_gust",
    "maxdailygust":       "max_daily_gust",
    "winddir":            "wind_dir",
    "windgustdir":        "wind_gust_dir",
    "windspdmph_avg2m":   "wind_speed_avg2m",
    "winddir_avg2m":      "wind_dir_avg2m",
    "windspdmph_avg10m":  "wind_speed_avg10m",
    "winddir_avg10m":     "wind_dir_avg10m",
    "baromrelin":         "pressure_rel",
    "baromabsin":         "pressure_abs",
    "hourlyrainin":       "rain_hourly",
    "dailyrainin":        "rain_daily",
    "24hourrainin":       "rain_24h",
    "weeklyrainin":       "rain_weekly",
    "monthlyrainin":      "rain_monthly",
    "yearlyrainin":       "rain_yearly",
    "eventrainin":        "rain_event",
    "totalrainin":        "rain_total",
    "uv":                 "uv_index",
    "solarradiation":     "solar_radiation",
    "co2":                "co2",
    "pm25":               "pm25",
    "pm25_24h":           "pm25_24h",
    "lightning_day":      "lightning_day",
    "lightning_hour":     "lightning_hour",
    "lightning_distance": "lightning_distance",
    "lastRain":           "last_rain",
}


def _build_ambient_insert_sql():
    cols = ["station", "timestamp"] + list(AMBIENT_FIELD_MAP.values())
    placeholders = ", ".join("?" for _ in cols)
    col_str = ", ".join(cols)
    return (f"INSERT OR REPLACE INTO station_obs ({col_str}) "
            f"VALUES ({placeholders})")


def _extract_ambient_record(rec):
    date_str = rec.get("date")
    if not date_str:
        return None

    dt = pd.to_datetime(date_str)
    dt = (dt.tz_localize("UTC") if dt.tzinfo is None
          else dt.tz_convert("UTC"))
    iso_ts = dt.isoformat()

    values = []
    for api_field, _db_col in AMBIENT_FIELD_MAP.items():
        val = rec.get(api_field)
        if val is not None:
            try:
                if api_field in ("lightning_day", "lightning_hour"):
                    values.append(int(val))
                else:
                    values.append(float(val))
            except (ValueError, TypeError):
                values.append(None)
        else:
            values.append(None)

    return iso_ts, tuple(values)


def fetch_ambient_g6964(conn, start_time, end_time, force=False,
                        fields_only=False):
    """Fetch G6964 Ambient Weather station data via paginated API.
    Rate limited to 1 req/sec with 429 exponential backoff retry.

    Modes:
      Normal (force=False, fields_only=False):
          Skip existing rows, insert new rows with all fields.
      Force (force=True, fields_only=False):
          Overwrite all rows in range with fresh API data + all fields.
      Fields-only backfill (fields_only=True):
          Re-paginate API to populate expanded fields into existing rows.
          Does NOT insert new rows. Skips rows that already have wind_speed
          (idempotent — safe to run multiple times).

    Pagination strategy:
      The Ambient Weather API returns records newest-first. We paginate
      backwards by setting endDate to the oldest timestamp seen in each
      batch. When we reach records older than start_time, we stop.

      Key fix: we track the NEWEST timestamp in each page and use it to
      detect when the API is returning the same page repeatedly (stuck
      loop). We also track a global "last endDate sent" to ensure we
      never request the same endDate twice.
    """
    if fields_only:
        mode = "BACKFILL FIELDS (populate missing fields only)"
    elif force:
        mode = "FORCE (overwrite all with full fields)"
    else:
        mode = "SKIP EXISTING (insert new with full fields)"

    print(f"Polling Ambient Weather API for G6964 [{mode}]...")
    print(f"  Range: {start_time.strftime('%Y-%m-%d %H:%M')} -> "
          f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Rate limit: 1 req/sec -- adding 1.1s delay between paginated "
          f"calls")
    print(f"  Fields per record: {len(AMBIENT_FIELD_MAP)}")
    print(f"  Safety valve: max {AMBIENT_MAX_PAGES} pages")

    cursor = conn.cursor()
    cursor.execute("SELECT timestamp FROM station_obs WHERE station = 'G6964'")
    existing_timestamps = {row[0] for row in cursor.fetchall()}

    insert_sql = _build_ambient_insert_sql()

    url = f"https://rt.ambientweather.net/v1/devices/{G6964_MAC}"
    end_date_str = None
    page_num = 0
    total_new = 0
    total_overwritten = 0
    total_skipped = 0
    total_backfilled = 0

    # Track the last endDate we sent to the API to detect stuck loops
    last_end_date_sent = None

    while True:
        # Safety valve
        if page_num >= AMBIENT_MAX_PAGES:
            print(f"\n  ⚠️  SAFETY VALVE: Reached {AMBIENT_MAX_PAGES} pages. "
                  f"Stopping to prevent infinite loop.")
            break

        params = {"apiKey": AMBIENT_API_KEY,
                  "applicationKey": AMBIENT_APP_KEY, "limit": 288}
        if end_date_str:
            params["endDate"] = end_date_str

        # Detect stuck loop: if we're about to send the same endDate again
        if end_date_str is not None and end_date_str == last_end_date_sent:
            print(f"\n  ⚠️  STUCK LOOP DETECTED: endDate hasn't advanced "
                  f"({end_date_str}). Stopping.")
            break
        last_end_date_sent = end_date_str

        max_retries = 5
        resp = None
        for attempt in range(max_retries):
            try:
                resp = SESSION.get(url, params=params, timeout=15)
            except Exception as e:
                print(f"  Network error (attempt {attempt + 1}/"
                      f"{max_retries}): {e}")
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:
                wait = 2 ** attempt
                print(f"  Rate limited (429). Backing off {wait}s (attempt "
                      f"{attempt + 1}/{max_retries})")
                time.sleep(wait)
                resp = None
                continue
            elif resp.status_code != 200:
                print(f"  Ambient API error {resp.status_code}: "
                      f"{resp.text[:200]}")
                break
            else:
                break

        if resp is None or resp.status_code != 200:
            print(f"  Aborting G6964 pagination after {max_retries} retries.")
            break

        time.sleep(1.1)

        page_num += 1
        records = resp.json()
        if not records or not isinstance(records, list):
            print(f"  [Page {page_num}] No records returned. Done.")
            break

        new_in_batch = 0
        overwritten_in_batch = 0
        skipped_in_batch = 0
        backfilled_in_batch = 0
        oldest_timestamp_in_batch = None
        newest_timestamp_in_batch = None
        reached_cutoff = False
        processed_any_in_range = False

        with conn:
            for rec in records:
                result = _extract_ambient_record(rec)
                if result is None:
                    continue
                iso_ts, field_values = result

                dt = pd.to_datetime(iso_ts)

                # Track newest and oldest timestamps seen in this
                if newest_timestamp_in_batch is None or dt > newest_timestamp_in_batch:
                    newest_timestamp_in_batch = dt
                if oldest_timestamp_in_batch is None or dt < oldest_timestamp_in_batch:
                    oldest_timestamp_in_batch = dt


                if dt < start_time:
                    reached_cutoff = True
                    break
                if dt > end_time:
                    continue

                processed_any_in_range = True

                is_existing = iso_ts in existing_timestamps

                if fields_only:
                    if not is_existing:
                        continue
                    cursor.execute(
                        """SELECT wind_speed FROM station_obs
                           WHERE station = 'G6964' AND timestamp = ?""",
                        (iso_ts,))
                    row = cursor.fetchone()
                    if row and row[0] is not None:
                        skipped_in_batch += 1
                        total_skipped += 1
                        continue
                    conn.execute(insert_sql,
                                 ("G6964", iso_ts) + field_values)
                    backfilled_in_batch += 1
                    total_backfilled += 1
                    continue

                if not force and is_existing:
                    skipped_in_batch += 1
                    total_skipped += 1
                    continue

                conn.execute(insert_sql, ("G6964", iso_ts) + field_values)
                existing_timestamps.add(iso_ts)

                if force and is_existing:
                    overwritten_in_batch += 1
                    total_overwritten += 1
                else:
                    new_in_batch += 1
                    total_new += 1

        if fields_only:
            print(f"  [Page {page_num}] {backfilled_in_batch} backfilled, "
                  f"{skipped_in_batch} already had fields | "
                  f"Range: "
                  f"{newest_timestamp_in_batch.strftime('%m-%d %H:%M') if newest_timestamp_in_batch else '?'}"
                  f" -> "
                  f"{oldest_timestamp_in_batch.strftime('%m-%d %H:%M') if oldest_timestamp_in_batch else '?'}")
        else:
            oldest_str = (oldest_timestamp_in_batch.strftime(
                              "%Y-%m-%d %H:%M UTC")
                          if oldest_timestamp_in_batch else "?")
            newest_str = (newest_timestamp_in_batch.strftime(
                              "%Y-%m-%d %H:%M UTC")
                          if newest_timestamp_in_batch else "?")
            print(f"  [Page {page_num}] {new_in_batch} new, "
                  f"{overwritten_in_batch} overwritten, "
                  f"{skipped_in_batch} skipped | "
                  f"Newest: {newest_str} | Oldest: {oldest_str}")

        # --- STOP CONDITIONS ---

        if reached_cutoff:
            print(f"  Reached start of range "
                  f"({start_time.strftime('%Y-%m-%d %H:%M')} UTC). Stopping.")
            break

        # If we got records but none were in our time range, and we
        # have an oldest timestamp, use it to advance pagination
        if not processed_any_in_range and oldest_timestamp_in_batch:
            # All records were either too new or too old.
            # If oldest is still newer than start_time, advance endDate
            if oldest_timestamp_in_batch > start_time:
                # -1s: the API's endDate appears to be INCLUSIVE, so
                # requesting the oldest timestamp we just saw re-fetches
                # that same boundary record forever instead of moving
                # past it. Step one second earlier to guarantee progress.
                end_date_str = (oldest_timestamp_in_batch
                                - timedelta(seconds=1)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z")
                continue
            else:
                # Oldest is older than our start — we're done
                break

        # Normal pagination: advance endDate to just before the oldest
        # timestamp in batch (see -1s note above -- without this, an
        # inclusive API boundary causes the loop to stall re-fetching the
        # same single record and trip the stuck-loop guard below well
        # short of the actual requested lookback window).
        if oldest_timestamp_in_batch:
            new_end_date = (oldest_timestamp_in_batch
                            - timedelta(seconds=1)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z")
            # If the new endDate is the same as what we just sent,
            # we're stuck (API returning same page)
            if new_end_date == last_end_date_sent:
                print(f"\n  ⚠️  Pagination not advancing (endDate stuck at "
                      f"{new_end_date}). Stopping.")
                break
            end_date_str = new_end_date
        else:
            # No timestamps extracted from this page — done
            break

    if fields_only:
        print(f"Finished G6964 backfill: {total_backfilled} rows updated, "
              f"{total_skipped} already had fields, "
              f"across {page_num} page(s).")
    else:
        print(f"Finished G6964: {total_new} new, {total_overwritten} "
              f"overwritten, {total_skipped} skipped "
              f"across {page_num} page(s).")


# ---------------------------------------------------------------------------
# AMBIENT WEATHER CSV IMPORT (one-time historical/manual dashboard exports)
# ---------------------------------------------------------------------------
# The Ambient Weather dashboard's "Export" CSV uses human-readable headers
# with units baked in, NOT the API's compact field names (AMBIENT_FIELD_MAP
# above) -- these are two separate naming conventions for the same station.
# Header text is whitespace-normalized on both sides (collapse runs of
# whitespace to a single space) before matching, so small formatting
# differences between export batches don't silently drop a column.
#
# Pressure is imported as-is in hPa (NOT converted to inHg like the API
# path's pressure_rel/pressure_abs) -- confirmed intentional, to stay
# consistent with NWS station units elsewhere in this archive. This means
# pressure_rel/pressure_abs are NOT directly comparable in raw form between
# API-sourced rows (inHg) and CSV-imported rows (hPa) without a unit-aware
# conversion at query time.

def _normalize_header(h):
    return " ".join(h.strip().split())

CSV_IMPORT_COLUMN_MAP = {
    "Outdoors Temperature (°F)":        "temperature",
    "Feels Like (°F)":                  "feels_like",
    "Dew Point (°F)":                   "dewpoint",
    "Wind Speed (mph)":                 "wind_speed",
    "Wind Gust (mph)":                  "wind_gust",
    "Max Daily Gust (mph)":             "max_daily_gust",
    "Wind Direction (°)":               "wind_dir",
    "Avg Wind Direction (10 mins) (°)": "wind_dir_avg10m",
    "Rain Rate (in/hr)":                "rain_hourly",
    "Event Rain (in)":                  "rain_event",
    "Daily Rain (in)":                  "rain_daily",
    "Weekly Rain (in)":                 "rain_weekly",
    "Monthly Rain (in)":                "rain_monthly",
    "Yearly Rain (in)":                 "rain_yearly",
    "Relative Pressure (hPa)":          "pressure_rel",
    "Absolute Pressure (hPa)":          "pressure_abs",
    "Humidity (%)":                     "relative_humidity",
    "Ultra-Violet Radiation Index":     "uv_index",
    "Solar Radiation (W/m^2)":          "solar_radiation",
    "Indoor Temperature (°F)":          "temp_indoor",
    "Indoor Humidity (%)":              "humidity_indoor",
    "Indoor Feels Like (°F)":           "indoor_feels_like",
    "Indoor Dew Point (°F)":            "indoor_dew_point",
    "Porch Temperature (°F)":           "porch_temp",
    "Porch Humidity (%)":               "porch_humidity",
    "Porch Feels Like (°F)":            "porch_feels_like",
    "Porch Dew Point (°F)":             "porch_dew_point",
    "Outdoors Battery":                 "battery_outdoor",
    "Indoor Battery":                   "battery_indoor",
    "Porch Battery":                    "battery_porch",
    "PM2.5 Outdoor Battery":            "battery_pm25_outdoor",
    "PM2.5 Indoor Battery":             "battery_pm25_indoor",
}
_BATTERY_DB_COLS = {"battery_outdoor", "battery_indoor", "battery_porch",
                     "battery_pm25_outdoor", "battery_pm25_indoor"}

# This export has each of these fields twice under two different header
# spellings (one with units, one without) -- almost certainly from the
# manual edit rather than a real duplicate sensor. Both are empty in every
# sample row seen so far, so which one "wins" hasn't been provable from
# data yet: for each DB column, take the first non-empty value found among
# its candidate headers, checked in the listed order (units-suffixed
# version preferred, since it lines up with the original AMBIENT_FIELD_MAP
# naming convention for these same two fields).
PM25_DUP_COLUMN_MAP = {
    "pm25":             ["PM2.5 Outdoor (µg/m^3)", "PM2.5 Outdoor"],
    "pm25_24h":         ["PM2.5 Outdoor 24 Hour Average (µg/m^3)",
                          "PM2.5 Outdoor 24 Hour Average"],
    "pm25_indoor":      ["PM2.5 Indoor (µg/m^3)", "PM2.5 Indoor"],
    "pm25_indoor_24h":  ["PM2.5 Indoor 24 Hour Average (µg/m^3)",
                          "PM2.5 Indoor 24 Hour Average"],
}

_CSV_IMPORT_DB_COLUMNS = (list(CSV_IMPORT_COLUMN_MAP.values())
                           + list(PM25_DUP_COLUMN_MAP.keys()))


def _build_csv_import_insert_sql():
    cols = ["station", "timestamp"] + _CSV_IMPORT_DB_COLUMNS
    placeholders = ", ".join("?" for _ in cols)
    col_str = ", ".join(cols)
    return (f"INSERT OR REPLACE INTO station_obs ({col_str}) "
            f"VALUES ({placeholders})")


def import_ambient_csv(conn, csv_path, station="G6964"):
    """One-time bulk import of an Ambient Weather dashboard CSV export into
    station_obs. Intended to run once per exported file, not on a schedule
    -- unlike fetch_ambient_g6964, there's no pagination/rate-limiting here,
    just a local file read.

    Existing rows for (station, timestamp) are overwritten (INSERT OR
    REPLACE), matching the --force semantics used elsewhere in this
    pipeline, since a manual CSV import is expected to be an intentional
    "this is the correct data" operation.
    """
    print(f"Importing Ambient Weather CSV: {csv_path}")
    print(f"  Target station: {station}")

    insert_sql = _build_csv_import_insert_sql()

    total_rows = 0
    imported = 0
    skipped_bad_timestamp = 0
    unmapped_headers = set()

    # Try multiple encodings: Ambient Weather exports sometimes use
    # Windows-1252 or Latin-1 instead of UTF-8, especially if headers
    # contain special characters (°, µ, etc.).
    encodings = ["utf-8-sig", "cp1252", "latin-1", "utf-16"]
    reader = None
    used_encoding = None

    for encoding in encodings:
        try:
            f = open(csv_path, newline="", encoding=encoding)
            reader = csv.DictReader(f, delimiter=",")
            # Try to read the first line to validate encoding
            _ = reader.fieldnames
            used_encoding = encoding
            print(f"  Detected encoding: {encoding}")
            break
        except (UnicodeDecodeError, UnicodeError):
            if f is not None:
                f.close()
            continue
        except Exception as e:
            if f is not None:
                f.close()
            raise

    if reader is None or used_encoding is None:
        print(f"  ERROR: Could not decode CSV with any encoding "
              f"({', '.join(encodings)})")
        return

    try:
        # Normalize the file's actual headers once, and build a lookup from
        # normalized header -> original header, so later dict lookups can
        # use the original DictReader keys.
        raw_headers = reader.fieldnames or []
        norm_to_raw = {_normalize_header(h): h for h in raw_headers}

        # Sanity-check: warn (don't fail) about any expected header that
        # isn't present in this particular export.
        expected = set(CSV_IMPORT_COLUMN_MAP) | {
            h for pair in PM25_DUP_COLUMN_MAP.values() for h in pair
        }
        for h in expected:
            if h not in norm_to_raw:
                unmapped_headers.add(h)

        with conn:
            for row in reader:
                total_rows += 1

                date_str = row.get("Date") or row.get(
                    norm_to_raw.get("Date", ""))
                if not date_str:
                    skipped_bad_timestamp += 1
                    continue

                try:
                    dt = pd.to_datetime(date_str)
                    dt = (dt.tz_localize("UTC") if dt.tzinfo is None
                          else dt.tz_convert("UTC"))
                    iso_ts = dt.isoformat()
                except (ValueError, TypeError):
                    skipped_bad_timestamp += 1
                    continue

                values = []
                for header, db_col in CSV_IMPORT_COLUMN_MAP.items():
                    raw_key = norm_to_raw.get(header)
                    raw_val = row.get(raw_key, "") if raw_key else ""
                    raw_val = (raw_val or "").strip()
                    if raw_val == "":
                        values.append(None)
                    elif db_col in _BATTERY_DB_COLS:
                        try:
                            values.append(int(float(raw_val)))
                        except (ValueError, TypeError):
                            values.append(None)
                    else:
                        try:
                            values.append(float(raw_val))
                        except (ValueError, TypeError):
                            values.append(None)

                for db_col, candidates in PM25_DUP_COLUMN_MAP.items():
                    resolved = None
                    for header in candidates:
                        raw_key = norm_to_raw.get(header)
                        raw_val = (row.get(raw_key, "") if raw_key
                                   else "").strip()
                        if raw_val != "":
                            try:
                                resolved = float(raw_val)
                            except (ValueError, TypeError):
                                resolved = None
                            break
                    values.append(resolved)

                conn.execute(insert_sql, (station, iso_ts) + tuple(values))
                imported += 1

        if unmapped_headers:
            print(f"  Note: {len(unmapped_headers)} expected header(s) not "
                  f"found in this file (left as NULL for all rows): "
                  f"{sorted(unmapped_headers)}")
        print(f"  Rows read: {total_rows} | Imported: {imported} | "
              f"Skipped (bad/missing timestamp): {skipped_bad_timestamp}")

        return imported, skipped_bad_timestamp
    finally:
        if f is not None:
            f.close()


# ---------------------------------------------------------------------------
# GLADSTONE ANALYSIS (eccodes-free fallback for the model_analysis table)
# ---------------------------------------------------------------------------
# Scrapes a small CSV of point-analysis values (temp, dewpoint, pressure,
# humidity, wind) from weather.gladstonefamily.net for a given Ambient
# Weather station/site ID. Uses only requests + csv/io -- no herbie,
# cfgrib, or eccodes. Precision/spatial resolution is much worse than
# real URMA/RTMA/HRRR GRIB extraction (this is a single point value the
# Gladstone site itself derives, not a raw model grid you're sampling),
# but it needs nothing beyond the stdlib + requests, so it's the tier to
# use where eccodes can't be installed. See collect_gladstone.py.

GLADSTONE_IQY_BASE = "https://weather.gladstonefamily.net/cgi-bin/wxobservations.pl"

# Gladstone CSV column -> model_analysis column. Only temp_f and
# wind_speed map onto existing model_analysis columns (which were built
# around GRIB-extraction fields); dewpoint/humidity/pressure/wind
# direction have no home in that schema and are intentionally dropped
# rather than bolted on with new columns other tiers don't populate.
# temp_sd, cloud_cover, precip stay NULL -- Gladstone gives one point
# value, not a grid to compute spread/cover/precip from.
GLADSTONE_COLUMN_MAP = {
    "Analysis Temperature (degrees F)": "temp_f",
    "Analysis Wind speed (mph)": "wind_speed",
}

MBAR_TO_INHG = 0.02953


def build_gladstone_url(site, days):
    return f"{GLADSTONE_IQY_BASE}?site={site}&days={days}"


def fetch_gladstone_analysis(conn, site, lat, lon, days, dry_run=False):
    """
    Fetch Gladstone's point-analysis CSV and upsert into model_analysis
    as model="gladstone". Gladstone's CSV isn't necessarily on the hour
    (readings can come every few minutes), but model_analysis is meant to
    hold one row per model per hour, matching urma/rtma/hrrr's on-the-hour
    convention. So rows are bucketed by clock hour first, and only the
    single reading closest to :00:00 in each bucket is kept -- this is
    what prevents duplicate same-hour rows from ever being written,
    rather than relying on cleanup after the fact.
    Returns (created, updated, unchanged, skipped).
    """
    url = build_gladstone_url(site, days)
    print(f"\nGladstone analysis: site={site}, days={days}")
    print(f"  URL: {url}")

    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"  Error fetching Gladstone URL: {e}")
        return 0, 0, 0, 0

    content = resp.text
    if not content.strip():
        print("  Error: Gladstone returned empty content")
        return 0, 0, 0, 0

    size_kb = len(content) / 1024
    print(f"  Downloaded {size_kb:.1f} KB")

    reader = csv.DictReader(io.StringIO(content))
    headers = reader.fieldnames
    if not headers or "Time (UTC)" not in headers:
        print(f"  Error: 'Time (UTC)' not in headers: {headers}")
        return 0, 0, 0, 0

    skipped = 0
    # hour_key (tz-aware Timestamp floored to the hour) -> (offset_seconds, dt, values)
    # keep only the entry with the smallest offset_seconds per hour_key.
    hourly_best = {}

    for row in reader:
        time_str = (row.get("Time (UTC)") or "").strip()
        if not time_str:
            skipped += 1
            continue

        try:
            dt = parse_date_arg(time_str)
        except (ValueError, TypeError):
            skipped += 1
            continue
        if dt is None:
            skipped += 1
            continue

        values = {}
        for csv_col, db_col in GLADSTONE_COLUMN_MAP.items():
            raw_val = (row.get(csv_col) or "").strip()
            if not raw_val:
                continue
            try:
                val = float(raw_val)
            except ValueError:
                continue
            values[db_col] = val

        if not values:
            skipped += 1
            continue

        hour_key = dt.floor("h")
        offset_seconds = abs((dt - hour_key).total_seconds())

        current_best = hourly_best.get(hour_key)
        if current_best is None or offset_seconds < current_best[0]:
            hourly_best[hour_key] = (offset_seconds, dt, values)
        else:
            # A reading exists closer to the top of this hour already;
            # this row is the one being "deduplicated" -- not written.
            skipped += 1

    created = updated = unchanged = 0

    for hour_key, (offset_seconds, dt, values) in hourly_best.items():
        # Store at the exact hour boundary, not the original reading time
        # -- matches urma/rtma/hrrr's on-the-hour valid_time convention
        # and is what makes future runs collide on the same PK instead of
        # creating a new row a few minutes off from an existing one.
        valid_time = hour_key.isoformat()

        existing = conn.execute(
            "SELECT temp_f, wind_speed FROM model_analysis "
            "WHERE model = 'gladstone' AND valid_time = ? "
            "AND latitude = ? AND longitude = ?",
            (valid_time, lat, lon),
        ).fetchone()

        if existing is None:
            if not dry_run:
                conn.execute(
                    "INSERT INTO model_analysis "
                    "(model, valid_time, latitude, longitude, temp_f, wind_speed) "
                    "VALUES ('gladstone', ?, ?, ?, ?, ?)",
                    (valid_time, lat, lon,
                     values.get("temp_f"), values.get("wind_speed")),
                )
            created += 1
        else:
            old_temp_f, old_wind_speed = existing
            new_temp_f = values.get("temp_f", old_temp_f)
            new_wind_speed = values.get("wind_speed", old_wind_speed)
            if new_temp_f != old_temp_f or new_wind_speed != old_wind_speed:
                if not dry_run:
                    conn.execute(
                        "UPDATE model_analysis SET temp_f = ?, wind_speed = ? "
                        "WHERE model = 'gladstone' AND valid_time = ? "
                        "AND latitude = ? AND longitude = ?",
                        (new_temp_f, new_wind_speed, valid_time, lat, lon),
                    )
                updated += 1
            else:
                unchanged += 1

    if not dry_run:
        conn.commit()

    return created, updated, unchanged, skipped


def dedupe_gladstone_hourly(conn, lat, lon, dry_run=False):
    """
    One-time (or periodic) cleanup for model="gladstone" rows already in
    the DB from before fetch_gladstone_analysis started bucketing by
    hour: collapses any rows that landed in the same clock hour into a
    single row (keeping the one closest to :00:00, matching the same
    rule fetch_gladstone_analysis uses going forward) and normalizes its
    valid_time to exactly the hour boundary. Safe to re-run -- it's a
    no-op once everything is deduped/normalized, so it's fine to run
    this at the start of every collect_gladstone.py invocation.
    Returns (rows_removed, rows_normalized).
    """
    rows = conn.execute(
        "SELECT rowid, valid_time FROM model_analysis "
        "WHERE model = 'gladstone' AND latitude = ? AND longitude = ? "
        "ORDER BY valid_time",
        (lat, lon),
    ).fetchall()

    buckets = {}
    for rowid, valid_time in rows:
        dt = pd.to_datetime(valid_time)
        hour_key = dt.floor("h")
        buckets.setdefault(hour_key, []).append((rowid, dt))

    removed = 0
    normalized = 0

    for hour_key, entries in buckets.items():
        entries.sort(key=lambda e: abs((e[1] - hour_key).total_seconds()))
        keeper_rowid, keeper_dt = entries[0]
        losers = entries[1:]

        if keeper_dt != hour_key:
            if not dry_run:
                conn.execute(
                    "UPDATE model_analysis SET valid_time = ? WHERE rowid = ?",
                    (hour_key.isoformat(), keeper_rowid),
                )
            normalized += 1

        if losers:
            if not dry_run:
                conn.executemany(
                    "DELETE FROM model_analysis WHERE rowid = ?",
                    [(rowid,) for rowid, _ in losers],
                )
            removed += len(losers)

    if not dry_run:
        conn.commit()

    return removed, normalized


# ---------------------------------------------------------------------------
# MODEL DATA INGESTION
# ---------------------------------------------------------------------------

def _prefetch_cascades_parallel(target_times, enable_rrfs, tiers,
                                hrrr_max_lead, lat, lon, max_workers):
    """Fetch model data for many target_times CONCURRENTLY (this is
    I/O-bound: network round-trips for .idx files + GRIB byte-range
    reads), returning {target_time: (chosen_model, result, models_tried,
    last_exception)}.

    Deliberately scoped to ONLY the hours the caller has already
    confirmed have NO existing row -- this sidesteps the harder question
    of whether a cached "full cascade" result is still valid for an
    UPGRADE comparison (which walks a FILTERED subset of the cascade,
    tiers strictly better than whatever's already stored -- the winning
    tier there can differ from a full-cascade walk in ways that would
    need careful reconciliation). Hours with an existing row stay
    entirely on the original serial path, untouched.

    Runs the exact same cascade-walk as the serial loop, just concurrently
    across different target_times -- no database access happens here at
    all, so there's no write-ordering or locking concern. The caller's
    existing serial loop still makes every skip/upgrade/write decision in
    its original order; this only speeds up the "get the raw fetched
    value" step by doing many of them at once instead of one at a time.

    IMPORTANT CAVEAT, untested here: cfgrib/eccodes (used inside
    extract_grib_fields, called via try_model_extraction) has no verified
    thread-safety guarantee, and this couldn't be tested against a real
    GRIB/eccodes install in the environment this was written in (no
    network, no eccodes available). Start with a small max_workers (3-4)
    and watch closely for garbled/inconsistent values or crashes before
    increasing it -- if you see corruption, this is the first thing to
    suspect, and max_workers=1 disables it entirely (falls through to the
    normal serial path with zero behavior change).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _walk_cascade(target_time):
        cascade = resolve_model_cascade(target_time, enable_rrfs=enable_rrfs,
                                        tiers=tiers, hrrr_max_lead=hrrr_max_lead)
        if not cascade:
            return target_time, (None, (None, None, None, None, None), [], None)

        chosen_model = None
        result = (None, None, None, None, None)
        models_tried = []
        last_exception = None
        for model_key, fxx in cascade:
            models_tried.append(model_key)
            try:
                result = try_model_extraction(target_time, model_key, fxx, lat, lon)
                if result[0] is not None:
                    chosen_model = model_key
                    break
            except Exception as e:
                last_exception = e
                continue
        return target_time, (chosen_model, result, models_tried, last_exception)

    cache = {}
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = [executor.submit(_walk_cascade, t) for t in target_times]
        for future in as_completed(futures):
            target_time, outcome = future.result()
            cache[target_time] = outcome
    except KeyboardInterrupt:
        # The default `with ThreadPoolExecutor(...)` context manager calls
        # shutdown(wait=True) on exit, which blocks joining EVERY submitted
        # future -- including ones still queued and not yet started -- and
        # produces an ugly "Exception ignored on threading shutdown"
        # traceback if Ctrl-C lands while that join is happening during
        # interpreter teardown. cancel_futures=True drops anything not yet
        # started immediately instead of waiting for the whole queue to
        # drain. Futures already mid-network-call can't be force-killed
        # (Python has no API to forcibly terminate a running thread), so
        # those still finish naturally -- this just stops piling up more
        # unnecessary waiting on top of that.
        print("\n  Interrupted -- cancelling queued (not-yet-started) "
             "fetches. Already-running ones will finish naturally "
             "(Python can't force-kill a thread mid-network-call).")
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return cache


def fetch_models(conn, lat, lon, start_time, end_time, force=False,
                 extend_future=True, enable_rrfs=False, tiers=None,
                 hrrr_max_lead=HRRR_MAX_LEAD, max_workers=1):
    mode_str = ("FORCE (overwrite)" if force
                else "SKIP EXISTING (delta-only + upgrade)")
    print(f"\nFetching models for lat={lat}, lon={lon}")
    print(f"  Range: {start_time.strftime('%Y-%m-%d %H:%M')} -> "
          f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Mode: {mode_str}")
    print(f"  Cascade: URMA -> RTMA -> HRRR f00 -> HRRR f01-f05 (forecast)")
    if extend_future:
        print(f"  Future extension: ENABLED (extends {hrrr_max_lead}h into forecast)")
    if force:
        print(f"  Rolling check active: will prompt on delta > "
              f"{ROLLING_DELTA_THRESHOLD_F}F")

    cursor = conn.cursor()

    now = datetime.now(timezone.utc)
    current = start_time.replace(minute=0, second=0, microsecond=0)
    end_hour = end_time.replace(minute=0, second=0, microsecond=0)

    if extend_future:
        # Extend the window to cover the full HRRR forecast range, not just
        # the next hour. This ensures f01-f06 get fetched for multiple hours
        # into the future, not just a single next-hour slot.
        future_extension = (now.replace(minute=0, second=0, microsecond=0)
                           + timedelta(hours=hrrr_max_lead))
        if end_hour < future_extension:
            end_hour = future_extension
        if end_hour > future_extension:
            # But respect an explicit end_time that's already further out
            # than our max_lead calculation (edge case)
            pass
    else:
        if end_hour > now:
            end_hour = now.replace(minute=0, second=0, microsecond=0)

    timestamps = []
    t = current
    while t <= end_hour:
        timestamps.append(t)
        t += timedelta(hours=1)

    total = len(timestamps)
    print(f"  Total hours to check: {total}")

    processed = 0
    overwritten = 0
    skipped = 0
    failed = 0
    upgraded = 0
    tier_counts = {}
    aborted = False

    upgrade_deltas = []

    for i, target_time in enumerate(timestamps):
        if aborted:
            break

        target_iso = target_time.isoformat()
        cascade = resolve_model_cascade(target_time, enable_rrfs=enable_rrfs,
                                        tiers=tiers, hrrr_max_lead=hrrr_max_lead)

        if not cascade:
            skipped += 1
            continue

        cursor.execute(
            """SELECT model FROM model_analysis
               WHERE valid_time = ? AND latitude = ? AND longitude = ?""",
            (target_iso, lat, lon),
        )
        existing_rows = cursor.fetchall()
        was_existing = len(existing_rows) > 0
        existing_model = existing_rows[0][0] if existing_rows else None
        existing_tier = (MODEL_TIER.get(existing_model, -1)
                         if existing_model else -1)

        if not force:
            if was_existing:
                best_available_tier = max(
                    MODEL_TIER.get(mk, -1) for mk, _ in cascade
                )

                if existing_tier >= best_available_tier:
                    skipped += 1
                    continue

                upgrade_candidates = [
                    (mk, fx) for mk, fx in cascade
                    if MODEL_TIER.get(mk, -1) > existing_tier
                ]

                if not upgrade_candidates:
                    skipped += 1
                    continue

                print(f"  Upgrade attempt: {existing_model} -> better tier "
                      f"for {target_iso}")

                cursor.execute(
                    """SELECT temp_f FROM model_analysis
                       WHERE valid_time = ? AND latitude = ? AND
                             longitude = ?""",
                    (target_iso, lat, lon),
                )
                old_row = cursor.fetchone()
                old_temp = old_row[0] if old_row else None

                temp_f = temp_sd = cloud_cover = wind_speed = precip = None
                chosen_model = None

                for model_key, fxx in upgrade_candidates:
                    try:
                        result = try_model_extraction(
                            target_time, model_key, fxx, lat, lon)
                        (temp_f, temp_sd, cloud_cover,
                         wind_speed, precip) = result

                        if temp_f is not None:
                            chosen_model = model_key
                            break
                    except Exception as e:
                        print(f"  {model_key} upgrade failed for "
                              f"{target_iso}: {e}")
                        continue

                if temp_f is not None and chosen_model is not None:
                    with conn:
                        conn.execute(
                            """DELETE FROM model_analysis
                               WHERE valid_time = ? AND latitude = ? AND
                                     longitude = ?""",
                            (target_iso, lat, lon),
                        )
                        conn.execute(
                            """INSERT OR REPLACE INTO model_analysis
                               (model, valid_time, latitude, longitude,
                                temp_f, temp_sd,
                                cloud_cover, wind_speed, precip)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (chosen_model, target_iso, lat, lon,
                             temp_f, temp_sd, cloud_cover, wind_speed,
                             precip),
                        )

                    upgraded += 1
                    processed += 1
                    tier_counts[chosen_model] = \
                        tier_counts.get(chosen_model, 0) + 1

                    if old_temp is not None:
                        delta = temp_f - old_temp
                        sign = "+" if delta > 0 else ""
                        print(f"  UPGRADED: {existing_model} -> "
                              f"{chosen_model} for {target_iso}")
                        print(f"     {old_temp:.1f}F -> {temp_f:.1f}F  "
                              f"(delta {sign}{delta:.2f}F)")
                        upgrade_deltas.append(
                            (target_iso, existing_model, chosen_model,
                             old_temp, temp_f, delta))
                    else:
                        print(f"  UPGRADED: {existing_model} -> "
                              f"{chosen_model} for {target_iso}")
                    continue
                else:
                    skipped += 1
                    continue

        temp_f = temp_sd = cloud_cover = wind_speed = precip = None
        chosen_model = None
        models_tried = []
        last_exception = None

        for model_key, fxx in cascade:
            models_tried.append(model_key)
            try:
                result = try_model_extraction(
                    target_time, model_key, fxx, lat, lon)
                (temp_f, temp_sd, cloud_cover,
                 wind_speed, precip) = result

                if temp_f is not None:
                    chosen_model = model_key
                    break
            except Exception as e:
                last_exception = e
                err_str = str(e).lower()
                if len(cascade) > 1:
                    if ("not found" not in err_str
                            and "could not" not in err_str):
                        print(f"  {model_key} failed for {target_iso}: {e}")
                else:
                    print(f"  {model_key} failed for {target_iso}: {e}")
                continue

        if temp_f is not None and chosen_model is not None:
            if force:
                cursor.execute(
                    """SELECT temp_f FROM model_analysis
                       WHERE latitude = ? AND longitude = ?
                         AND valid_time < ?
                       ORDER BY valid_time DESC LIMIT 1""",
                    (lat, lon, target_iso),
                )
                prev_row = cursor.fetchone()
                if prev_row and prev_row[0] is not None:
                    prev_temp = prev_row[0]
                    delta = abs(temp_f - prev_temp)
                    if delta > ROLLING_DELTA_THRESHOLD_F:
                        print(f"\n  ROLLING CHECK TRIGGERED for "
                              f"{target_iso} "
                              f"({chosen_model})")
                        print(f"     Previous stored temp: {prev_temp:.1f}F")
                        print(f"     New extracted temp:   {temp_f:.1f}F")
                        print(f"     Delta: {delta:.1f}F (threshold: "
                              f"{ROLLING_DELTA_THRESHOLD_F}F)")

                        choice = input("     [A]ccept / [R]eject (skip) / "
                                       "[S]top entirely? > "
                                       ).strip().lower()
                        if choice == "a":
                            print(f"     -> Accepted.\n")
                        elif choice == "s":
                            print(f"     -> ABORT. Stopping.\n")
                            aborted = True
                            break
                        else:
                            print(f"     -> Rejected. Skipping "
                                  f"{target_iso}.\n")
                            failed += 1
                            continue

            old_temp = None
            if force and was_existing:
                cursor.execute(
                    """SELECT temp_f FROM model_analysis
                       WHERE valid_time = ? AND latitude = ? AND
                             longitude = ?""",
                    (target_iso, lat, lon),
                )
                old_row = cursor.fetchone()
                old_temp = old_row[0] if old_row else None

            with conn:
                if force and was_existing:
                    conn.execute(
                        """DELETE FROM model_analysis
                           WHERE valid_time = ? AND latitude = ? AND
                                 longitude = ?""",
                        (target_iso, lat, lon),
                    )
                conn.execute(
                    """INSERT OR REPLACE INTO model_analysis
                       (model, valid_time, latitude, longitude, temp_f,
                        temp_sd,
                        cloud_cover, wind_speed, precip)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (chosen_model, target_iso, lat, lon,
                     temp_f, temp_sd, cloud_cover, wind_speed, precip),
                )

            if force and was_existing:
                overwritten += 1
                if old_temp is not None:
                    delta = temp_f - old_temp
                    sign = "+" if delta > 0 else ""
                    print(f"  {target_iso} ({chosen_model}): "
                          f"{old_temp:.1f}F -> {temp_f:.1f}F  "
                          f"(delta {sign}{delta:.2f}F)")
            processed += 1
            tier_counts[chosen_model] = \
                tier_counts.get(chosen_model, 0) + 1

            if processed % 24 == 0:
                remaining = total - i - 1
                tier_summary = ", ".join(
                    f"{k}:{v}" for k, v in sorted(tier_counts.items()))
                print(f"  Progress: {processed} fetched ({overwritten} "
                      f"overwritten, "
                      f"{upgraded} upgraded) [{tier_summary}] "
                      f"{skipped} cached, {failed} failed ({remaining}h "
                      f"remaining)")
        else:
            failed += 1
            if not was_existing:
                tried_str = " -> ".join(models_tried)
                if last_exception is not None:
                    print(f"  All tiers failed for {target_iso} (tried: "
                          f"{tried_str}) -- last error: {last_exception}")
                else:
                    print(f"  All tiers failed for {target_iso} (tried: "
                          f"{tried_str})")

    if aborted:
        print(f"\n  Stopped early by user.")

    tier_summary = ", ".join(
        f"{k}:{v}" for k, v in sorted(tier_counts.items()))
    print(f"\n  Model fetch complete: {processed} fetched ({overwritten} "
          f"overwritten, "
          f"{upgraded} upgraded) [{tier_summary}] "
          f"{skipped} cached, {failed} failed")

    return upgrade_deltas


def print_delta_summary(deltas, title):
    """Shared °F-change aggregate summary for a list of
    (valid_time, old_model, new_model, old_temp, new_temp, delta) tuples --
    used by fetch_models()'s upgrade deltas, backfill_anomalies()'s repair
    deltas, and any combined view a caller wants to build from both.
    Printing lives here (called explicitly by each entrypoint script)
    rather than inline inside fetch_models/backfill_anomalies themselves,
    so scripts that combine multiple delta sources (like collect_urma.py's
    fetch_models + backfill_anomalies) aren't stuck seeing the same
    numbers printed twice.
    """
    if not deltas:
        return
    print(f"\n  --- {title} ({len(deltas)}) ---")
    max_entry = max(deltas, key=lambda x: abs(x[5]))
    min_entry = min(deltas, key=lambda x: abs(x[5]))
    avg_abs_delta = sum(abs(d[5]) for d in deltas) / len(deltas)
    print(f"  Largest shift:  {max_entry[1]} -> {max_entry[2]} at "
          f"{max_entry[0]}: {max_entry[3]:.1f}F -> {max_entry[4]:.1f}F "
          f"(delta {max_entry[5]:+.2f}F)")
    print(f"  Smallest shift: {min_entry[1]} -> {min_entry[2]} at "
          f"{min_entry[0]}: {min_entry[3]:.1f}F -> {min_entry[4]:.1f}F "
          f"(delta {min_entry[5]:+.2f}F)")
    print(f"  Avg |delta|:    {avg_abs_delta:.2f}F")
    print(f"  ----------------------------------------")


# ---------------------------------------------------------------------------
# REVERSE BACKFILL (newest → oldest)
# ---------------------------------------------------------------------------

def fetch_models_reverse(conn, lat, lon, until_date, force=False,
                          enable_rrfs=False, tiers=None,
                          hrrr_max_lead=HRRR_MAX_LEAD, max_workers=1):
    """Backfill model data by working BACKWARDS from now.

    For each hour from now → until_date (reversed), check if a row already
    exists in the DB. If yes, skip. If no, attempt the full model cascade
    (URMA → RTMA → HRRR). This prioritizes the most recent missing data
    first, so if the process is interrupted, the freshest gaps are filled.

    Also attempts to UPGRADE existing lower-tier rows to better tiers,
    same as the normal fetch_models() upgrade path.

    Args:
        conn: SQLite connection
        lat, lon: target coordinates
        until_date: UTC datetime — stop when reaching this date
        force: if True, overwrite existing rows (re-extract everything)
    """
    mode_str = ("FORCE (overwrite all)" if force
                else "SKIP EXISTING + UPGRADE")
    print(f"\n{'=' * 60}")
    print(f"REVERSE BACKFILL: model data from NOW backwards to "
          f"{until_date.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Target: lat={lat}, lon={lon}")
    print(f"  Mode: {mode_str}")
    print(f"  Priority: newest missing hours first")
    print(f"  Cascade: URMA -> RTMA -> HRRR f00 -> HRRR f01-f05")
    print(f"{'=' * 60}")

    cursor = conn.cursor()

    # --- Check what we already have ---
    cursor.execute(
        """SELECT MIN(valid_time), MAX(valid_time), COUNT(*)
           FROM model_analysis
           WHERE latitude = ? AND longitude = ?""",
        (lat, lon),
    )
    row = cursor.fetchone()
    db_oldest = row[0] if row[0] else None
    db_newest = row[1] if row[1] else None
    db_count = row[2] if row[2] else 0

    if db_count > 0:
        print(f"\n  DB inventory: {db_count} rows")
        print(f"    Oldest: {db_oldest}")
        print(f"    Newest: {db_newest}")
    else:
        print(f"\n  DB inventory: empty — will backfill everything")

    # --- Build the hour list (now → until_date, reversed) ---
    now = datetime.now(timezone.utc)
    end_hour = now.replace(minute=0, second=0, microsecond=0)
    floor_hour = until_date.replace(minute=0, second=0, microsecond=0)

    # Don't try to fetch the current hour (analysis models haven't
    # published yet). Start from one hour ago.
    end_hour = end_hour - timedelta(hours=1)

    timestamps = []
    t = end_hour
    while t >= floor_hour:
        timestamps.append(t)
        t -= timedelta(hours=1)

    total = len(timestamps)
    print(f"\n  Hours to check: {total}")
    print(f"    From: {end_hour.strftime('%Y-%m-%d %H:%M')}Z")
    print(f"    To:   {floor_hour.strftime('%Y-%m-%d %H:%M')}Z")

    # --- Bulk-load existing hours for fast skip checks ---
    existing_hours = set()
    cursor.execute(
        """SELECT valid_time, model FROM model_analysis
           WHERE latitude = ? AND longitude = ?""",
        (lat, lon),
    )
    existing_model_map = {}
    for vt, model_name in cursor.fetchall():
        existing_hours.add(vt)
        existing_model_map[vt] = model_name

    if not force:
        missing = [t for t in timestamps if t.isoformat() not in existing_hours]
        print(f"  Already in DB: {total - len(missing)}")
        print(f"  Missing (to fetch): {len(missing)}")
    else:
        missing = timestamps
        print(f"  FORCE mode: will re-fetch all {total} hours")

    # `missing` is exactly the right set to pre-fetch in parallel: every
    # hour in it goes through the FULL-cascade "fresh fetch" code path
    # below (force mode never enters the upgrade-comparison branch, which
    # walks a FILTERED subset of the cascade and stays on the untouched
    # serial path -- see the comment on _prefetch_cascades_parallel for
    # why that path specifically isn't included here). Pre-fetching
    # doesn't touch the database or change processing order at all: the
    # main loop below still walks `timestamps` in the exact same order,
    # applies the same consecutive_failures/boundary-detection logic in
    # that same order, and just checks this cache before doing its own
    # network fetch instead of always doing one.
    prefetch_cache = {}
    if max_workers > 1 and missing:
        print(f"\n  Pre-fetching {len(missing)} hour(s) in parallel "
             f"(max_workers={max_workers})...")
        prefetch_cache = _prefetch_cascades_parallel(
            missing, enable_rrfs, tiers, hrrr_max_lead, lat, lon, max_workers)
        print(f"  Pre-fetch complete.\n")

    print()

    processed = 0
    skipped = 0
    failed = 0
    upgraded = 0
    consecutive_failures = 0
    tier_counts = {}
    last_progress_time = datetime.now()
    last_processed_time = end_hour

    for i, target_time in enumerate(timestamps):
        target_iso = target_time.isoformat()
        last_processed_time = target_time

        cascade = resolve_model_cascade(target_time, enable_rrfs=enable_rrfs,
                                        tiers=tiers, hrrr_max_lead=hrrr_max_lead)
        if not cascade:
            skipped += 1
            continue

        was_existing = target_iso in existing_hours
        existing_model = existing_model_map.get(target_iso)
        existing_tier = (MODEL_TIER.get(existing_model, -1)
                         if existing_model else -1)

        # --- Upgrade path (skip mode only) ---
        if not force and was_existing:
            best_available_tier = max(
                MODEL_TIER.get(mk, -1) for mk, _ in cascade
            )

            if existing_tier >= best_available_tier:
                skipped += 1
                consecutive_failures = 0
                continue

            upgrade_candidates = [
                (mk, fx) for mk, fx in cascade
                if MODEL_TIER.get(mk, -1) > existing_tier
            ]

            if not upgrade_candidates:
                skipped += 1
                continue

            # Try to upgrade
            temp_f = temp_sd = cloud_cover = wind_speed = precip = None
            chosen_model = None

            for model_key, fxx in upgrade_candidates:
                try:
                    result = try_model_extraction(
                        target_time, model_key, fxx, lat, lon)
                    (temp_f, temp_sd, cloud_cover,
                     wind_speed, precip) = result
                    if temp_f is not None:
                        chosen_model = model_key
                        break
                except Exception as e:
                    err_str = str(e).lower()
                    if ("not found" not in err_str
                            and "could not" not in err_str):
                        print(f"  {model_key} upgrade failed for "
                              f"{target_iso}: {e}")
                    continue

            if temp_f is not None and chosen_model is not None:
                with conn:
                    conn.execute(
                        """DELETE FROM model_analysis
                           WHERE valid_time = ? AND latitude = ? AND
                                 longitude = ?""",
                        (target_iso, lat, lon),
                    )
                    conn.execute(
                        """INSERT OR REPLACE INTO model_analysis
                           (model, valid_time, latitude, longitude,
                            temp_f, temp_sd,
                            cloud_cover, wind_speed, precip)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (chosen_model, target_iso, lat, lon,
                         temp_f, temp_sd, cloud_cover, wind_speed,
                         precip),
                    )
                upgraded += 1
                processed += 1
                consecutive_failures = 0
                tier_counts[chosen_model] = \
                    tier_counts.get(chosen_model, 0) + 1
                existing_model_map[target_iso] = chosen_model
                existing_hours.add(target_iso)

                if processed % 24 == 0:
                    _reverse_progress(processed, skipped, failed,
                                      upgraded, tier_counts, target_time,
                                      len(missing) if not force else total,
                                      last_progress_time)
                    last_progress_time = datetime.now()
            else:
                skipped += 1
            continue

        # --- Fresh fetch (missing hour or force mode) ---
        temp_f = temp_sd = cloud_cover = wind_speed = precip = None
        chosen_model = None
        models_tried = []
        last_exception = None

        if target_time in prefetch_cache:
            chosen_model, result, models_tried, last_exception = prefetch_cache[target_time]
            temp_f, temp_sd, cloud_cover, wind_speed, precip = result
        else:
            for model_key, fxx in cascade:
                models_tried.append(model_key)
                try:
                    result = try_model_extraction(
                        target_time, model_key, fxx, lat, lon)
                    (temp_f, temp_sd, cloud_cover,
                     wind_speed, precip) = result
                    if temp_f is not None:
                        chosen_model = model_key
                        break
                except Exception as e:
                    last_exception = e
                    err_str = str(e).lower()
                    if len(cascade) > 1:
                        if ("not found" not in err_str
                                and "could not" not in err_str):
                            print(f"  {model_key} failed for {target_iso}: {e}")
                    else:
                        print(f"  {model_key} failed for {target_iso}: {e}")
                    continue

        if temp_f is not None and chosen_model is not None:
            with conn:
                if force and was_existing:
                    conn.execute(
                        """DELETE FROM model_analysis
                           WHERE valid_time = ? AND latitude = ? AND
                                 longitude = ?""",
                        (target_iso, lat, lon),
                    )
                conn.execute(
                    """INSERT OR REPLACE INTO model_analysis
                       (model, valid_time, latitude, longitude, temp_f,
                        temp_sd,
                        cloud_cover, wind_speed, precip)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (chosen_model, target_iso, lat, lon,
                     temp_f, temp_sd, cloud_cover, wind_speed, precip),
                )
            processed += 1
            consecutive_failures = 0
            tier_counts[chosen_model] = \
                tier_counts.get(chosen_model, 0) + 1
            existing_model_map[target_iso] = chosen_model
            existing_hours.add(target_iso)

            if processed % 24 == 0:
                _reverse_progress(processed, skipped, failed,
                                  upgraded, tier_counts, target_time,
                                  len(missing) if not force else total,
                                  last_progress_time)
                last_progress_time = datetime.now()
        else:
            failed += 1
            consecutive_failures += 1

            if consecutive_failures == REVERSE_BACKFILL_WARN_THRESHOLD:
                print(f"\n  ⚠️  {REVERSE_BACKFILL_WARN_THRESHOLD} consecutive "
                      f"failures at {target_time.strftime('%Y-%m-%d %H:%M')}Z "
                      f"— may be approaching archive boundary\n")
            elif consecutive_failures == REVERSE_BACKFILL_MAX_CONSECUTIVE_FAILURES:
                print(f"\n  ⚠️  {REVERSE_BACKFILL_MAX_CONSECUTIVE_FAILURES} "
                      f"consecutive failures — likely past archive boundary. "
                      f"Stopping.\n")
                break

            if not was_existing and len(models_tried) > 1:
                tried_str = " -> ".join(models_tried)
                if last_exception is not None:
                    print(f"  All tiers failed for {target_iso} "
                          f"(tried: {tried_str}) -- last error: "
                          f"{last_exception}")
                else:
                    print(f"  All tiers failed for {target_iso} "
                          f"(tried: {tried_str})")

    # --- Final summary ---
    tier_summary = ", ".join(
        f"{k}:{v}" for k, v in sorted(tier_counts.items()))
    print(f"\n{'=' * 60}")
    print(f"  Reverse backfill complete:")
    print(f"    Fetched:  {processed} ({upgraded} upgraded)")
    print(f"    Skipped:  {skipped} (already in DB or no cascade)")
    print(f"    Failed:   {failed}")
    if tier_summary:
        print(f"    Tiers:    [{tier_summary}]")
    print(f"    Range:    {end_hour.strftime('%m-%d %H:%M')}Z → "
          f"{last_processed_time.strftime('%m-%d %H:%M')}Z")
    print(f"{'=' * 60}\n")


def _reverse_progress(processed, skipped, failed, upgraded, tier_counts,
                      target_time, total_missing, last_time):
    """Print a progress checkpoint for reverse backfill."""
    tier_summary = ", ".join(
        f"{k}:{v}" for k, v in sorted(tier_counts.items()))
    elapsed = (datetime.now() - last_time).total_seconds()
    remaining = total_missing - processed
    print(f"  [{processed}/{total_missing}] fetched | "
          f"Current: {target_time.strftime('%m-%d %H:%M')}Z | "
          f"[{tier_summary}] | "
          f"{skipped} cached, {failed} failed, {upgraded} upgraded | "
          f"{remaining}h remaining "
          f"({elapsed:.0f}s since last checkpoint)")


# ---------------------------------------------------------------------------
# ANOMALY DETECTION & BACKFILL
# ---------------------------------------------------------------------------

def backfill_anomalies(conn, lat, lon, enable_rrfs=False, tiers=None,
                       hrrr_max_lead=HRRR_MAX_LEAD):
    print(f"\n{'=' * 60}")
    print(f"Scanning for temperature anomalies (threshold: "
          f"+/-{ANOMALY_THRESHOLD_F}F)...")
    print(f"{'=' * 60}")

    cursor = conn.cursor()
    cursor.execute(
        """SELECT model, valid_time, temp_f FROM model_analysis
           WHERE latitude = ? AND longitude = ?
           ORDER BY valid_time ASC""",
        (lat, lon),
    )
    rows = cursor.fetchall()

    if len(rows) < 3:
        print("  Not enough data points for anomaly detection (need at "
              "least 3). Skipping.")
        return

    time_map = {}
    for model, valid_time, temp_f in rows:
        if temp_f is not None:
            time_map[valid_time] = (model, temp_f)

    sorted_times = sorted(time_map.keys())
    flagged = []

    for i in range(1, len(sorted_times) - 1):
        current_time = sorted_times[i]
        current_model, current_temp = time_map[current_time]

        prev_time = sorted_times[i - 1]
        prev_dt = pd.to_datetime(prev_time)
        curr_dt = pd.to_datetime(current_time)
        gap_before = (curr_dt - prev_dt).total_seconds() / 3600.0

        next_time = sorted_times[i + 1]
        next_dt = pd.to_datetime(next_time)
        gap_after = (next_dt - curr_dt).total_seconds() / 3600.0

        if gap_before > 1.5 or gap_after > 1.5:
            continue

        prev_temp = time_map[prev_time][1]
        next_temp = time_map[next_time][1]

        delta_before = abs(current_temp - prev_temp)
        delta_after = abs(current_temp - next_temp)

        if (delta_before > ANOMALY_THRESHOLD_F
                and delta_after > ANOMALY_THRESHOLD_F):
            flagged.append((current_time, current_model, current_temp,
                            delta_before, delta_after))
            print(f"  ANOMALY: {current_time} ({current_model})")
            print(f"     Temp: {current_temp:.1f}F | d-1hr: "
                  f"{delta_before:.1f}F | d+1hr: {delta_after:.1f}F")

    if not flagged:
        print("  No anomalies detected. All model points look consistent.")
        return

    print(f"\n  Found {len(flagged)} anomalous point(s). Re-downloading via "
          f"cascade...\n")

    replaced = 0
    anomaly_deltas = []

    for valid_time, model_name, old_temp, _, _ in flagged:
        target_dt = pd.to_datetime(valid_time, utc=True)
        cascade = resolve_model_cascade(target_dt, enable_rrfs=enable_rrfs,
                                        tiers=tiers, hrrr_max_lead=hrrr_max_lead)

        if not cascade:
            print(f"  No cascade available for {valid_time}. Keeping "
                  f"original.")
            continue

        new_temp = new_sd = new_cloud = new_wind = new_precip = None
        model_used = None

        for model_key, fxx in cascade:
            try:
                result = try_model_extraction(
                    target_dt, model_key, fxx, lat, lon)
                (new_temp, new_sd, new_cloud,
                 new_wind, new_precip) = result

                if new_temp is not None:
                    model_used = model_key
                    break
            except Exception as e:
                print(f"  {model_key} re-download failed for "
                      f"{valid_time}: {e}")
                continue

        if new_temp is not None and model_used is not None:
            with conn:
                conn.execute(
                    """DELETE FROM model_analysis
                       WHERE valid_time = ? AND latitude = ? AND
                             longitude = ?""",
                    (valid_time, lat, lon),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO model_analysis
                       (model, valid_time, latitude, longitude, temp_f,
                        temp_sd,
                        cloud_cover, wind_speed, precip)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (model_used, valid_time, lat, lon,
                     new_temp, new_sd, new_cloud, new_wind, new_precip),
                )
            delta = new_temp - old_temp
            sign = "+" if delta > 0 else ""
            print(f"  REPLACED {valid_time}: {old_temp:.1f}F -> "
                  f"{new_temp:.1f}F "
                  f"(delta {sign}{delta:.2f}F) [{model_used}]")
            replaced += 1
            anomaly_deltas.append(
                (valid_time, model_name, model_used, old_temp, new_temp,
                 delta))
        else:
            print(f"  Re-download returned no temperature for "
                  f"{valid_time}. Keeping original.")

    print(f"\n  Anomaly backfill complete. Replaced {replaced} point(s).")

    if anomaly_deltas:
        print(f"\n  --- Anomaly Repair Delta Summary ({len(anomaly_deltas)} "
              f"replacements) ---")
        max_entry = max(anomaly_deltas, key=lambda x: abs(x[5]))
        min_entry = min(anomaly_deltas, key=lambda x: abs(x[5]))
        avg_abs_delta = (sum(abs(d[5]) for d in anomaly_deltas)
                         / len(anomaly_deltas))
        print(f"  Largest shift:  {max_entry[1]} -> {max_entry[2]} at "
              f"{max_entry[0]}: {max_entry[3]:.1f}F -> {max_entry[4]:.1f}F "
              f"(delta {max_entry[5]:+.2f}F)")
        print(f"  Smallest shift: {min_entry[1]} -> {min_entry[2]} at "
              f"{min_entry[0]}: {min_entry[3]:.1f}F -> {min_entry[4]:.1f}F "
              f"(delta {min_entry[5]:+.2f}F)")
        print(f"  Avg |delta|:    {avg_abs_delta:.2f}F")
        print(f"  ----------------------------------------")

    return anomaly_deltas

