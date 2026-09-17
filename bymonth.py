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
import base64
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

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))
except ImportError:
    print("note: python-dotenv not installed; any .env file is ignored -- "
          "only real environment variables are used. "
          "Install with: pip install python-dotenv", file=sys.stderr)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_db_env = os.environ.get("WEATHER_DB_PATH", "weather_archive.db")
DEFAULT_DB = (_db_env if os.path.isabs(_db_env)
             else os.path.join(_SCRIPT_DIR, _db_env))
DEFAULT_TZ = "America/Chicago"
DEWPOINT_IN_C = set()
STATION_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]

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


def unpack_observation_data(raw_data):
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
    obj = unpack_observation_data(raw)
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    return []


def map_payload(obs):
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


def parse_ts(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        if v > 1e11:
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
    try:
        v = float(s)
        if v > 1e11:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def get_tz(name):
    try:
        if ZoneInfo is None:
            raise RuntimeError
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


def _cleanup_tmp(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def open_database(path):
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
    elif (head[0] & 0x0F) == 8 and (head[0] >> 4) <= 7:
        try:
            data, how = zlib.decompress(blob), "zlib"
        except zlib.error:
            pass
    if data is None or data[:15] != b"SQLite format 3":
        sys.exit(f"error: {path} is neither a SQLite database nor a gzip/zlib-compressed one (starts with {head[:8]!r})")

    fd, tmp = tempfile.mkstemp(prefix="weather_unpacked_", suffix=".db")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    atexit.register(_cleanup_tmp, tmp)
    print(f"note: {path} was {how}-compressed; unpacked to {tmp}")
    return sqlite3.connect(f"file:{tmp}?mode=ro", uri=True), tmp

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
    try:
        lo_, hi_ = conn.execute(f'SELECT MIN("{ts_col}"), MAX("{ts_col}") FROM "{table}"').fetchone()
    except sqlite3.Error:
        return "text", (None, None)
    if (isinstance(lo_, (int, float)) and isinstance(hi_, (int, float)) and not isinstance(lo_, bool)):
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
                if _pick({c: c for c in cols}, STATION_CANDIDATES) and _pick({c: c for c in cols}, TS_CANDIDATES):
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
    if not dcol:
        for c in cols:
            lc = c.lower()
            if lc == "info_json":
                continue
            if "json" in lc or "payload" in lc or "blob" in lc or lc.endswith("data"):
                dcol = c
                break
    if not dcol:
        return None
    return {"table": tname, "mode": "payload", "station_col": scol, "ts_col": tcol,
            "data_col": dcol, "cols": [], "ts_kind": kind, "ts_range": ts_range}


def load_device_names(conn):
    try:
        if "devices" not in list_tables(conn):
            return {}
        cols = {c.lower(): c for c in table_columns(conn, "devices")}
        mac, name = cols.get("mac_address"), cols.get("name")
        if not (mac and name):
            return {}
        return {str(m): (str(n) if n else str(m)) for m, n in conn.execute(f'SELECT "{mac}", "{name}" FROM "devices"')}
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
    print(f"   time column    : {info['ts_col']}" + {"ms": "  (epoch milliseconds)", "s": "  (epoch seconds)", "text": ""}[info["ts_kind"]])
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
            print("   sample payload could NOT be unpacked as JSON/zlib — check --data-col.")


def iter_records(conn, info, lo_utc, hi_utc, stations):
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

    for station, ts, rec in iter_records(conn, info, lo, hi, stations):
        d = ts.astimezone(tz).date()
        if (d.year, d.month) != (year, month):
            continue
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

    return {st: {metric: date_val[0] for metric, date_val in metrics.items()} for st, metrics in per_station.items()}


def build_day_matrix(by_col_per_station, stations, current_date=None, highlights=None):
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
            if highlights and current_date is not None and label in ("Tmax", "Tavg", "Tmin"):
                is_hl = highlights.get(st, {}).get(label) == current_date
            hl_row[st] = is_hl
        if any_value:
            row_labels.append(label if label else "temp")
            cells.append(row)
            highlight_flags.append(hl_row)
    return row_labels, col_stations, cells, highlight_flags


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
        cands = [month_ext.get(st, {}).get(c, {}).get("max") for c in ("wind_gust", "max_daily_gust")]
        cands = [c for c in cands if c]
        if not cands:
            return None
        v, d = max(cands, key=lambda e: e[0])
        return f"{fnum(v)} mph ({fmt_day(d)})"

    def rain(st):
        per_day = {d: max(c["rain_daily"]) for (s, d), c in day_data.items() if s == st and c.get("rain_daily")}
        if not per_day:
            return None
        days = sum(1 for v in per_day.values() if v > 0.01)
        return f'{sum(per_day.values()):.2f}" on {days} day(s)'

    def lightning(st):
        per_day = [max(c["lightning_day"]) for (s, d), c in day_data.items() if s == st and c.get("lightning_day")]
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
    add("Avg RH", lambda st: none_fmt(avg(st, "relative_humidity"), "%"))
    add("Peak gust", peak_gust)
    add("Avg wind", lambda st: none_fmt(avg(st, "wind_speed"), " mph", 1))
    add("Rain", rain)
    add("Max hourly rain", lambda st: ext_(st, "rain_hourly", "max", '"', 2))
    add("Max 24h rain", lambda st: ext_(st, "rain_24h", "max", '"', 2))
    add("Pressure (rel)", lambda st: pres_range(st, "pressure_rel"))
    add("Pressure (abs)", lambda st: pres_range(st, "pressure_abs"))
    add("Max UV", lambda st: ext_(st, "uv_index", "max", ""))
    add("Max solar", lambda st: ext_(st, "solar_radiation", "max", " W/m²"))
    add("Max CO₂", lambda st: ext_(st, "co2", "max", " ppm"))
    add("Avg PM2.5", lambda st: none_fmt(avg(st, "pm25"), " µg/m³", 1))
    add("Lightning strikes", lightning)
    add("Closest strike", lambda st: ext_(st, "lightning_distance", "min", " mi"))
    add("Avg indoor temp", lambda st: none_fmt(avg(st, "temp_indoor"), "°F", 1))
    return rows


def day_tint(day_data, d):
    means = [sum(c["temperature"]) / len(c["temperature"]) for (s, dd), c in day_data.items() if dd == d and c.get("temperature")]
    if not means:
        return None
    x = max(0.0, min(1.0, (sum(means) / len(means) + 10.0) / 115.0))
    hue = 225.0 * (1.0 - x)
    return f"hsla({hue:.0f}, 70%, 50%, 0.13)"


def render_html(year, month, day_data, month_vals, month_ext, stations, display, st_colors, tzname, firstweekday, title):
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
    st_list = ", ".join(f"<b style='color:{st_colors[s]}'>" f"{html.escape(display.get(s, s))}</b>" for s in stations)
    p.append(f"<div class='sub'>stations: {st_list} — {legend}</div>")

    p.append("<table class='cal'><thead><tr><th class='wk'>Wk</th>" + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>")
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
            by_col_per_station = {st: day_data[(st, d)] for st in stations if (st, d) in day_data}
            row_labels, col_stations, matrix_cells, highlight_flags = build_day_matrix(by_col_per_station, stations, current_date=d, highlights=temp_highlights)
            if row_labels:
                header = "<th></th>" + "".join(f"<th style='color:{st_colors[s]}'>" f"{html.escape(display.get(s, s))}</th>" for s in col_stations)
                body_rows = []
                for label, row, hl_row in zip(row_labels, matrix_cells, highlight_flags):
                    tds = "".join((f'<td class="hl">' if hl_row.get(s) else "<td>") + f"{html.escape(row[s]) if row[s] else '·'}</td>" for s in col_stations)
                    body_rows.append(f"<tr><td class='mlabel'>{html.escape(label)}</td>{tds}</tr>")
                cell.append(f"<table class='dmatrix'><thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>")
            cls = "day weekend" if d.weekday() >= 5 else "day"
            cells.append(f"<td class='{cls}'{style}>{''.join(cell)}</td>")
        p.append("<tr>" + "".join(cells) + "</tr>")

    p.append("</tbody></table>")

    rows = month_summary_rows(day_data, month_vals, month_ext, stations)
    if rows:
        p.append("<table class='foot'><thead><tr><th>Month summary</th>" + "".join(f"<th style='color:{st_colors[s]}'>" f"{html.escape(display.get(s, s))}</th>" for s in stations) + "</tr></thead><tbody>")
        for label, vals in rows:
            tds = "".join(f"<td>{html.escape(vals[s]) if vals[s] else '·'}</td>" for s in stations)
            p.append(f"<tr><td>{label}</td>{tds}</tr>")
        p.append("</tbody></table>")

    p.append(f"<div class='gen'>generated {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} from weather_archive.db</div></body></html>")
    return "\n".join(p)


def render_text(year, month, day_data, month_vals, month_ext, stations, display, tzname, firstweekday, title):
    weeks = calendar.Calendar(firstweekday=firstweekday).monthdatescalendar(year, month)
    out = [f"{title} — {calendar.month_name[month]} {year}", f"(day boundaries in {tzname}; temps °F · wind mph · rain in; * = monthly Tmax/Tavg high or Tmin low for that station)", ""]
    temp_highlights = compute_month_temp_highlights(day_data, stations)
    for week in weeks:
        out.append("─" * 96)
        for d in week:
            label = d.strftime("%a %b ") + f"{d.day:>2}"
            if d.month != month:
                out.append(f"{label}  ·")
                continue
            by_col_per_station = {st: day_data[(st, d)] for st in stations if (st, d) in day_data}
            row_labels, col_stations, matrix_cells, highlight_flags = build_day_matrix(by_col_per_station, stations, current_date=d, highlights=temp_highlights)
            if not row_labels:
                out.append(f"{label}  — no data")
                continue
            out.append(label)
            col_w = max(8, max((len(display.get(s, s)) for s in col_stations), default=8) + 1)
            header = " " * 9 + "".join(f"{display.get(s, s):<{col_w}}" for s in col_stations)
            out.append(header)
            for lbl, row, hl_row in zip(row_labels, matrix_cells, highlight_flags):
                vals = "".join(f"{((row[s] or '·') + ('*' if hl_row.get(s) else '')):<{col_w}}" for s in col_stations)
                out.append(f"  {lbl:<7}{vals}")
    out.append("─" * 96)
    for label, vals in month_summary_rows(day_data, month_vals, month_ext, stations):
        bits = [f"{display.get(s, s)} {vals[s]}" for s in stations if vals[s]]
        if bits:
            out.append(f"  {label:<18} " + " · ".join(bits))
    return "\n".join(out)

DEFAULT_VIS_STATION = "G6964"
SOLAR_CONSTANT = 1361.0


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def sunrise_sunset(lat, lon, tz, d):
    day_num = d.toordinal() - (734124 - 40529)
    noon = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=tz)
    offset = noon.utcoffset()
    tz_hours = offset.seconds / 3600.0 if offset else 0
    Jday = day_num + 2415018.5 + 0.5 - tz_hours / 24
    Jcent = (Jday - 2451545) / 36525
    Manom = 357.52911 + Jcent * (35999.05029 - 0.0001537 * Jcent)
    Mlong = (280.46646 + Jcent * (36000.76983 + Jcent * 0.0003032)) % 360
    Eccent = 0.016708634 - Jcent * (0.000042037 + 0.0001537 * Jcent)
    Mobliq = 23 + (26 + ((21.448 - Jcent * (46.815 + Jcent * (0.00059 - Jcent * 0.001813)))) / 60) / 60
    obliq = Mobliq + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * Jcent))
    vary = math.tan(math.radians(obliq / 2)) ** 2
    Seqcent = (math.sin(math.radians(Manom)) * (1.914602 - Jcent * (0.004817 + 0.000014 * Jcent)) +
               math.sin(math.radians(2 * Manom)) * (0.019993 - 0.000101 * Jcent) +
               math.sin(math.radians(3 * Manom)) * 0.000289)
    Struelong = Mlong + Seqcent
    Sapplong = Struelong - 0.00569 - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * Jcent))
    declination = math.degrees(math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(Sapplong))))
    eqtime = 4 * math.degrees(vary * math.sin(2 * math.radians(Mlong)) - 2 * Eccent * math.sin(math.radians(Manom)) + 4 * Eccent * vary * math.sin(math.radians(Manom)) * math.cos(2 * math.radians(Mlong)) - 0.5 * vary * vary * math.sin(4 * math.radians(Mlong)) - 1.25 * Eccent * Eccent * math.sin(2 * math.radians(Manom)))
    try:
        hourangle = math.degrees(math.acos(math.cos(math.radians(90.833)) / (math.cos(math.radians(lat)) * math.cos(math.radians(declination))) - math.tan(math.radians(lat)) * math.tan(math.radians(declination))))
    except ValueError:
        return None, None
    solarnoon = (720 - 4 * lon - eqtime + tz_hours * 60) / 1440
    sunrise_frac = solarnoon - hourangle * 4 / 1440
    sunset_frac = solarnoon + hourangle * 4 / 1440
    base = datetime(d.year, d.month, d.day, tzinfo=tz)
    return (base + timedelta(days=sunrise_frac), base + timedelta(days=sunset_frac))


