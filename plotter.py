#!/usr/bin/env python3
"""
plotter.py — Reads weather_archive.db and generates a two-panel comparison plot:
Top: Station temperatures + cascaded unified model line with shaded SD band.
Bottom: G6964 Sensor Error Analysis with error-relative SD uncertainty band
        anchored to 0°F, 3-hour rolling average, 6-hour horizontal mean
        segments with bias badges (including avg G6964 wind speed), and summary
        annotation.

Model cascade: URMA -> RTMA -> HRRR f00 -> HRRR f01-f05 (forecast)

A vertical "URMA Analysis Frontier" line marks the most recent URMA valid_time.
Everything to the right is provisional (RTMA/HRRR fallback data).

A vertical "Now" line marks the current time. Model data to the right of
Now is HRRR forecast for hours that haven't happened yet.

Legends are placed BELOW each respective axes to avoid overlapping data.

6-hour block badges include the average G6964 STATION wind speed (mph)
for that block period — using the actual anemometer readings from
station_obs, NOT the model wind_speed.

Daily bias & SD summary uses LOCAL MIDNIGHT (not UTC) as the cutoff for
each calendar day, matching sunrise/sunset timing.

Daily extremes: for each station (top panel) and the bias analysis
(bottom panel), a marker is plotted at each calendar day's max and min
value (local midnight-to-midnight), annotated with the time and value.

Usage:
    python plotter.py                     # Default: 48 hours
    python plotter.py --days 60           # 60 days
    python plotter.py --hours 144         # Fine-grained: 144 hours
    python plotter.py --days 7 --stations G6964,KSGF
    python plotter.py --no-model          # URMA/RTMA only -- skip HRRR blend & forecast
    python plotter.py --start-date 2026-06-01 --end-date 2026-07-01
"""

import os
import argparse
import sqlite3
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from astral import Observer
from astral.sun import sun

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os as _os
_script_dir = _os.path.dirname(_os.path.abspath(__file__))
_db_env = _os.environ.get("WEATHER_DB_PATH", "weather_archive.db")
DB_PATH = (_db_env if _os.path.isabs(_db_env)
           else _os.path.join(_script_dir, _db_env))

# Model tier hierarchy for cascade unification (higher = better)
MODEL_TIER = {
    "urma": 7,
    "rtma": 6,
    "hrrr_f00": 5, "hrrr_f01": 4, "hrrr_f02": 3,
    "hrrr_f03": 2, "hrrr_f04": 1, "hrrr_f05": 0, "hrrr_f06": -1,
}

# Try to get location from environment, default to Kansas City area
_lat_str = _os.environ.get("WEATHER_LAT")
_lon_str = _os.environ.get("WEATHER_LON")
DEFAULT_LAT = float(_lat_str) if _lat_str else 39
DEFAULT_LON = float(_lon_str) if _lon_str else -94
DEFAULT_TZ = "America/Chicago"


def get_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        print(f"warning: timezone '{name}' unavailable, falling back to UTC")
        return timezone.utc


def get_astral_events(start_dt, end_dt, lat, lon):
    observer = Observer(latitude=lat, longitude=lon)
    events = []
    curr = start_dt.date() - timedelta(days=1)
    end_date = end_dt.date() + timedelta(days=1)
    while curr <= end_date:
        try:
            s = sun(observer, date=curr)
            if start_dt <= s["sunset"] <= end_dt:
                events.append((s["sunset"], "Sunset"))
            if start_dt <= s["sunrise"] <= end_dt:
                events.append((s["sunrise"], "Sunrise"))
        except Exception:
            pass
        curr += timedelta(days=1)
    return events


def get_sunrise_sunset_for_date(date_obj, lat, lon):
    observer = Observer(latitude=lat, longitude=lon)
    try:
        s = sun(observer, date=date_obj)
        return s["sunrise"], s["sunset"]
    except Exception:
        return None, None


def get_analysis_frontier(conn, lat, lon, model_name="urma"):
    cursor = conn.cursor()
    cursor.execute(
        """SELECT MAX(valid_time) FROM model_analysis
           WHERE model = ? AND ABS(latitude - ?) < 0.05 AND ABS(longitude - ?) < 0.05""",
        (model_name, lat, lon),
    )
    row = cursor.fetchone()
    if row and row[0]:
        return pd.to_datetime(row[0], utc=True)
    return None


def _unify_pivoted_column(pivoted):
    """Cascade-unify a single pivoted (index=valid_time, columns=model)
    DataFrame into one series, best-tier-wins per timestamp. Factored out
    of unify_model_cascade so the same logic can be reused for precip
    without a third copy-pasted copy."""
    if pivoted.empty:
        return pd.Series(dtype=float)
    available = [(MODEL_TIER.get(col, -99), col) for col in pivoted.columns
                if MODEL_TIER.get(col, -99) >= -10]
    available.sort(key=lambda x: -x[0])
    if not available:
        return pd.Series(dtype=float)
    combined = pd.Series(dtype=float)
    for _, model_name in available:
        combined = combined.combine_first(pivoted[model_name].dropna())
    combined = combined.sort_index()
    return combined[~combined.index.duplicated(keep="last")]


def unify_model_cascade(pivoted_models, pivoted_sd):
    return _unify_pivoted_column(pivoted_models), _unify_pivoted_column(pivoted_sd)


def print_sd_coverage_diagnostic(unified_sd, cutoff, plot_end, lat, lon):
    """Diagnostic for the 'is temp_sd real, and is it actually bigger at
    night' question. Reports what fraction of the plotted window has a
    real (non-NaN) temp_sd value, and -- for whatever real values exist --
    splits them into day/night buckets using the same sunrise/sunset
    logic as the daily bias summary, so this is an apples-to-apples check
    against the nt_sd/dt_sd numbers already printed there.
    """
    print("\n  --- temp_sd Coverage Diagnostic ---")
    if unified_sd.empty:
        print("  temp_sd: no data at all (column empty/missing) -- "
              "every band you've seen so far is the flat 1.5F fallback.")
        print("  ------------------------------------")
        return

    windowed = unified_sd[(unified_sd.index >= cutoff) & (unified_sd.index <= plot_end)]
    total = len(windowed)
    real = windowed.dropna()
    n_real = len(real)
    pct = (n_real / total * 100) if total else 0.0
    print(f"  Coverage: {n_real}/{total} timestamps have a real temp_sd "
          f"({pct:.1f}%) -- the rest were the 1.5F fallback.")

    if n_real == 0:
        print("  ------------------------------------")
        return

    day_vals, night_vals = [], []
    for ts, val in real.items():
        local_date = ts.date()
        sunrise, _ = get_sunrise_sunset_for_date(local_date, lat, lon)
        # Same longitude quirk as the daily bias/SD summary: the sunset
        # returned for local_date itself is the trailing edge of the
        # PREVIOUS night, not the one ending local_date's own daylight.
        # The real evening sunset for local_date comes from querying
        # local_date + 1 day instead.
        _, evening_sunset = get_sunrise_sunset_for_date(
            local_date + timedelta(days=1), lat, lon)
        if sunrise is None or evening_sunset is None:
            continue
        if sunrise <= ts <= evening_sunset:
            day_vals.append(val)
        else:
            night_vals.append(val)

    if day_vals:
        print(f"  Daytime   real temp_sd: mean {sum(day_vals)/len(day_vals):.2f}F "
              f"over n={len(day_vals)}")
    else:
        print("  Daytime   real temp_sd: no real values in window")

    if night_vals:
        print(f"  Nighttime real temp_sd: mean {sum(night_vals)/len(night_vals):.2f}F "
              f"over n={len(night_vals)}")
    else:
        print("  Nighttime real temp_sd: no real values in window")

    if day_vals and night_vals:
        ratio = (sum(night_vals) / len(night_vals)) / (sum(day_vals) / len(day_vals))
        print(f"  Night/Day ratio: {ratio:.2f}x")
    print("  ------------------------------------")


def compute_period_stats(error_series, period_start, period_end):
    mask = (error_series.index >= period_start) & (error_series.index < period_end)
    subset = error_series[mask]
    n = len(subset)
    if n < 2:
        if n == 1:
            bias_val = subset.iloc[0]
            sign = "+" if bias_val > 0 else ""
            return f"{sign}{bias_val:.2f}F", "  —", n
        return None, None, n
    bias_val = subset.mean()
    sd_val = subset.std()
    sign = "+" if bias_val > 0 else ""
    return f"{sign}{bias_val:.2f}F", f"{sd_val:.2f}F", n


def split_into_8am_periods(series, tz):
    """Split a series into local 8am-to-8am calendar periods. Returns a
    list of (period_start_date, sub_series) tuples in chronological order.

    Uses the standard 'shift back 8h, then take the local date' trick: a
    local timestamp of 08:00-23:59 shifts to that same calendar date, and
    00:00-07:59 shifts to the PREVIOUS date -- exactly matching a period
    that begins at that date's 8am and runs to the next date's 8am.
    """
    s = series.dropna()
    if s.empty:
        return []
    local_idx = s.index.tz_convert(tz)
    shifted = local_idx - pd.Timedelta(hours=8)
    period_key = shifted.date
    df = pd.DataFrame({"value": s.values, "period": period_key}, index=s.index)
    periods = [(period, group["value"]) for period, group in df.groupby("period")]
    periods.sort(key=lambda x: x[0])
    return periods


def compute_period_rows(rows, tz=None):
    """Compute the (label, series, color) rows add_extremes_header-style
    drawing would need, WITHOUT drawing anything -- used to get an exact
    row count before the figure's final size is known (see
    draw_extremes_header_fig for why this two-step split exists)."""
    if tz is not None:
        expanded = []
        for label, series, color in rows:
            for period_date, period_series in split_into_8am_periods(series, tz):
                if period_series.dropna().empty:
                    continue
                expanded.append(
                    (f"{label} {period_date.strftime('%m-%d')}:", period_series, color))
        return expanded
    return [(label, series, color) for label, series, color in rows
           if not series.dropna().empty]


