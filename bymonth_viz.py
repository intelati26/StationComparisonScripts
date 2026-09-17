#!/usr/bin/env python3
"""
bymonth_viz.py — Compact monthly weather calendar with integrated mini-charts.

Each day cell contains:
  - Delineated date box in top-left
  - Enlarged high/low temperatures + rain total + ⚡ lightning count
  - Hourly temperature sparkline with integrated sunrise/sunset background shading
  - Hourly precipitation bar chart (24 bins) with per-week row scaling & y-axis labels
  - Solar energy stats (RAW MJ + clearness percentage)
  - Cloudiness-tinted background gradient

Stdlib + matplotlib only. Python 3.9+.
"""

import argparse
import calendar as calmod
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, time, timezone, timedelta, date

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    # Explicit script-relative path, not bare load_dotenv()'s frame-based
    # auto-detection -- same "resolve against THIS SCRIPT'S directory, not
    # the shell's cwd (or whatever invoked us)" fix as WEATHER_DB_PATH
    # below, applied to .env discovery itself so a WEATHER_LAT/WEATHER_LON
    # that's correctly set in .env next to this script can't silently go
    # unfound just because of how/where it was launched from.
    load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))
except ImportError:
    print("note: python-dotenv not installed; any .env file is ignored -- "
          "only real environment variables are used. "
          "Install with: pip install python-dotenv", file=sys.stderr)

_db_env = os.environ.get("WEATHER_DB_PATH", "weather_archive.db")
# Relative WEATHER_DB_PATH (including the bare default) resolves against
# THIS SCRIPT'S directory, not the shell's current working directory --
# matches the identical fix already applied to tempAnalysis.py, plotter.py,
# and bymonth.py for the same "sqlite3.connect() silently creates an empty
# file at the wrong cwd-relative path" failure mode.
DB_PATH = (_db_env if os.path.isabs(_db_env)
          else os.path.join(_SCRIPT_DIR, _db_env))

DEFAULT_STATION = "G6964"  # the only station with rain/solar/lightning data
SOLAR_CONSTANT = 1361.0  # W/m²


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
# SUNRISE / SUNSET — NOAA algorithm
# ═══════════════════════════════════════════════════════════════════════════

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
    Mobliq = 23 + (26 + ((21.448 - Jcent * (46.815 + Jcent *
                    (0.00059 - Jcent * 0.001813)))) / 60) / 60
    obliq = Mobliq + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * Jcent))
    vary = math.tan(math.radians(obliq / 2)) ** 2

    Seqcent = (math.sin(math.radians(Manom)) *
               (1.914602 - Jcent * (0.004817 + 0.000014 * Jcent)) +
               math.sin(math.radians(2 * Manom)) *
               (0.019993 - 0.000101 * Jcent) +
               math.sin(math.radians(3 * Manom)) * 0.000289)
    Struelong = Mlong + Seqcent
    Sapplong = Struelong - 0.00569 - 0.00478 * math.sin(
        math.radians(125.04 - 1934.136 * Jcent))
    declination = math.degrees(math.asin(
        math.sin(math.radians(obliq)) * math.sin(math.radians(Sapplong))))

    eqtime = 4 * math.degrees(
        vary * math.sin(2 * math.radians(Mlong)) -
        2 * Eccent * math.sin(math.radians(Manom)) +
        4 * Eccent * vary * math.sin(math.radians(Manom)) *
        math.cos(2 * math.radians(Mlong)) -
        0.5 * vary * vary * math.sin(4 * math.radians(Mlong)) -
        1.25 * Eccent * Eccent * math.sin(2 * math.radians(Manom)))

    try:
        hourangle = math.degrees(math.acos(
            math.cos(math.radians(90.833)) /
            (math.cos(math.radians(lat)) * math.cos(math.radians(declination))) -
            math.tan(math.radians(lat)) * math.tan(math.radians(declination))))
    except ValueError:
        return None, None

    solarnoon = (720 - 4 * lon - eqtime + tz_hours * 60) / 1440
    sunrise_frac = solarnoon - hourangle * 4 / 1440
    sunset_frac = solarnoon + hourangle * 4 / 1440

    base = datetime(d.year, d.month, d.day, tzinfo=tz)
    return (base + timedelta(days=sunrise_frac),
            base + timedelta(days=sunset_frac))