def all_sun_times(lat, lon, tz, year, month):
    result = {}
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        d = date(year, month, day)
        result[d] = sunrise_sunset(lat, lon, tz, d)
    return result


def solar_declination(day_of_year):
    return 23.45 * math.sin(math.radians(360 * (284 + day_of_year) / 365))


def equation_of_time(day_of_year):
    B = math.radians(360 * (day_of_year - 1) / 365)
    return 229.18 * (0.000075 + 0.001868 * math.cos(B) - 0.032077 * math.sin(B) - 0.014615 * math.cos(2 * B) - 0.04089 * math.sin(2 * B))


def solar_zenith(lat, lon, tz, dt):
    d = dt.date()
    doy = d.timetuple().tm_yday
    decl = solar_declination(doy)
    eot = equation_of_time(doy)
    local_standard_time = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    offset = dt.utcoffset()
    tz_hours = offset.seconds / 3600.0 if offset else 0
    solar_time = local_standard_time + (4 * lon / 60) + (eot / 60) - tz_hours
    hour_angle = (solar_time - 12) * 15
    lat_rad = math.radians(lat)
    decl_rad = math.radians(decl)
    ha_rad = math.radians(hour_angle)
    cos_zenith = (math.sin(lat_rad) * math.sin(decl_rad) + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(ha_rad))
    cos_zenith = max(-1, min(1, cos_zenith))
    return math.degrees(math.acos(cos_zenith))


