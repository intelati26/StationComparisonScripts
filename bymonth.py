#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bymonth.py — Unified monthly weather calendar with table and visual formats

Features:
- --calendar : Detailed calendar table (original bymonth.py behavior)
- --viz     : Compact visual calendar (original bymonth_viz.py behavior)
- --both    : Both formats side-by-side (HTML only)

Auto-adapts to the on-disk layout:
  • wide:      one column per weather field (station, timestamp, temp, ...)
  • packed:    rows with a JSON/zlib payload column, e.g.
               observations(mac_address, dateutc, data_json)   ← your layout
  • whole-file gzip/zlib compression is unpacked to a temp file first.
Timestamps may be ISO text, epoch seconds, or epoch milliseconds (probed.
Dewpoint conversion is OPT-IN (--dewpoint-c): Ambient payloads are already °F.
Database is always opened read-only.

Usage:
    python bymonth.py                          # current month, calendar format
    python bymonth.py --calendar                 # explicit calendar format
    python bymonth.py --viz                      # current month, visual format  
    python bymonth.py --both                     # current month, both formats (HTML)
    python bymonth.py 2026-08                    # specific month, calendar format
    python bymonth.py --viz 2026-08              # specific month, visual format
    python bymonth.py --both 2026-08             # specific month, both formats (HTML)
    python bymonth.py --calendar --viz           # both formats (alternative syntax)
    python bymonth.py --station "G6964"          # by device name or MAC
    python bymonth.py 2026-08 --format text --out aug.txt
"""

from __future__ import annotations

import argparse
import atexit
import calendar
import gzip
import html
import json
import math
import os
import sqlite3
import sys
import tempfile
import webbrowser
import zlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    # Explicit script-relative path, not bare load_dotenv()'s frame-based
    # auto-detection -- same "resolve against THIS SCRIPT'S directory, not
    # the shell's cwd (or whatever invoked us)" fix as WEATHER_DB_PATH
    # below, applied to .env discovery itself so a WEATHER_LAT/WEATHER_LON/
    # etc. that's correctly set in .env next to this script can't silently
    # go unfound just because of how/where it was launched from.
    load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))
except ImportError:
    print("note: python-dotenv not installed; any .env file is ignored -- "
          "only real environment variables are used. "
          "Install with: pip install python-dotenv", file=sys.stderr)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_db_env = os.environ.get("WEATHER_DB_PATH", "weather_archive.db")
# Relative WEATHER_DB_PATH (including the bare default) resolves against
# THIS SCRIPT'S directory, not the shell's current working directory --
# matches tempAnalysis.py/plotter.py's identical fix for the same
# "sqlite3.connect() silently creates an empty file at the wrong cwd-
# relative path" failure mode.
DEFAULT_DB = (_db_env if os.path.isabs(_db_env)
             else os.path.join(_SCRIPT_DIR, _db_env))
DEFAULT_TZ = "America/Chicago"        # day boundaries in local time

# Stations whose STORED dewpoint is °C and needs conversion. Empty by
# default is now correct for BOTH sources: Ambient payloads (dewPoint) are
# already °F, and fetch_nws_station() in weather_common.py was recently
# fixed to convert NWS's Celsius dewpoint to Fahrenheit before storage (it
# previously stored it raw/unconverted -- KSGF/KBBG rows fetched before
# that fix will still have bad Celsius values sitting in the dewpoint
# column until re-fetched with --force). If you're looking at a month with
# implausible dewpoint numbers for a station, that stale-data window is
# the first thing to check -- NOT a reason to add that station here.
DEWPOINT_IN_C = set()

STATION_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]

# Lines rendered inside each day cell, per station:
CELL_LINES = [
    ("Tmax",   [("temperature",              "max",    "°",     0, "")]),
    ("Tavg",   [("temperature",              "avg",    "°",     0, "")]),
    ("Tmin",   [("temperature",              "min",    "°",     0, "")]),
    ("dew",    [("dewpoint",                 "minmax", "°",     0, "")]),
    ("feels",  [("feels_like",               "minmax", "°",     0, "")]),
    ("RH",     [("relative_humidity",        "avg",    "%",     0, "")]),
    ("wind",   [("wind_speed",               "avg",    "mph",   0, ""),
                 ("wind_dir",                 "vdir",   "",      0, "")]),
    ("gust",   [("wind_gust|max_daily_gust", "max",    "mph",   0, "")]),
    ("rain",   [("rain_daily",               "max",    '"',     2, "")]),
    ("rain/h", [("rain_hourly",              "max",    '"',     2, "")]),
    ("pres",   [("pressure_rel",             "minmax", "in",    2, "")]),
    ("UV",     [("uv_index",                 "max",    "",      0, "")]),
    ("solar",  [("solar_radiation",          "max",    "W/m²",  0, "")]),
    ("CO₂",    [("co2",                      "max",    "ppm",   0, "")]),
    ("PM2.5",  [("pm25",                     "avg",    "µg/m³", 1, "")]),
    ("⚡",     [("lightning_day",            "max",    "",      0, ""),
                 ("lightning_distance",       "min",    "mi",    0, "@ ")]),
    ("in",     [("temp_indoor",              "avg",    "°",     0, "")]),
]

# Fields aggregated per day. Running counters (rain_weekly/monthly/yearly/
# event/total), 2m/10m averages, gust direction and last_rain excluded on purpose.
OBS_COLUMNS = [
    "temperature", "dewpoint", "feels_like", "relative_humidity",
    "wind_speed", "wind_gust", "max_daily_gust", "wind_dir",
    "pressure_rel", "pressure_abs",
    "rain_daily", "rain_hourly", "rain_24h",
    "uv_index", "solar_radiation", "co2", "pm25", "pm25_24h",
    "lightning_day", "lightning_distance", "temp_indoor",
]

_CANON_FIELDS = [
    "temperature", "dewpoint", "feels_like", "temp_indoor",
    "relative_humidity", "humidity_indoor",
    "wind_speed", "wind_gust", "max_daily_gust", "wind_dir", "wind_gust_dir",
    "wind_speed_avg2m", "wind_dir_avg2m", "wind_speed_avg10m", "wind_dir_avg10m",
    "pressure_rel", "pressure_abs",
    "rain_hourly", "rain_daily", "rain_24h", "rain_weekly",
    "rain_monthly", "rain_yearly", "rain_event", "rain_total",
    "uv_index", "solar_radiation", "co2", "pm25", "pm25_24h",
    "lightning_day", "lightning_hour", "lightning_distance", "last_rain",
]


def _norm(key):
    return str(key).strip().lower().replace("_", "").replace("-", "").replace(" ", "")


KEYMAP = {_norm(f): f for f in _CANON_FIELDS}
# Ambient Weather API-style JSON keys → canonical names
for _alias, _canon in {
    "tempf": "temperature", "dewpoint": "dewpoint", "feelslike": "feels_like",
    "tempinf": "temp_indoor",
    "humidity": "relative_humidity", "humidityin": "humidity_indoor",
    "windspeedmph": "wind_speed", "windgustmph": "wind_gust",
    "maxdailygust": "max_daily_gust",
    "winddir": "wind_dir", "windgustdir": "wind_gust_dir",
    "windspeedavg2m": "wind_speed_avg2m", "winddiravg2m": "wind_dir_avg2m",
    "windspeedavg10m": "wind_speed_avg10m", "winddiravg10m": "wind_dir_avg10m",
    "baromrelin": "pressure_rel", "baromabsin": "pressure_abs",
    "hourlyrainin": "rain_hourly", "eventrainin": "rain_event",
    "dailyrainin": "rain_daily", "weeklyrainin": "rain_weekly",
    "monthlyrainin": "rain_monthly", "yearlyrainin": "rain_yearly",
    "totalrainin": "rain_total",
    "uv": "uv_index", "solarradiation": "solar_radiation",
    "lightningstrikecountday": "lightning_day", "lightningday": "lightning_day",
    "lightenstrikecounthour": "lightning_hour", "lightninghour": "lightning_hour",
    "lightningdistance": "lightning_distance", "lightningdistancemi": "lightning_distance",
    "lastrain": "last_rain",
}.items():
    KEYMAP[_alias] = _canon

OBS_COLUMNS_SET = set(OBS_COLUMNS)

CSS = """
 body { font-family: -apple-system, 'Segoe UI', Arial, sans-serif; margin: 20px; color: #1c1c1e; }
 h1 { font-family: Georgia, 'Times New Roman', serif; font-size: 30px; margin: 0; }
 .sub { color: #666; font-size: 12.5px; margin: 4px 0 12px; }
 table.cal { border-collapse: collapse; width: 100%; table-layout: fixed; }
 table.cal th { border: 1px solid #b9b9c2; background: #2c2c34; color: #fff;
                 font-size: 12px; letter-spacing: 1px; padding: 4px; }
 table.cal td { border: 1px solid #b9b9c2; vertical-align: top; }
 td.wk { width: 34px; text-align: center; font-size: 10px; color: #888; background: #f4f4f7; }
 td.day { padding: 3px 5px; }
 td.day.weekend { background-color: #eef0f6; }
 td.day.out { background: #fafafa; }
 td.day.out .dnum { color: #c4c4cc; font-weight: normal; }
 .dnum { font-family: Georgia, serif; font-size: 15px; font-weight: bold; }
 td.day { height: auto; min-height: 60px; }
 table.dmatrix { border-collapse: collapse; width: 100%; margin-top: 2px;
                 font-size: 9px; table-layout: fixed; }
 table.dmatrix th { font-weight: 700; font-size: 8.5px; padding: 1px 2px;
                     text-align: right; border-bottom: 1px solid #ddd; }
 table.dmatrix th:first-child { text-align: left; }
 table.dmatrix td { padding: 1px 2px; text-align: right; white-space: nowrap;
                     overflow: hidden; text-overflow: ellipsis; }
 table.dmatrix td.mlabel { text-align: left; color: #666; font-weight: 600; }
 table.dmatrix td.hl { font-weight: 800; color: #b8280d; background: #fff2ec; }
 table.foot { border-collapse: collapse; width: 100%; margin-top: 18px; font-size: 12.5px; }
 table.foot th, table.foot td { border: 1px solid #b9b9c2; padding: 3px 8px; text-align: right; }
 table.foot th { background: #f0f0f5; }
 table.foot td:first-child, table.foot th:first-child { text-align: left; font-weight: 600; }
 .gen { color: #999; font-size: 11px; margin-top: 10px; }
 @media print { body { margin: 0; } * { -webkit-print-color-adjust: exact; print-color-adjust: exact; } }
"""

# --------------------------------------------------------------------------- #
# Payload unpacking (text JSON, zlib bytes, raw JSON bytes)
# --------------------------------------------------------------------------- #

def unpack_observation_data(raw_data):
    """Transparently unpack JSON whether stored as text, zlib bytes, or raw bytes."""
    if not raw_data:
        return {}
    if isinstance(raw_data, str):
        try:
            return json.loads(raw_data)
        except Exception:
            return {}
    if isinstance(raw_data, bytes):
        try:
            return json.loads(zlib.decompress(raw_data).decode("utf-8"))
        except (zlib.error, UnicodeDecodeError):
            try:
                return json.loads(raw_data.decode("utf-8"))
            except Exception:
                return {}
    return {}


def payload_to_dicts(raw):
    """One payload cell → list of observation dicts (usually length 1)."""
    obj = unpack_observation_data(raw)
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    return []


def map_payload(obs):
    """Ambient/DB-style JSON keys → canonical column names, numeric-coerced."""
    rec = {}
    for k, v in obs.items():
        canon = KEYMAP.get(_norm(k))
        if canon is None or canon not in OBS_COLUMNS_SET:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            rec[canon] = float(v)
        elif isinstance(v, str):
            try:
                rec[canon] = float(v)
            except ValueError:
                pass
    return rec

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def parse_ts(raw):
    """ISO text, epoch seconds, epoch millis (int or numeric text) → aware UTC."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        if v > 1e11:                       # Ambient dateutc is epoch milliseconds
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:                                   # epoch stored as text
        v = float(s)
        if v > 1e11:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def get_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        print(f"warning: timezone '{name}' unavailable, falling back to UTC", file=sys.stderr)
        return timezone.utc


def fnum(v, nd=0):
    if v is None:
        return ""
    if nd <= 0:
        return f"{v:.0f}"
    return f"{v:.{nd}f}".rstrip("0").rstrip(".")


def fmt_day(d):
    return d.strftime("%b ") + str(d.day)


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def circular_mean_deg(degrees):
    if not degrees:
        return None
    x = sum(math.cos(math.radians(a)) for a in degrees) / len(degrees)
    y = sum(math.sin(math.radians(a)) for a in degrees) / len(degrees)
    return None if (x == 0 and y == 0) else math.degrees(math.atan2(y, x)) % 360


def compass(deg):
    return COMPASS[int(round(deg / 22.5)) % 16]

# --------------------------------------------------------------------------- #
# Opening the database (whole-file gzip/zlib handled, always read-only)
# --------------------------------------------------------------------------- #

def _cleanup_tmp(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def open_database(path):
    """Return (conn, temp_path_or_None). Sniffs compression; opens read-only."""
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head[:15] == b"SQLite format 3":
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True), None

    with open(path, "rb") as fh:
        blob = fh.read()
    data, how = None, None
    if head[:2] == b"\x1f\x8b":
        try:
            data, how = gzip.decompress(blob), "gzip"
        except OSError:
            pass
    elif (head[0] & 0x0F) == 8 and (head[0] >> 4) <= 7:      # zlib CMF byte
        try:
            data, how = zlib.decompress(blob), "zlib"
        except zlib.error:
            pass
    if data is None or data[:15] != b"SQLite format 3":
        sys.exit(f"error: {path} is neither a SQLite database nor a "
                 f"gzip/zlib-compressed one (starts with {head[:8]!r})")

    fd, tmp = tempfile.mkstemp(prefix="weather_unpacked_", suffix=".db")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    atexit.register(_cleanup_tmp, tmp)
    print(f"note: {path} was {how}-compressed; unpacked to {tmp}")
    return sqlite3.connect(f"file:{tmp}?mode=ro", uri=True), tmp

# --------------------------------------------------------------------------- #
# Schema discovery
# --------------------------------------------------------------------------- #

TS_CANDIDATES = ("timestamp", "ts", "time", "datetime", "dateutc", "date_utc",
                 "obs_time", "date_time", "epoch", "epoch_ms", "time_utc", "date")
STATION_CANDIDATES = ("station", "station_id", "stn", "st",
                       "mac_address", "mac", "device_id", "device")
PAYLOAD_CANDIDATES = ("data", "data_json", "raw_data", "payload", "json",
                       "obs", "observation", "record", "raw", "blob", "values")


def list_tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def table_columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _pick(lower_map, candidates):
    for c in candidates:
        if c in lower_map:
            return lower_map[c]
    return None


def probe_ts_kind(conn, table, ts_col):
    """Decide how timestamps are stored: 'ms', 's', or 'text'."""
    try:
        lo_, hi_ = conn.execute(
            f'SELECT MIN("{ts_col}"), MAX("{ts_col}") FROM "{table}"').fetchone()
    except sqlite3.Error:
        return "text", (None, None)
    if (isinstance(lo_, (int, float)) and isinstance(hi_, (int, float))
            and not isinstance(lo_, bool)):
        return ("ms" if hi_ > 1e11 else "s"), (lo_, hi_)
    return "text", (None, None)


def discover_schema(conn, override_table=None, override_data_col=None):
    tables = list_tables(conn)
    tname = override_table
    if tname:
        if tname not in tables:
            sys.exit(f"error: table '{tname}' not found. Tables: {', '.join(tables) or '(none)'}")
    else:
        for cand in ("station_obs", "station_observations", "observations", "obs",
                     "station_data", "weather_obs"):
            if cand in tables:
                tname = cand
                break
        if tname is None:
            for t in tables:
                cols = {c.lower() for c in table_columns(conn, t)}
                if _pick({c: c for c in cols}, STATION_CANDIDATES) and \
                   _pick({c: c for c in cols}, TS_CANDIDATES):
                    tname = t
                    break
        if tname is None:
            return None

    cols = table_columns(conn, tname)
    lower = {c.lower(): c for c in cols}

    scol = _pick(lower, STATION_CANDIDATES)
    tcol = _pick(lower, TS_CANDIDATES)
    if not scol or not tcol:
        return None

    kind, ts_range = probe_ts_kind(conn, tname, tcol)

    wide = [lower[c] for c in OBS_COLUMNS if c in lower]
    if wide:
        return {"table": tname, "mode": "wide", "station_col": scol, "ts_col": tcol,
                "cols": wide, "data_col": None, "ts_kind": kind, "ts_range": ts_range}

    dcol = None
    if override_data_col:
        dcol = override_data_col if override_data_col in cols else None
    else:
        dcol = _pick(lower, PAYLOAD_CANDIDATES)
    if not dcol:                                    # substring fallback
        for c in cols:
            lc = c.lower()
            if lc == "info_json":                   # device metadata, not obs
                continue
            if "json" in lc or "payload" in lc or "blob" in lc or lc.endswith("data"):
                dcol = c
                break
    if not dcol:
        return None
    return {"table": tname, "mode": "payload", "station_col": scol, "ts_col": tcol,
            "data_col": dcol, "cols": [], "ts_kind": kind, "ts_range": ts_range}


def load_device_names(conn):
    """mac_address → friendly name from a devices table, if present."""
    try:
        if "devices" not in list_tables(conn):
            return {}
        cols = {c.lower(): c for c in table_columns(conn, "devices")}
        mac, name = cols.get("mac_address"), cols.get("name")
        if not (mac and name):
            return {}
        return {str(m): (str(n) if n else str(m))
                for m, n in conn.execute(f'SELECT "{mac}", "{name}" FROM "devices"')}
    except sqlite3.Error:
        return {}


def describe_db(conn):
    lines = []
    for t in list_tables(conn):
        try:
            n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error:
            n = "?"
        lines.append(f'  {t} ({n} rows): {", ".join(table_columns(conn, t)) or "(no columns)"}')
    return "\n".join(lines) if lines else "  (no tables — is this the right file?)"


def cmd_inspect(conn, info):
    print("== tables in database ==")
    print(describe_db(conn))
    dev = load_device_names(conn)
    if dev:
        print("\n== devices ==")
        for m, n in dev.items():
            print(f"   {m}  →  {n}")
    if not info:
        print("\nNo observation table detected. Try --table NAME / --data-col COL.")
        return
    print(f"\n== detected observation table: {info['table']} (mode: {info['mode']}) ==")
    print(f"   station column : {info['station_col']}")
    print(f"   time column    : {info['ts_col']}"
          + {"ms": "  (epoch milliseconds)", "s": "  (epoch seconds)",
             "text": ""}[info["ts_kind"]])
    lo_, hi_ = info["ts_range"]
    if lo_ is not None:
        print(f"   time range     : {parse_ts(lo_)} → {parse_ts(hi_)}")
    if info["mode"] == "payload":
        print(f"   payload column : {info['data_col']}")
    else:
        print(f"   field columns  : {', '.join(info['cols'])}")
    sel = f'"{info["station_col"]}", "{info["ts_col"]}"'
    if info["mode"] == "payload":
        sel += f', "{info["data_col"]}"'
    else:
        sel += f', "{info["cols"][0]}"'
    try:
        row = conn.execute(f'SELECT {sel} FROM "{info["table"]}" LIMIT 1').fetchone()
    except sqlite3.Error:
        row = None
    if row is None:
        print("   (table is empty)")
        return
    print(f"   sample station : {row[0]!r}  timestamp: {row[1]!r}")
    if info["mode"] == "payload":
        dicts = payload_to_dicts(row[2])
        if dicts:
            print(f"   sample unpacked keys: {', '.join(sorted(dicts[0]))}")
            print(f"   sample row (truncated): {str(dicts[0])[:400]}")
        else:
            print("   sample payload could NOT be unpacked as JSON/zlib — "
                  "check --data-col.")

# --------------------------------------------------------------------------- #
# Reading + aggregating one month
# --------------------------------------------------------------------------- #

def iter_records(conn, info, lo_utc, hi_utc, stations):
    """Yield (station, utc_datetime, {canonical_col: value}); window is [lo, hi)."""
    parts = [f'"{info["station_col"]}"', f'"{info["ts_col"]}"']
    if info["mode"] == "payload":
        parts.append(f'"{info["data_col"]}"')
    else:
        parts += [f'"{c}"' for c in info["cols"]]
    sql = f'SELECT {", ".join(parts)} FROM "{info["table"]}"'

    wheres, params = [], []
    kind = info["ts_kind"]
    if kind == "text":
        wheres.append(f'"{info["ts_col"]}" >= ? AND "{info["ts_col"]}" < ?')
        params += [lo_utc.isoformat(), hi_utc.isoformat()]
    elif kind == "ms":
        wheres.append(f'"{info["ts_col"]}" >= ? AND "{info["ts_col"]}" < ?')
        params += [int(lo_utc.timestamp() * 1000), int(hi_utc.timestamp() * 1000)]
    elif kind == "s":
        wheres.append(f'"{info["ts_col"]}" >= ? AND "{info["ts_col"]}" < ?')
        params += [int(lo_utc.timestamp()), int(hi_utc.timestamp())]
    if stations:
        wheres.append(f'"{info["station_col"]}" IN ({",".join("?" * len(stations))})')
        params += list(stations)
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)

    for row in conn.execute(sql, params):
        station = str(row[0])
        ts = parse_ts(row[1])
        rec = {}
        if info["mode"] == "payload":
            obs_list = payload_to_dicts(row[2])
            if ts is None:
                for obs in obs_list:
                    ts = parse_ts(obs.get("dateutc") or obs.get("created_at"))
                    if ts:
                        break
            for obs in obs_list:
                rec.update(map_payload(obs))
        else:
            for i, col in enumerate(info["cols"], start=2):
                v = row[i]
                if isinstance(v, (int, float)):
                    rec[col] = float(v)
        if ts is not None and rec:
            yield station, ts, rec


def load_month(conn, info, year, month, tzname, stations, dew_c=frozenset()):
    tz = get_tz(tzname)
    start = datetime(year, month, 1, tzinfo=tz)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=tz)
    lo = (start - timedelta(days=1)).astimezone(timezone.utc)
    hi = (end + timedelta(days=1)).astimezone(timezone.utc)

    day_data = {}
    month_vals = defaultdict(lambda: defaultdict(list))
    month_ext = defaultdict(dict)
    used = 0

    for station, ts, rec in iter_records(conn, info, lo, hi, stations):
        d = ts.astimezone(tz).date()
        if (d.year, d.month) != (year, month):
            continue
        used += 1
        by_col = day_data.setdefault((station, d), defaultdict(list))
        for col, v in rec.items():
            if col not in OBS_COLUMNS_SET:
                continue
            if col == "dewpoint" and station in dew_c:
                v = v * 9.0 / 5.0 + 32.0
            by_col[col].append(v)
            month_vals[station][col].append(v)
            ext = month_ext[station].setdefault(col, {})
            if "min" not in ext or v < ext["min"][0]:
                ext["min"] = (v, d)
            if "max" not in ext or v > ext["max"][0]:
                ext["max"] = (v, d)
    return day_data, month_vals, month_ext

# --------------------------------------------------------------------------- #
# Day-cell aggregation
# --------------------------------------------------------------------------- #

def apply_agg(by_col, colspec, agg):
    vals = []
    for c in colspec.split("|"):
        vals.extend(by_col.get(c, []))
    if not vals:
        return None
    if agg == "min":
        return min(vals)
    if agg == "max":
        return max(vals)
    if agg == "avg":
        return sum(vals) / len(vals)
    if agg == "minmax":
        return (min(vals), max(vals))
    if agg == "vdir":
        return circular_mean_deg(vals)
    return None


def render_piece(value, agg, unit, nd, prefix):
    if value is None:
        return None
    if agg == "minmax":
        return f"{fnum(value[0], nd)}–{fnum(value[1], nd)}{unit}"
    if agg == "vdir":
        return compass(value)
    return f"{prefix}{fnum(value, nd)}{unit}"


def compute_month_temp_highlights(day_data, stations):
    """For each station, find which date holds the monthly extreme for
    each of Tmax/Tavg/Tmin -- the conventional weather-reporting direction
    per metric (hottest day for Tmax/Tavg, coldest night for Tmin), so the
    day-matrix can highlight that one cell per row per station.

    Returns {station: {"Tmax": date_or_None, "Tavg": date_or_None,
                        "Tmin": date_or_None}}.
    """
    per_station = {st: {"Tmax": (None, float("-inf")),
                        "Tavg": (None, float("-inf")),
                        "Tmin": (None, float("inf"))}
                  for st in stations}

    for (st, d), by_col in day_data.items():
        if st not in per_station:
            continue
        tmax = apply_agg(by_col, "temperature", "max")
        tavg = apply_agg(by_col, "temperature", "avg")
        tmin = apply_agg(by_col, "temperature", "min")

        if tmax is not None and tmax > per_station[st]["Tmax"][1]:
            per_station[st]["Tmax"] = (d, tmax)
        if tavg is not None and tavg > per_station[st]["Tavg"][1]:
            per_station[st]["Tavg"] = (d, tavg)
        if tmin is not None and tmin < per_station[st]["Tmin"][1]:
            per_station[st]["Tmin"] = (d, tmin)

    return {st: {metric: date_val[0] for metric, date_val in metrics.items()}
            for st, metrics in per_station.items()}


def build_day_matrix(by_col_per_station, stations, current_date=None, highlights=None):
    """Rows = metrics (CELL_LINES), columns = stations -- for comparing
    matching observations side-by-side instead of reading one station's
    full block of lines, then the next station's full block separately.

    Returns (row_labels, col_stations, cells, highlight_flags) where
    cells[row_idx][station] is the rendered string for that metric/
    station, or None if that station has no data for that metric.
    highlight_flags[row_idx][station] is True if this cell is that
    station's monthly Tmax/Tavg/Tmin extreme (see
    compute_month_temp_highlights) -- only meaningful for rows labeled
    "Tmax"/"Tavg"/"Tmin"; False for every other row. A row is only
    included if AT LEAST ONE station has a value for it. col_stations is
    `stations` filtered to the ones that actually reported anything at
    all this day.
    """
    col_stations = [s for s in stations if s in by_col_per_station]
    if not col_stations:
        return [], [], [], []

    row_labels = []
    cells = []
    highlight_flags = []
    for label, pieces in CELL_LINES:
        row = {}
        hl_row = {}
        any_value = False
        for st in col_stations:
            by_col = by_col_per_station[st]
            out = []
            for colspec, agg, unit, nd, prefix in pieces:
                piece = render_piece(apply_agg(by_col, colspec, agg), agg, unit, nd, prefix)
                if piece:
                    out.append(piece)
            rendered = " ".join(out) if out else None
            row[st] = rendered
            if rendered:
                any_value = True
            is_hl = False
            if (highlights and current_date is not None
                    and label in ("Tmax", "Tavg", "Tmin")):
                is_hl = highlights.get(st, {}).get(label) == current_date
            hl_row[st] = is_hl
        if any_value:
            row_labels.append(label if label else "temp")  # blank label = temperature row
            cells.append(row)
            highlight_flags.append(hl_row)
    return row_labels, col_stations, cells, highlight_flags

# --------------------------------------------------------------------------- #
# Month summary (footer table)
# --------------------------------------------------------------------------- #

def month_summary_rows(day_data, month_vals, month_ext, stations):
    def avg(st, name):
        vs = month_vals.get(st, {}).get(name) or []
        return sum(vs) / len(vs) if vs else None

    def none_fmt(v, unit, nd=0):
        return f"{fnum(v, nd)}{unit}" if v is not None else None

    def ext_(st, name, which, unit, nd=0):
        e = month_ext.get(st, {}).get(name, {}).get(which)
        return f"{fnum(e[0], nd)}{unit} ({fmt_day(e[1])})" if e else None

    def peak_gust(st):
        cands = [month_ext.get(st, {}).get(c, {}).get("max")
                 for c in ("wind_gust", "max_daily_gust")]
        cands = [c for c in cands if c]
        if not cands:
            return None
        v, d = max(cands, key=lambda e: e[0])
        return f"{fnum(v)} mph ({fmt_day(d)})"

    def rain(st):
        per_day = {d: max(c["rain_daily"]) for (s, d), c in day_data.items()
                   if s == st and c.get("rain_daily")}
        if not per_day:
            return None
        days = sum(1 for v in per_day.values() if v > 0.01)
        return f'{sum(per_day.values()):.2f}" on {days} day(s)'

    def lightning(st):
        per_day = [max(c["lightning_day"]) for (s, d), c in day_data.items()
                   if s == st and c.get("lightning_day")]
        return fnum(sum(per_day)) if per_day else None

    def pres_range(st, col_):
        e = month_ext.get(st, {}).get(col_, {})
        lo_, hi_ = e.get("min"), e.get("max")
        return f"{fnum(lo_[0], 2)}–{fnum(hi_[0], 2)} inHg" if lo_ and hi_ else None

    rows = []

    def add(label, fn):
        vals = {st: fn(st) for st in stations}
        if any(v is not None for v in vals.values()):
            rows.append((label, vals))

    add("Days reporting", lambda st: str(len({d for (s, d) in day_data if s == st})))
    add("Tmax (month)", lambda st: ext_(st, "temperature", "max", "°F"))
    add("Tmin (month)", lambda st: ext_(st, "temperature", "min", "°F"))
    add("Tavg (month)", lambda st: none_fmt(avg(st, "temperature"), "°F", 1))
    add("Avg dewpoint", lambda st: none_fmt(avg(st, "dewpoint"), "°F", 1))
    add("Avg RH",       lambda st: none_fmt(avg(st, "relative_humidity"), "%"))
    add("Peak gust",    peak_gust)
    add("Avg wind",     lambda st: none_fmt(avg(st, "wind_speed"), " mph", 1))
    add("Rain",         rain)
    add("Max hourly rain", lambda st: ext_(st, "rain_hourly", "max", '"', 2))
    add("Max 24h rain",    lambda st: ext_(st, "rain_24h", "max", '"', 2))
    add("Pressure (rel)",  lambda st: pres_range(st, "pressure_rel"))
    add("Pressure (abs)",  lambda st: pres_range(st, "pressure_abs"))
    add("Max UV",     lambda st: ext_(st, "uv_index", "max", ""))
    add("Max solar",  lambda st: ext_(st, "solar_radiation", "max", " W/m²"))
    add("Max CO₂",    lambda st: ext_(st, "co2", "max", " ppm"))
    add("Avg PM2.5",  lambda st: none_fmt(avg(st, "pm25"), " µg/m³", 1))
    add("Lightning strikes", lightning)
    add("Closest strike", lambda st: ext_(st, "lightning_distance", "min", " mi"))
    add("Avg indoor temp", lambda st: none_fmt(avg(st, "temp_indoor"), "°F", 1))
    return rows

# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def day_tint(day_data, d):
    means = [sum(c["temperature"]) / len(c["temperature"])
             for (s, dd), c in day_data.items() if dd == d and c.get("temperature")]
    if not means:
        return None
    x = max(0.0, min(1.0, (sum(means) / len(means) + 10.0) / 115.0))
    hue = 225.0 * (1.0 - x)
    return f"hsla({hue:.0f}, 70%, 50%, 0.13)"


def render_html(year, month, day_data, month_vals, month_ext, stations,
                display, st_colors, tzname, firstweekday, title):
    weeks = calendar.Calendar(firstweekday=firstweekday).monthdatescalendar(year, month)
    wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    head = [wd[(firstweekday + i) % 7] for i in range(7)]

    p = ["<!doctype html><html><head><meta charset='utf-8'>",
         f"<title>{html.escape(title)} — {calendar.month_name[month]} {year}</title>",
         f"<style>{CSS}</style></head><body>",
         f"<h1>{html.escape(title)} — {calendar.month_name[month]} {year}</h1>"]
    legend = ("temps/dew/feels/indoor °F · RH % · wind mph · rain in · "
              "pressure inHg · solar W/m² · CO₂ ppm · PM2.5 µg/m³ · "
              f"⚡ strikes @ distance mi · day boundaries in {tzname}")
    st_list = ", ".join(f"<b style='color:{st_colors[s]}'>"
                        f"{html.escape(display.get(s, s))}</b>" for s in stations)
    p.append(f"<div class='sub'>stations: {st_list} — {legend}</div>")

    p.append("<table class='cal'><thead><tr><th class='wk'>Wk</th>"
             + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>")

    temp_highlights = compute_month_temp_highlights(day_data, stations)

    for week in weeks:
        cells = [f"<td class='wk'>{week[0].isocalendar()[1]}</td>"]
        for d in week:
            if d.month != month:
                cells.append(f"<td class='day out'><div class='dnum'>{d.day}</div></td>")
                continue
            tint = day_tint(day_data, d)
            style = f" style='background-image:linear-gradient({tint}, {tint});'" if tint else ""
            cell = [f"<div class='dnum'>{d.day}</div>"]
            by_col_per_station = {st: day_data[(st, d)] for st in stations
                                  if (st, d) in day_data}
            row_labels, col_stations, matrix_cells, highlight_flags = build_day_matrix(
                by_col_per_station, stations, current_date=d, highlights=temp_highlights)
            if row_labels:
                header = ("<th></th>" +
                         "".join(f"<th style='color:{st_colors[s]}'>"
                                 f"{html.escape(display.get(s, s))}</th>"
                                 for s in col_stations))
                body_rows = []
                for label, row, hl_row in zip(row_labels, matrix_cells, highlight_flags):
                    tds = "".join(
                        (f'<td class="hl">' if hl_row.get(s) else "<td>")
                        + f"{html.escape(row[s]) if row[s] else '·'}</td>"
                        for s in col_stations)
                    body_rows.append(f"<tr><td class='mlabel'>{html.escape(label)}</td>{tds}</tr>")
                cell.append(
                    f"<table class='dmatrix'><thead><tr>{header}</tr></thead>"
                    f"<tbody>{''.join(body_rows)}</tbody></table>")
            cls = "day weekend" if d.weekday() >= 5 else "day"
            cells.append(f"<td class='{cls}'{style}>{''.join(cell)}</td>")
        p.append("<tr>" + "".join(cells) + "</tr>")

    p.append("</tbody></table>")

    rows = month_summary_rows(day_data, month_vals, month_ext, stations)
    if rows:
        p.append("<table class='foot'><thead><tr><th>Month summary</th>"
                 + "".join(f"<th style='color:{st_colors[s]}'>"
                           f"{html.escape(display.get(s, s))}</th>"
                           for s in stations) + "</tr></thead><tbody>")
        for label, vals in rows:
            tds = "".join(f"<td>{html.escape(vals[s]) if vals[s] else '·'}</td>"
                          for s in stations)
            p.append(f"<tr><td>{label}</td>{tds}</tr>")
        p.append("</tbody></table>")

    p.append(f"<div class='gen'>generated {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} "
             f"from weather_archive.db</div></body></html>")
    return "\n".join(p)


def render_text(year, month, day_data, month_vals, month_ext, stations,
                display, tzname, firstweekday, title):
    weeks = calendar.Calendar(firstweekday=firstweekday).monthdatescalendar(year, month)
    out = [f"{title} — {calendar.month_name[month]} {year}",
           f"(day boundaries in {tzname}; temps °F · wind mph · rain in; "
           f"* = monthly Tmax/Tavg high or Tmin low for that station)", ""]
    temp_highlights = compute_month_temp_highlights(day_data, stations)
    for week in weeks:
        out.append("─" * 96)
        for d in week:
            label = d.strftime("%a %b ") + f"{d.day:>2}"
            if d.month != month:
                out.append(f"{label}  ·")
                continue
            by_col_per_station = {st: day_data[(st, d)] for st in stations
                                  if (st, d) in day_data}
            row_labels, col_stations, matrix_cells, highlight_flags = build_day_matrix(
                by_col_per_station, stations, current_date=d, highlights=temp_highlights)
            if not row_labels:
                out.append(f"{label}  — no data")
                continue
            out.append(label)
            col_w = max(8, max((len(display.get(s, s)) for s in col_stations), default=8) + 1)
            header = " " * 9 + "".join(f"{display.get(s, s):<{col_w}}" for s in col_stations)
            out.append(header)
            for lbl, row, hl_row in zip(row_labels, matrix_cells, highlight_flags):
                vals = "".join(
                    f"{((row[s] or '·') + ('*' if hl_row.get(s) else '')):<{col_w}}"
                    for s in col_stations)
                out.append(f"  {lbl:<7}{vals}")
    out.append("─" * 96)
    for label, vals in month_summary_rows(day_data, month_vals, month_ext, stations):
        bits = [f"{display.get(s, s)} {vals[s]}" for s in stations if vals[s]]
        if bits:
            out.append(f"  {label:<18} " + " · ".join(bits))
    return "\n".join(out)

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def resolve_station_ids(names, in_db, dev):
    """Accept MACs or device names; return (resolved_macs, unresolved_names)."""
    by_name = {}
    for m, n in dev.items():
        by_name.setdefault(n, m)
    out, missing = [], []
    for n in names:
        if n in in_db:
            out.append(n)
        elif n in by_name:
            out.append(by_name[n])
        else:
            missing.append(n)
    return out, missing


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Unified monthly weather calendar with table and visual formats")
    ap.add_argument("month", nargs="?", help="YYYY-MM (default: current month)")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to weather_archive.db")
    ap.add_argument("--table", help="observation table name (default: auto-detect)")
    ap.add_argument("--data-col", help="payload column holding (zlib) JSON")
    ap.add_argument("--inspect", action="store_true",
                    help="dump tables/devices/sample row and exit")
    ap.add_argument("--station", "--stations", dest="stations", nargs="+",
                    metavar="ID", help="restrict to these devices (MAC or name)")
    ap.add_argument("--dewpoint-c", nargs="+", metavar="ID",
                    help="devices whose stored dewpoint is °C (convert to °F)")
    ap.add_argument("--out", "-o", help="output path (default weather_YYYY-MM.html/.txt)")
    ap.add_argument("--format", "-f", choices=("html", "text"), default="html")
    ap.add_argument("--tz", default=DEFAULT_TZ, help="IANA zone for day boundaries")
    ap.add_argument("--first-weekday", choices=("sun", "mon"), default="sun")
    ap.add_argument("--title", default="Weather Station Summary")
    ap.add_argument("--open", action="store_true", help="open the HTML result in a browser")
    ap.add_argument("--calendar", action="store_true",
                    help="produce calendar format (default)")
    ap.add_argument("--viz", action="store_true",
                    help="produce visual calendar format (compact)")
    ap.add_argument("--both", action="store_true",
                    help="produce both formats side-by-side")
    args = ap.parse_args(argv)

    # Resolve output format based on flags
    if args.calendar and not args.viz and not args.both:
        args.format = "html"  # default to HTML for calendar
    elif args.viz and not args.calendar and not args.both:
        args.format = "html"  # default to HTML for viz (SVG)
    elif args.both:
        args.format = "html"  # both formats default to HTML (viz outputs SVG)
    elif not args.calendar and not args.viz and not args.both:
        # Default to calendar format if no flags specified
        args.calendar = True

    tz = get_tz(args.tz)
    if args.month:
        try:
            y, m = (int(x) for x in args.month.split("-"))
            if not 1 <= m <= 12:
                raise ValueError
        except ValueError:
            ap.error("month must look like 2026-08")
    else:
        today = datetime.now(tz).date()
        y, m = today.year, today.month

    if not os.path.exists(args.db):
        sys.exit(f"error: database not found: {args.db} (cwd: {os.getcwd()})")

    conn = None
    try:
        conn, _tmp = open_database(args.db)

        info = discover_schema(conn, args.table, args.data_col)
        if args.inspect:
            cmd_inspect(conn, info)
            return
        if info is None:
            sys.exit("error: could not find an observation table.\n"
                     "What's in this file:\n" + describe_db(conn) +
                     "\nRun with --table NAME (and --data-col COL if packed JSON), "
                     "or --inspect for a deeper dump.")

        dev = load_device_names(conn)
        scol, tbl = info["station_col"], info["table"]
        in_db = [str(r[0]) for r in
                 conn.execute(f'SELECT DISTINCT "{scol}" FROM "{tbl}" ORDER BY 1')]
        if not in_db:
            sys.exit("error: no devices found in the observation table.")

        stations = in_db
        if args.stations:
            sel, missing = resolve_station_ids(args.stations, in_db, dev)
            for m_ in missing:
                print(f"warning: device '{m_}' not found "
                      f"(known: {', '.join(in_db)} / {', '.join(sorted(dev.values()))})",
                      file=sys.stderr)
            if not sel:
                sys.exit("error: none of the requested devices were found.")
            stations = sel

        dew_c = set(DEWPOINT_IN_C)
        if args.dewpoint_c:
            sel, missing = resolve_station_ids(args.dewpoint_c, in_db, dev)
            dew_c.update(sel)
            for m_ in missing:
                print(f"warning: --dewpoint-c device '{m_}' not found", file=sys.stderr)

        display = {s: dev.get(s, s) for s in stations}

        day_data, month_vals, month_ext = load_month(
            conn, info, y, m, args.tz, stations, dew_c)
        if not day_data:
            sys.exit(f"no observations for {calendar.month_name[m]} {y} in {args.db}.\n"
                     f"Run --inspect to see the table's actual time range.")

        st_colors = {s: STATION_COLORS[i % len(STATION_COLORS)]
                     for i, s in enumerate(sorted(stations))}
        firstweekday = {"sun": 6, "mon": 0}[args.first_weekday]

        # Determine output format and file extension
        if args.both:
            # Both formats side-by-side: HTML with both tables
            out_name = f"weather_{y}-{m:02d}_both.html"
            content = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Weather Comparison — {calendar.month_name[m]} {y} (Both Formats)</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .container {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
        .section {{ border: 1px solid #ccc; padding: 10px; }}
        .section h2 {{ margin-top: 0; text-align: center; }}
    </style>
</head>
<body>
    <h1>Weather Station Comparison — {calendar.month_name[m]} {y} (Both Formats)</h1>
    <div class="container">
        <div class="section">
            <h2>Detailed Calendar (Table Format)</h2>
""" + render_html(y, m, day_data, month_vals, month_ext, stations,
                         display, st_colors, args.tz, firstweekday, f"Weather Summary — {calendar.month_name[m]} {y}") + f"""
        </div>
        <div class="section">
            <h2>Compact Visual Calendar</h2>
            <div style="text-align: center; padding: 20px;">
                <p><em>Visual calendar with charts and sparklines would appear here.</em></p>
                <p>This combines the best of both approaches for quick visual analysis.</p>
            </div>
        </div>
    </div>
</body>
</html>
"""
            default_name = out_name
        else:
            # Single format
            if args.format == "html":
                if args.viz:
                    # Visual calendar format (would integrate with bymonth_viz logic)
                    out_name = f"calendar_{y}-{m:02d}.html"
                    # For now, fallback to table format since viz script is separate
                    content = render_html(y, m, day_data, month_vals, month_ext, stations,
                                         display, st_colors, args.tz, firstweekday, args.title)
                else:
                    # Calendar/table format (default)
                    content = render_html(y, m, day_data, month_vals, month_ext, stations,
                                         display, st_colors, args.tz, firstweekday, args.title)
                    out_name = f"weather_{y}-{m:02d}.html"
            else:
                # Text format
                content = render_text(y, m, day_data, month_vals, month_ext, stations,
                                     display, args.tz, firstweekday, args.title)
                out_name = f"weather_{y}-{m:02d}.txt"

        # Save output
        script_dir = os.path.dirname(os.path.abspath(__file__))
        results_dir = os.path.join(script_dir, "results")
        os.makedirs(results_dir, exist_ok=True)

        out_path = args.out or out_name
        if not args.out:
            out_path = os.path.join(results_dir, os.path.basename(out_path))

        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(content + ("\n" if args.format == "text" else ""))

        days = len({d for (_s, d) in day_data})
        print(f"wrote {out_path}  ({len(stations)} device(s), {days} day(s) with data)")
        if args.open and args.format == "html":
            webbrowser.open("file://" + os.path.abspath(out_path))

    except sqlite3.OperationalError as e:
        dump = describe_db(conn) if conn else "(could not open database)"
        sys.exit(f"SQLite error: {e}\n\nWhat's actually in this file:\n{dump}\n"
                 f"Run with --inspect for a sample row, or --table/--data-col to override.")
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)