def mark_extremes_on_graph(ax, rows):
    """Plot a plain marker dot at each row's max and min point, directly
    on the line graph -- same (label, series, color) rows already used
    for the above-graph header text, so the marker granularity always
    matches whatever the header shows (per-8am-period for ax1, overall
    for ax2). No text/labels here, just the dot: the header rows already
    say what the value is and when, this just shows where on the line it
    actually is. Deliberately no annotate()/text -- that's exactly what
    collided with 6h block badges before; a bare marker has no bounding
    box wide enough to meaningfully collide with anything.
    """
    for label, series, color in rows:
        s = series.dropna()
        if s.empty:
            continue
        max_val, max_ts = s.max(), s.idxmax()
        min_val, min_ts = s.min(), s.idxmin()
        for ts, val in ((max_ts, max_val), (min_ts, min_val)):
            ax.plot(ts, val, marker="o", markersize=6, color=color or "gray",
                    mec="black", mew=0.8, zorder=6, linestyle="none")


def draw_extremes_header_fig(fig, top_edge, rows, row_height_frac):
    """Draw pre-computed (label, series, color) rows via fig.text() at
    explicit FIGURE-fraction y positions starting just above `top_edge`
    (an axes' top edge in figure-fraction terms, read via
    ax.get_position() AFTER subplots_adjust has run).

    Deliberately fig.text()/figure-fraction, not ax.text()/ax.transAxes:
    transAxes coordinates are relative to the axes' CURRENT box, which
    changes size the moment subplots_adjust/set_size_inches runs --
    creating a circular dependency between "how much space do these rows
    need" (used to size the figure) and "how big is the box they're
    positioned relative to" (which depends on that same sizing). This
    bit first (rendered against the old default box size, then the
    figure got resized and the reserved space didn't match where the
    text actually ended up -- rows clipped off the top of the figure and
    collided with the title). Figure-fraction coordinates have no such
    loop: both the reserved space and the row positions are computed
    from the same already-known total figure height.
    """
    n = len(rows)
    for i, (label, series, color) in enumerate(rows):
        s = series.dropna()
        if s.empty:
            continue
        max_val, max_ts = s.max(), s.idxmax()
        min_val, min_ts = s.min(), s.idxmin()
        text = (f"{label}  Max {max_val:.1f}\u00b0F ({max_ts.strftime('%m-%d %H:%M')})"
               f"   |   Min {min_val:.1f}\u00b0F ({min_ts.strftime('%m-%d %H:%M')})")
        y = top_edge + row_height_frac * (n - i) - row_height_frac * 0.3
        fig.text(0.5, y, text, ha="center", va="bottom", fontsize=8,
                 fontweight="bold", color=color or "black")


def print_daily_extremes(series, label_prefix="", unit="F"):
    """Console-only per-day max/min diagnostic (local calendar days).
    Used to plot these directly on the graph via annotate_daily_extremes,
    which collided with 6h block badges and other in-graph elements at
    wider date ranges -- the visual representation moved to
    add_extremes_header (period-level rows above each panel) instead, but
    the per-day console detail is still useful on its own, so it's kept
    here without the plotting/annotation code that's no longer used."""
    s = series.dropna()
    if s.empty:
        return
    for day, group in s.groupby(s.index.date):
        if len(group) < 2:
            continue
        print(f"  {day} {label_prefix or 'series'}"
             f" Max {group.max():.1f}{unit} @ {group.idxmax().strftime('%H:%M')}"
             f" | Min {group.min():.1f}{unit} @ {group.idxmin().strftime('%H:%M')}")