def _earth_sun_distance_factor(day_of_year):
    B = math.radians(360 * (day_of_year - 1) / 365)
    return (1.00011 + 0.034221 * math.cos(B) + 0.00128 * math.sin(B) + 0.000719 * math.cos(2 * B) + 0.000077 * math.sin(2 * B))


def _air_mass(zenith_deg, altitude_m=0):
    z = math.radians(zenith_deg)
    am = 1.0 / (math.cos(z) + 0.50572 * (96.07995 - zenith_deg) ** -1.6364)
    p_ratio = math.exp(-altitude_m / 8400.0)
    return am * p_ratio, am


def bird_clearsky_ghi(lat, lon, tz, dt, ozone_cm=0.3, water_cm=1.5, tau_a_380=0.1, tau_a_500=0.15, ground_albedo=0.2, altitude_m=0):
    zenith = solar_zenith(lat, lon, tz, dt)
    if zenith >= 87.9:
        return 0.0
    doy = dt.date().timetuple().tm_yday
    e0 = _earth_sun_distance_factor(doy)
    dni_extra = SOLAR_CONSTANT * e0
    am_p, am = _air_mass(zenith, altitude_m)
    T_R = math.exp(-0.0903 * am_p ** 0.84 * (1 + am_p - am_p ** 1.01))
    x_O = ozone_cm * am
    T_O3 = (1 - 0.1611 * x_O * (1 + 139.48 * x_O) ** -0.3035 - 0.002715 * x_O / (1 + 0.044 * x_O + 0.0003 * x_O ** 2))
    T_g = math.exp(-0.0127 * am_p ** 0.26)
    x_W = water_cm * am
    T_W = 1 - 2.4959 * x_W / ((1 + 79.034 * x_W) ** 0.6828 + 6.385 * x_W)
    tau_A = 0.2758 * tau_a_380 + 0.35 * tau_a_500
    T_A = math.exp(-tau_A ** 0.873 * (1 + tau_A - tau_A ** 0.7088) * am ** 0.9108)
    K1 = 0.1
    T_AA = 1 - K1 * (1 - am + am ** 1.06) * (1 - T_A)
    T_AS = T_A / T_AA
    b_A = 0.85
    cos_z = math.cos(math.radians(zenith))
    dni_clear = max(0, dni_extra * 0.9662 * T_R * T_O3 * T_g * T_W * T_A)
    dhi_num = 0.5 * (1 - T_R) + b_A * (1 - T_AS)
    dhi_den = 1 - am + am ** 1.02
    dhi_clear = max(0, dni_extra * cos_z * 0.79 * T_O3 * T_g * T_W * T_AA * dhi_num / dhi_den)
    rho_s = 0.0685 + (1 - b_A) * (1 - T_AS)
    denom = 1 - ground_albedo * rho_s
    ghi = (dni_clear * cos_z + dhi_clear) / denom if abs(denom) > 1e-6 else 0
    return max(0, ghi)


def daily_clearsky_mj(lat, lon, tz, d, step_min=10, **kwargs):
    total_j = 0.0
    dt_prev, ghi_prev = None, None
    for minute in range(0, 24 * 60, step_min):
        h, m = divmod(minute, 60)
        dt = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=tz)
        ghi = bird_clearsky_ghi(lat, lon, tz, dt, **kwargs)
        if dt_prev is not None and ghi_prev is not None:
            total_j += (ghi_prev + ghi) / 2.0 * (dt - dt_prev).total_seconds()
        dt_prev, ghi_prev = dt, ghi
    return total_j / 1e6


