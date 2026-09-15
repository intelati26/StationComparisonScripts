#!/usr/bin/env python3
"""
BarMatrix.py — Temperature×Humidity matrix with marginal bars and density contours

Layout:
  ┌───────────────────────────────┬──────────┐
  │                               │  Humidity│
  │    Scatter + KDE contour      │  bar     │
  │    (temp on X, humidity on Y) │  (vert)  │
  ├───────────────────────────────┼──────────┤
  │  Temperature bar (horizontal) │  (empty) │
  └───────────────────────────────┴──────────┘

Reads from weather_archive.db, station_obs table.
"""

import argparse
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from scipy.stats import gaussian_kde
import os  

DB_PATH = "weather_archive.db"

def parse_args():
    p = argparse.ArgumentParser(
        description="Temperature×Humidity matrix with marginal bars and density contours",
    )
    p.add_argument("--days", type=float, default=7.0,
                   help="Lookback window in days (default: 7)")
    p.add_argument("--hours", type=float, default=None,
                   help="Lookback window in hours (overrides --days)")
    p.add_argument("--start-date", type=str, default=None,
                   help="Start date (inclusive).")
    p.add_argument("--end-date", type=str, default=None,
                   help="End date (inclusive). Defaults to now.")
    p.add_argument("--station", type=str, default="G6964",
                   help="Station ID: G6964, KJLN, or KEOS (default: G6964)")
    p.add_argument("--db", type=str, default=DB_PATH,
                   help="Path to SQLite DB")
    p.add_argument("--output", "-o", type=str, default=None,
                   help="Output file path. Auto-saves if omitted.")
    p.add_argument("--bins", type=int, default=15,
                   help="Number of bins for marginal histograms (default: 30)")
    p.add_argument("--contour-levels", type=int, default=8,
                   help="Number of contour levels for density (default: 8)")
    return p.parse_args()


def resolve_time_range(args):
    now = datetime.now(timezone.utc)
    if args.start_date:
        start = pd.to_datetime(args.start_date)
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = pd.to_datetime(args.end_date).tz_localize("UTC") if args.end_date else now
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        return start, end
    if args.hours is not None:
        lookback = timedelta(hours=args.hours)
    else:
        lookback = timedelta(days=args.days)
    return now - lookback, now


def load_data(conn, station, start_time, end_time):
    query = """
        SELECT timestamp, temperature, relative_humidity
        FROM station_obs
        WHERE station = ?
          AND timestamp >= ?
          AND timestamp <= ?
        ORDER BY timestamp ASC
    """
    df = pd.read_sql_query(
        query, conn,
        params=(station, start_time.isoformat(), end_time.isoformat()),
    )
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def compute_kde(x, y):
    """Compute 2D KDE for contour overlay."""
    # Filter out NaNs
    mask = ~(np.isnan(x) | np.isnan(y))
    if mask.sum() < 5:
        return None

    x_clean = x[mask]
    y_clean = y[mask]

    # Use Scott's rule for bandwidth, with a small floor to avoid over-smoothing
    # when there are few points
    xy = np.vstack([x_clean, y_clean])
    try:
        kde = gaussian_kde(xy, bw_method="scott")
    except np.linalg.LinAlgError:
        return None

    # Grid for evaluation
    x_min, x_max = x_clean.min(), x_clean.max()
    y_min, y_max = y_clean.min(), y_clean.max()
    x_pad = (x_max - x_min) * 0.05
    y_pad = (y_max - y_min) * 0.05

    x_grid = np.linspace(x_min - x_pad, x_max + x_pad, 120)
    y_grid = np.linspace(y_min - y_pad, y_max + y_pad, 120)
    X, Y = np.meshgrid(x_grid, y_grid)
    positions = np.vstack([X.ravel(), Y.ravel()])
    Z = kde(positions).reshape(X.shape)

    return X, Y, Z


