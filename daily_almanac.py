#!/usr/bin/env python3
"""
daily_almanac.py -- A scripted recreation of IEM's Autoplot #218 ("Daily
NWS CLImate Report Infographic"), but as bell curves instead of gauge
dials, and sourced from our own pipeline (local db + ACIS fallback)
instead of the NWS CLI text product -- which only exists for first-order
NWS climate sites and wouldn't cover a personal station like G6964 anyway.

WHY BELL CURVES, NOT GAUGES: a gauge just places today's value on a
min/max scale. A bell curve fit to the calendar day's full history (mean,
std dev, +-1 SD band) shows how unusual today's value actually is --
which is the more interesting question, and lets the standard deviation
itself be a first-class part of the chart rather than an invisible input.

WHY NOT A BELL CURVE FOR PRECIP: daily precip on a given calendar day
across years is heavily right-skewed and zero-inflated (most years near
zero, a handful of years with a big event) -- fitting a normal
distribution to that would misrepresent the actual shape of the data. So
precip is shown as a straightforward bar comparison instead: today vs.
the multi-year mean vs. the all-time record for that calendar day.

DATA SOURCE FOR THE DISTRIBUTION: rather than ACIS's official 1991-2020
normals product (which some stations lack -- see the KMIO issue) plus a
separate records lookup, this computes mean/std/record directly from the
full daily history itself (local db for stations archived there, ACIS's
full period-of-record via load_daily_from_acis(..., 'por', 'por') for
ACIS-registered stations not in the local db) -- one consistent source
for all three statistics, cached the same way as the rest of this
pipeline's ACIS calls.

SAMPLE SIZE CAVEAT / NEAREST-LONG-TERM-SITE FALLBACK: a short-record
personal station (e.g. a 2-year-old G6964) will have a very small n for
this calendar day. When n falls below --min-years, this looks up the
closest ACIS-registered station with a long enough period of record (via
ACIS StnMeta's bbox search) and substitutes ITS temperature distribution
for the bell curves -- today's actual measured value still comes from
your own station, only the mean/std/record curve underneath it is
borrowed. This is deliberately TEMPERATURE-ONLY: precip climatology does
not transfer well over even a few miles (convective events can dump 7" at
one site and 1" a few miles away -- see this pipeline's own precip
divergence chart), so precip fallback is off by default; pass
--precip-fallback to opt in anyway. The nearest-site search needs
coordinates for a non-ACIS station like G6964 -- pass --lat/--lon.

Usage:
    python daily_almanac.py KSGF --date 2026-09-15
    python daily_almanac.py KSGF                      # defaults to most recent archived day
    python daily_almanac.py KJLN --date 2026-07-04 --format png,svg

    # Short-record personal station: borrow temp climatology from the
    # nearest long-term ACIS site (needs coordinates since G6964 isn't
    # ACIS-registered):
    python daily_almanac.py G6964 --lat 37.19 --lon -93.29 --min-years 10
"""

import argparse
import math
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import matplotlib.pyplot as plt

from climate_normals import (
    load_daily_summary, discover_stations, DB_PATH, DEFAULT_TZ, get_tz,
    load_daily_from_acis, station_exists_in_acis,
)