def discover_stations(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT station FROM station_obs ORDER BY station")]


def load_month_data(conn, station, year, month, tz):
    start = datetime(year, month, 1, tzinfo=tz)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(ny, nm, 1, tzinfo=tz)
    start_utc = start.astimezone(timezone.utc).isoformat()
    end_utc = end.astimezone(timezone.utc).isoformat()

    try:
        rows = conn.execute(
            "SELECT timestamp, temperature, dewpoint, rain_daily, solar_radiation, lightning_day, lightning_hour, lightning_distance FROM station_obs WHERE station = ? AND timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC",
            (station, start_utc, end_utc)
        ).fetchall()
        has_dewpoint = True
    except sqlite3.OperationalError:
        rows = conn.execute(
            "SELECT timestamp, temperature, rain_daily, solar_radiation, lightning_day, lightning_hour, lightning_distance FROM station_obs WHERE station = ? AND timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC",
            (station, start_utc, end_utc)
        ).fetchall()
        has_dewpoint = False

    by_day = defaultdict(list)
    for row in rows:
        if has_dewpoint:
            ts, temperature, dewpoint, rain_daily, solar_radiation, lightning_day, lightning_hour, lightning_distance = row
        else:
            ts, temperature, rain_daily, solar_radiation, lightning_day, lightning_hour, lightning_distance = row
            dewpoint = None
        utc_dt = datetime.fromisoformat(ts)
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        local = utc_dt.astimezone(tz)
        d = local.date()
        if d.year == year and d.month == month:
            rec = {"temperature": temperature, "dewpoint": dewpoint, "rain_daily": rain_daily, "solar_radiation": solar_radiation, "lightning_day": lightning_day, "lightning_hour": lightning_hour, "lightning_distance": lightning_distance}
            by_day[d].append((local, rec))
    return by_day


def hourly_temps(entries):
    by_hour = {}
    for dt, obs in entries:
        t = num(obs.get("temperature"))
        if t is not None:
            h = dt.hour
            if h not in by_hour or abs(dt.minute) < abs(by_hour[h][1]):
                by_hour[h] = (t, dt.minute)
    return [(h, by_hour[h][0] if h in by_hour else None) for h in range(24)]


def hourly_precipitation(entries):
    hour_buckets = defaultdict(list)
    for dt, obs in entries:
        dr = num(obs.get("rain_daily"))
        if dr is not None:
            hour_buckets[dt.hour].append((dt, dr))
    hourly_inches = [0.0] * 24
    for h in sorted(hour_buckets.keys()):
        pts = hour_buckets[h]
        if not pts:
            continue
        pts.sort(key=lambda x: x[0])
        delta = max(0.0, pts[-1][1] - pts[0][1])
        hourly_inches[h] = delta
    return hourly_inches


def daily_summary(entries, lat=None, lon=None, tz=None):
    temps = [num(o.get("temperature")) for _, o in entries]
    temps = [t for t in temps if t is not None]
    dews = [num(o.get("dewpoint")) for _, o in entries]
    dews = [t for t in dews if t is not None]
    rain_values = [num(o.get("rain_daily")) for _, o in entries]
    rain_values = [r for r in rain_values if r is not None]
    rain_total = max(rain_values) if rain_values else 0.0
    solar = [(dt, num(o.get("solar_radiation"))) for dt, o in entries]
    solar = [(dt, s) for dt, s in solar if s is not None]
    lightning_count = 0
    lightning_by_hour = defaultdict(float)
    for dt, o in entries:
        ld = num(o.get("lightning_day"))
        if ld is not None and ld > lightning_count:
            lightning_count = int(ld)
        lh = num(o.get("lightning_hour"))
        if lh is not None and lh > lightning_by_hour[dt.hour]:
            lightning_by_hour[dt.hour] = lh
    lightning_active_hours = {h for h, v in lightning_by_hour.items() if v > 0}
    solar_j = 0.0
    if len(solar) >= 2:
        for i in range(1, len(solar)):
            dt_prev, w_prev = solar[i - 1]
            dt_cur, w_cur = solar[i]
            dt_s = (dt_cur - dt_prev).total_seconds()
            if 0 < dt_s < 7200:
                solar_j += (w_prev + w_cur) / 2.0 * dt_s
    clearness = None
    ideal_mj = None
    if lat is not None and lon is not None and tz is not None and entries:
        d = entries[0][0].astimezone(tz).date()
        ideal_mj = daily_clearsky_mj(lat, lon, tz, d)
        if ideal_mj is not None and ideal_mj > 0.5 and solar_j > 0:
            clearness = min(solar_j / 1e6 / ideal_mj, 1.0)
    return {"temp_hi": max(temps) if temps else None, "temp_lo": min(temps) if temps else None, "dew_hi": max(dews) if dews else None, "dew_lo": min(dews) if dews else None, "rain_total": rain_total, "solar_mj": solar_j / 1e6, "lightning_count": lightning_count, "lightning_active_hours": lightning_active_hours, "lightning_by_hour": dict(lightning_by_hour), "clearness": clearness, "ideal_mj": ideal_mj, "n_obs": len(entries)}


def compute_month_summary(by_day, all_summaries):
    temp_max = temp_min = None
    dew_max = dew_min = None
    warm_low = None
    cool_high = None
    pooled_temps, pooled_dews = [], []
    rain_total = 0.0
    rain_days = 0
    lightning_total = 0
    solar_total = 0.0
    clearness_vals = []
    days_reporting = 0
    for d, entries in by_day.items():
        s = all_summaries.get(d)
        if not entries or s is None:
            continue
        days_reporting += 1
        if s["temp_hi"] is not None and (temp_max is None or s["temp_hi"] > temp_max[0]):
            temp_max = (s["temp_hi"], d)
        if s["temp_lo"] is not None and (temp_min is None or s["temp_lo"] < temp_min[0]):
            temp_min = (s["temp_lo"], d)
        if s["temp_lo"] is not None and (warm_low is None or s["temp_lo"] > warm_low[0]):
            warm_low = (s["temp_lo"], d)
        if s["temp_hi"] is not None and (cool_high is None or s["temp_hi"] < cool_high[0]):
            cool_high = (s["temp_hi"], d)
        if s["dew_hi"] is not None and (dew_max is None or s["dew_hi"] > dew_max[0]):
            dew_max = (s["dew_hi"], d)
        if s["dew_lo"] is not None and (dew_min is None or s["dew_lo"] < dew_min[0]):
            dew_min = (s["dew_lo"], d)
        for _, o in entries:
            t = num(o.get("temperature"))
            if t is not None:
                pooled_temps.append(t)
            dp = num(o.get("dewpoint"))
            if dp is not None:
                pooled_dews.append(dp)
        if s["rain_total"] > 0.01:
            rain_days += 1
        rain_total += s["rain_total"]
        lightning_total += s["lightning_count"]
        solar_total += s["solar_mj"]
        if s["clearness"] is not None:
            clearness_vals.append(s["clearness"])
    return {"temp_max": temp_max, "temp_min": temp_min, "warm_low": warm_low, "cool_high": cool_high, "temp_avg": (sum(pooled_temps) / len(pooled_temps)) if pooled_temps else None, "dew_max": dew_max, "dew_min": dew_min, "dew_avg": (sum(pooled_dews) / len(pooled_dews)) if pooled_dews else None, "rain_total": rain_total, "rain_days": rain_days, "lightning_total": lightning_total, "solar_total": solar_total, "clearness_avg": (sum(clearness_vals) / len(clearness_vals)) if clearness_vals else None, "days_reporting": days_reporting}


def render_viz_svg(year, month, by_day, sun_times, station_name, tz, lat, lon, out_path=None, dpi=150):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.patheffects as mpeffects
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("matplotlib is required for --viz output") from exc

    C_BG = "#ffffff"
    C_CELL = "#eef4fa"
    C_WKND = "#e2e8ef"
    C_NODATA = "#f5f5f5"
    C_FUTURE = "#eef2f7"
    C_PAD = "#fafafa"
    C_GRID = "#aaaaaa"
    C_TXT = "#1a1a1a"
    C_MUTED = "#777777"
    C_TEMP_HI = "#c0392b"
    C_TEMP_LO = "#2980b9"
    C_TEMP_LN = "#34495e"
    C_TEMP_FILL = "#e07b3e"
    C_RAIN = "#2e86c1"
    C_RAIN_EDGE = "#1a5276"
    C_LIGHT = "#f1c40f"
    C_LIGHT_EDGE = "#8e44ad"

    now_date = datetime.now(timezone.utc).astimezone(tz).date()
    cal = calendar.Calendar(firstweekday=6)
    weeks = cal.monthdatescalendar(year, month)
    nrows = len(weeks)
    has_sun = any(v[0] is not None for v in sun_times.values()) if sun_times else False
    all_summaries = {d: daily_summary(entries, lat=lat, lon=lon, tz=tz) for d, entries in by_day.items()}

    cell_w = 2.4
    cell_h = 2.25
    fig_w = 7 * cell_w + 0.6
    old_fig_h = nrows * cell_h + 2.0
    footer_in = 1.3
    title_band_in = 0.10 * old_fig_h
    grid_band_in = 0.84 * old_fig_h
    fig_h = title_band_in + grid_band_in + footer_in

    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=C_BG)
    fig.suptitle(f"{calendar.month_name[month]} {year} — {station_name}", fontsize=15, fontweight="bold", y=0.99)
    ax_left = 0.015
    ax_w = 0.97
    ax_bottom = footer_in / fig_h
    ax_h = grid_band_in / fig_h
    ax = fig.add_axes([ax_left, ax_bottom, ax_w, ax_h])
    ax.set_xlim(0, 7)
    ax.set_ylim(nrows, 0)
    ax.set_axis_off()

    for c in range(7):
        fig_x = ax_left + (c + 0.5) / 7 * ax_w
        fig.text(fig_x, 0.935, calendar.day_abbr[c].upper(), ha="center", va="center", fontsize=10, fontweight="bold", color=C_TXT)

    all_temps = [t for d, entries in by_day.items() for _, o in entries if (t := num(o.get("temperature"))) is not None]
    t_lo_global = min(all_temps) - 2 if all_temps else 0
    t_hi_global = max(all_temps) + 2 if all_temps else 100
    week_temp_ranges = {}
    week_rain_max = {}
    for r, week in enumerate(weeks):
        week_temps = []
        wk_max_r = 0.01
        for d in week:
            if d.month != month:
                continue
            for _, o in by_day.get(d, []):
                t = num(o.get("temperature"))
                if t is not None:
                    week_temps.append(t)
            hr_vals = hourly_precipitation(by_day.get(d, []))
            if hr_vals:
                wk_max_r = max(wk_max_r, max(hr_vals))
        week_temp_ranges[r] = (min(week_temps) - 2, max(week_temps) + 2) if week_temps else (t_lo_global, t_hi_global)
        week_rain_max[r] = wk_max_r

    for r, week in enumerate(weeks):
        for c, d in enumerate(week):
            x, y = c, r
            in_month = d.month == month
            entries = by_day.get(d, [])
            has_data = bool(entries)
            is_future = d > now_date
            is_weekend = d.weekday() >= 5
            face = C_PAD if not in_month else (C_FUTURE if is_future else (C_WKND if is_weekend else C_CELL) if has_data else C_NODATA)
            rect = mpatches.FancyBboxPatch((x + 0.03, y + 0.03), 0.94, 0.94, boxstyle="round,pad=0.02", facecolor=face, edgecolor=C_GRID, linewidth=0.6, hatch=("////" if is_future else None))
            ax.add_patch(rect)
            date_box = mpatches.FancyBboxPatch((x + 0.06, y + 0.06), 0.24, 0.24, boxstyle="round,pad=0.02", facecolor="#ffffff", edgecolor=C_GRID, linewidth=0.7)
            ax.add_patch(date_box)
            ax.text(x + 0.18, y + 0.18, str(d.day), ha="center", va="center", fontsize=10, fontweight="bold", color=C_TXT if (in_month and has_data) else C_MUTED)
            if has_data:
                s = all_summaries[d]
                if s["temp_hi"] is not None:
                    ax.text(x + 0.90, y + 0.09, f"{s['temp_hi']:.0f}°", ha="right", va="center", fontsize=13, color=C_TEMP_HI, fontweight="bold")
                    ax.text(x + 0.90, y + 0.23, f"{s['temp_lo']:.0f}°", ha="right", va="center", fontsize=13, color=C_TEMP_LO, fontweight="bold")
                if s["rain_total"] > 0.01:
                    ax.text(x + 0.90, y + 0.36, f"{s['rain_total']:.2f}\"", ha="right", va="center", fontsize=9.5, color=C_RAIN_EDGE, fontweight="bold")
                if s["lightning_count"] > 0:
                    ax.text(x + 0.35, y + 0.18, f"⚡{s['lightning_count']}", ha="left", va="center", fontsize=9, color=C_LIGHT_EDGE, fontweight="bold")
                solar_text = f"{s['solar_mj']:.1f} MJ"
                if s.get("clearness") is not None:
                    k = s["clearness"]
                    c_col = "#27ae60" if k > 0.7 else ("#f1c40f" if k > 0.5 else ("#e67e22" if k > 0.3 else "#c0392b"))
                    solar_text += f"  (☀{k*100:.0f}%)"
                else:
                    c_col = C_MUTED
                ax.text(x + 0.50, y + 0.35, solar_text, ha="center", va="center", fontsize=8, color=c_col, fontweight="bold")
            if not in_month:
                continue
            if not has_data:
                ax.text(x + 0.5, y + 0.55, "upcoming" if is_future else "no data", ha="center", va="center", fontsize=8, color=C_MUTED, style="italic")
                continue
            grad_left = ax_left + (x + 0.03) / 7 * ax_w
            grad_w = (0.94 / 7) * ax_w
            grad_bottom = ax_bottom + (nrows - (y + 0.97)) / nrows * ax_h
            grad_h = (0.94 / nrows) * ax_h
            ax_grad = fig.add_axes([grad_left, grad_bottom, grad_w, grad_h])
            ax_grad.set_facecolor("none")
            solar_hr = []
            for h_idx in range(24):
                matches = [num(o.get("solar_radiation")) for dt, o in entries if dt.hour == h_idx]
                matches = [m for m in matches if m is not None]
                solar_hr.append(max(matches) if matches else 0.0)
            solar_arr = np.array(solar_hr, dtype=float)
            solar_norm = solar_arr / solar_arr.max() if solar_arr.max() > 0 else np.zeros_like(solar_arr)
            k = all_summaries[d].get("clearness")
            if k is not None:
                r_c = int(150 + (255 - 150) * k)
                g_c = int(165 + (220 - 165) * k)
                b_c = int(185 + (120 - 185) * k)
                base_clear = np.array([r_c, g_c, b_c]) / 255.0
                base_cloudy = np.array([180, 190, 205]) / 255.0
            else:
                base_clear = np.array([0.95, 0.85, 0.50])
                base_cloudy = np.array([0.90, 0.90, 0.90])
            grad_img = np.zeros((2, 24, 4))
            for col in range(24):
                intensity = solar_norm[col]
                color = base_cloudy * (1 - intensity) + base_clear * intensity
                grad_img[:, col, :3] = color
                grad_img[:, col, 3] = intensity ** 0.5 * 0.30
            ax_grad.imshow(grad_img, aspect="auto", extent=[0, 1, 0, 1], origin="lower", zorder=0)
            ax_grad.set_axis_off()
            rain_left = ax_left + (x + 0.08) / 7 * ax_w
            rain_w = (0.84 / 7) * ax_w
            rain_bottom = ax_bottom + (nrows - (y + 0.88)) / nrows * ax_h
            rain_h = (0.20 / nrows) * ax_h
            ax_rain = fig.add_axes([rain_left, rain_bottom, rain_w, rain_h])
            ax_rain.set_facecolor("none")
            hr_rain = hourly_precipitation(entries)
            wk_max_rain = week_rain_max.get(r, 0.5)
            top_y = 0.25 if wk_max_rain <= 0.25 else (0.5 if wk_max_rain <= 0.5 else (1.0 if wk_max_rain <= 1.0 else math.ceil(wk_max_rain * 2) / 2.0))
            ax_rain.bar(list(range(24)), hr_rain, width=0.85, color=C_RAIN, edgecolor=C_RAIN_EDGE, linewidth=0.2, alpha=0.85, zorder=3)
            ax_rain.set_xlim(-0.5, 23.5)
            ax_rain.set_ylim(0, top_y)
            if c == 0:
                ax_rain.set_yticks([top_y])
                ax_rain.set_yticklabels([f"{top_y:g}\""], fontsize=6, color=C_MUTED, fontweight="bold")
                ax_rain.tick_params(axis="y", length=2, pad=1, colors=C_MUTED, left=True)
            else:
                ax_rain.set_yticks([])
                ax_rain.tick_params(axis="y", left=False)
            ax_rain.set_xticks([])
            for spine in ax_rain.spines.values():
                spine.set_visible(False)
        wk_lo, wk_hi = week_temp_ranges.get(r, (t_lo_global, t_hi_global))
        week_left = ax_left + (0.08 / 7) * ax_w
        week_w = ((6.92 - 0.08) / 7) * ax_w
        week_bottom = ax_bottom + (nrows - (r + 0.64)) / nrows * ax_h
        week_h = (0.26 / nrows) * ax_h
        ax_temp = fig.add_axes([week_left, week_bottom, week_w, week_h])
        ax_temp.set_facecolor("none")
        week_hours = list(range(7 * 24))
        week_y = [float("nan")] * (7 * 24)
        lightning_markers = []
        for day_idx, d in enumerate(week):
            if d.month != month or d > now_date:
                continue
            entries = by_day.get(d, [])
            if not entries:
                continue
            offset = day_idx * 24
            if has_sun:
                sr, ss = sun_times.get(d, (None, None))
                if sr and ss:
                    sr_hr = offset + sr.hour + sr.minute / 60.0
                    ss_hr = offset + ss.hour + ss.minute / 60.0
                    ax_temp.axvspan(sr_hr, ss_hr, alpha=0.12, color="#f39c12", zorder=0)
                    ax_temp.axvline(sr_hr, color="#f39c12", linewidth=0.5, alpha=0.5, zorder=1)
                    ax_temp.axvline(ss_hr, color="#f39c12", linewidth=0.5, alpha=0.5, zorder=1)
            hrs = hourly_temps(entries)
            temp_by_hour = {h: t for h, t in hrs if t is not None}
            s_day = all_summaries.get(d)
            if s_day and s_day["lightning_count"] > 0:
                for hr in s_day.get("lightning_active_hours", set()):
                    ax_temp.axvspan(offset + hr, offset + hr + 1, alpha=0.25, color=C_LIGHT, zorder=2)
                    y_pos = temp_by_hour.get(hr)
                    if y_pos is None and temp_by_hour:
                        nearest_h = min(temp_by_hour, key=lambda hh: abs(hh - hr))
                        y_pos = temp_by_hour[nearest_h]
                    if y_pos is not None:
                        lightning_markers.append((offset + hr + 0.5, y_pos, s_day.get("lightning_by_hour", {}).get(hr, 0)))
            for h, t in temp_by_hour.items():
                week_y[offset + h] = t
        for day_idx in range(1, 7):
            ax_temp.axvline(day_idx * 24, color=C_GRID, linewidth=0.4, alpha=0.4, zorder=1)
        valid_mask = [not math.isnan(v) for v in week_y]
        if sum(valid_mask) >= 2:
            y_arr = np.array(week_y)
            ax_temp.fill_between(week_hours, [wk_lo] * len(week_hours), y_arr, alpha=0.12, color=C_TEMP_FILL, zorder=3)
            ax_temp.plot(week_hours, y_arr, "-", color=C_TEMP_LN, linewidth=1.0, zorder=4)
            valid_x = [h for h, ok in zip(week_hours, valid_mask) if ok]
            valid_y = [v for v, ok in zip(week_y, valid_mask) if ok]
            ax_temp.plot(valid_x, valid_y, "o", color=C_TEMP_LN, markersize=1.2, zorder=5)
        for x_hour, y_temp, strike_ct in lightning_markers:
            ax_temp.text(x_hour, y_temp, "⚡", fontsize=11, ha="center", va="center", color=C_LIGHT_EDGE, zorder=6, path_effects=[mpeffects.withStroke(linewidth=1.5, foreground=C_LIGHT)])
        ax_temp.set_xlim(-0.5, 7 * 24 - 0.5)
        ax_temp.set_ylim(wk_lo, wk_hi)
        ax_temp.set_yticks([wk_lo, wk_hi])
        ax_temp.set_yticklabels([f"{wk_lo:.0f}°", f"{wk_hi:.0f}°"], fontsize=6, color=C_MUTED, fontweight="bold")
        ax_temp.tick_params(axis="y", length=2, pad=1, colors=C_MUTED, left=True)
        ax_temp.set_xticks([])
        for spine in ax_temp.spines.values():
            spine.set_visible(False)

    ms = compute_month_summary(by_day, all_summaries)
    def fmt_extreme(pair):
        if not pair:
            return "—"
        v, d = pair
        return f"{v:.0f}°F ({d.strftime('%b')} {d.day})"
    def fmt_avg(v):
        return f"{v:.1f}°F" if v is not None else "—"
    solar_str = f'{ms["solar_total"]:.1f} MJ'
    if ms["clearness_avg"] is not None:
        solar_str += f'  (avg clearness {ms["clearness_avg"] * 100:.0f}%)'
    footer_cols = [("TEMPERATURE", [("Tmax", fmt_extreme(ms["temp_max"])), ("Tmin", fmt_extreme(ms["temp_min"])), ("Tavg", fmt_avg(ms["temp_avg"]))]), ("DEWPOINT", [("Tdmax", fmt_extreme(ms["dew_max"])), ("Tdmin", fmt_extreme(ms["dew_min"])), ("Tdavg", fmt_avg(ms["dew_avg"]))]), ("DAILY EXTREMES", [("Warm low", fmt_extreme(ms["warm_low"])), ("Cool high", fmt_extreme(ms["cool_high"])), ("", "")]), ("PRECIP / LTNG", [("Total", f'{ms["rain_total"]:.2f}" on {ms["rain_days"]} day(s)'), ("Strikes", f'{ms["lightning_total"]}' if ms["lightning_total"] else "—"), ("", "")]), ("SOLAR / COVERAGE", [("Energy", solar_str), ("Reporting", f'{ms["days_reporting"]} of {calendar.monthrange(year, month)[1]} day(s)'), ("", "")])]
    ax_foot = fig.add_axes([ax_left, 0.0, ax_w, ax_bottom])
    ax_foot.set_xlim(0, 1)
    ax_foot.set_ylim(0, 1)
    ax_foot.set_axis_off()
    note_frac = 0.16
    box_bottom = note_frac + 0.02
    ax_foot.add_patch(mpatches.FancyBboxPatch((0.0, box_bottom), 1.0, 0.97 - box_bottom, boxstyle="round,pad=0.01", facecolor=C_CELL, edgecolor=C_GRID, linewidth=0.7, transform=ax_foot.transAxes))
    ax_foot.text(0.015, 0.885, "MONTH SUMMARY", fontsize=9.5, fontweight="bold", color=C_TXT, va="center")
    col_xs = [0.02, 0.216, 0.412, 0.608, 0.804]
    label_w = 0.075
    heading_y = 0.735
    row_ys = [0.575, 0.41, 0.245]
    for cx, (heading, rows_) in zip(col_xs, footer_cols):
        ax_foot.text(cx, heading_y, heading, fontsize=6.5, fontweight="bold", color=C_MUTED, va="center")
        for ry, (label, val) in zip(row_ys, rows_):
            if not label:
                continue
            ax_foot.text(cx, ry, label, fontsize=7.5, color=C_MUTED, fontweight="bold", va="center")
            ax_foot.text(cx + label_w, ry, val, fontsize=8.5, color=C_TXT, va="center")
    ax_foot.text(0.015, note_frac / 2, "Compact monthly weather summary · Daylight band integrated into temp sparkline · Hourly rain & ⚡ lightning overlay", ha="left", va="center", fontsize=7, family="monospace", color=C_TXT)
    buf = __import__("io").BytesIO()
    fig.savefig(buf, format="svg", dpi=dpi, facecolor=C_BG)
    svg = buf.getvalue().decode("utf-8")
    plt.close(fig)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(svg)
    return svg