def build_matrix_plot(df, station, start_time, end_time, args):
    """Create the matrix-style plot with marginal bars and central scatter+contour."""

    # Drop rows missing either value
    plot_df = df.dropna(subset=["temperature", "relative_humidity"]).copy()
    plot_df = plot_df[(plot_df["temperature"].between(-50, 140)) &
                      (plot_df["relative_humidity"].between(0, 100))]

    if plot_df.empty:
        print(f"No valid temperature + humidity rows for station {station}.")
        return None

    n_points = len(plot_df)
    print(f"  Plotting {n_points} data points")

    # --- Extract arrays ---
    temps = plot_df["temperature"].values
    hums = plot_df["relative_humidity"].values

    # --- KDE for contours ---
    kde_result = compute_kde(temps, hums)

    # --- Figure layout using GridSpec ---
    fig = plt.figure(figsize=(12, 10))
    gs = GridSpec(
        2, 2,
        width_ratios=[4, 1],
        height_ratios=[4, 1],
        hspace=0.04,
        wspace=0.04,
    )

    ax_main = fig.add_subplot(gs[0, 0])     # scatter + contours
    ax_hum = fig.add_subplot(gs[0, 1], sharey=ax_main)   # humidity bar (horizontal)
    ax_temp = fig.add_subplot(gs[1, 0], sharex=ax_main)   # temp bar (vertical)
    ax_corner = fig.add_subplot(gs[1, 1])   # empty corner
    ax_corner.axis("off")

    # --- Colors: color scatter points by density ---
    if kde_result is not None:
        X, Y, Z = kde_result
        # Evaluate density at each data point for coloring
        xy_points = np.vstack([temps, hums])
        point_density = gaussian_kde(xy_points, bw_method="scott")(xy_points)
        # Sort by density so dense points render on top
        order = point_density.argsort()
        temps_sorted = temps[order]
        hums_sorted = hums[order]
        dens_sorted = point_density[order]
    else:
        temps_sorted = temps
        hums_sorted = hums
        dens_sorted = np.ones(len(temps))

    # --- Main scatter (colored by density) ---
    scatter = ax_main.scatter(
        temps_sorted, hums_sorted,
        c=dens_sorted, cmap="plasma", s=12, alpha=0.6,
        edgecolors="none", zorder=3,
    )

    # --- Contour overlay ---
    if kde_result is not None:
        # Convert KDE to percentile levels (reoccurrence rates)
        Z_flat = Z.ravel()
        Z_sorted = np.sort(Z_flat)[::-1]
        Z_cumsum = np.cumsum(Z_sorted)
        Z_cumsum /= Z_cumsum[-1]

        # Contour levels at 10%, 25%, 50%, 75%, 90% of density mass
        percentiles = [90, 75, 50, 25, 10]
        levels = []
        for pct in percentiles:
            idx = np.searchsorted(Z_cumsum, pct / 100.0)
            idx = min(idx, len(Z_sorted) - 1)
            levels.append(Z_sorted[idx])
        levels = sorted(levels)

        cs = ax_main.contour(
            X, Y, Z, levels=levels,
            colors="0.5", linewidths=1.2, alpha=0.2, zorder=4,
        )
        ax_main.clabel(cs, inline=True, fontsize=7, fmt="%d%%")

    # --- Temperature marginal bar (bottom, vertical bars) ---
    temp_bins = np.linspace(temps.min(), temps.max(), args.bins + 1)
    temp_counts, temp_edges = np.histogram(temps, bins=temp_bins)
    temp_centers = (temp_edges[:-1] + temp_edges[1:]) / 2
    temp_widths = np.diff(temp_edges)

    ax_temp.bar(
        temp_centers, temp_counts,
        width=temp_widths * 0.9, align="center",
        color="#ff6b35", alpha=0.8, edgecolor="#cc4c1a",
    )
    ax_temp.set_ylabel("Count", fontsize=9)
    ax_temp.set_xlabel("Temperature (°F)", fontsize=11)
    ax_temp.grid(axis="y", alpha=0.3)

    # --- Humidity marginal bar (right, horizontal bars) ---
    hum_bins = np.linspace(hums.min(), hums.max(), args.bins + 1)
    hum_counts, hum_edges = np.histogram(hums, bins=hum_bins)
    hum_centers = (hum_edges[:-1] + hum_edges[1:]) / 2
    hum_heights = np.diff(hum_edges)

    ax_hum.barh(
        hum_centers, hum_counts,
        height=hum_heights * 0.9, align="center",
        color="#2ecc71", alpha=0.8, edgecolor="#27ae60",
    )
    ax_hum.set_xlabel("Count", fontsize=9)
    ax_hum.grid(axis="x", alpha=0.3)

    # --- Clean up axis sharing ---
    plt.setp(ax_main.get_xticklabels(), visible=False)
    plt.setp(ax_hum.get_yticklabels(), visible=False)
    ax_hum.tick_params(left=False)
    ax_temp.tick_params(bottom=False, labelbottom=True)

    # --- Main axis labels ---
    ax_main.set_ylabel("Relative Humidity (%)", fontsize=11)
    ax_main.grid(alpha=0.2, zorder=0)

    # --- Colorbar for density ---
    cbar = fig.colorbar(scatter, ax=ax_corner, orientation="horizontal",
                        fraction=0.8, pad=0.1, shrink=0.6)
    cbar.set_label("Point Density", fontsize=8)
    cbar.ax.tick_params(labelsize=4)

    # --- Title ---
    fig.suptitle(
        f"Temperature × Humidity Matrix — Station {station}\n"
        f"{start_time.strftime('%Y-%m-%d %H:%M')} to {end_time.strftime('%Y-%m-%d %H:%M')} UTC  "
        f"({n_points} observations)",
        fontsize=13, fontweight="bold", y=0.98,
    )

    return fig, plot_df