def load_env_file(path=".env"):
    """
    Minimal .env parser (KEY=VALUE per line, '#' comments, optional
    quotes) -- no python-dotenv dependency. Lets --lat/--lon/--db/etc
    default from a .env file (e.g. ALMANAC_LAT=37.19) instead of being
    retyped on every run.
    """
    import os
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def find_nearest_long_term_station(lat, lon, min_years=10, exclude=None):
    """
    Search ACIS StnMeta within an expanding bounding box around (lat, lon)
    for the closest station with at least min_years of maxt period of
    record. Returns (distance_miles, station_id, name, years) or None if
    nothing qualifying turns up even at the widest search radius.
    """
    exclude = exclude or set()
    for radius_deg in (1.0, 2.0, 4.0, 8.0):  # roughly 70/140/280/550 mi at mid-latitudes
        bbox = f"{lon - radius_deg},{lat - radius_deg},{lon + radius_deg},{lat + radius_deg}"
        try:
            resp = requests.post(
                "http://data.rcc-acis.org/StnMeta",
                json={"bbox": bbox, "elems": ["maxt"],
                      "meta": ["sids", "name", "ll", "valid_daterange"]},
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            print(f"Warning: ACIS StnMeta nearest-station search failed ({e}).")
            return None

        candidates = []
        for st in result.get("meta", []):
            ll = st.get("ll")
            if not ll or len(ll) != 2:
                continue
            st_lon, st_lat = ll  # ACIS returns [lon, lat]

            years = 0
            for span in (st.get("valid_daterange") or []):
                if span and len(span) == 2 and span[0] and span[1]:
                    try:
                        years = max(years, int(str(span[1])[:4]) - int(str(span[0])[:4]))
                    except ValueError:
                        continue
            if years < min_years:
                continue

            sids = st.get("sids") or []
            icao = next((s.split()[0] for s in sids
                        if len(s.split()[0]) == 4 and s.split()[0].upper().startswith("K")), None)
            station_id = icao or (sids[0].split()[0] if sids else None)
            if not station_id or station_id in exclude:
                continue

            dist = haversine_miles(lat, lon, st_lat, st_lon)
            candidates.append((dist, station_id, st.get("name"), years))

        if candidates:
            candidates.sort(key=lambda c: c[0])
            return candidates[0]

    return None


def get_full_daily_history(conn, station, local_stations):
    """
    Every daily tmax/tmin this station has, unfiltered by calendar day --
    needed for year-to-date and trailing-365-day degree day totals, which
    have to sum across a date RANGE rather than looking at one calendar
    day across years. Same source-routing as get_calendar_day_history.
    """
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if station in local_stations:
        return load_daily_summary(conn, [station], "1900-01-01", today_str, DEFAULT_TZ)
    return load_daily_from_acis(station, "por", "por", conn=conn)


def compute_dd_period_ranges(full_hist, target_date, base_heat=65.0, base_cool=65.0, base_gdd=50.0):
    """
    For year-to-date (Jan 1 -> target_date) and trailing-365-days (target
    date minus 365 -> target_date), compute this year's actual HDD/CDD/GDD
    total alongside the historical max/min of that SAME period length,
    anchored to the same calendar day-of-year, across every other year in
    the record -- e.g. "how does this year's heating deficit through
    Sep 15 compare to every other year's heating deficit through Sep 15
    ever recorded". Skips Feb 29 anchor years that don't apply (leap-day
    edge case) rather than crashing.
    """
    full = full_hist.dropna(subset=["tmax", "tmin"]).copy()
    if full.empty:
        return None
    full["tavg"] = (full["tmax"] + full["tmin"]) / 2
    full["hdd"] = (base_heat - full["tavg"]).clip(lower=0)
    full["cdd"] = (full["tavg"] - base_cool).clip(lower=0)
    full["gdd"] = (full["tavg"] - base_gdd).clip(lower=0)
    full = full.sort_values("date").set_index("date")

    target_ts = pd.Timestamp(target_date)
    target_md = target_ts.strftime("%m-%d")
    years = sorted(full.index.year.unique())

    def sum_range(start, end):
        window = full.loc[(full.index >= start) & (full.index <= end)]
        return {f: window[f].sum() for f in ("hdd", "cdd", "gdd")}, len(window)

    current_ytd, _ = sum_range(target_ts.replace(month=1, day=1), target_ts)
    current_365, _ = sum_range(target_ts - pd.Timedelta(days=365), target_ts)

    hist_ytd = {"hdd": [], "cdd": [], "gdd": []}
    hist_365 = {"hdd": [], "cdd": [], "gdd": []}
    for y in years:
        if y == target_ts.year:
            continue
        try:
            y_end = pd.Timestamp(f"{y}-{target_md}")
        except ValueError:
            continue  # Feb 29 anchor in a non-leap year
        y_start = pd.Timestamp(f"{y}-01-01")
        vals, n = sum_range(y_start, y_end)
        if n > 0:
            for f in ("hdd", "cdd", "gdd"):
                hist_ytd[f].append(vals[f])
        vals365, n2 = sum_range(y_end - pd.Timedelta(days=365), y_end)
        if n2 > 0:
            for f in ("hdd", "cdd", "gdd"):
                hist_365[f].append(vals365[f])

    def minmax(lst):
        return (max(lst), min(lst)) if lst else (None, None)

    result = {"ytd": {}, "last365": {}}
    for f in ("hdd", "cdd", "gdd"):
        hi, lo = minmax(hist_ytd[f])
        result["ytd"][f] = {"current": current_ytd[f], "max": hi, "min": lo, "n_years": len(hist_ytd[f])}
        hi2, lo2 = minmax(hist_365[f])
        result["last365"][f] = {"current": current_365[f], "max": hi2, "min": lo2, "n_years": len(hist_365[f])}
    return result


def compute_precip_mtd_stats(full_hist, target_date):
    """
    Month-to-date precip: this month's actual total (1st -> target_date)
    vs. the historical normal (mean) and record MTD-through-this-day-of-
    month, anchored to the same day-of-month across every other year on
    record -- same "same point in the period, across years" methodology
    as compute_dd_period_ranges(), just for precip and month-length
    instead of temperature-derived degree days at year/365-day length.
    Months without enough days (e.g. day-of-month 31 in a 30-day month)
    are skipped for that year rather than crashing.
    """
    full = full_hist.dropna(subset=["precip"]).copy()
    if full.empty:
        return None
    full["date"] = pd.to_datetime(full["date"])
    full = full.sort_values("date").set_index("date")

    target_ts = pd.Timestamp(target_date)
    day_of_month = target_ts.day

    def sum_range(start, end):
        window = full.loc[(full.index >= start) & (full.index <= end)]
        return window["precip"].sum(), len(window)

    current_mtd, _ = sum_range(target_ts.replace(day=1), target_ts)

    hist_vals = []
    for y in sorted(full.index.year.unique()):
        if y == target_ts.year:
            continue
        try:
            y_start = pd.Timestamp(year=y, month=target_ts.month, day=1)
            y_end = pd.Timestamp(year=y, month=target_ts.month, day=day_of_month)
        except ValueError:
            continue  # this day-of-month doesn't exist in this month/year
        val, n = sum_range(y_start, y_end)
        if n > 0:
            hist_vals.append((val, y))

    if not hist_vals:
        return {"current": current_mtd, "normal": None, "record": None,
               "record_year": None, "n_years": 0}

    normal = sum(v for v, _ in hist_vals) / len(hist_vals)
    record_val, record_year = max(hist_vals, key=lambda t: t[0])
    return {"current": current_mtd, "normal": normal, "record": record_val,
           "record_year": record_year, "n_years": len(hist_vals)}



def get_calendar_day_history(conn, station, target_date, local_stations):
    """
    Every historical tmax/tmin/precip value this station has on the same
    calendar month-day as target_date, across all archived years (local)
    or the full ACIS period of record (ACIS-registered, not-local
    stations). One consistent source feeds mean, std, and record alike.
    """
    month_day = pd.Timestamp(target_date).strftime("%m-%d")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if station in local_stations:
        daily = load_daily_summary(conn, [station], "1900-01-01", today_str, DEFAULT_TZ)
    else:
        daily = load_daily_from_acis(station, "por", "por", conn=conn)

    if daily.empty:
        return daily
    daily = daily.copy()
    daily["month_day"] = pd.to_datetime(daily["date"]).dt.strftime("%m-%d")
    return daily[daily["month_day"] == month_day]


def compute_field_stats(hist, field):
    """mean/std/record-high/record-low (both tails matter: e.g. for tmax,
    the record-max is the record HIGH, but the record-min is the coldest
    high temperature ever recorded that day -- info IEM's own gauge
    specifically calls out as not present in the raw CLI text product.

    Also keeps the raw per-year values and the sample skewness (pandas'
    built-in Fisher-Pearson adjusted skew, no scipy needed) -- the fitted
    bell curve is necessarily symmetric (built from just mean/std), so
    skew and the actual values are what let the chart show whether the
    real historical distribution actually looks that way."""
    vals = hist[field].dropna()
    if vals.empty:
        return None
    idx_max = vals.idxmax()
    idx_min = vals.idxmin()
    return {
        "mean": vals.mean(),
        "std": vals.std(),
        "skew": vals.skew() if len(vals) >= 3 else float("nan"),
        "n": len(vals),
        "values": vals.values,
        "record_max": vals.loc[idx_max],
        "record_max_year": pd.Timestamp(hist.loc[idx_max, "date"]).year,
        "record_min": vals.loc[idx_min],
        "record_min_year": pd.Timestamp(hist.loc[idx_min, "date"]).year,
    }


def compute_tavg_stats(hist):
    """Same shape as compute_field_stats(), but for tavg=(tmax+tmin)/2 --
    computed per-row like the degree day stats (needs both tmax AND tmin
    from the same day/year, not independently aggregated)."""
    sub = hist.dropna(subset=["tmax", "tmin"]).copy()
    if sub.empty:
        return None
    sub["tavg"] = (sub["tmax"] + sub["tmin"]) / 2
    return compute_field_stats(sub, "tavg")


def get_today_values(conn, station, target_date, local_stations):
    """The single day's actual tmax/tmin/precip, from whichever source
    has it."""
    if station in local_stations:
        daily = load_daily_summary(conn, [station], target_date, target_date, DEFAULT_TZ)
    else:
        daily = load_daily_from_acis(station, target_date, target_date, conn=conn)
    if daily.empty:
        return {"tmax": None, "tmin": None, "precip": None}
    row = daily.iloc[0]
    return {"tmax": row.get("tmax"), "tmin": row.get("tmin"), "precip": row.get("precip")}


def gaussian_pdf(x, mean, std):
    return (1.0 / (std * np.sqrt(2 * np.pi))) * np.exp(-0.5 * ((x - mean) / std) ** 2)


def normal_cdf(z):
    """Standard normal CDF via math.erf -- no scipy dependency needed."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def z_and_percentile(value, mean, std):
    """Z-score and percentile (0-100) of `value` under N(mean, std)."""
    z = (value - mean) / std
    pct = normal_cdf(z) * 100.0
    return z, pct


def plot_bell(ax, stats, today_val, title, color, source_note=None):
    if stats is None or not stats["std"] or np.isnan(stats["std"]) or stats["n"] < 2:
        ax.text(0.5, 0.5, "Not enough history\nfor this calendar day",
                ha="center", va="center", transform=ax.transAxes, fontsize=10)
        ax.set_title(title)
        ax.set_xticks([])
        ax.set_yticks([])
        return

    mean, std = stats["mean"], stats["std"]
    lo = min(mean - 4 * std, stats["record_min"] - 2)
    hi = max(mean + 4 * std, stats["record_max"] + 2)
    x = np.linspace(lo, hi, 400)
    y = gaussian_pdf(x, mean, std)

    # Actual historical values as a density histogram, drawn BEHIND the
    # fitted curve, so any real skew (the fit itself is always symmetric
    # by construction) is visible rather than hidden by the idealized bell.
    values = stats.get("values")
    if values is not None and len(values) >= 5:
        n_bins = int(np.clip(len(values) // 3, 5, 20))
        counts, bin_edges = np.histogram(values, bins=n_bins)
        bin_width = bin_edges[1] - bin_edges[0]
        bin_centers = bin_edges[:-1] + bin_width / 2
        densities = counts / (len(values) * bin_width)  # matches hist(density=True) scaling

        ax.bar(bin_centers, densities, width=bin_width, color=color, alpha=0.22,
              edgecolor="white", linewidth=0.5, zorder=1,
              label=f"Actual years (n={len(values)}, bin width={bin_width:.1f}\u00b0F)")

        for center, height, count in zip(bin_centers, densities, counts):
            if count > 0:
                ax.text(center, height + y.max() * 0.015, str(int(count)),
                        ha="center", va="bottom", fontsize=6.5, color=color, zorder=5)

    ax.plot(x, y, color=color, lw=1.8, zorder=3, label="Fitted normal (symmetric)")
    ax.fill_between(x, y, where=(x >= mean - std) & (x <= mean + std),
                     color=color, alpha=0.12, zorder=2)
    ax.fill_between(x, y, where=(x >= mean - 2 * std) & (x <= mean + 2 * std),
                     color=color, alpha=0.06, zorder=2)

    ax.axvline(mean, color="gray", ls="--", lw=1.3,
                label=f"Normal (mean): {mean:.1f}\u00b0F")
    ax.axvline(stats["record_max"], color="#e74c3c", ls=":", lw=1.2,
                label=f"Record high: {stats['record_max']:.0f}\u00b0F ({stats['record_max_year']})")
    ax.axvline(stats["record_min"], color="#85c1e9", ls=":", lw=1.2,
                label=f"Record low: {stats['record_min']:.0f}\u00b0F ({stats['record_min_year']})")

    if today_val is not None and not (isinstance(today_val, float) and np.isnan(today_val)):
        z, pct = z_and_percentile(today_val, mean, std)
        ax.axvline(today_val, color=color, lw=2.6,
                    label=f"Today: {today_val:.0f}\u00b0F  (Z={z:+.2f}, {pct:.0f}th pct)")
        ax.plot([today_val], [gaussian_pdf(today_val, mean, std)], "o",
                color=color, ms=9, zorder=6, markeredgecolor="black", markeredgewidth=0.6)
        ax.annotate(f"Z={z:+.2f}\n{pct:.0f}th pctile",
                    xy=(today_val, gaussian_pdf(today_val, mean, std)),
                    xytext=(28, 22), textcoords="offset points",
                    ha="center", va="center", fontsize=8, fontweight="bold", color=color,
                    bbox=dict(boxstyle="round,pad=0.35", facecolor="white",
                              edgecolor=color, linewidth=1.1, alpha=0.95),
                    arrowprops=dict(arrowstyle="-", color=color, lw=0.8,
                                    shrinkA=2, shrinkB=4))

    # Rug plot of individual years along the x-axis -- with a small n a
    # histogram is too coarse to mean much, but the actual year-by-year
    # points are still worth showing directly rather than only the curve.
    if values is not None and len(values) < 15:
        ax.plot(values, np.zeros_like(values) - 0.02 * y.max(), "|",
                color=color, ms=14, mew=1.5, zorder=4, clip_on=False)

    skew = stats.get("skew", float("nan"))
    skew_text = f", skew={skew:+.2f}" if skew == skew else ""  # NaN check without extra import
    title_text = f"{title}   (SD: {std:.1f}\u00b0F{skew_text}, n={stats['n']} yrs)"
    if source_note:
        title_text += f"\n{source_note}"
    ax.set_title(title_text, fontsize=10.5)
    ax.set_xlabel("\u00b0F")
    ax.set_yticks([])
    ax.legend(fontsize=7.5, loc="upper left", framealpha=0.85)
    ax.grid(alpha=0.25, axis="x")


def plot_dd_table(ax, period_data, title, current_year_label):
    """
    Numeric table: for HDD/CDD/GDD, this year's total vs. the historical
    max/min of that same period (YTD or trailing-365), anchored to the
    same calendar day-of-year across every other year on record. Same
    table styling as plot_summary_table for visual consistency.
    """
    ax.axis("off")

    if period_data is None:
        ax.text(0.5, 0.5, "Not enough history", ha="center", va="center",
                transform=ax.transAxes, fontsize=10)
        ax.set_title(title)
        return

    def fmt(v):
        return f"{v:.1f}" if v is not None else "\u2013"

    fields = ["hdd", "cdd", "gdd"]
    row_labels = ["HDD", "CDD", "GDD"]
    n_years = period_data["hdd"]["n_years"]

    rows = [["", current_year_label, "Max", "Min"]]
    for field, label in zip(fields, row_labels):
        d = period_data[field]
        rows.append([label, fmt(d["current"]), fmt(d["max"]), fmt(d["min"])])

    table = ax.table(cellText=rows, loc="center", cellLoc="center",
                     colWidths=[0.22, 0.26, 0.26, 0.26])
    table.auto_set_font_size(False)
    table.set_fontsize(9.5)
    table.scale(1, 1.9)

    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor("#dddddd")
        if r == 0:
            cell.set_facecolor("#f0f0f0")
            cell.set_text_props(fontweight="bold")
        elif c == 0:
            cell.set_facecolor("#f7f7f7")
            cell.set_text_props(fontweight="bold")
        if r > 0 and c == 2:
            cell.set_text_props(color="#e74c3c")
        if r > 0 and c == 3:
            cell.set_text_props(color="#85c1e9")

    ax.set_title(f"{title}  (n={n_years} prior yrs)", fontsize=10)


def plot_precip_bar(ax, stats, today_val, mtd_stats=None, target_date=None):
    if stats is None or stats["n"] < 1:
        ax.text(0.5, 0.5, "Not enough history\nfor this calendar day",
                ha="center", va="center", transform=ax.transAxes, fontsize=10)
        ax.set_title("Precipitation")
        return

    today_val = 0.0 if today_val is None or (isinstance(today_val, float) and np.isnan(today_val)) else today_val
    labels = ["Today", f"Normal\n(mean, n={stats['n']})", f"Record\n({stats['record_max_year']})"]
    values = [today_val, stats["mean"], stats["record_max"]]
    colors = ["#16a085", "#7f8c8d", "#e74c3c"]

    bars = ax.bar(labels, values, color=colors, alpha=0.9, width=0.55)
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 0.02, f'{val:.2f}"',
                ha="center", va="bottom", fontsize=9, fontweight="bold")

    if mtd_stats and mtd_stats.get("n_years"):
        month_label = pd.Timestamp(target_date).strftime("%b") if target_date else "Month"
        cur = mtd_stats["current"]
        normal = mtd_stats["normal"]
        record = mtd_stats["record"]
        record_year = mtd_stats["record_year"]
        mtd_text = (f"{month_label} MTD: {cur:.2f}\"\n"
                   f"Normal: {normal:.2f}\"\n"
                   f"Record: {record:.2f}\" ({record_year})\n"
                   f"(n={mtd_stats['n_years']} yrs)")
        ax.text(0.02, 0.96, mtd_text, transform=ax.transAxes, ha="left", va="top",
                fontsize=8.5, linespacing=1.4,
                bbox=dict(boxstyle="round,pad=0.4", facecolor="#f7f7f7",
                          edgecolor="#999999", linewidth=0.8))

    ax.set_ylabel("Precipitation (in)")
    ax.set_title("Precipitation  (bar chart, not a bell curve -- "
                 "daily precip is right-skewed/zero-inflated, not normal)",
                 fontsize=9)
    ax.grid(alpha=0.25, axis="y")


def print_stats_debug(label, stats):
    if stats is None:
        print(f"  [{label}] no data")
        return
    skew = stats.get("skew", float("nan"))
    skew_str = f"{skew:+.2f}" if skew == skew else "n/a"
    print(f"  [{label}] n={stats['n']} yrs | mean={stats['mean']:.1f} | "
         f"std={stats['std']:.1f} | skew={skew_str} | max={stats['record_max']:.2f} "
         f"({stats['record_max_year']}) | min={stats['record_min']:.2f} "
         f"({stats['record_min_year']})")


def safe_filename_component(text):
    """
    Strip anything Windows (or any OS) disallows in a filename -- notably
    : < > " | ? * and control/non-printable characters -- replacing each
    with an underscore. Applied to the filename itself, never the
    directory portion of a path.
    """
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(text))
    cleaned = cleaned.strip(" .")  # Windows also disallows trailing space/dot
    return cleaned or "output"


def save_figure(fig, out_base, formats):
    out_path = Path(out_base)
    safe_stem = safe_filename_component(out_path.name)
    safe_base = out_path.with_name(safe_stem)

    for fmt in formats:
        path = safe_base.with_suffix(f".{fmt}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(str(path), dpi=150, format=fmt, bbox_inches="tight")
            print(f"Saved: {path}")
        except OSError as e:
            print(f"Warning: could not save to {path!r} ({e}). "
                 f"Retrying in the current directory with a plain filename...")
            fallback = Path.cwd() / f"almanac_output.{fmt}"
            try:
                fig.savefig(str(fallback), dpi=150, format=fmt, bbox_inches="tight")
                print(f"Saved (fallback): {fallback}")
            except OSError as e2:
                print(f"Fallback save also failed ({e2}). Path attempted: "
                     f"{fallback!r} (len={len(str(fallback))}). This usually "
                     f"means a permissions issue or a Windows path-length "
                     f"limit -- try --out-dir pointing somewhere shorter, "
                     f"e.g. --out-dir C:\\tmp.")


def compute_degree_day_stats(hist, base_heat=65.0, base_cool=65.0, base_gdd=50.0):
    """
    Historical HDD/CDD/GDD for this calendar day, computed per-year from
    the same tmax/tmin pairs used for the temp bell curves (not summed
    independently max/min like temp records -- degree days need tmax and
    tmin from the SAME day together to get tavg right). Returns a dict
    keyed 'hdd'/'cdd'/'gdd', each with mean/n/record_max(+year)/
    record_min(+year), same shape as compute_field_stats() for temp.
    """
    sub = hist.dropna(subset=["tmax", "tmin"]).copy()
    if sub.empty:
        return None
    sub["tavg"] = (sub["tmax"] + sub["tmin"]) / 2
    sub["hdd"] = (base_heat - sub["tavg"]).clip(lower=0)
    sub["cdd"] = (sub["tavg"] - base_cool).clip(lower=0)
    sub["gdd"] = (sub["tavg"] - base_gdd).clip(lower=0)

    result = {}
    for field in ("hdd", "cdd", "gdd"):
        vals = sub[field]
        idx_max, idx_min = vals.idxmax(), vals.idxmin()
        result[field] = {
            "mean": vals.mean(),
            "n": len(vals),
            "record_max": vals.loc[idx_max],
            "record_max_year": pd.Timestamp(sub.loc[idx_max, "date"]).year,
            "record_min": vals.loc[idx_min],
            "record_min_year": pd.Timestamp(sub.loc[idx_min, "date"]).year,
        }
    return result


def plot_summary_table(ax, tmax_stats, tmin_stats, tavg_stats, today, dd_stats,
                       base_heat=65.0, base_cool=65.0, base_gdd=50.0):
    """Compact Today | Normal | High | Low reference table for Tmax/Tmin/
    Tavg (Tavg's Today cell also shows departure from normal, color-coded)
    and for today's Heating/Cooling/Growing degree days, sitting above the
    bell curves -- the numbers the curves visualize, in a form you can
    read at a glance without parsing the charts themselves.

    Degree day Normal/Record columns come from compute_degree_day_stats()
    -- the actual climatological mean/record HDD/CDD/GDD for this
    calendar day across history, not just today's single value."""
    ax.axis("off")

    def fmt_temp(val):
        return f"{val:.0f}\u00b0F" if val is not None and val == val else "\u2013"

    def fmt_temp_record(stats, which):
        if stats is None:
            return "\u2013"
        return f"{stats[f'record_{which}']:.0f}\u00b0F ({stats[f'record_{which}_year']})"

    def fmt_dd(val):
        return f"{val:.1f}" if val is not None and val == val else "\u2013"

    def fmt_dd_record(field_stats, which):
        if field_stats is None:
            return "\u2013"
        return f"{field_stats[f'record_{which}']:.1f} ({field_stats[f'record_{which}_year']})"

    tmax_v, tmin_v = today.get("tmax"), today.get("tmin")
    has_temps = tmax_v is not None and tmin_v is not None and tmax_v == tmax_v and tmin_v == tmin_v
    tavg_today = (tmax_v + tmin_v) / 2 if has_temps else None

    departure = None
    if tavg_today is not None and tavg_stats is not None:
        departure = tavg_today - tavg_stats["mean"]
    tavg_today_text = fmt_temp(tavg_today)
    if departure is not None:
        tavg_today_text += f"  ({departure:+.0f}\u00b0)"

    rows = [
        ["", "Today", "Normal", "Record High", "Record Low"],
        ["TMAX", fmt_temp(today["tmax"]),
         fmt_temp(tmax_stats["mean"]) if tmax_stats else "\u2013",
         fmt_temp_record(tmax_stats, "max"), fmt_temp_record(tmax_stats, "min")],
        ["TMIN", fmt_temp(today["tmin"]),
         fmt_temp(tmin_stats["mean"]) if tmin_stats else "\u2013",
         fmt_temp_record(tmin_stats, "max"), fmt_temp_record(tmin_stats, "min")],
        ["TAVG", tavg_today_text,
         fmt_temp(tavg_stats["mean"]) if tavg_stats else "\u2013",
         fmt_temp_record(tavg_stats, "max"), fmt_temp_record(tavg_stats, "min")],
    ]
    tavg_row_index = 3

    today_dd = None
    if has_temps:
        today_dd = {
            "hdd": max(0.0, base_heat - tavg_today),
            "cdd": max(0.0, tavg_today - base_cool),
            "gdd": max(0.0, tavg_today - base_gdd),
        }

    for field, base in (("hdd", base_heat), ("cdd", base_cool), ("gdd", base_gdd)):
        field_stats = dd_stats[field] if dd_stats else None
        rows.append([
            f"{field.upper()} ({base:.0f}\u00b0)",
            fmt_dd(today_dd[field]) if today_dd else "\u2013",
            fmt_dd(field_stats["mean"]) if field_stats else "\u2013",
            fmt_dd_record(field_stats, "max"),
            fmt_dd_record(field_stats, "min"),
        ])
    dd_row_start = tavg_row_index + 1

    table = ax.table(cellText=rows, loc="center", cellLoc="center",
                     colWidths=[0.14, 0.20, 0.16, 0.25, 0.25])
    table.auto_set_font_size(False)
    table.set_fontsize(9.5)
    table.scale(1, 1.9)

    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor("#dddddd")
        if r == 0:
            cell.set_facecolor("#f0f0f0")
            cell.set_text_props(fontweight="bold")
        elif c == 0:
            cell.set_facecolor("#f7f7f7")
            cell.set_text_props(fontweight="bold")
        if r in (1, 2, tavg_row_index) and c == 3:
            cell.set_text_props(color="#e74c3c")
        if r in (1, 2, tavg_row_index) and c == 4:
            cell.set_text_props(color="#85c1e9")
        if r == tavg_row_index and c == 1 and departure is not None:
            cell.set_text_props(color="#c0392b" if departure > 0 else
                                "#2980b9" if departure < 0 else "black",
                                fontweight="bold")
        if r >= dd_row_start and c == 0:
            cell.set_facecolor("#eef7f0")


def make_almanac(conn, station, target_date, out_base, formats,
                 lat=None, lon=None, min_years=10, precip_fallback=False,
                 nearest_station_override=None,
                 base_heat=65.0, base_cool=65.0, base_gdd=50.0):
    local_stations = set(discover_stations(conn))

    if station not in local_stations and not station_exists_in_acis(station):
        raise SystemExit(f"'{station}' not found in local db or in the ACIS network.")

    hist = get_calendar_day_history(conn, station, target_date, local_stations)
    if hist.empty:
        raise SystemExit(f"No historical data found for {station} on "
                          f"{pd.Timestamp(target_date).strftime('%m-%d')} "
                          f"across any year.")

    tmax_stats = compute_field_stats(hist, "tmax")
    tmin_stats = compute_field_stats(hist, "tmin")
    precip_stats = compute_field_stats(hist, "precip")
    tavg_stats = compute_tavg_stats(hist)
    dd_stats = compute_degree_day_stats(hist, base_heat, base_cool, base_gdd)

    print(f"[debug] {station} own history for "
         f"{pd.Timestamp(target_date).strftime('%m-%d')}:")
    print_stats_debug("tmax", tmax_stats)
    print_stats_debug("tmin", tmin_stats)
    print_stats_debug("precip", precip_stats)
    print_stats_debug("tavg", tavg_stats)

    temp_source_note = None
    substitute = None
    short_n = min(s["n"] for s in (tmax_stats, tmin_stats) if s) if (tmax_stats or tmin_stats) else 0
    needs_fallback = short_n < min_years

    if needs_fallback:
        substitute, dist_note = None, ""
        if nearest_station_override:
            substitute = nearest_station_override
            dist_note = "manual override"
        elif lat is not None and lon is not None:
            found = find_nearest_long_term_station(lat, lon, min_years=min_years, exclude={station})
            if found:
                dist, substitute, name, years = found
                dist_note = f"{dist:.0f} mi away, {years} yrs of record"
                print(f"{station}: only {short_n} yr(s) of history for this calendar day -- "
                      f"borrowing temperature climatology from {substitute} ({name}, {dist_note}).")
            else:
                print(f"Warning: no qualifying long-term ACIS station found near "
                      f"({lat}, {lon}) with >= {min_years} years of record.")
        else:
            print(f"{station}: only {short_n} yr(s) of history for this calendar day -- "
                  f"pass --lat/--lon (or set ALMANAC_LAT/ALMANAC_LON in a .env file) to "
                  f"borrow temperature climatology from the nearest long-term ACIS station.")

        if substitute:
            # Force full ACIS period-of-record for the substitute, even if
            # it happens to also have a partial local archive (e.g. KJLN
            # from earlier ASOS testing) -- passing an empty local_stations
            # set here means get_calendar_day_history() always takes the
            # ACIS 'por' branch instead of that tiny local slice.
            sub_hist = get_calendar_day_history(conn, substitute, target_date, set())
            if sub_hist.empty:
                print(f"Warning: {substitute} returned no data for this calendar day; "
                      f"keeping {station}'s own (short) distribution.")
            else:
                sub_tmax = compute_field_stats(sub_hist, "tmax")
                sub_tmin = compute_field_stats(sub_hist, "tmin")
                if sub_tmax:
                    tmax_stats = sub_tmax
                if sub_tmin:
                    tmin_stats = sub_tmin
                sub_tavg = compute_tavg_stats(sub_hist)
                if sub_tavg:
                    tavg_stats = sub_tavg
                sub_dd = compute_degree_day_stats(sub_hist, base_heat, base_cool, base_gdd)
                if sub_dd:
                    dd_stats = sub_dd
                temp_source_note = f"Temp distribution from {substitute} ({dist_note})"
                print(f"[debug] {substitute} substitute history for "
                     f"{pd.Timestamp(target_date).strftime('%m-%d')}:")
                print_stats_debug("tmax", tmax_stats)
                print_stats_debug("tmin", tmin_stats)

                if precip_fallback:
                    sub_precip = compute_field_stats(sub_hist, "precip")
                    if sub_precip:
                        precip_stats = sub_precip
                        print_stats_debug("precip", precip_stats)
                        print(f"--precip-fallback set: also borrowing precip distribution "
                              f"from {substitute}. Note: precip climatology is far more "
                              f"spatially localized than temperature -- treat this precip "
                              f"panel with more caution than the temp bell curves.")

    today = get_today_values(conn, station, target_date, local_stations)
    print(f"[debug] {station} actual on {target_date}: "
         f"tmax={today['tmax']} tmin={today['tmin']} precip={today['precip']}")
    tmax_v_dbg, tmin_v_dbg = today["tmax"], today["tmin"]
    tavg_today_dbg = ((tmax_v_dbg + tmin_v_dbg) / 2
                      if tmax_v_dbg is not None and tmin_v_dbg is not None else None)
    for field_label, field_stats, field_val in (
        ("tmax", tmax_stats, today["tmax"]), ("tmin", tmin_stats, today["tmin"]),
        ("tavg", tavg_stats, tavg_today_dbg),
    ):
        if field_stats and field_val is not None and field_stats["std"]:
            z, pct = z_and_percentile(field_val, field_stats["mean"], field_stats["std"])
            print(f"  [{field_label}] today Z={z:+.2f}, {pct:.1f}th percentile "
                 f"(vs mean={field_stats['mean']:.1f}, std={field_stats['std']:.1f})")

    # YTD / trailing-365 degree day comparison -- uses the station that's
    # actually supplying the temperature stats above (the substitute
    # station if the fallback triggered, station's own record otherwise),
    # for the same consistency reason as the summary table's degree-day rows.
    dd_source_station = substitute if substitute is not None else station
    full_hist = get_full_daily_history(conn, dd_source_station,
                                       local_stations if dd_source_station == station else set())
    dd_periods = compute_dd_period_ranges(full_hist, target_date, base_heat, base_cool, base_gdd)

    # Precip MTD respects the same precip_fallback opt-in as the single-day
    # precip stats above: default to the station's OWN full history (not
    # borrowed from a temp-fallback substitute), only using the
    # substitute's history if --precip-fallback was explicitly set.
    precip_source_station = dd_source_station if precip_fallback else station
    if precip_source_station == dd_source_station:
        precip_full_hist = full_hist
    else:
        precip_full_hist = get_full_daily_history(conn, station, local_stations)
    mtd_stats = compute_precip_mtd_stats(precip_full_hist, target_date)

    fig = plt.figure(figsize=(13, 11.5))
    gs = fig.add_gridspec(4, 2, height_ratios=[0.35, 1.3, 0.8, 0.8])
    ax_title = fig.add_subplot(gs[0, 0])
    ax_summary = fig.add_subplot(gs[0, 1])
    ax_high = fig.add_subplot(gs[1, 0])
    ax_low = fig.add_subplot(gs[1, 1])
    ax_precip = fig.add_subplot(gs[2, :])
    ax_dd_ytd = fig.add_subplot(gs[3, 0])
    ax_dd_365 = fig.add_subplot(gs[3, 1])

    date_label = pd.Timestamp(target_date).strftime("%B %d, %Y")
    ax_title.axis("off")
    ax_title.text(0.0, 0.6, f"{station}\nDaily Almanac", fontsize=17,
                  fontweight="bold", ha="left", va="center", transform=ax_title.transAxes)
    ax_title.text(0.0, 0.15, date_label, fontsize=12, color="#555555",
                  ha="left", va="center", transform=ax_title.transAxes)

    plot_summary_table(ax_summary, tmax_stats, tmin_stats, tavg_stats, today, dd_stats,
                       base_heat=base_heat, base_cool=base_cool, base_gdd=base_gdd)
    plot_bell(ax_high, tmax_stats, today["tmax"], "High Temperature", "#c0392b",
             source_note=temp_source_note)
    plot_bell(ax_low, tmin_stats, today["tmin"], "Low Temperature", "#2980b9",
             source_note=temp_source_note)
    plot_precip_bar(ax_precip, precip_stats, today["precip"], mtd_stats=mtd_stats, target_date=target_date)

    year_label = str(pd.Timestamp(target_date).year)
    ytd_data = dd_periods["ytd"] if dd_periods else None
    last365_data = dd_periods["last365"] if dd_periods else None
    plot_dd_table(ax_dd_ytd, ytd_data, "Degree Days: Year-to-Date", year_label)
    plot_dd_table(ax_dd_365, last365_data, "Degree Days: Trailing 365 Days", year_label)

    fig.tight_layout()

    save_figure(fig, out_base, formats)


def main():
    env = load_env_file()

    ap = argparse.ArgumentParser(
        description="Bell-curve daily almanac (IEM Autoplot #218-style, "
                    "recreated from this pipeline's own data instead of "
                    "the NWS CLI product).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("station")
    ap.add_argument("--date", type=str, default=None,
                    help="YYYY-MM-DD. Default: most recent day with data for this station.")
    ap.add_argument("--db", default=env.get("ALMANAC_DB", DB_PATH))
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--out-prefix", default=None,
                    help="Filename prefix. Default: <station>_almanac_<date>")
    ap.add_argument("--format", default="png", help="Comma-separated: png,svg,pdf")
    ap.add_argument("--lat", type=float,
                    default=float(env["ALMANAC_LAT"]) if env.get("ALMANAC_LAT") else None,
                    help="Coordinates for the nearest-long-term-station fallback search "
                         "(needed for a non-ACIS station like G6964). "
                         "Default: ALMANAC_LAT in .env if present.")
    ap.add_argument("--lon", type=float,
                    default=float(env["ALMANAC_LON"]) if env.get("ALMANAC_LON") else None,
                    help="Default: ALMANAC_LON in .env if present.")
    ap.add_argument("--min-years", type=int,
                    default=int(env.get("ALMANAC_MIN_YEARS", 10)),
                    help="Below this many years of history for the calendar day, fall back "
                         "to the nearest long-term ACIS station's temp climatology. Default: 10.")
    ap.add_argument("--nearest-station", default=env.get("ALMANAC_NEAREST_STATION"),
                    help="Skip the auto-search and use this ACIS station directly for the "
                         "fallback. Default: ALMANAC_NEAREST_STATION in .env if present.")
    ap.add_argument("--precip-fallback", action="store_true",
                    default=env.get("ALMANAC_PRECIP_FALLBACK", "").lower() in ("1", "true", "yes"),
                    help="Also borrow the substitute station's precip distribution (off by "
                         "default -- precip climatology doesn't transfer well spatially).")
    ap.add_argument("--base-heat", type=float, default=float(env.get("ALMANAC_BASE_HEAT", 65.0)),
                    help="HDD base temp, degrees F. Default: 65.")
    ap.add_argument("--base-cool", type=float, default=float(env.get("ALMANAC_BASE_COOL", 65.0)),
                    help="CDD base temp, degrees F. Default: 65.")
    ap.add_argument("--base-gdd", type=float, default=float(env.get("ALMANAC_BASE_GDD", 50.0)),
                    help="GDD base temp, degrees F. Default: 50.")
    args = ap.parse_args()

    import os
    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return
    conn = sqlite3.connect(args.db)

    if args.date:
        target_date = args.date
    else:
        # Mimic the NWS CLI product's own convention: it's typically
        # issued around 4:30 PM local time and describes the current
        # calendar day's stats up to that point. Before 4:30 PM local,
        # today's day isn't "done" in that same sense, so default to the
        # previous full day instead -- same logic CLI itself effectively
        # follows by not existing yet for today until late afternoon.
        tz = get_tz(DEFAULT_TZ)
        now_local = datetime.now(tz)
        cli_cutoff = now_local.replace(hour=16, minute=30, second=0, microsecond=0)
        if now_local < cli_cutoff:
            target_date = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            target_date = now_local.strftime("%Y-%m-%d")
        print(f"No --date given -- {now_local.strftime('%I:%M %p %Z').lstrip('0')} is "
             f"{'before' if now_local < cli_cutoff else 'at/after'} the "
             f"4:30 PM CLI-style cutoff, using {target_date}.")

    from pathlib import Path
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_prefix = args.out_prefix or f"{args.station}_almanac_{target_date}"
    formats = [f.strip().lower() for f in args.format.split(",") if f.strip()]

    make_almanac(conn, args.station, target_date, str(out_dir / out_prefix), formats,
                lat=args.lat, lon=args.lon, min_years=args.min_years,
                precip_fallback=args.precip_fallback,
                nearest_station_override=args.nearest_station,
                base_heat=args.base_heat, base_cool=args.base_cool, base_gdd=args.base_gdd)
    conn.close()


if __name__ == "__main__":
    main()