def all_sun_times(lat, lon, tz, year, month):
    result = {}
    for day in range(1, calmod.monthrange(year, month)[1] + 1):
        d = date(year, month, day)
        result[d] = sunrise_sunset(lat, lon, tz, d)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# BIRD CLEAR SKY MODEL
# ═══════════════════════════════════════════════════════════════════════════

def solar_declination(day_of_year):
    return 23.45 * math.sin(math.radians(360 * (284 + day_of_year) / 365))


def equation_of_time(day_of_year):
    B = math.radians(360 * (day_of_year - 1) / 365)
    return 229.18 * (0.000075 + 0.001868 * math.cos(B) -
                     0.032077 * math.sin(B) -
                     0.014615 * math.cos(2 * B) -
                     0.04089 * math.sin(2 * B))


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

    cos_zenith = (math.sin(lat_rad) * math.sin(decl_rad) +
                  math.cos(lat_rad) * math.cos(decl_rad) * math.cos(ha_rad))
    cos_zenith = max(-1, min(1, cos_zenith))
    return math.degrees(math.acos(cos_zenith))


def _earth_sun_distance_factor(day_of_year):
    B = math.radians(360 * (day_of_year - 1) / 365)
    return (1.00011 + 0.034221 * math.cos(B) + 0.00128 * math.sin(B)
            + 0.000719 * math.cos(2 * B) + 0.000077 * math.sin(2 * B))


def _air_mass(zenith_deg, altitude_m=0):
    z = math.radians(zenith_deg)
    am = 1.0 / (math.cos(z) + 0.50572 * (96.07995 - zenith_deg) ** -1.6364)
    p_ratio = math.exp(-altitude_m / 8400.0)
    return am * p_ratio, am


def bird_clearsky_ghi(lat, lon, tz, dt, ozone_cm=0.3, water_cm=1.5,
                      tau_a_380=0.1, tau_a_500=0.15,
                      ground_albedo=0.2, altitude_m=0):
    zenith = solar_zenith(lat, lon, tz, dt)
    if zenith >= 87.9:
        return 0.0

    doy = dt.date().timetuple().tm_yday
    e0 = _earth_sun_distance_factor(doy)
    dni_extra = SOLAR_CONSTANT * e0

    am_p, am = _air_mass(zenith, altitude_m)

    T_R = math.exp(-0.0903 * am_p ** 0.84 * (1 + am_p - am_p ** 1.01))
    x_O = ozone_cm * am
    T_O3 = (1 - 0.1611 * x_O * (1 + 139.48 * x_O) ** -0.3035
            - 0.002715 * x_O / (1 + 0.044 * x_O + 0.0003 * x_O ** 2))
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
    dhi_clear = max(0, dni_extra * cos_z * 0.79 * T_O3 * T_g * T_W * T_AA
                    * dhi_num / dhi_den)
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


# ═══════════════════════════════════════════════════════════════════════════
# DATA LOADING & HOURLY AGGREGATION
# ═══════════════════════════════════════════════════════════════════════════