def compute_stats(plot_df):
    """Max temp, dew point at that time, and max heat index."""
    idx = plot_df["temperature"].idxmax()
    t_max = plot_df.loc[idx, "temperature"]
    rh = plot_df.loc[idx, "relative_humidity"]

    # Magnus formula for dew point
    a, b = 17.625, 243.04
    alpha = np.log(rh / 100.0) + (a * t_max) / (b + t_max)
    td = (b * alpha) / (a - alpha)

    # Rothfusz regression for heat index (°F)
    t = plot_df["temperature"].values
    r = plot_df["relative_humidity"].values
    hi = (0.5 * (t + 61.0 + ((t - 68.0) * 0.092) + (r * 0.0325)))  # simple Steadman first
    full = (-42.379 + 2.04901523 * t + 10.14333127 * r
            - 0.22475541 * t * r - 6.83783e-3 * t**2
            - 5.481717e-2 * r**2 + 1.22874e-3 * t**2 * r
            + 8.5282e-4 * t * r**2 - 1.99e-6 * t**2 * r**2)
    hi = np.where((t >= 80) & (r >= 40), full, hi)  # Rothfusz only valid when t≥80, RH≥40
    hi_max = np.nanmax(hi)

    return t_max, td, hi_max

def main():
    args = parse_args()
    start_time, end_time = resolve_time_range(args)

    print(f"Station: {args.station}")
    print(f"Range:   {start_time.strftime('%Y-%m-%d %H:%M')} -> "
          f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"DB:      {args.db}")

    conn = sqlite3.connect(args.db)

    df = load_data(conn, args.station, start_time, end_time)
    print(f"Rows loaded: {len(df)}")

    conn.close()

    if df.empty:
        print("No data found. Check station ID and time range.")
        return

    fig, plot_df = build_matrix_plot(df, args.station, start_time, end_time, args)
    if fig is None:
        return

    # --- Save ---
    script_dir = os.path.dirname(os.path.abspath(__file__))
    results_dir = os.path.join(script_dir, "results")
    os.makedirs(results_dir, exist_ok=True)

    t_max, td, hi_max = compute_stats(plot_df)
    stamp = end_time.strftime("%Y%m%d%H%M")
    name = f"{stamp}-t{t_max:.0f}-td{td:.0f}-maxix{hi_max:.0f}.svg"
    output_path = args.output or os.path.join(results_dir, name)

    fig.savefig(output_path)
    print(f"\nSaved: {output_path}  (max {t_max:.1f}°F, td {td:.1f}°F, maxix {hi_max:.1f}°F)")
    plt.close(fig)


if __name__ == "__main__":
    main()
