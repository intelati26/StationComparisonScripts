#!/usr/bin/env python3
"""
top5_daily_extremes.py -- For every one of the 366 possible calendar days
(Jan 1 through Dec 31, Feb 29 included), the top 5 warmest TMAX and the
top 5 coldest TMIN ever recorded on that day across a station's full
history, each with the year it happened.

This is a "top 5" widening of the single record TMAX/TMIN that
daily_almanac.py's bell curves already mark (see compute_field_stats()
there) -- same per-calendar-day grouping idea, just ranked instead of
reduced to one extreme, and run across all 366 days at once instead of
a single day.

Reuses get_full_daily_history() from daily_almanac.py (local db for
stations archived there, ACIS's full period-of-record for ACIS-registered
stations not local) rather than duplicating that source-routing logic.

Usage:
    # Full 366-day reference table, saved as CSV:
    python top5_daily_extremes.py KSGF

    # Just one calendar day, printed to console:
    python top5_daily_extremes.py KSGF --date 07-04
    python top5_daily_extremes.py KSGF --date 2026-07-04   # year is ignored, just the mm-dd

    # Fewer/more than 5:
    python top5_daily_extremes.py KSGF --top 10
"""

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from climate_normals import discover_stations, DB_PATH, station_exists_in_acis, load_daily_from_acis
from daily_almanac import get_full_daily_history


def get_full_daily_history_prefer_acis(conn, station, local_stations):
    """
    Like get_full_daily_history(), but with the routing priority flipped:
    ACIS's full period of record wins whenever the station is
    ACIS-registered, even if it also happens to have a partial local
    archive (e.g. KJLN has ~500 rows in station_obs from early testing --
    a fraction of its real ~80+ year ACIS record). This script's whole
    point is maximal historical coverage for ranking extremes, so a
    smaller-but-present local slice is the wrong thing to prefer here,
    unlike climate_normals.py's reports where the local archive is
    usually the intentionally-maintained source. Falls back to the local
    db only for non-ACIS (personal) stations, or if ACIS unexpectedly
    returns nothing.
    """
    if station_exists_in_acis(station):
        acis_hist = load_daily_from_acis(station, "por", "por", conn=conn)
        if not acis_hist.empty:
            return acis_hist
        print(f"Warning: {station} is ACIS-registered but ACIS returned no data; "
             f"falling back to local db.")
    return get_full_daily_history(conn, station, local_stations)


def compute_top_n_tables(full_hist, n=5):
    """
    Returns (tmax_top, tmin_top): dicts keyed by month_day ('MM-DD'),
    each value a list of (value, year) tuples, tmax sorted warmest-first,
    tmin sorted coldest-first, length up to n (fewer if that calendar day
    doesn't have n years of data).
    """
    df = full_hist.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["month_day"] = df["date"].dt.strftime("%m-%d")
    df["year"] = df["date"].dt.year

    tmax_df = df.dropna(subset=["tmax"])
    tmin_df = df.dropna(subset=["tmin"])

    tmax_top, tmin_top = {}, {}
    for md, group in tmax_df.groupby("month_day"):
        top = group.nlargest(n, "tmax")[["tmax", "year"]].sort_values("tmax", ascending=False)
        tmax_top[md] = list(zip(top["tmax"], top["year"]))
    for md, group in tmin_df.groupby("month_day"):
        top = group.nsmallest(n, "tmin")[["tmin", "year"]].sort_values("tmin", ascending=True)
        tmin_top[md] = list(zip(top["tmin"], top["year"]))

    return tmax_top, tmin_top


def all_366_month_days():
    """Jan 1 -> Dec 31 of a leap year (2000), so Feb 29 is included."""
    return [d.strftime("%m-%d") for d in pd.date_range("2000-01-01", "2000-12-31")]