def discover_stations(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT station FROM station_obs ORDER BY station")]


def load_month_data(conn, station, year, month, tz):
    """Returns {date: [(local_datetime, rec_dict), ...]} where rec_dict
    uses CANONICAL station_obs column names (temperature, rain_daily,
    solar_radiation, lightning_day, lightning_hour, lightning_distance) --
    NOT raw Ambient API field names (tempf, dailyrainin, solarradiation)
    like the old JSON-blob schema this was originally written against.
    Downstream functions (hourly_temps/hourly_precipitation/daily_summary)
    read these canonical names.

    lightning_hour is a separately-populated rolling per-hour strike count
    (distinct from lightning_day, the cumulative daily total) -- this is
    what gives genuine per-hour strike timing, rather than inferring
    "active hours" indirectly from when lightning_day happens to increase.
    """
    start = datetime(year, month, 1, tzinfo=tz)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    end = datetime(ny, nm, 1, tzinfo=tz)
    start_utc = start.astimezone(timezone.utc).isoformat()
    end_utc = end.astimezone(timezone.utc).isoformat()

    try:
        rows = conn.execute(
            "SELECT timestamp, temperature, dewpoint, rain_daily, solar_radiation, "
            "lightning_day, lightning_hour, lightning_distance FROM station_obs "
            "WHERE station = ? AND timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp ASC",
            (station, start_utc, end_utc)).fetchall()
        has_dewpoint = True
    except sqlite3.OperationalError:
        # Some station_obs tables were created without a dewpoint column --
        # degrade gracefully (Tdmin/Tdmax/Tdavg just won't be available)
        # rather than crashing the whole render.
        print("note: station_obs has no 'dewpoint' column -- monthly "
              "dewpoint stats will be skipped.", file=sys.stderr)
        rows = conn.execute(
            "SELECT timestamp, temperature, rain_daily, solar_radiation, "
            "lightning_day, lightning_hour, lightning_distance FROM station_obs "
            "WHERE station = ? AND timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp ASC",
            (station, start_utc, end_utc)).fetchall()
        has_dewpoint = False

    by_day = defaultdict(list)
    for row in rows:
        if has_dewpoint:
            (ts, temperature, dewpoint, rain_daily, solar_radiation,
             lightning_day, lightning_hour, lightning_distance) = row
        else:
            (ts, temperature, rain_daily, solar_radiation,
             lightning_day, lightning_hour, lightning_distance) = row
            dewpoint = None
        utc_dt = datetime.fromisoformat(ts)
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        local = utc_dt.astimezone(tz)
        d = local.date()
        if d.year == year and d.month == month:
            rec = {
                "temperature": temperature,
                "dewpoint": dewpoint,
                "rain_daily": rain_daily,
                "solar_radiation": solar_radiation,
                "lightning_day": lightning_day,
                "lightning_hour": lightning_hour,
                "lightning_distance": lightning_distance,
            }
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

    # lightning_day (cumulative daily total) still drives the daily badge
    # count -- it's the authoritative end-of-day total. lightning_hour is
    # used separately, directly, for WHEN strikes happened: it's a
    # per-observation rolling hourly count, so this reads it straight
    # rather than inferring timing indirectly from when lightning_day
    # happens to tick up (which is what this used to do, and which is
    # exactly the "no timed lightning" gap being fixed here).
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
    if lat is not None and lon is not None and tz is not None:
        if entries:
            d = entries[0][0].astimezone(tz).date()
            ideal_mj = daily_clearsky_mj(lat, lon, tz, d)
            if ideal_mj is not None and ideal_mj > 0.5 and solar_j > 0:
                clearness = min(solar_j / 1e6 / ideal_mj, 1.0)

    return {
        "temp_hi": max(temps) if temps else None,
        "temp_lo": min(temps) if temps else None,
        "dew_hi": max(dews) if dews else None,
        "dew_lo": min(dews) if dews else None,
        "rain_total": rain_total,
        "solar_mj": solar_j / 1e6,
        "lightning_count": lightning_count,
        "lightning_active_hours": lightning_active_hours,
        "lightning_by_hour": dict(lightning_by_hour),
        "clearness": clearness,
        "ideal_mj": ideal_mj,
        "n_obs": len(entries),
    }


def compute_month_summary(by_day, all_summaries):
    """Whole-month rollup for the footer: Tmax/Tmin/Tavg, Tdmax/Tdmin/Tdavg,
    total precip, lightning strikes, and solar energy -- built from the
    same by_day / daily_summary() data already used for the day cells, so
    it stays consistent with what's drawn above it.

    Tmax/Tmin/Tdmax/Tdmin are the extreme of each day's daily_summary()
    hi/lo (so the returned date is the day that extreme actually occurred
    on); Tavg/Tdavg are pooled means over every individual observation in
    the month (each reading weighted equally), not an average of daily
    averages.

    warm_low/cool_high are a different, easy-to-conflate pair of extremes:
    not "the hottest/coldest reading all month" (that's temp_max/temp_min
    above), but the extreme of each day's OWN low/high -- e.g. "we didn't
    get below 80F last night" is a candidate warm_low; warm_low is the max
    of every day's temp_lo, cool_high is the min of every day's temp_hi.
    """
    temp_max = temp_min = None   # (value, date)
    dew_max = dew_min = None
    warm_low = None    # highest daily LOW all month -- "never got below X° last night"
    cool_high = None   # lowest daily HIGH all month -- the day that never warmed up
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
        # Extremes of the DAILY extremes, not of individual readings: the
        # warmest a night's low ever got (temp_lo maxed across days), and
        # the coolest a day's high ever got (temp_hi minned across days).
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

    return {
        "temp_max": temp_max, "temp_min": temp_min,
        "warm_low": warm_low, "cool_high": cool_high,
        "temp_avg": (sum(pooled_temps) / len(pooled_temps)) if pooled_temps else None,
        "dew_max": dew_max, "dew_min": dew_min,
        "dew_avg": (sum(pooled_dews) / len(pooled_dews)) if pooled_dews else None,
        "rain_total": rain_total,
        "rain_days": rain_days,
        "lightning_total": lightning_total,
        "solar_total": solar_total,
        "clearness_avg": (sum(clearness_vals) / len(clearness_vals)) if clearness_vals else None,
        "days_reporting": days_reporting,
    }


# ═══════════════════════════════════════════════════════════════════════════
# COMPACT CALENDAR RENDERER
# ═══════════════════════════════════════════════════════════════════════════

def render_calendar(year, month, by_day, sun_times, station_name, tz,
                    lat, lon, out_path, dpi):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.patheffects as mpeffects
    import numpy as np

    C_BG       = "#ffffff"
    C_CELL     = "#eef4fa"
    C_WKND     = "#e2e8ef"
    C_NODATA   = "#f5f5f5"
    C_FUTURE   = "#eef2f7"
    C_PAD      = "#fafafa"
    C_GRID     = "#aaaaaa"
    C_TXT      = "#1a1a1a"
    C_MUTED    = "#777777"
    C_TEMP_HI  = "#c0392b"
    C_TEMP_LO  = "#2980b9"
    C_TEMP_LN  = "#34495e"
    C_TEMP_FILL= "#e07b3e"
    C_RAIN     = "#2e86c1"
    C_RAIN_EDGE= "#1a5276"
    C_LIGHT    = "#f1c40f"
    C_LIGHT_EDGE="#8e44ad"

    now_date = datetime.now(timezone.utc).astimezone(tz).date()
    cal = calmod.Calendar(firstweekday=6)
    weeks = cal.monthdatescalendar(year, month)
    nrows = len(weeks)

    has_sun = any(v[0] is not None for v in sun_times.values()) if sun_times else False
    all_summaries = {d: daily_summary(entries, lat=lat, lon=lon, tz=tz) for d, entries in by_day.items()}

    cell_w = 2.4
    cell_h = 2.25
    fig_w = 7 * cell_w + 0.6
    old_fig_h = nrows * cell_h + 2.0        # calendar+title, as before

    # Extra fixed height (inches), added at the BOTTOM only, for the
    # month-summary footer -- the table AND the small caption note both
    # live packed together in this one band now (see ax_foot below), so
    # there's no separate empty margin between them and everything above
    # keeps the exact same absolute size it always had.
    footer_in = 1.3
    title_band_in = 0.10 * old_fig_h
    grid_band_in = 0.84 * old_fig_h
    fig_h = title_band_in + grid_band_in + footer_in

    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=C_BG)
    fig.suptitle(f"{calmod.month_name[month]} {year} — {station_name}",
                 fontsize=15, fontweight="bold", y=0.99)

    ax_left = 0.015
    ax_w = 0.97
    ax_bottom = footer_in / fig_h
    ax_h = grid_band_in / fig_h
    ax = fig.add_axes([ax_left, ax_bottom, ax_w, ax_h])
    ax.set_xlim(0, 7)
    ax.set_ylim(nrows, 0)
    ax.set_axis_off()

    # Weekday header drawn via fig.text() at a FIXED figure-fraction y,
    # independent of nrows -- the old version placed this via ax.text() at
    # a small negative data-coordinate offset from the axes' inverted
    # y-limit, which put it at a different actual figure position
    # depending on nrows and collided with the title for typical 5-row
    # months. This fixed position guarantees consistent clearance.
    for c in range(7):
        fig_x = ax_left + (c + 0.5) / 7 * ax_w
        fig.text(fig_x, 0.935, calmod.day_abbr[c].upper(),
                 ha="center", va="center", fontsize=10, fontweight="bold",
                 color=C_TXT)

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

            if not in_month:
                face = C_PAD
            elif is_future:
                face = C_FUTURE
            elif has_data:
                face = C_WKND if is_weekend else C_CELL
            else:
                face = C_NODATA

            rect = mpatches.FancyBboxPatch(
                (x + 0.03, y + 0.03), 0.94, 0.94,
                boxstyle="round,pad=0.02",
                facecolor=face, edgecolor=C_GRID, linewidth=0.6,
                hatch=("////" if is_future else None))
            ax.add_patch(rect)

            # ── Delineated box around date number ───────────────────────────
            date_box = mpatches.FancyBboxPatch(
                (x + 0.06, y + 0.06), 0.24, 0.24,
                boxstyle="round,pad=0.02",
                facecolor="#ffffff", edgecolor=C_GRID, linewidth=0.7)
            ax.add_patch(date_box)
            ax.text(x + 0.18, y + 0.18, str(d.day),
                    ha="center", va="center", fontsize=10,
                    fontweight="bold",
                    color=C_TXT if (in_month and has_data) else C_MUTED)

            if has_data:
                s = all_summaries[d]

                # Larger High/Low Temperatures & Rain Total
                if s["temp_hi"] is not None:
                    ax.text(x + 0.90, y + 0.09, f"{s['temp_hi']:.0f}°",
                            ha="right", va="center", fontsize=13,
                            color=C_TEMP_HI, fontweight="bold")
                    ax.text(x + 0.90, y + 0.23, f"{s['temp_lo']:.0f}°",
                            ha="right", va="center", fontsize=13,
                            color=C_TEMP_LO, fontweight="bold")

                # Rain total with bold styling
                if s["rain_total"] > 0.01:
                    ax.text(x + 0.90, y + 0.36, f"{s['rain_total']:.2f}\"",
                            ha="right", va="center", fontsize=9.5,
                            color=C_RAIN_EDGE, fontweight="bold")

                # Lightning symbol + count if present
                if s["lightning_count"] > 0:
                    ax.text(x + 0.35, y + 0.18, f"⚡{s['lightning_count']}",
                            ha="left", va="center", fontsize=9,
                            color=C_LIGHT_EDGE, fontweight="bold")

                # Solar & Clearness stats (Combined MJ + %)
                solar_text = f"{s['solar_mj']:.1f} MJ"
                if s.get("clearness") is not None:
                    k = s["clearness"]
                    c_col = "#27ae60" if k > 0.7 else ("#f1c40f" if k > 0.5 else ("#e67e22" if k > 0.3 else "#c0392b"))
                    solar_text += f"  (☀{k*100:.0f}%)"
                else:
                    c_col = C_MUTED

                ax.text(x + 0.50, y + 0.35, solar_text,
                        ha="center", va="center", fontsize=8,
                        color=c_col, fontweight="bold")

            if not in_month:
                continue
            if not has_data:
                ax.text(x + 0.5, y + 0.55, "upcoming" if is_future else "no data",
                        ha="center", va="center", fontsize=8, color=C_MUTED, style="italic")
                continue

            # ── Cloudiness-tinted background gradient ───────────────────────
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

            k = s.get("clearness")
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

            # ── MINI CHART: Hourly Precipitation Bar Chart + Restored Y-Axis ──
            rain_left = ax_left + (x + 0.08) / 7 * ax_w
            rain_w = (0.84 / 7) * ax_w
            rain_bottom = ax_bottom + (nrows - (y + 0.88)) / nrows * ax_h
            rain_h = (0.20 / nrows) * ax_h

            ax_rain = fig.add_axes([rain_left, rain_bottom, rain_w, rain_h])
            ax_rain.set_facecolor("none")

            hr_rain = hourly_precipitation(entries)
            wk_max_rain = week_rain_max.get(r, 0.5)
            top_y = 0.25 if wk_max_rain <= 0.25 else (0.5 if wk_max_rain <= 0.5 else (1.0 if wk_max_rain <= 1.0 else math.ceil(wk_max_rain * 2) / 2.0))

            ax_rain.bar(list(range(24)), hr_rain, width=0.85, color=C_RAIN,
                        edgecolor=C_RAIN_EDGE, linewidth=0.2, alpha=0.85, zorder=3)

            ax_rain.set_xlim(-0.5, 23.5)
            ax_rain.set_ylim(0, top_y)

            # Restored weekly precipitation Y-axis on the leftmost cell
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

        # ── CONTINUOUS PER-WEEK TEMP GRAPH: one line spanning all 7 days ──
        # Replaces the old isolated per-day sparklines (each reset to hours
        # 0-23 with no connection to neighboring days). This spans the full
        # week row's width in the SAME vertical band the old per-day
        # sparklines occupied, using "hours since week start" (0-167) as x,
        # so the line flows continuously day-to-day. Gaps (out-of-month
        # padding, future days, or days with no data) are left as NaN,
        # which matplotlib naturally renders as a break in the line rather
        # than a misleading interpolated bridge across missing data.
        wk_lo, wk_hi = week_temp_ranges.get(r, (t_lo_global, t_hi_global))

        week_left = ax_left + (0.08 / 7) * ax_w
        week_w = ((6.92 - 0.08) / 7) * ax_w
        week_bottom = ax_bottom + (nrows - (r + 0.64)) / nrows * ax_h
        week_h = (0.26 / nrows) * ax_h

        ax_temp = fig.add_axes([week_left, week_bottom, week_w, week_h])
        ax_temp.set_facecolor("none")

        week_hours = list(range(7 * 24))
        week_y = [float("nan")] * (7 * 24)
        lightning_markers = []  # (x_hour, y_temp, strike_count) for this week

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
                    # Background band still shows the general window...
                    ax_temp.axvspan(offset + hr, offset + hr + 1, alpha=0.25,
                                    color=C_LIGHT, zorder=2)
                    # ...but the actual marker at the specific hour is what
                    # makes the timing genuinely readable rather than just
                    # a diffuse tinted region.
                    y_pos = temp_by_hour.get(hr)
                    if y_pos is None and temp_by_hour:
                        # No exact reading that hour -- fall back to the
                        # nearest available hour's temp so the marker still
                        # lands on the line rather than floating in space.
                        nearest_h = min(temp_by_hour, key=lambda hh: abs(hh - hr))
                        y_pos = temp_by_hour[nearest_h]
                    if y_pos is not None:
                        strike_ct = s_day.get("lightning_by_hour", {}).get(hr, 0)
                        lightning_markers.append((offset + hr + 0.5, y_pos, strike_ct))

            for h, t in temp_by_hour.items():
                week_y[offset + h] = t

        # Day-boundary dividers so the continuous strip still reads as
        # one-cell-per-day at a glance, matching the calendar grid above it
        for day_idx in range(1, 7):
            ax_temp.axvline(day_idx * 24, color=C_GRID, linewidth=0.4,
                            alpha=0.4, zorder=1)

        valid_mask = [not math.isnan(v) for v in week_y]
        if sum(valid_mask) >= 2:
            y_arr = np.array(week_y)
            ax_temp.fill_between(week_hours, [wk_lo] * len(week_hours), y_arr,
                                 alpha=0.12, color=C_TEMP_FILL, zorder=3)
            ax_temp.plot(week_hours, y_arr, "-", color=C_TEMP_LN,
                        linewidth=1.0, zorder=4)
            valid_x = [h for h, ok in zip(week_hours, valid_mask) if ok]
            valid_y = [v for v, ok in zip(week_y, valid_mask) if ok]
            ax_temp.plot(valid_x, valid_y, "o", color=C_TEMP_LN,
                        markersize=1.2, zorder=5)

        for x_hour, y_temp, strike_ct in lightning_markers:
            # An actual bolt glyph (same ⚡ used on the day-cell badges)
            # instead of a plain "*" star marker -- a light halo behind it
            # keeps it legible over both the fill and the temp line.
            ax_temp.text(x_hour, y_temp, "⚡", fontsize=11, ha="center",
                        va="center", color=C_LIGHT_EDGE, zorder=6,
                        path_effects=[mpeffects.withStroke(
                            linewidth=1.5, foreground=C_LIGHT)])

        ax_temp.set_xlim(-0.5, 7 * 24 - 0.5)
        ax_temp.set_ylim(wk_lo, wk_hi)
        ax_temp.set_yticks([wk_lo, wk_hi])
        ax_temp.set_yticklabels([f"{wk_lo:.0f}°", f"{wk_hi:.0f}°"], fontsize=6,
                                color=C_MUTED, fontweight="bold")
        ax_temp.tick_params(axis="y", length=2, pad=1, colors=C_MUTED, left=True)
        ax_temp.set_xticks([])
        for spine in ax_temp.spines.values():
            spine.set_visible(False)

    # ── Month-summary footer table ──────────────────────────────────────────
    # Sits in the space reserved by footer_in above the old bottom margin --
    # a bordered box, styled like the day cells, holding whole-month
    # Tmax/Tmin/Tavg, Tdmax/Tdmin/Tdavg, precip, lightning, and solar.
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

    footer_cols = [
        ("TEMPERATURE", [
            ("Tmax", fmt_extreme(ms["temp_max"])),
            ("Tmin", fmt_extreme(ms["temp_min"])),
            ("Tavg", fmt_avg(ms["temp_avg"])),
        ]),
        ("DEWPOINT", [
            ("Tdmax", fmt_extreme(ms["dew_max"])),
            ("Tdmin", fmt_extreme(ms["dew_min"])),
            ("Tdavg", fmt_avg(ms["dew_avg"])),
        ]),
        # Extremes of the daily extremes, not of raw readings (see
        # compute_month_summary's docstring) -- e.g. "never got below 80
        # last night" is a warm-low candidate, distinct from Tmin above.
        ("DAILY EXTREMES", [
            ("Warm low", fmt_extreme(ms["warm_low"])),
            ("Cool high", fmt_extreme(ms["cool_high"])),
            ("", ""),
        ]),
        ("PRECIP / LTNG", [
            ("Total", f'{ms["rain_total"]:.2f}" on {ms["rain_days"]} day(s)'),
            ("Strikes", f'{ms["lightning_total"]}' if ms["lightning_total"] else "—"),
            ("", ""),
        ]),
        ("SOLAR / COVERAGE", [
            ("Energy", solar_str),
            ("Reporting", f'{ms["days_reporting"]} of '
                          f'{calmod.monthrange(year, month)[1]} day(s)'),
            ("", ""),
        ]),
    ]

    # ax_foot holds BOTH the table and the small caption note, packed
    # tightly one right under the other -- a thin note_frac slice at the
    # very bottom for the caption, the rest of the box above it, instead of
    # the table sitting in its own axes with the note stranded in a
    # separate, mostly-empty margin band well below it.
    ax_foot = fig.add_axes([ax_left, 0.0, ax_w, ax_bottom])
    ax_foot.set_xlim(0, 1)
    ax_foot.set_ylim(0, 1)
    ax_foot.set_axis_off()

    note_frac = 0.16
    box_bottom = note_frac + 0.02
    ax_foot.add_patch(mpatches.FancyBboxPatch(
        (0.0, box_bottom), 1.0, 0.97 - box_bottom, boxstyle="round,pad=0.01",
        facecolor=C_CELL, edgecolor=C_GRID, linewidth=0.7,
        transform=ax_foot.transAxes))
    ax_foot.text(0.015, 0.885, "MONTH SUMMARY", fontsize=9.5,
                fontweight="bold", color=C_TXT, va="center")

    # Each stat is one line: bold muted label, then the value just to its
    # right -- avoids stacking label/value on separate lines, which is what
    # collided with the column heading above it in the first pass.
    col_xs = [0.02, 0.216, 0.412, 0.608, 0.804]
    label_w = 0.075
    heading_y = 0.735
    row_ys = [0.575, 0.41, 0.245]
    for cx, (heading, rows_) in zip(col_xs, footer_cols):
        ax_foot.text(cx, heading_y, heading, fontsize=6.5, fontweight="bold",
                    color=C_MUTED, va="center")
        for ry, (label, val) in zip(row_ys, rows_):
            if not label:
                continue
            ax_foot.text(cx, ry, label, fontsize=7.5, color=C_MUTED,
                        fontweight="bold", va="center")
            ax_foot.text(cx + label_w, ry, val, fontsize=8.5, color=C_TXT,
                        va="center")

    # Caption note, squeezed directly under the table (same axes) rather
    # than pinned to the absolute bottom of a taller figure.
    ax_foot.text(0.015, note_frac / 2, "Compact monthly weather summary · "
                "Daylight band integrated into temp sparkline · Hourly "
                "rain & ⚡ lightning overlay",
                ha="left", va="center", fontsize=7, family="monospace",
                color=C_TXT)

    fig.savefig(out_path, dpi=dpi, facecolor=C_BG)
    plt.close(fig)


