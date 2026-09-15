#!/usr/bin/env python3
"""
diurnal.py — Categorizes G6964 bias deviations into Day vs Night 
using astral's solar elevation angle, organizing strip points left-to-right 
by their progression through the solar cycle (sunset-to-sunrise / sunrise-to-
sunset).
"""

import os
import argparse
import sqlite3
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from astral import Observer
from astral.sun import sun, elevation

DB_PATH = "weather_archive.db"
LAT = 37.0842
LON = -94.5133

# Model tier hierarchy for cascade unification (higher = better)
MODEL_TIER = {
    "urma": 5,
    "rtma": 4,
    "hrrr_f00": 3,
    "hrrr_f01": 2,
    "hrrr_f02": 1,
    "hrrr_f03": 0,
}

def get_solar_bounds(dt, observer):
    """Finds the surrounding sunset/sunrise bounds for a given timestamp."""
    curr_date = dt.date()
    for offset in [-1, 0, 1]:
        try:
            s = sun(observer, date=curr_date + timedelta(days=offset))
            sr = s["sunrise"]
            ss = s["sunset"]
            if sr <= dt <= ss:
                return sr, ss, "day"
        except Exception:
            pass

    try:
        s_prev = sun(observer, date=curr_date - timedelta(days=1))
        s_curr = sun(observer, date=curr_date)
        s_next = sun(observer, date=curr_date + timedelta(days=1))

        events = sorted([
            (s_prev["sunset"], "ss"), (s_prev["sunrise"], "sr"),
            (s_curr["sunset"], "ss"), (s_curr["sunrise"], "sr"),
            (s_next["sunset"], "ss"), (s_next["sunrise"], "sr")
        ], key=lambda x: x[0])

        for i in range(len(events) - 1):
            t1, t2 = events[i][0], events[i+1][0]
            if t1 <= dt <= t2:
                return t1, t2, "night"
    except Exception:
        pass

    return None, None, None

def unify_model_cascade(df_model):
    """
    Combine multiple model tiers into a single unified series using
    quality-ordered cascade: URMA > RTMA > HRRR f00 > HRRR f01 > ...
    """
    if df_model.empty:
        return pd.Series(dtype=float)

    model_parts = []
    for col in df_model["model"].unique():
        tier = MODEL_TIER.get(col, -1)
        if tier >= 0:
            subset = df_model[df_model["model"] == col][["ts_naive", "temp_f"]].dropna()
            subset = subset.set_index("ts_naive")["temp_f"]
            if not subset.empty:
                model_parts.append((tier, subset))

    if not model_parts:
        return pd.Series(dtype=float)

    # Sort by tier descending (best first)
    model_parts.sort(key=lambda x: -x[0])

    combined = pd.Series(dtype=float)
    for _, series in model_parts:
        combined = combined.combine_first(series)

    combined = combined.sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.dropna()