def print_single_day(station, month_day, tmax_top, tmin_top, n):
    label = pd.Timestamp(f"2000-{month_day}").strftime("%B %d")
    print(f"\n{'=' * 60}")
    print(f"  TOP {n} EXTREMES: {station}, {label}")
    print(f"{'=' * 60}")

    warm = tmax_top.get(month_day, [])
    print(f"\n  Warmest TMAX ever recorded on {label} ({len(warm)} on file):")
    if not warm:
        print("    No data.")
    for i, (val, year) in enumerate(warm, 1):
        print(f"    {i}. {val:.0f}\u00b0F ({year})")

    cold = tmin_top.get(month_day, [])
    print(f"\n  Coldest TMIN ever recorded on {label} ({len(cold)} on file):")
    if not cold:
        print("    No data.")
    for i, (val, year) in enumerate(cold, 1):
        print(f"    {i}. {val:.0f}\u00b0F ({year})")


def write_full_table(station, tmax_top, tmin_top, n, out_path):
    """366-row CSV: month_day, tmax_1..tmax_n (+ tmax_N_year), same for tmin."""
    rows = []
    for md in all_366_month_days():
        row = {"month_day": md}
        warm = tmax_top.get(md, [])
        cold = tmin_top.get(md, [])
        for i in range(1, n + 1):
            if i <= len(warm):
                row[f"tmax_{i}"], row[f"tmax_{i}_year"] = warm[i - 1]
            else:
                row[f"tmax_{i}"], row[f"tmax_{i}_year"] = None, None
            if i <= len(cold):
                row[f"tmin_{i}"], row[f"tmin_{i}_year"] = cold[i - 1]
            else:
                row[f"tmin_{i}"], row[f"tmin_{i}_year"] = None, None
        rows.append(row)

    out_df = pd.DataFrame(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_path, index=False)
    print(f"Saved: {out_path}  ({len(out_df)} calendar days, top {n} each)")

    covered_tmax = sum(1 for md in tmax_top if tmax_top[md])
    covered_tmin = sum(1 for md in tmin_top if tmin_top[md])
    print(f"Coverage: {covered_tmax}/366 days have TMAX data, "
         f"{covered_tmin}/366 days have TMIN data.")


def main():
    ap = argparse.ArgumentParser(
        description="Top N warmest TMAX / coldest TMIN per calendar day, "
                    "across a station's full history.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("station")
    ap.add_argument("--date", type=str, default=None,
                    help="MM-DD (or YYYY-MM-DD, year ignored) to print just that day's "
                         "top N to the console instead of writing the full 366-day CSV.")
    ap.add_argument("--top", type=int, default=5, help="How many per day. Default: 5.")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--out-prefix", default=None,
                    help="CSV filename prefix. Default: <station>_top<N>_daily_extremes")
    ap.add_argument("--local-only", action="store_true",
                    help="Force the local db even for ACIS-registered stations "
                         "(default prefers ACIS's full period of record for "
                         "any ACIS station, since this script wants maximal "
                         "historical coverage).")
    args = ap.parse_args()

    import os
    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return
    conn = sqlite3.connect(args.db)

    local_stations = set(discover_stations(conn))
    if args.station not in local_stations and not station_exists_in_acis(args.station):
        print(f"'{args.station}' not found in local db or in the ACIS network.")
        conn.close()
        return

    if args.local_only:
        full_hist = get_full_daily_history(conn, args.station, local_stations)
    else:
        full_hist = get_full_daily_history_prefer_acis(conn, args.station, local_stations)
    if full_hist.empty:
        print(f"No historical data found for {args.station}.")
        conn.close()
        return

    tmax_top, tmin_top = compute_top_n_tables(full_hist, n=args.top)

    if args.date:
        month_day = pd.Timestamp(args.date if len(args.date) > 5 else f"2000-{args.date}").strftime("%m-%d")
        print_single_day(args.station, month_day, tmax_top, tmin_top, args.top)
    else:
        out_dir = Path(args.out_dir)
        prefix = args.out_prefix or f"{args.station}_top{args.top}_daily_extremes"
        write_full_table(args.station, tmax_top, tmin_top, args.top, out_dir / f"{prefix}.csv")

    conn.close()


if __name__ == "__main__":
    main()