def resolve_station_ids(names, in_db, dev):
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
    ap = argparse.ArgumentParser(description="Unified monthly weather calendar with table and visual formats")
    ap.add_argument("month", nargs="?", help="YYYY-MM (default: current month)")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to weather_archive.db")
    ap.add_argument("--table", help="observation table name (default: auto-detect)")
    ap.add_argument("--data-col", help="payload column holding (zlib) JSON")
    ap.add_argument("--inspect", action="store_true", help="dump tables/devices/sample row and exit")
    ap.add_argument("--station", "--stations", dest="stations", nargs="+", metavar="ID", help="restrict to these devices (MAC or name)")
    ap.add_argument("--dewpoint-c", nargs="+", metavar="ID", help="devices whose stored dewpoint is °C (convert to °F)")
    ap.add_argument("--out", "-o", help="output path (default weather_YYYY-MM.html/.txt)")
    ap.add_argument("--format", "-f", choices=("html", "text", "svg"), default="html")
    ap.add_argument("--tz", default=DEFAULT_TZ, help="IANA zone for day boundaries")
    ap.add_argument("--first-weekday", choices=("sun", "mon"), default="sun")
    ap.add_argument("--title", default="Weather Station Summary")
    ap.add_argument("--open", action="store_true", help="open the HTML result in a browser")
    ap.add_argument("--calendar", action="store_true", help="produce calendar format (default)")
    ap.add_argument("--viz", action="store_true", help="produce visual calendar format (compact)")
    ap.add_argument("--both", action="store_true", help="produce both formats side-by-side")
    args = ap.parse_args(argv)

    if args.calendar and not args.viz and not args.both:
        args.format = "html"
    elif args.viz and not args.calendar and not args.both:
        args.format = "svg"
    elif args.both:
        args.format = "html"
    elif not args.calendar and not args.viz and not args.both:
        args.calendar = True
        args.format = "html"

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
            sys.exit("error: could not find an observation table.\n" "What's in this file:\n" + describe_db(conn) + "\nRun with --table NAME (and --data-col COL if packed JSON), or --inspect for a deeper dump.")

        dev = load_device_names(conn)
        scol, tbl = info["station_col"], info["table"]
        in_db = [str(r[0]) for r in conn.execute(f'SELECT DISTINCT "{scol}" FROM "{tbl}" ORDER BY 1')]
        if not in_db:
            sys.exit("error: no devices found in the observation table.")

        stations = in_db
        if args.stations:
            sel, missing = resolve_station_ids(args.stations, in_db, dev)
            for m_ in missing:
                print(f"warning: device '{m_}' not found (known: {', '.join(in_db)} / {', '.join(sorted(dev.values()))})", file=sys.stderr)
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
        day_data, month_vals, month_ext = load_month(conn, info, y, m, args.tz, stations, dew_c)
        if not day_data:
            sys.exit(f"no observations for {calendar.month_name[m]} {y} in {args.db}.\nRun --inspect to see the table's actual time range.")

        st_colors = {s: STATION_COLORS[i % len(STATION_COLORS)] for i, s in enumerate(sorted(stations))}
        firstweekday = {"sun": 6, "mon": 0}[args.first_weekday]

        if args.viz or args.both:
            visual_station = None
            if args.stations:
                visual_station = stations[0]
            elif "station_obs" in list_tables(conn):
                visual_station = discover_stations(conn)[0] if discover_stations(conn) else None
            else:
                visual_station = stations[0]
            if not visual_station:
                visual_station = DEFAULT_VIS_STATION
            lat = os.environ.get("WEATHER_LAT")
            lon = os.environ.get("WEATHER_LON")
            lat_f = float(lat) if lat is not None else None
            lon_f = float(lon) if lon is not None else None
            if lat_f is None or lon_f is None:
                lat_f = float(os.environ["WEATHER_LAT"]) if "WEATHER_LAT" in os.environ else None
                lon_f = float(os.environ["WEATHER_LON"]) if "WEATHER_LON" in os.environ else None
            by_day_visual = load_month_data(conn, visual_station, y, m, tz)
            sun_times = all_sun_times(lat_f, lon_f, tz, y, m) if lat_f is not None and lon_f is not None else {}
            if args.both:
                svg_text = render_viz_svg(y, m, by_day_visual, sun_times, display.get(visual_station, visual_station), tz, lat_f, lon_f, None, 150)
                svg_b64 = base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
                svg_data = f"data:image/svg+xml;base64,{svg_b64}"
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
        .viz {{ text-align: center; }}
        img {{ max-width: 100%; height: auto; border: 1px solid #ddd; }}
    </style>
</head>
<body>
    <h1>Weather Station Comparison — {calendar.month_name[m]} {y} (Both Formats)</h1>
    <div class="container">
        <div class="section">
            <h2>Detailed Calendar (Table Format)</h2>
{render_html(y, m, day_data, month_vals, month_ext, stations, display, st_colors, args.tz, firstweekday, f"Weather Summary — {calendar.month_name[m]} {y}")}
        </div>
        <div class="section">
            <h2>Compact Visual Calendar</h2>
            <div class="viz"><img src="{svg_data}" alt="Visual calendar"></div>
        </div>
    </div>
</body>
</html>
"""
            elif args.viz:
                out_name = f"calendar_{y}-{m:02d}.svg"
                if args.out:
                    out_path = args.out
                else:
                    script_dir = os.path.dirname(os.path.abspath(__file__))
                    results_dir = os.path.join(script_dir, "results")
                    os.makedirs(results_dir, exist_ok=True)
                    out_path = os.path.join(results_dir, out_name)
                render_viz_svg(y, m, by_day_visual, sun_times, display.get(visual_station, visual_station), tz, lat_f, lon_f, out_path, 150)
                print(f"wrote {out_path}  ({len(stations)} device(s), {len({d for (_s, d) in day_data})} day(s) with data)")
                if args.open:
                    webbrowser.open("file://" + os.path.abspath(out_path))
                return

        if args.both:
            content = content
            out_name = f"weather_{y}-{m:02d}_both.html"
        else:
            if args.format == "html":
                content = render_html(y, m, day_data, month_vals, month_ext, stations, display, st_colors, args.tz, firstweekday, args.title)
                out_name = f"weather_{y}-{m:02d}.html"
            else:
                content = render_text(y, m, day_data, month_vals, month_ext, stations, display, args.tz, firstweekday, args.title)
                out_name = f"weather_{y}-{m:02d}.txt"

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
        sys.exit(f"SQLite error: {e}\n\nWhat's actually in this file:\n{dump}\nRun with --inspect for a sample row, or --table/--data-col to override.")
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