def render_windrose(df, cutoff, end_time, out_path=None, ax=None, show_calm=False):
    """Render a wind rose (direction/speed frequency) for the given
    timeframe. df must have 'wind_speed' (mph) and 'wind_dir' (compass
    degrees, 0=N/90=E/etc.) columns already filtered to the window of
    interest.

    Two modes:
      - ax=None (default): creates its own standalone figure and saves
        it to out_path as an SVG. Returns the saved path (or None if
        there was nothing to plot).
      - ax=<a polar-projection Axes>: draws directly into that axes
        instead (for embedding as a section of a larger figure) and
        does NOT save/close anything itself -- the caller owns the
        figure's lifecycle. Returns the same ax (or None if there was
        nothing to plot, in which case a short explanatory message is
        left in the axes instead of an empty polar grid).

    By default, speeds below CALM_THRESHOLD_MPH are excluded from the
    directional bins and reported separately as %calm -- a near-zero-speed
    reading's direction is essentially noise, and including it would blur
    every sector's frequency toward the calm-heavy hours instead of
    showing where the real wind actually came from. Pass show_calm=True
    to instead bin calm observations into their own low-speed wedge like
    the direction was trustworthy -- useful for visually matching tools
    that don't exclude calm, at the cost of that statistical caveat.
    """
    CALM_THRESHOLD_MPH = 2.0
    if show_calm:
        SPEED_BINS = [0, CALM_THRESHOLD_MPH, 5, 10, 15, 20, 25, np.inf]
        SPEED_LABELS = [f"<{CALM_THRESHOLD_MPH:g}", f"{CALM_THRESHOLD_MPH:g}-5",
                         "5-10", "10-15", "15-20", "20-25", "25+"]
        SPEED_COLORS = ["#f0f0f0", "#c6dbef", "#6baed6", "#2171b5",
                         "#f4a582", "#d6604d", "#67000d"]
    else:
        SPEED_BINS = [0, 5, 10, 15, 20, 25, np.inf]
        SPEED_LABELS = ["0-5", "5-10", "10-15", "15-20", "20-25", "25+"]
        SPEED_COLORS = ["#c6dbef", "#6baed6", "#2171b5", "#f4a582", "#d6604d", "#67000d"]
    N_SECTORS = 16
    SECTOR_WIDTH = 360.0 / N_SECTORS
    SECTOR_LABELS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                      "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

    embedded = ax is not None
    standalone_fig = None

    def _empty(msg):
        print(f"  [windrose] {msg}")
        if embedded:
            ax.set_xticks([])
            ax.set_yticks([])
            ax.text(0.5, 0.5, msg, transform=ax.transAxes,
                    horizontalalignment="center", verticalalignment="center",
                    fontsize=9, wrap=True)
            return ax
        return None

    total_n = len(df)
    if total_n == 0:
        return _empty("No wind data in this timeframe.")

    calm_mask = df["wind_speed"] < CALM_THRESHOLD_MPH
    calm_pct = calm_mask.mean() * 100

    active = df.copy() if show_calm else df.loc[~calm_mask].copy()
    if active.empty:
        return _empty("All observations below the calm threshold -- nothing to plot.")

    # Some sources report direction on a 0-359 scale that can drift
    # slightly outside that range (e.g. a stray 360) -- normalize rather
    # than let a boundary value silently fall into the wrong sector.
    active["wind_dir"] = active["wind_dir"].astype(float) % 360
    active["sector"] = (((active["wind_dir"] + SECTOR_WIDTH / 2) // SECTOR_WIDTH)
                         % N_SECTORS).astype(int)
    active["speed_bin"] = pd.cut(active["wind_speed"], bins=SPEED_BINS,
                                  labels=SPEED_LABELS, right=False)

    freq = (active.groupby(["sector", "speed_bin"], observed=True).size()
                  .unstack(fill_value=0))
    freq = freq.reindex(index=range(N_SECTORS), fill_value=0)
    freq = freq.reindex(columns=SPEED_LABELS, fill_value=0)
    # Percent of ALL observations (calm included in the denominator), so
    # the bars honestly reflect "how often did wind blow from here at
    # this speed" out of the whole timeframe, not just the non-calm hours.
    freq_pct = freq / total_n * 100

    theta = np.deg2rad(np.arange(N_SECTORS) * SECTOR_WIDTH)

    if not embedded:
        standalone_fig = plt.figure(figsize=(7, 7))
        ax = standalone_fig.add_subplot(111, projection="polar")

    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)

    bottom = np.zeros(N_SECTORS)
    bar_width = np.deg2rad(SECTOR_WIDTH * 0.9)
    for label, color in zip(SPEED_LABELS, SPEED_COLORS):
        vals = freq_pct[label].to_numpy()
        ax.bar(theta, vals, width=bar_width, bottom=bottom, color=color,
               edgecolor="white", linewidth=0.4, label=f"{label} mph")
        bottom += vals

    ax.set_xticks(theta)
    # Cardinal letter + exact degree on each tick, so the mapping between
    # a raw wind_dir value and its plotted position can be checked at a
    # glance against the actual data instead of trusting the compass
    # labels alone.
    tick_labels = [f"{label}\n{deg:g}°"
                   for label, deg in zip(SECTOR_LABELS, np.arange(N_SECTORS) * SECTOR_WIDTH)]
    ax.set_xticklabels(tick_labels, fontsize=8 if not embedded else 7)
    max_r = bottom.max() if bottom.max() > 0 else 1.0
    ax.set_ylim(0, max_r * 1.15)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter())
    ax.tick_params(axis="y", labelsize=8 if not embedded else 7)

    calm_note = (f"Calm (<{CALM_THRESHOLD_MPH:.0f}mph, binned): {calm_pct:.1f}%" if show_calm
                 else f"Calm (<{CALM_THRESHOLD_MPH:.0f}mph, excluded): {calm_pct:.1f}%")
    title = (f"G6964 Wind Rose  |  {cutoff.strftime('%Y-%m-%d %H:%M')} -> "
             f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC\n"
             f"n={total_n}  |  {calm_note}")
    ax.set_title(title, fontsize=11 if not embedded else 9.5, pad=24 if not embedded else 18)
    ax.legend(loc="upper left", bbox_to_anchor=(1.05, 1.08),
              fontsize=8 if not embedded else 7,
              title="Speed (mph)", frameon=True)

    if embedded:
        return ax

    standalone_fig.savefig(out_path, format="svg", bbox_inches="tight")
    plt.close(standalone_fig)
    print(f"  [windrose] Saved to {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate weather station comparison and bias analysis plots."
    )
    parser.add_argument("--days", type=float, default=None,
                        help="Lookback window in DAYS. Overrides --hours.")
    parser.add_argument("--hours", type=float, default=48.0,
                        help="Lookback window in hours (default: 48).")
    parser.add_argument("--start-date", type=str, default=None,
                        help="Start date (inclusive). Flexible parsing.")
    parser.add_argument("--end-date", type=str, default=None,
                        help="End date (inclusive). Defaults to now.")
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT,
                        help="Latitude for astral solar calculations")
    parser.add_argument("--lon", type=float, default=DEFAULT_LON,
                        help="Longitude for astral solar calculations")
    parser.add_argument("--stations", type=str, default=None,
                        help="Comma-separated station IDs to plot (default: all in DB).")
    parser.add_argument("--no-model", action="store_true",
                        help="Skip the HRRR blend and forecast tail on the top "
                             "panel; URMA/RTMA (observation analysis) still plots")
    parser.add_argument("--windrose", action="store_true",
                        help=("Also render a wind rose (direction vs speed "
                              "frequency) for G6964 station data over the "
                              "SAME timeframe as the main plot (cutoff -> "
                              "now), added as its own section below the "
                              "main two panels in the SAME output file. "
                              "The line-graph panels stay the same size; "
                              "the figure just gets taller."))
    parser.add_argument("--windrose-show-calm", action="store_true",
                        help=("Bin calm (<2mph) observations into the rose as "
                              "their own low-speed wedge instead of excluding "
                              "them and reporting calm as a title percentage. "
                              "Matches tools that don't exclude calm from the "
                              "directional bins; no effect without --windrose."))
    parser.add_argument("--tz", default=DEFAULT_TZ,
                        help=f"IANA zone for the 8am-8am local period table "
                             f"(default: {DEFAULT_TZ})")
    args = parser.parse_args()
    tz = get_tz(args.tz)

    now_utc = pd.Timestamp.now(tz="UTC")
    FORECAST_EXT_HOURS = 7
    # This buffer only exists to give the HRRR forecast tail somewhere to
    # render (lead-in on the left, forecast room on the right). --no-model
    # drops HRRR entirely -- URMA/RTMA (pure observation) are drawn
    # separately and need neither a lead-in nor forward room, since they're
    # just plotted straight within [cutoff, plot_end] -- so with --no-model
    # this buffer has nothing left to serve on either end.
    model_lead_hours = 0.0 if args.no_model else FORECAST_EXT_HOURS

    if args.start_date:
        start_time = pd.to_datetime(args.start_date, utc=True)
        end_time = pd.to_datetime(args.end_date, utc=True) if args.end_date else now_utc
        if end_time > now_utc:
            end_time = now_utc
        lookback_hours = (now_utc - start_time).total_seconds() / 3600
        print(f"Date range: {start_time.strftime('%Y-%m-%d %H:%M')} -> "
              f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")
    elif args.days is not None:
        lookback_hours = args.days * 24.0
        end_time = now_utc
        start_time = now_utc - timedelta(hours=lookback_hours + model_lead_hours)
        print(f"Lookback: {args.days} days ({lookback_hours:.0f} hours)")
    else:
        lookback_hours = args.hours
        end_time = now_utc
        start_time = now_utc - timedelta(hours=lookback_hours + model_lead_hours)
        print(f"Lookback: {lookback_hours:.0f} hours ({lookback_hours/24:.1f} days)")

    cutoff = start_time

    # The windrose deliberately shares this SAME cutoff with the station
    # observations plotted on the top panel, rather than computing its
    # own tightened window -- so "what wind directions actually occurred"
    # always matches "what temperature/humidity data is shown" for the
    # exact same stretch of time, instead of two subtly different date
    # ranges that happen to both say "G6964" on the same page. This does
    # mean an explicit --hours N won't produce an exactly-N-hour windrose
    # (cutoff includes the +FORECAST_EXT_HOURS lead-in used by the model
    # cascade on the line graph) -- that's the accepted tradeoff for
    # consistency over precision here.

    # plotter.py is a short-range diagnostic tool -- hourly bias tracking,
    # 6h block summaries, per-day Full/Day/Night breakdown -- not a
    # calendar/monthly view. Trying to render a multi-month span here is
    # what caused a MAXTICKS locator crash and an unbounded summary-box
    # height in earlier runs. Rather than just making long ranges not
    # crash, redirect to the tool actually built for that: bymonth.py (or
    # bymonth_viz.py for the compact calendar-with-charts version).
    MAX_PLOTTER_DAYS = 14
    requested_days = lookback_hours / 24.0
    if requested_days > MAX_PLOTTER_DAYS:
        print(f"\nRequested range is {requested_days:.0f} days, but "
             f"plotter.py tops out at {MAX_PLOTTER_DAYS} days -- it's a "
             f"short-range diagnostic tool (hourly bias tracking, 6h "
             f"block summaries), not built for month-plus spans.")
        print(f"\nFor a {requested_days:.0f}-day view, use bymonth.py "
             f"instead:")
        print(f"  python bymonth.py YYYY-MM                 "
             f"# one calendar month, text/HTML matrix")
        print(f"  python bymonth_viz.py --station <ID> "
             f"--year YYYY --month MM   # compact calendar with charts")
        print(f"\n(Or pass --days {MAX_PLOTTER_DAYS} or less to stay "
             f"within plotter.py's range.)")
        return

    station_filter = None
    if args.stations:
        station_filter = [s.strip().upper() for s in args.stations.split(",")]
        print(f"Station filter: {station_filter}")

    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found. Run collector.py first.")
        return

    conn = sqlite3.connect(DB_PATH)
    cutoff_iso = cutoff.isoformat()

    # --- LOAD STATION DATA ---
    # NOTE: filtered by timestamp >= cutoff at the SQL level. Without this,
    # every run loads the ENTIRE station_obs table into memory regardless
    # of --hours/--days, which doesn't scale once IEM full-history backfills
    # make this table much larger than just the requested plot window.
    if station_filter:
        placeholders = ",".join("?" for _ in station_filter)
        query = (f"SELECT station, timestamp, temperature "
                 f"FROM station_obs WHERE station IN ({placeholders}) "
                 f"AND timestamp >= ? ORDER BY timestamp")
        df_stations = pd.read_sql(query, conn,
                                   params=station_filter + [cutoff_iso],
                                   parse_dates=["timestamp"])
    else:
        df_stations = pd.read_sql(
            "SELECT station, timestamp, temperature FROM station_obs "
            "WHERE timestamp >= ? ORDER BY timestamp",
            conn, params=[cutoff_iso], parse_dates=["timestamp"])

    if not df_stations.empty:
        df_stations["timestamp"] = pd.to_datetime(df_stations["timestamp"], utc=True)
        pivoted_stations = df_stations.pivot(index="timestamp", columns="station", values="temperature")
    else:
        pivoted_stations = pd.DataFrame()

    # --- LOAD PER-STATION HOURLY RAINFALL ---
    # Two genuinely different data sources need genuinely different
    # handling here, not one query for both:
    #
    #   - Ambient stations (G6964): rain_hourly ("hourlyrainin") is a
    #     RATE (in/hr at that instant), NOT a period-accumulated total --
    #     confirmed by bymonth_viz.py's hourly_precipitation(), which
    #     deliberately never uses rain_hourly at all and instead derives
    #     true hourly rain from the DELTA of rain_daily (a running
    #     counter that resets at local midnight) within each clock hour.
    #     Plotting rain_hourly directly, even just one reading per hour,
    #     can badly overstate actual rainfall (a brief heavy burst's
    #     instantaneous rate isn't what fell over the whole hour).
    #
    #   - ASOS stations (KSGF/KBBG): rain_hourly (p01i) genuinely IS a
    #     trailing-60-minute accumulated total, but routine+SPECI reports
    #     can put several overlapping-window readings in the same clock
    #     hour during active rain -- confirmed against real data. Fixed
    #     separately by keeping just the last report per hour.
    #
    # Distinguished by whether rain_daily has any real data for that
    # station in this window, not by hardcoding "G6964" by name -- if a
    # future station source also reports via a Ambient-style running
    # counter, this picks up the correct method automatically.
    station_rain_series = {}  # {station: pd.Series}
    if not pivoted_stations.empty:
        stations_to_load = list(pivoted_stations.columns)
        placeholders = ",".join("?" for _ in stations_to_load)
        rain_query = (
            f"SELECT station, timestamp, rain_hourly, rain_daily FROM station_obs "
            f"WHERE station IN ({placeholders}) "
            f"AND (rain_hourly IS NOT NULL OR rain_daily IS NOT NULL) "
            f"AND timestamp >= ? ORDER BY timestamp")
        rain_df = pd.read_sql(rain_query, conn,
                              params=stations_to_load + [cutoff_iso],
                              parse_dates=["timestamp"])
        if not rain_df.empty:
            rain_df["timestamp"] = pd.to_datetime(rain_df["timestamp"], utc=True)
            rain_df = rain_df.sort_values("timestamp")
            rain_df = rain_df[~rain_df[["timestamp", "station"]].duplicated(keep="last")]
            rain_df["hour_bucket"] = rain_df["timestamp"].dt.floor("h")

            for st, grp in rain_df.groupby("station"):
                uses_running_counter = grp["rain_daily"].notna().any()

                if uses_running_counter:
                    # Ambient-style: delta of rain_daily's first vs last
                    # reading within each hour bucket. max(0, ...) guards
                    # the local-midnight reset boundary the same way
                    # bymonth_viz.py's identical logic already does --
                    # accepted existing tradeoff, not a new gap introduced
                    # here (an hour spanning the reset can understate
                    # slightly; not attempting a harder fix than what's
                    # already established elsewhere in this codebase).
                    daily = grp.dropna(subset=["rain_daily"]).sort_values("timestamp")
                    hourly_vals = {}
                    for hour, hgrp in daily.groupby("hour_bucket"):
                        delta = max(0.0, hgrp["rain_daily"].iloc[-1] - hgrp["rain_daily"].iloc[0])
                        hourly_vals[hgrp["timestamp"].iloc[-1]] = delta
                    if hourly_vals:
                        station_rain_series[st] = pd.Series(hourly_vals).sort_index()
                        print(f"  {st} station rain (derived from rain_daily "
                             f"delta, Ambient-style): "
                             f"{len(station_rain_series[st])} hour(s) loaded")
                else:
                    # ASOS-style: rain_hourly IS a real period total;
                    # collapse same-hour routine+SPECI overlap to the
                    # last (most recent) report per clock hour.
                    hourly = grp.dropna(subset=["rain_hourly"])
                    if hourly.empty:
                        continue
                    before_n = len(hourly)
                    hourly = hourly.groupby("hour_bucket", as_index=False).last()
                    deduped_n = before_n - len(hourly)
                    if deduped_n:
                        print(f"  (collapsed {deduped_n} overlapping same-hour "
                             f"rain report(s) for {st} -- ASOS routine+SPECI "
                             f"reports share overlapping 60-min precip windows)")
                    station_rain_series[st] = hourly.set_index("timestamp")["rain_hourly"]
                    print(f"  {st} station rain_hourly: "
                         f"{len(station_rain_series[st])} points loaded")

    # --- SELECT WIND SPEED+DIRECTION COLUMNS (schema-introspected, shared) ---
    # Done once, unconditionally, so every wind figure on this plot --
    # the 6h block badges AND the windrose -- reads from the SAME
    # physical quantity. station_obs commonly has raw instantaneous
    # wind_speed/wind_dir alongside 2-minute and 10-minute averaged
    # versions (and a separate, NOT interchangeable, gust direction) --
    # picking different columns for different parts of the plot would
    # have the badges quoting a gustier/noisier number than the rose's
    # own direction distribution implies, which reads as inconsistent
    # even though both would be individually "correct" in isolation.
    #
    # Existence in the schema isn't enough to pick a pair -- confirmed
    # against a real DB where wind_dir_avg10m was fully populated but
    # wind_speed_avg10m was 100% NULL (same class of bug as the earlier
    # temp_sd issue: a column that exists but was never actually written
    # for this station). Each candidate pair's ACTUAL non-null counts in
    # the plotted window are checked before committing to it.
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(station_obs)")
    existing_cols = {row[1].lower(): row[1] for row in cursor.fetchall()}
    PAIR_PRIORITY = [
        ("wind_speed_avg10m", "wind_dir_avg10m"),
        ("wind_speed_avg2m", "wind_dir_avg2m"),
        ("wind_speed", "wind_dir"),
        ("wind_speed", "winddir"),
        ("windspeed", "winddir"),
        ("wind_speed", "wind_direction"),
    ]
    wind_speed_col = None
    wind_dir_col = None
    for speed_cand, dir_cand in PAIR_PRIORITY:
        if speed_cand not in existing_cols or dir_cand not in existing_cols:
            continue
        speed_real, dir_real = existing_cols[speed_cand], existing_cols[dir_cand]
        pop_check = cursor.execute(
            f"SELECT COUNT({speed_real}), COUNT({dir_real}) FROM station_obs "
            f"WHERE station = 'G6964' AND timestamp >= ?", [cutoff_iso]
        ).fetchone()
        speed_n, dir_n = pop_check
        if speed_n > 0 and dir_n > 0:
            wind_speed_col, wind_dir_col = speed_real, dir_real
            break
        else:
            print(f"  [wind] Skipping {speed_real}+{dir_real}: only "
                  f"{speed_n}/{dir_n} non-null in this window (both must "
                  f"be > 0).")
    if wind_speed_col is None and "wind_speed" in existing_cols:
        # No direction-paired column has real data in this window, but
        # raw wind_speed alone might still -- keep it for the badges
        # even though the windrose (which needs direction too) won't
        # have anything to draw from.
        speed_real = existing_cols["wind_speed"]
        speed_n = cursor.execute(
            f"SELECT COUNT({speed_real}) FROM station_obs "
            f"WHERE station = 'G6964' AND timestamp >= ?", [cutoff_iso]
        ).fetchone()[0]
        if speed_n > 0:
            wind_speed_col = speed_real

    if wind_speed_col:
        pairing_note = f" (paired with {wind_dir_col} for --windrose)" if wind_dir_col else ""
        print(f"  Wind speed source: {wind_speed_col}{pairing_note}")
    else:
        print(f"  No wind_speed-like column found in station_obs. "
              f"Available columns: {sorted(existing_cols.values())}")

    # --- LOAD G6964 STATION WIND SPEED (6h block badges) ---
    station_wind_series = pd.Series(dtype=float)
    if not df_stations.empty and wind_speed_col:
        wind_query = (f"SELECT timestamp, {wind_speed_col} AS wind_speed FROM station_obs "
                     f"WHERE station = 'G6964' AND {wind_speed_col} IS NOT NULL "
                     f"AND timestamp >= ? ORDER BY timestamp")
        wind_df = pd.read_sql(wind_query, conn, params=[cutoff_iso],
                              parse_dates=["timestamp"])
        if not wind_df.empty:
            wind_df["timestamp"] = pd.to_datetime(wind_df["timestamp"], utc=True)
            wind_df = wind_df.sort_values("timestamp")
            wind_df = wind_df[~wind_df["timestamp"].duplicated(keep="last")]
            station_wind_series = wind_df.set_index("timestamp")["wind_speed"]
            print(f"  G6964 station wind_speed ({wind_speed_col}): "
                  f"{len(station_wind_series)} points loaded")

    # --- LOAD G6964 WIND SPEED+DIRECTION FOR --windrose ---
    windrose_df = pd.DataFrame()
    if args.windrose:
        if wind_dir_col is None:
            print(f"  [windrose] No matched wind speed+direction column pair "
                  f"found in station_obs (checked "
                  f"{', '.join(f'{s}+{d}' for s, d in PAIR_PRIORITY)}). "
                  f"Available columns: {sorted(existing_cols.values())}. "
                  f"Skipping --windrose.")
        else:
            windrose_query = (
                f"SELECT timestamp, {wind_speed_col} AS wind_speed, "
                f"{wind_dir_col} AS wind_dir FROM station_obs "
                f"WHERE station = 'G6964' AND {wind_speed_col} IS NOT NULL "
                f"AND {wind_dir_col} IS NOT NULL "
                f"AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp")
            windrose_df = pd.read_sql(
                windrose_query, conn, params=[cutoff_iso, now_utc.isoformat()],
                parse_dates=["timestamp"])
            if not windrose_df.empty:
                windrose_df["timestamp"] = pd.to_datetime(windrose_df["timestamp"], utc=True)
                windrose_df = windrose_df.sort_values("timestamp")
                windrose_df = windrose_df[~windrose_df["timestamp"].duplicated(keep="last")]
                print(f"  [windrose] G6964 {wind_speed_col}+{wind_dir_col}: "
                      f"{len(windrose_df)} points loaded ({cutoff.strftime('%Y-%m-%d %H:%M')} "
                      f"-> {now_utc.strftime('%Y-%m-%d %H:%M')} UTC)")
            else:
                print(f"  [windrose] No overlapping {wind_speed_col}+{wind_dir_col} "
                      f"rows in this timeframe. Skipping --windrose plot.")

    # --- LOAD MODEL DATA ---
    # No upper bound here (deliberately) -- future HRRR forecast rows past
    # "now" are wanted for the forecast-extension display, only the lower
    # bound trims years of backfilled history we're not plotting.
    df_model = pd.read_sql(
        "SELECT model, valid_time, temp_f, temp_sd, precip FROM model_analysis "
        "WHERE valid_time >= ? ORDER BY valid_time",
        conn, params=[cutoff_iso], parse_dates=["valid_time"])

    urma_frontier = get_analysis_frontier(conn, args.lat, args.lon, "urma")
    rtma_frontier = get_analysis_frontier(conn, args.lat, args.lon, "rtma")

    conn.close()

    if not df_model.empty:
        df_model["valid_time"] = pd.to_datetime(df_model["valid_time"], utc=True)
        df_model = df_model.drop_duplicates(subset=["valid_time", "model"], keep="last")
        pivoted_models = df_model.pivot(index="valid_time", columns="model", values="temp_f")

        if "temp_sd" in df_model.columns:
            pivoted_sd = df_model.pivot(index="valid_time", columns="model", values="temp_sd")
        else:
            pivoted_sd = pd.DataFrame()

        if "precip" in df_model.columns:
            pivoted_precip = df_model.pivot(index="valid_time", columns="model", values="precip")
        else:
            pivoted_precip = pd.DataFrame()
    else:
        pivoted_models = pd.DataFrame()
        pivoted_sd = pd.DataFrame()
        pivoted_precip = pd.DataFrame()

    # --- UNIFY MODEL CASCADE ---
    unified_model, unified_sd = unify_model_cascade(pivoted_models, pivoted_sd)
    unified_precip = _unify_pivoted_column(pivoted_precip)

    # model_for_bias/sd_for_bias are the single source of truth for BOTH
    # the top-panel line and the bottom-panel bias math -- previously the
    # top panel switched to URMA/RTMA-only under --no-model but the bias
    # panel kept reading the full unified_model (HRRR included) regardless,
    # so the two panels disagreed once RTMA's frontier fell behind the
    # station feed: the bias line kept going past that frontier by
    # silently switching to comparing station obs against HRRR forecast
    # instead of ending there. Deriving both panels from the same series
    # makes that impossible.
    if args.no_model:
        obs_cols = [c for c in ("urma", "rtma") if c in pivoted_models.columns]
        model_for_bias = (_unify_pivoted_column(pivoted_models[obs_cols])
                           if obs_cols else pd.Series(dtype=float))
        sd_obs_cols = [c for c in ("urma", "rtma") if c in pivoted_sd.columns] if not pivoted_sd.empty else []
        sd_for_bias = (_unify_pivoted_column(pivoted_sd[sd_obs_cols])
                        if sd_obs_cols else pd.Series(dtype=float))
        if not model_for_bias.empty:
            model_for_bias.index = pd.to_datetime(model_for_bias.index, utc=True)
        if not sd_for_bias.empty:
            sd_for_bias.index = pd.to_datetime(sd_for_bias.index, utc=True)
    else:
        model_for_bias = unified_model
        sd_for_bias = unified_sd

    if not unified_model.empty:
        unified_model.index = pd.to_datetime(unified_model.index, utc=True)
    if not unified_sd.empty:
        unified_sd.index = pd.to_datetime(unified_sd.index, utc=True)
    if not unified_precip.empty:
        unified_precip.index = pd.to_datetime(unified_precip.index, utc=True)
        print(f"  Model precip (unified cascade): {len(unified_precip)} points loaded")

    # --- PRINT FRONTIER INFO ---
    if urma_frontier is not None:
        age_h = (now_utc - urma_frontier).total_seconds() / 3600
        print(f"\n  URMA Analysis Frontier: {urma_frontier.strftime('%Y-%m-%d %H:%M')} UTC "
              f"({age_h:.1f}h old)")
    else:
        print(f"\n  No URMA data in DB.")

    if rtma_frontier is not None:
        rtma_age = (now_utc - rtma_frontier).total_seconds() / 3600
        print(f"  RTMA Analysis Frontier: {rtma_frontier.strftime('%Y-%m-%d %H:%M')} UTC "
              f"({rtma_age:.1f}h old)")

    if not pivoted_models.empty:
        print(f"\n  Model tier breakdown in DB:")
        for col in sorted(pivoted_models.columns, key=lambda c: -MODEL_TIER.get(c, -99)):
            count = pivoted_models[col].dropna().shape[0]
            max_valid = pivoted_models[col].dropna().index.max()
            hours_ahead = (max_valid - now_utc).total_seconds() / 3600 if max_valid else 0
            print(f"    {col:12s}: {count:4d} points | "
                  f"latest: {max_valid.strftime('%Y-%m-%d %H:%M')} UTC "
                  f"({hours_ahead:+.1f}h from now)")
    else:
        print(f"\n  No model data in DB.")

    # --- CHECK FOR HRRR FORECAST DATA ---
    hrrr_models = [col for col in pivoted_models.columns if col.startswith("hrrr_f")]
    if hrrr_models:
        hrrr_data = pivoted_models[hrrr_models].dropna(how="all")
        if not hrrr_data.empty:
            max_hrrr_time = hrrr_data.index.max()
            hours_future = (max_hrrr_time - now_utc).total_seconds() / 3600
            print(f"\n  HRRR forecast extends {hours_future:.1f}h into future "
                  f"(to {max_hrrr_time.strftime('%Y-%m-%d %H:%M')} UTC)")
        else:
            print(f"\n  HRRR models found in DB schema but all rows are NULL")
    else:
        print(f"\n  WARNING: No HRRR forecast data in DB. "
              f"Run 'python collect_models.py' to fetch it.")

    # --- DETERMINE PLOT END ---
    plot_end = end_time
    future_cap = now_utc + pd.Timedelta(hours=FORECAST_EXT_HOURS)

    # Same reasoning as model_lead_hours above: this only exists to leave
    # room for the HRRR forecast tail, which --no-model drops entirely
    # (URMA/RTMA are drawn separately and never need forward room). Model
    # rows are loaded from the DB unconditionally (unified_model isn't
    # gated on --no-model, only the top-panel plot call is), so without
    # this guard the window could still balloon forward even with the
    # HRRR blend turned off.
    if not args.no_model and not unified_model.empty:
        max_model_time = unified_model.index.max()
        if max_model_time > end_time:
            plot_end = min(max_model_time, future_cap)
            future_hours = (plot_end - now_utc).total_seconds() / 3600
            print(f"\n  Model forecast extends {future_hours:.1f}h into future — "
                  f"plot window extended to {plot_end.strftime('%H:%M')} UTC")

    print_sd_coverage_diagnostic(unified_sd, cutoff, plot_end, args.lat, args.lon)

    os.makedirs("results", exist_ok=True)

    # --- FIGURE ---
    # Keep the original compact 14" width through 72h (plotter.py's
    # primary short-range use case), only expanding beyond that. Now that
    # daily max/min moved to header rows above the graph (see
    # add_extremes_header), the only remaining in-graph crowding concern
    # is the 6h block badges bumping into each other as more days get
    # packed into the same width.
    INCHES_PER_EXTRA_DAY = 2
    plot_span_hours = (plot_end - cutoff).total_seconds() / 3600
    if plot_span_hours <= 72:
        fig_w = 16
    else:
        extra_days = (plot_span_hours - 72) / 24
        fig_w = 16 + extra_days * INCHES_PER_EXTRA_DAY

    fig, (ax1, ax2) = plt.subplots(
        nrows=2, ncols=1, figsize=(fig_w, 13.5), sharex=True,
        gridspec_kw={"height_ratios": [3.5, 1.8]},
    )

    # --- SOLAR EVENT LINES ---
    solar_events = get_astral_events(cutoff, plot_end, args.lat, args.lon)
    seen_labels = set()
    for event_time, label in solar_events:
        line_style = "--" if label == "Sunset" else ":"
        line_color = "dimgray" if label == "Sunset" else "goldenrod"
        lbl = label if label not in seen_labels else ""
        if lbl:
            seen_labels.add(lbl)
        ax1.axvline(event_time, color=line_color, linestyle=line_style,
                    linewidth=1.0, alpha=0.8, label=lbl)
        ax2.axvline(event_time, color=line_color, linestyle=line_style,
                    linewidth=1.0, alpha=0.8, zorder=11)

    # --- ANALYSIS FRONTIER LINES ---
    if urma_frontier is not None and cutoff <= urma_frontier <= plot_end:
        ax1.axvline(urma_frontier, color="darkgreen", linestyle="-", linewidth=2.0,
                    alpha=0.7, label=f"URMA Frontier ({urma_frontier.strftime('%H:%M')} UTC)")
        ax2.axvline(urma_frontier, color="darkgreen", linestyle="-", linewidth=2.0,
                    alpha=0.7, zorder=11)
        ax1.axvspan(urma_frontier, plot_end, color="orange", alpha=0.04, zorder=0)
        ax2.axvspan(urma_frontier, plot_end, color="orange", alpha=0.04, zorder=0)

    if rtma_frontier is not None and cutoff <= rtma_frontier <= plot_end:
        if urma_frontier is None or rtma_frontier > urma_frontier:
            ax1.axvline(rtma_frontier, color="teal", linestyle="--", linewidth=1.5,
                        alpha=0.6, label=f"RTMA Frontier ({rtma_frontier.strftime('%H:%M')} UTC)")
            ax2.axvline(rtma_frontier, color="teal", linestyle="--", linewidth=1.5,
                        alpha=0.6, zorder=11)

    # --- "NOW" VERTICAL LINE ---
    if cutoff <= now_utc <= plot_end:
        ax1.axvline(now_utc, color="red", linestyle="-", linewidth=1.5,
                    alpha=0.6, label=f"Now ({now_utc.strftime('%H:%M')} UTC)")
        ax2.axvline(now_utc, color="red", linestyle="-", linewidth=1.5, alpha=0.6, zorder=11)
        ax1.axvspan(now_utc, plot_end, color="purple", alpha=0.06, zorder=0)
        ax2.axvspan(now_utc, plot_end, color="purple", alpha=0.06, zorder=0)
        ax2.axvspan(now_utc, plot_end, hatch="///", facecolor="none",
                    edgecolor="purple", alpha=0.3, zorder=0)

    # --- TOP PANEL: TEMPERATURE TIME SERIES ---
    station_colors = {
        "G6964": "crimson", "KSGF": "darkorange", "KBBG": "forestgreen",
    }

    STATION_GAP_THRESHOLD = pd.Timedelta(hours=2)

    plotted_stations = []
    station_series_for_summary = {}
    for col in pivoted_stations.columns:
        sub = pivoted_stations[[col]].dropna()
        sub = sub[(sub.index >= cutoff) & (sub.index <= end_time)]
        if not sub.empty:
            color = station_colors.get(col, None)

            time_diffs = sub.index.to_series().diff()
            gap_mask = time_diffs > STATION_GAP_THRESHOLD

            if gap_mask.any():
                segments = []
                for idx, (ts, val) in enumerate(sub[col].items()):
                    if idx > 0 and gap_mask.iloc[idx]:
                        prev_ts = sub.index[idx - 1]
                        midpoint = prev_ts + (ts - prev_ts) / 2
                        segments.append(pd.Series([np.nan], index=[midpoint]))
                    segments.append(pd.Series([val], index=[ts]))
                plot_series = pd.concat(segments).sort_index()
            else:
                plot_series = sub[col]

            ax1.plot(plot_series.index, plot_series.values, linestyle="-",
                     linewidth=1.2, alpha=0.9, color=color, label=f"Station: {col}")
            plotted_stations.append(col)
            # Daily max/min used to be annotated directly on the graph
            # here -- moved to a summary row above the plot instead (see
            # add_extremes_header below), since in-graph annotations
            # collided with 6h block badges and other elements at wider
            # date ranges with no clean fix within the plot area itself.
            station_series_for_summary[col] = (sub[col], color or "gray")
            print_daily_extremes(sub[col], label_prefix=f"{col}: ")

    # --- TOP PANEL: MODEL LINE(S) ---
    # URMA/RTMA are observation-quality analysis, not a forecast -- they
    # stay visible even under --no-model. --no-model only turns off HRRR
    # (the forecast tier) and its forward-looking tail; it does not mean
    # "hide everything model-derived." Uses model_for_bias/sd_for_bias so
    # this panel and the bias panel below always agree on which series
    # they're drawing from.
    if args.no_model:
        obs_series = model_for_bias[(model_for_bias.index >= cutoff) & (model_for_bias.index <= plot_end)]
        if not obs_series.empty:
            ax1.plot(obs_series.index, obs_series.values, linestyle="--",
                     linewidth=1.8, color="purple",
                     label="URMA/RTMA (observation analysis)")
    elif not model_for_bias.empty:
        sub_unified = model_for_bias.dropna()
        sub_unified = sub_unified[(sub_unified.index >= cutoff) & (sub_unified.index <= plot_end)]
        if not sub_unified.empty:
            observed_mask = sub_unified.index <= now_utc
            future_mask = sub_unified.index > now_utc

            if observed_mask.any():
                ax1.plot(sub_unified.index[observed_mask], sub_unified[observed_mask],
                         linestyle="--", linewidth=1.8,
                         color="purple", label="Model (URMA->RTMA->HRRR Cascade)")

            if future_mask.any():
                ax1.plot(sub_unified.index[future_mask], sub_unified[future_mask],
                         linestyle=":", linewidth=1.8, marker="o", markersize=4,
                         color="purple", alpha=0.7,
                         label="HRRR Forecast (future hours)")

            if not sd_for_bias.empty:
                sub_sd = sd_for_bias.reindex(sub_unified.index)
                if sub_sd.notna().any():
                    # Only fill_between where temp_sd is real; NaN stretches
                    # render as gaps in the band instead of a fabricated
                    # flat value, so what you see is what the DB actually
                    # knows.
                    ax1.fill_between(sub_unified.index, sub_unified - sub_sd, sub_unified + sub_sd,
                                     color="purple", alpha=0.12, linewidth=0,
                                     label="Model Spatial Stencil SD (+/-1sigma, real values only)")

    # --- HOURLY RAINFALL (twin y-axis): station(s) vs model ---
    # Separate y-axis since rain (inches) and temperature (F) are
    # different units/scales entirely -- twinx() shares the same x-axis
    # (time) so the two stay time-aligned without forcing a shared scale.
    # One bar series per plotted station (reusing that station's own line
    # color for visual consistency) plus one for the model, generalized
    # from an earlier G6964-only version now that KSGF/KBBG also populate
    # rain_hourly.
    ax1_rain = ax1.twinx()
    rain_max_seen = 0.01  # floor avoids a degenerate ylim when there's no rain

    for st, rain_series in station_rain_series.items():
        sub_rain = rain_series[(rain_series.index >= cutoff) &
                               (rain_series.index <= end_time)]
        if not sub_rain.empty:
            color = station_colors.get(st, "steelblue")
            ax1_rain.bar(sub_rain.index, sub_rain.values, width=0.03,
                         color=color, alpha=0.5, zorder=1,
                         label=f"{st} Rain (hourly, in)")
            rain_max_seen = max(rain_max_seen, sub_rain.max())

    if not unified_precip.empty:
        sub_precip = unified_precip[(unified_precip.index >= cutoff) &
                                    (unified_precip.index <= plot_end)]
        if not sub_precip.empty:
            ax1_rain.bar(sub_precip.index, sub_precip.values, width=0.03,
                         color="darkorange", alpha=0.4, zorder=0,
                         label="Model Precip (hourly, in)")
            rain_max_seen = max(rain_max_seen, sub_precip.max())

    # Confine bars to roughly the bottom third of the panel so they read
    # as a subtle accent under the temperature lines, not a competing
    # full-height chart occupying the same visual space.
    ax1_rain.set_ylim(0, rain_max_seen * 3.3)
    ax1_rain.set_ylabel("Rain (in)", fontsize=9, color="steelblue")
    ax1_rain.tick_params(axis="y", labelcolor="steelblue", labelsize=8)
    ax1_rain.grid(False)  # ax1's own grid is enough; a second grid would clutter

    ax1.set_title(
        f"Temperature Comparison & G6964 Sensor Bias (Solar Marked)",
        fontsize=12, fontweight="bold",
    )
    ax1.set_ylabel("Temperature (F)", fontsize=11)
    ax1.grid(True, linestyle=":", alpha=0.5)
    ax1.set_zorder(ax1_rain.get_zorder() + 1)  # keep temp lines/legend clickable above the rain bars
    ax1.patch.set_visible(False)  # ax1's background would otherwise hide ax1_rain's bars
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax1_rain.get_legend_handles_labels()
    ax1.legend(
        h1 + h2, l1 + l2,
        loc="upper center", bbox_to_anchor=(0.5, -0.08),
        ncol=3, frameon=True, facecolor="white", framealpha=0.9, fontsize=8.5,
    )
    ax1_period_rows = compute_period_rows(
        [(f"{col}:", series, color)
         for col, (series, color) in station_series_for_summary.items()],
        tz=tz)
    ax1_header_rows = len(ax1_period_rows)
    mark_extremes_on_graph(ax1, ax1_period_rows)

    # --- BOTTOM PANEL: G6964 BIAS ---
    daily_lines = []
    box_bottom = 0.0  # safe default if the daily summary box below never gets drawn

    if (not pivoted_stations.empty and "G6964" in pivoted_stations.columns
            and not model_for_bias.empty):
        station_df = pivoted_stations[["G6964"]].dropna().reset_index()
        station_df.columns = ["timestamp", "station_val"]
        station_df = station_df.dropna(subset=["timestamp"])
        station_df.set_index("timestamp", inplace=True)

        model_df = model_for_bias.dropna().reset_index()
        model_df.columns = ["timestamp", "model_val"]
        model_df = model_df.dropna(subset=["timestamp"])
        model_df = model_df.sort_values("timestamp")
        model_df = model_df[~model_df["timestamp"].duplicated(keep="last")]
        model_df.set_index("timestamp", inplace=True)

        combined_df = pd.concat([model_df, station_df], axis=1, sort=False)
        combined_df = combined_df[combined_df.index.notnull()]
        combined_df = combined_df.sort_index()
        # limit_area="inside" is load-bearing: plain interpolate(method="time")
        # doesn't just fill interior gaps, it also forward-fills trailing
        # NaNs with the last real value (and would back-fill leading ones
        # too). Without this, once either the model cascade or the station
        # feed stops updating, that column goes flat instead of ending --
        # and error_series/rolling_bias below would keep computing off that
        # fabricated flat tail instead of stopping where the real data does.
        # Note model_df is already restricted to model_for_bias -- under
        # --no-model that's URMA/RTMA only, so this interpolation can't
        # smuggle HRRR forecast values in past the RTMA frontier either;
        # the model column simply has nothing beyond it to interpolate from.
        combined_df["model_interpolated"] = combined_df["model_val"].interpolate(
            method="time", limit_area="inside")
        combined_df["station_interpolated"] = combined_df["station_val"].interpolate(
            method="time", limit_area="inside")

        # temp_sd needs the same treatment as model_val: it's only known
        # at the model's native hourly valid_times, but error_df's index
        # is the union of those hours WITH the station's much denser
        # sampling. An exact reindex() against that union index would
        # miss almost every station-only timestamp and come back NaN.
        # Interpolate it onto the union index the same way model_val is,
        # so the bias-panel band tracks the same real values the top
        # panel already shows correctly. Same limit_area="inside" reasoning
        # as above -- the SD band shouldn't extend flat past its own real
        # range either.
        if not sd_for_bias.empty:
            sd_df = sd_for_bias.reindex(combined_df.index.union(sd_for_bias.index))
            sd_df = sd_df.sort_index().interpolate(method="time", limit_area="inside")
            combined_df["sd_interpolated"] = sd_df.reindex(combined_df.index)
        else:
            combined_df["sd_interpolated"] = np.nan

        error_df = combined_df.dropna(subset=["station_interpolated", "model_interpolated"]).copy()
        error_df = error_df[(error_df.index >= cutoff) & (error_df.index <= now_utc)]

        if not error_df.empty:
            error_series = error_df["model_interpolated"] - error_df["station_interpolated"]
            timestamps = error_df.index

            n_total = len(error_df)
            bias_sum = error_series.sum()
            bias_mean = error_series.mean()

            rolling_bias = error_series.rolling(window="3h", center=True).mean()
            block_bias = error_series.resample("6h", closed="right", label="right").mean().dropna()

            # --- DAILY BIAS & SD SUMMARY (LOCAL MIDNIGHT CUTOFF) ---
            # Capped to the most recent MAX_SUMMARY_DAYS days: this box is
            # a fixed-size annotation, not a scrolling report, so an
            # unbounded per-day listing over a long --days/--start-date
            # range both (a) becomes unreadable well before it becomes a
            # crash, and (b) WAS a crash -- extra_bottom below had no
            # upper cap, so a long enough range pushed it past 1.0 and
            # matplotlib's subplots_adjust raised "bottom cannot be >= top".
            MAX_SUMMARY_DAYS = 21
            daily_groups = list(error_series.groupby(error_series.index.date))
            truncated_days = 0
            if len(daily_groups) > MAX_SUMMARY_DAYS:
                truncated_days = len(daily_groups) - MAX_SUMMARY_DAYS
                daily_groups = daily_groups[-MAX_SUMMARY_DAYS:]
            for day_date, day_errors in daily_groups:
                day_start = pd.Timestamp(day_date, tz="UTC")
                day_end = day_start + pd.Timedelta(days=1)
                is_in_progress = (day_start.date() == now_utc.date())

                fd_end = min(day_end, now_utc)
                fd_bias, fd_sd, fd_n = compute_period_stats(error_series, day_start, fd_end)

                date_str = day_start.strftime("%m-%d")
                if is_in_progress:
                    date_str += " (in progress)"

                if fd_bias is not None:
                    daily_lines.append(f"  {date_str}  Full Day     Bias {fd_bias}  SD {fd_sd}  n={fd_n}")
                elif fd_n > 0:
                    daily_lines.append(f"  {date_str}  Full Day     Bias —  SD —  n={fd_n}")
                else:
                    daily_lines.append(f"  {date_str}  Full Day     no data")

                # astral's sun(date=D) hands back sunrise correctly within
                # UTC calendar day D, but at this longitude (~6h of solar
                # time behind the UTC day boundary), the "sunset" it
                # returns for date=D is actually the trailing edge of the
                # PREVIOUS night bleeding into D's early hours -- not the
                # sunset that ends D's own daylight. That real evening
                # sunset for day D shows up when you query date=D+1
                # instead. Confirmed via debug tracing: sunset(D) was
                # consistently earlier in the clock than sunrise(D),
                # which is what silently broke both the daytime window
                # (impossible span -> "not yet started" on EVERY day, not
                # just today) and the nighttime window (used the wrong,
                # too-early sunset as its start, stretching "night" to
                # ~35h instead of ~12h and inflating its n far past the
                # Full Day bucket).
                sunrise, _ = get_sunrise_sunset_for_date(day_date, args.lat, args.lon)
                next_day = day_date + timedelta(days=1)
                next_sunrise, evening_sunset = get_sunrise_sunset_for_date(
                    next_day, args.lat, args.lon)

                if sunrise is not None and evening_sunset is not None:
                    dt_start = max(pd.Timestamp(sunrise), day_start)
                    dt_end = min(pd.Timestamp(evening_sunset), now_utc)
                    if dt_end > dt_start:
                        dt_bias, dt_sd, dt_n = compute_period_stats(error_series, dt_start, dt_end)
                        if dt_bias is not None:
                            daily_lines.append(f"           Daytime      Bias {dt_bias}  SD {dt_sd}  n={dt_n}")
                        elif dt_n > 0:
                            daily_lines.append(f"           Daytime      Bias —  SD —  n={dt_n}")
                        else:
                            daily_lines.append(f"           Daytime      no data yet")
                    else:
                        daily_lines.append(f"           Daytime      not yet started")
                else:
                    daily_lines.append(f"           Daytime      no sunrise/sunset")

                if evening_sunset is not None and next_sunrise is not None:
                    nt_start = max(pd.Timestamp(evening_sunset), day_start)
                    nt_end = min(pd.Timestamp(next_sunrise), now_utc)
                    if nt_end > nt_start:
                        nt_bias, nt_sd, nt_n = compute_period_stats(error_series, nt_start, nt_end)
                        if nt_bias is not None:
                            daily_lines.append(f"           Nighttime    Bias {nt_bias}  SD {nt_sd}  n={nt_n}")
                        elif nt_n > 0:
                            daily_lines.append(f"           Nighttime    Bias —  SD —  n={nt_n}")
                        else:
                            daily_lines.append(f"           Nighttime    no data yet")
                    else:
                        daily_lines.append(f"           Nighttime    not yet started")
                elif evening_sunset is None:
                    daily_lines.append(f"           Nighttime    no sunset data")
                else:
                    daily_lines.append(f"           Nighttime    no sunrise data")
                daily_lines.append("")

            if daily_lines and daily_lines[-1] == "":
                daily_lines.pop()
            if truncated_days:
                daily_lines.insert(
                    0, f"  (showing most recent {MAX_SUMMARY_DAYS} of "
                       f"{MAX_SUMMARY_DAYS + truncated_days} days -- "
                       f"{truncated_days} earlier day(s) omitted)")
                daily_lines.insert(1, "")
            daily_summary_text = "\n".join(daily_lines)

            print(f"\n--- Daily Bias & SD (Full / Day / Night) ---")
            for line in daily_lines:
                if line:
                    print(f"  {line}")
                else:
                    print()
            print("-------------------------------------------")

            ax2.plot(timestamps, error_series, linestyle="-", linewidth=0.8, color="crimson",
                     alpha=0.6, zorder=2, label="Bias (Interp. G6964)")
            # Daily max/min moved to a summary row above the plot (see
            # add_extremes_header) -- same reasoning as ax1 above.
            print_daily_extremes(error_series, label_prefix="Bias ")

            ax2.plot(timestamps, rolling_bias, linestyle="-", linewidth=2.0, color="darkred",
                     zorder=3, label="3-Hr Rolling Average Bias")

            ax2.axhline(0, color="black", linestyle="-", linewidth=0.8, alpha=0.7, zorder=1)

            ax2.fill_between(timestamps, error_series, 0, where=(error_series > 0),
                             color="royalblue", alpha=0.2, interpolate=True, zorder=1)
            ax2.fill_between(timestamps, error_series, 0, where=(error_series < 0),
                             color="crimson", alpha=0.2, interpolate=True, zorder=1)

            if not sd_for_bias.empty:
                error_sd = error_df["sd_interpolated"]
                if error_sd.notna().any():
                    # Drawn UNDER the data lines (zorder=1, below the
                    # crimson/darkred zorder=2/3) so the actual line shape
                    # and its endpoint at the observation boundary stay
                    # fully visible. De-emphasis of in-range noise comes
                    # from the band's own visual weight (darker/more
                    # opaque than the original placeholder) competing for
                    # attention, not from literally painting over the
                    # line -- an earlier version put this on top and it
                    # ended up erasing exactly the "where does the bias
                    # line stop" cue this is meant to preserve.
                    ax2.fill_between(timestamps, -error_sd, error_sd, color="gray", alpha=0.30,
                                     linewidth=0, zorder=1,
                                     label="Model Stencil Uncertainty (+/-1sigma, real values only)")

            # --- 6-HOUR BLOCK MEAN SEGMENTS WITH BADGES ---
            y_min, y_max = ax2.get_ylim()
            # Badges were at 10% up from the bottom -- but a daily MINIMUM
            # is, by definition, also near the bottom of the range, so
            # annotate_daily_extremes' Min labels and these badges were
            # competing for the same narrow zone. Moved up to 24% to
            # create real separation without pushing Min labels down into
            # clipping territory (tried that first -- see git history/
            # conversation; it didn't actually resolve the collision and
            # pushed labels off the visible axes instead).
            badge_y = y_min + (y_max - y_min) * 0.24
            label_y = y_min + (y_max - y_min) * 0.14

            first_hline = True
            for t, bias_val in block_bias.items():
                # .items() on a Series with a DatetimeIndex can hand back
                # a raw numpy.datetime64 rather than a pandas Timestamp
                # depending on pandas/numpy version -- Timestamp-Timedelta
                # arithmetic is fully unit-aware, but numpy's own
                # datetime64/timedelta64 arithmetic can fall back to a
                # "generic" unit that's now deprecated. Wrapping
                # explicitly sidesteps the ambiguity regardless of what
                # .items() happened to yield.
                t = pd.Timestamp(t)
                start_t = t - pd.Timedelta(hours=6)
                center_t = t - pd.Timedelta(hours=3)

                if t < cutoff or start_t > now_utc:
                    continue

                ax2.hlines(bias_val, xmin=start_t, xmax=t, colors="darkblue",
                           linestyles="-.", linewidth=2.0, zorder=12,
                           label="6-Hour Block Mean Segment" if first_hline else "")
                first_hline = False

                time_range_str = f"{start_t.strftime('%H:%M')}-{t.strftime('%H:%M')}"
                sign_str = "+" if bias_val > 0 else ""
                badge_text = f"{sign_str}{bias_val:.1f}F"

                if not station_wind_series.empty and t in station_wind_series.index:
                    wind_val = station_wind_series[t]
                    if not pd.isna(wind_val):
                        badge_text += f"\n{wind_val:.0f}mph"

                bg_color = "royalblue" if bias_val > 0 else "crimson"

                ax2.text(center_t, badge_y, badge_text,
                         horizontalalignment="center", verticalalignment="bottom",
                         fontsize=8, fontweight="bold", color="white", zorder=13,
                         bbox=dict(boxstyle="round,pad=0.2", facecolor=bg_color,
                                   alpha=0.85, edgecolor="none"))

            stats_text = (
                f"Sum: {bias_sum:+.1f}F  |  "
                f"Mean: {bias_mean:+.2f}F  |  "
                f"n={n_total}"
            )
            ax2.text(0.98, 0.95, stats_text,
                     transform=ax2.transAxes, fontsize=9, zorder=13,
                     verticalalignment="top", horizontalalignment="right",
                     bbox=dict(boxstyle="round,pad=0.3", facecolor="wheat", alpha=0.8))

            ax2.set_ylabel("Error (F)\n(Model - Station)", fontsize=10)
            ax2.grid(True, linestyle=":", alpha=0.5)
            ax2_period_rows = compute_period_rows([("Bias:", error_series, "crimson")])
            ax2_header_rows = len(ax2_period_rows)
            mark_extremes_on_graph(ax2, ax2_period_rows)
        else:
            ax2.text(0.5, 0.5, "No overlapping interpolated data within window",
                     horizontalalignment="center", verticalalignment="center",
                     transform=ax2.transAxes)
            ax2_period_rows = []
            ax2_header_rows = 0
    else:
        if (station_filter and "G6964" not in station_filter):
            msg = "G6964 not in station filter -- bias panel requires G6964 data"
        else:
            msg = "Model or G6964 station data missing from database"
        ax2.text(0.5, 0.5, msg,
                 horizontalalignment="center", verticalalignment="center",
                 transform=ax2.transAxes)
        ax2_period_rows = []
        ax2_header_rows = 0

    ax2.legend(
        loc="upper center", bbox_to_anchor=(0.5, -0.18),
        ncol=2, frameon=True, facecolor="white", framealpha=0.9, fontsize=9,
    )

    # --- DAILY BIAS & SD SUMMARY BOX ---
    if daily_lines:
        n_lines = len(daily_lines)
        box_height_frac = n_lines * 0.012 + 0.01
        box_top = 0.04
        box_bottom = box_top - box_height_frac

        fig.text(
            0.5, box_bottom, daily_summary_text,
            horizontalalignment="center", verticalalignment="bottom",
            fontsize=7.5, fontfamily="monospace",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="lightyellow",
                      edgecolor="dimgray", alpha=0.9),
        )
        # Hard cap regardless of box_height_frac's formula -- belt-and-
        # suspenders against exactly the "bottom cannot be >= top" crash
        # this box previously caused on a wide date range, even though
        # MAX_SUMMARY_DAYS above should already keep n_lines bounded.
        extra_bottom = min(max(box_height_frac + 0.02, 0.08), 0.45)
    else:
        extra_bottom = 0.05

    ax1.set_xlim(cutoff, plot_end)
    ax2.set_xlim(cutoff, plot_end)

    # Tick locators scaled to the actual plotted range -- the previous
    # hardcoded HourLocator(interval=1) minor ticks is exactly what
    # crashed: for a several-month range that's thousands of hourly
    # ticks, blowing past matplotlib's Locator.MAXTICKS (1000). A 48h
    # default run never hit this (only ~48 ticks), which is why it went
    # unnoticed until a wider --days/--start-date run.
    plot_span_days = (plot_end - cutoff).total_seconds() / 86400

    if plot_span_days <= 3:
        major_loc, minor_loc = mdates.HourLocator(interval=6), mdates.HourLocator(interval=1)
        date_fmt = "%m-%d %H:%M"
    elif plot_span_days <= 14:
        major_loc, minor_loc = mdates.DayLocator(interval=1), mdates.HourLocator(interval=6)
        date_fmt = "%m-%d %H:%M"
    elif plot_span_days <= 60:
        major_loc, minor_loc = mdates.DayLocator(interval=7), mdates.DayLocator(interval=1)
        date_fmt = "%Y-%m-%d"
    elif plot_span_days <= 400:
        major_loc, minor_loc = mdates.MonthLocator(), mdates.DayLocator(interval=7)
        date_fmt = "%Y-%m-%d"
    else:
        major_loc, minor_loc = mdates.MonthLocator(interval=3), mdates.MonthLocator()
        date_fmt = "%Y-%m"

    ax2.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))
    ax2.xaxis.set_major_locator(major_loc)
    ax2.xaxis.set_minor_locator(minor_loc)
    ax2.set_xlabel("Time (UTC)", fontsize=10)

    fig.autofmt_xdate()

    # Header row counts can now be much larger than the original design
    # (up to ~stations x days with per-8am-period rows), so this needs
    # actual added INCHES, not a shrinking fraction of a fixed figure
    # height -- a fraction-only approach is exactly what caused the
    # earlier overlap once row counts grew past a handful.
    ROW_HEIGHT_INCHES = 0.16
    fig_h_base = 13.5
    extra_h_ax1 = ax1_header_rows * ROW_HEIGHT_INCHES
    extra_h_ax2 = ax2_header_rows * ROW_HEIGHT_INCHES
    new_fig_h = fig_h_base + extra_h_ax1 + extra_h_ax2
    fig.set_size_inches(fig_w, new_fig_h)

    # Reserve room for the header rows as fractions of the CORRECTED
    # total figure height: ax1's rows eat into the figure's top margin
    # (nothing above ax1 except the figure edge), ax2's rows eat into the
    # hspace gap between the two panels (ax1's legend already lives below
    # ax1, so ax2's header rows share that same gap). hspace is a
    # fraction of the AVERAGE axes height (matplotlib's own definition),
    # not of the figure directly, so it's derived from the actual
    # height_ratios split rather than a guessed multiplier.
    top_margin = 1.0 - (extra_h_ax1 + 0.35 + 0.12) / new_fig_h
    avg_axes_height_frac = top_margin * (3.5 / 5.3 + 1.8 / 5.3) / 2
    hspace = 0.35 + ((extra_h_ax2 + 0.15) / new_fig_h) / max(avg_axes_height_frac, 0.01)
    fig.subplots_adjust(bottom=extra_bottom * fig_h_base / new_fig_h,
                        top=top_margin, hspace=hspace)

    # Draw the header rows now that layout is finalized, using each
    # axes' REAL position (read back via get_position(), not a
    # hand-derived value) -- self-correcting against any small mismatch
    # between the margin math above and what subplots_adjust actually
    # produced, rather than compounding two independent approximations.
    #
    # ax1 specifically needs an explicit gap here: matplotlib positions
    # set_title() with a small FIXED offset right above
    # ax1.get_position().y1, not near the top of the whole reserved
    # margin as originally assumed -- the lowest header row (closest to
    # the axis) was landing almost exactly on top of the title because
    # both sit right at that same boundary. ax2 has no title, so it
    # doesn't need this extra buffer.
    row_height_frac = ROW_HEIGHT_INCHES / new_fig_h
    title_buffer_frac = 0.35 / new_fig_h
    draw_extremes_header_fig(fig, ax1.get_position().y1 + title_buffer_frac,
                             ax1_period_rows, row_height_frac)
    draw_extremes_header_fig(fig, ax2.get_position().y1, ax2_period_rows, row_height_frac)

    # --- OPTIONAL WINDROSE SECTION (--windrose) ---
    # Added as its own axes positioned BELOW the figure's nominal y=0
    # (figure-fraction coordinates, same trick the daily summary box
    # above already relies on) rather than by growing fig_h_base and
    # recomputing ax1/ax2's positions. That would mean re-deriving the
    # whole top_margin/hspace/extra_bottom relationship above against a
    # new total height, which is exactly the kind of two-independent-
    # approximations situation the get_position()-readback comment above
    # warns about. Since savefig already uses bbox_inches="tight", any
    # content below y=0 just extends the exported canvas -- ax1/ax2 stay
    # completely untouched, same absolute size as without this section.
    if args.windrose:
        if not windrose_df.empty:
            # Station observations don't exist in the future, so bound by
            # now_utc regardless of whether plot_end was extended further
            # out for the HRRR forecast continuation on the main plot.
            windrose_end = min(now_utc, plot_end)

            WINDROSE_HEIGHT_INCHES = 4.4
            WINDROSE_GAP_INCHES = 0.5
            # Clearance below y=0 for ax2's own legend, which is anchored
            # at bbox_to_anchor=(0.5, -0.18) in AXES-fraction (not figure-
            # fraction) coordinates and so isn't captured by box_bottom
            # (that only accounts for the daily summary box, when drawn).
            LEGEND_CLEARANCE_FRAC = 0.06

            windrose_top_frac = (min(box_bottom, 0.0) - LEGEND_CLEARANCE_FRAC
                                  - WINDROSE_GAP_INCHES / new_fig_h)
            windrose_height_frac = WINDROSE_HEIGHT_INCHES / new_fig_h
            windrose_bottom_frac = windrose_top_frac - windrose_height_frac

            # Keep it roughly square rather than stretching it across the
            # full figure width, which would just leave the compass rose
            # small in the middle of a lot of empty horizontal space.
            windrose_width_inches = min(WINDROSE_HEIGHT_INCHES * 0.95, fig_w * 0.42)
            windrose_width_frac = windrose_width_inches / fig_w
            windrose_left_frac = (1.0 - windrose_width_frac) / 2.0

            windrose_ax = fig.add_axes(
                [windrose_left_frac, windrose_bottom_frac,
                 windrose_width_frac, windrose_height_frac],
                projection="polar")
            render_windrose(windrose_df, cutoff, windrose_end, ax=windrose_ax,
                            show_calm=args.windrose_show_calm)
        else:
            print("  [windrose] Skipped -- no wind_speed+direction data in this timeframe.")

    timestamp_str = now_utc.strftime("%Y-%m-%d-%H-%M-%S")
    if station_filter:
        out_path = f"results/{timestamp_str}_{'_'.join(station_filter)}.svg"
    else:
        out_path = f"results/{timestamp_str}.svg"

    plt.savefig(out_path, format="svg", bbox_inches="tight")
    print(f"\nSaved diagnostic plot to {out_path}")
    print(f"Plotted stations: {plotted_stations}")



if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