def resolve_tz(name):
    if name:
        if ZoneInfo is None:
            sys.exit("Error: --tz needs Python 3.9+ zoneinfo (pip install tzdata)")
        try:
            return ZoneInfo(name)
        except Exception:
            sys.exit(f"Error: unknown timezone '{name}'")
    return datetime.now().astimezone().tzinfo


def main():
    ap = argparse.ArgumentParser(description="Compact Monthly Weather Calendar")
    ap.add_argument("month", nargs="?", help="YYYY-MM (default: current month)")
    ap.add_argument("--db", default=DB_PATH, help=f"database path (default: {DB_PATH})")
    ap.add_argument("--station", default=None,
                    help=f"station code (default: {DEFAULT_STATION}, the "
                         f"only station with rain/solar/lightning data)")
    ap.add_argument("--tz", default=None)
    ap.add_argument("--lat", type=float,
                    default=(float(os.environ["WEATHER_LAT"])
                            if "WEATHER_LAT" in os.environ else None))
    ap.add_argument("--lon", type=float,
                    default=(float(os.environ["WEATHER_LON"])
                            if "WEATHER_LON" in os.environ else None))
    ap.add_argument("--out", default=None)
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"Error: database not found: {args.db}")
    conn = sqlite3.connect(args.db)

    stations = discover_stations(conn)
    if not stations:
        sys.exit("Error: no stations in DB")

    station = args.station or DEFAULT_STATION
    if station not in stations:
        sys.exit(f"Error: station '{station}' not found. Available: {stations}")

    tz = resolve_tz(args.tz)

    # ISO "YYYY-MM" month argument, matching bymonth.py's convention
    # (a single --year/--month pair of ints was the inconsistency here).
    if args.month:
        try:
            year, month = (int(x) for x in args.month.split("-"))
            if not 1 <= month <= 12:
                raise ValueError
        except ValueError:
            ap.error("month must look like 2026-08")
    else:
        today = datetime.now(tz).date()
        year, month = today.year, today.month

    lat, lon = args.lat, args.lon
    if lat is None or lon is None:
        print("Warning: no --lat/--lon and WEATHER_LAT/WEATHER_LON not set -- "
              "sunrise/sunset shading and solar clearness will be skipped.")

    sun_times = all_sun_times(lat, lon, tz, year, month) if (lat and lon) else {}
    by_day = load_month_data(conn, station, year, month, tz)

    out_path = args.out or f"calendar_{year}-{month:02d}.svg"
    render_calendar(year, month, by_day, sun_times, station, tz,
                    lat, lon, out_path, args.dpi)
    print(f"Saved: {out_path}")
    conn.close()


if __name__ == "__main__":
    main()