def main():
    parser = argparse.ArgumentParser(description="Diurnal bias analysis with time-ordered solar progression points")
    parser.add_argument("--hours", type=float, default=168.0, help="Total lookback window in hours (default: 168)")
    parser.add_argument("--highlight-hours", type=float, default=24.0, help="Recent window to highlight in hours (default: 24)")
    parser.add_argument("--lat", type=float, default=LAT)
    parser.add_argument("--lon", type=float, default=LON)
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found.")
        return

    conn = sqlite3.connect(DB_PATH)

    df_station = pd.read_sql(
        "SELECT timestamp, temperature FROM station_obs WHERE station = 'G6964'",
        conn, parse_dates=["timestamp"],
    )

    df_model = pd.read_sql(
        "SELECT model, valid_time, temp_f FROM model_analysis",
        conn, parse_dates=["valid_time"],
    )
    conn.close()

    if df_station.empty or df_model.empty:
        print("Insufficient data in database.")
        return

    # --- Convert to NAIVE UTC ---
    df_station["timestamp"] = pd.to_datetime(df_station["timestamp"], utc=True)
    df_station["ts_naive"] = df_station["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None)
    df_station = df_station.set_index("ts_naive")[["temperature"]].rename(columns={"temperature": "station_val"})
    df_station = df_station.dropna()

    df_model["valid_time"] = pd.to_datetime(df_model["valid_time"], utc=True)
    df_model["ts_naive"] = df_model["valid_time"].dt.tz_convert("UTC").dt.tz_localize(None)

    unified_model = unify_model_cascade(df_model)

    if unified_model.empty:
        print("No model data available.")
        return

    # --- Interpolate model to station timestamps ---
    combined = pd.concat([unified_model.rename("model_val"), df_station["station_val"]], axis=1)
    combined["model_interp"] = combined["model_val"].interpolate(method="time")

    error_df = combined.dropna(subset=["station_val", "model_interp"]).copy()
    error_df["bias"] = error_df["model_interp"] - error_df["station_val"]

    # --- Time windows ---
    now_naive = pd.Timestamp.now(tz="UTC").tz_localize(None)
    cutoff_total = now_naive - timedelta(hours=args.hours)
    cutoff_recent = now_naive - timedelta(hours=args.highlight_hours)

    error_df = error_df[(error_df.index >= cutoff_total) & (error_df.index <= now_naive)]

    if error_df.empty:
        print("No overlapping station/model data in the requested window.")
        return

    # --- Calculate solar phase progression for each point ---
    observer = Observer(latitude=args.lat, longitude=args.lon)

    segments = []
    progress_fractions = []

    for dt in error_df.index:
        dt_aware = dt.tz_localize("UTC")
        is_day = elevation(observer, dt_aware) > 0
        is_recent = dt >= cutoff_recent

        start_t, end_t, p_type = get_solar_bounds(dt_aware, observer)
        fraction = 0.5
        if start_t and end_t and start_t < end_t:
            total_duration = (end_t - start_t).total_seconds()
            elapsed = (dt_aware - start_t).total_seconds()
            if total_duration > 0:
                fraction = max(0.0, min(1.0, elapsed / total_duration))

        progress_fractions.append(fraction)

        if is_day and not is_recent:
            segments.append("Overall Daytime")
        elif is_day and is_recent:
            segments.append("Recent Daytime")
        elif not is_day and not is_recent:
            segments.append("Overall Nighttime")
        else:
            segments.append("Recent Nighttime")

    error_df["Segment"] = segments
    error_df["Progress"] = progress_fractions

    # Subsets
    overall_day = error_df[error_df["Segment"] == "Overall Daytime"]
    recent_day = error_df[error_df["Segment"] == "Recent Daytime"]
    overall_night = error_df[error_df["Segment"] == "Overall Nighttime"]
    recent_night = error_df[error_df["Segment"] == "Recent Nighttime"]

    def get_stats(sub_df):
        series = sub_df["bias"].dropna()
        if len(series) < 2:
            return (series.mean() if len(series) == 1 else 0.0), 0.0, sub_df
        m = series.mean()
        sem = series.std(ddof=1) / np.sqrt(len(series))
        return m, sem, sub_df

    od_mean, od_sem, od_sub = get_stats(overall_day)
    rd_mean, rd_sem, rd_sub = get_stats(recent_day)
    on_mean, on_sem, on_sub = get_stats(overall_night)
    rn_mean, rn_sem, rn_sub = get_stats(recent_night)

    total_comparisons = len(error_df)
    day_total = len(overall_day) + len(recent_day)
    night_total = len(overall_night) + len(recent_night)

    print(f"\n{'='*70}")
    print(f"  DIURNAL BIAS ANALYSIS (Solar Time-Ordered Progression) + SEM")
    print(f"{'='*70}")
    print(f"  Total comparisons: {total_comparisons} (Day: {day_total}, Night: {night_total})")
    print(f"  ☀️  Overall Daytime  (n={len(od_sub)}): Mean = {od_mean:+.2f}°F (±{od_sem:.2f})")
    print(f"  ☀️  Recent Daytime   (n={len(rd_sub)}): Mean = {rd_mean:+.2f}°F (±{rd_sem:.2f})")
    print(f"  🌙  Overall Nighttime(n={len(on_sub)}): Mean = {on_mean:+.2f}°F (±{on_sem:.2f})")
    print(f"  🌙  Recent Nighttime (n={len(rn_sub)}): Mean = {rn_mean:+.2f}°F (±{rn_sem:.2f})")
    print(f"{'='*70}")

    # --- GENERATE PLOT WITH TIME-ORDERED SCATTER ---
    os.makedirs("results", exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 6))

    tick_labels = [
        "Overall\nDaytime",
        f"Recent\nDaytime",
        "Overall\nNighttime",
        f"Recent\nNighttime"
    ]
    sub_dfs = [od_sub, rd_sub, on_sub, rn_sub]
    sub_counts = [len(od_sub), len(rd_sub), len(on_sub), len(rn_sub)]
    stats_list = [
        (od_mean, od_sem),
        (rd_mean, rd_sem),
        (on_mean, on_sem),
        (rn_mean, rn_sem)
    ]

    colors = ["goldenrod", "darkorange", "midnightblue", "deepskyblue"]
    alphas = [0.5, 0.85, 0.5, 0.85]

    data_arrays = [sub["bias"].values for sub in sub_dfs]

    bp = ax.boxplot(
        data_arrays, tick_labels=tick_labels, patch_artist=True, widths=0.45,
        showmeans=True,
        meanprops=dict(marker='o', markeredgecolor='black', markerfacecolor='white', markersize=8)
    )

    for patch, color, alpha in zip(bp['boxes'], colors, alphas):
        patch.set_facecolor(color)
        patch.set_alpha(alpha)
        patch.set_linewidth(1.5)

    # Plot scatter points ordered/mapped horizontally by their solar progression
    for i, sub in enumerate(sub_dfs, start=1):
        if not sub.empty:
            biases = sub["bias"].values
            progress = sub["Progress"].values
            x_coords = i + (progress - 0.5) * 0.3
            y_jittered = biases + np.random.normal(0, 0.02, size=len(biases))

            sc = ax.scatter(
                x_coords, y_jittered, c=progress, cmap="coolwarm",
                alpha=0.6, s=20, edgecolor="black", linewidth=0.3, zorder=3
            )

    ax.axhline(0, color='gray', linestyle='--', linewidth=1)
    ax.axvline(2.5, color='dimgray', linestyle=':', linewidth=1.2, alpha=0.7)

    ax.set_title(f"G6964 Diurnal Bias — Time-Ordered by Solar Cycle (Left=Start, Right=End) + SEM", fontsize=12, fontweight='bold')
    ax.set_ylabel("Bias Error (°F) [Model - Station]", fontsize=10)
    ax.grid(True, linestyle=":", alpha=0.5)

    # Annotate means, standard errors, and observation counts per box
    for i, (arr, (mean_val, sem_val), color, n) in enumerate(zip(data_arrays, stats_list, colors, sub_counts), start=1):
        if len(arr) > 0:
            text_str = f"Mean: {mean_val:+.2f}°F\n(±{sem_val:.2f} SEM)\nn={n}"
            ax.text(
                i, mean_val + 0.5, text_str,
                horizontalalignment='center', fontweight='bold', fontsize=8,
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.9, edgecolor=color)
            )

    # --- SUMMARY ANNOTATION: total observations/comparisons ---
    summary_text = (
        f"Total Comparisons: {total_comparisons}\n"
        f"Day: {day_total}  |  Night: {night_total}\n"
        f"Window: {args.hours:.0f}h (highlight: {args.highlight_hours:.0f}h)"
    )
    ax.text(
        0.98, 0.98, summary_text,
        transform=ax.transAxes, fontsize=9, fontweight='bold',
        verticalalignment='top', horizontalalignment='right',
        bbox=dict(boxstyle="round,pad=0.4", facecolor="wheat", alpha=0.85, edgecolor="gray"),
    )

    plt.tight_layout()
    timestamp_str = now_naive.strftime("%Y-%m-%d-%H-%M-%S")
    out_path = f"results/{timestamp_str}_diurnal_time_ordered.svg"
    plt.savefig(out_path, format="svg", bbox_inches="tight")
    print(f"\nSaved time-ordered diurnal plot to {out_path}")

if __name__ == "__main__":
    main()
