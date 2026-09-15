#!/usr/bin/env python3
"""
collect_urma.py — The slow, gold-standard sweep: attempts URMA (~6h
latency) and RTMA (~1h latency, as an interim upgrade) only. Never falls
back to HRRR for new fetches, since that's collect_models.py's job — this
script's cascade is urma -> rtma, nothing lower. Meant to run every few
hours; running it more often than URMA's own latency just re-checks rows
that can't have changed yet.

Also owns:
  --backfill-reverse   Deep archive backfill (URMA goes back to 2019 on
                        AWS), working backwards from now.
  anomaly repair pass   Re-checks flagged outlier temps against the full
                        cascade (urma/rtma/hrrr) for a best-effort fix,
                        since repairing bad data should use whatever's
                        available, not just the gold tiers.

Usage:
  python collect_urma.py                          # Last 48h, urma/rtma only
  python collect_urma.py --days 7
  python collect_urma.py --backfill-only           # Just the anomaly repair pass
  python collect_urma.py --backfill-reverse                    # Now -> 2025-01-01
  python collect_urma.py --backfill-reverse --backfill-until 2025-06-01
  python collect_urma.py --vacuum                  # Reclaim disk space (manual maintenance)
  python collect_urma.py --backfill-reverse --parallel 4   # Parallel fetch for a deep backfill
"""

import sys
import argparse
import sqlite3
from datetime import datetime, timezone, timedelta

from weather_common import (
    DB_PATH, init_db, parse_date_arg, env_lat_lon,
    fetch_models, fetch_models_reverse, backfill_anomalies, vacuum_db,
    backup_and_compress_db,
)

URMA_RTMA_TIERS = {"urma", "rtma"}


def _print_delta_summary(deltas, title):
    """Print an aggregate °F-change summary for a list of
    (valid_time, old_model, new_model, old_temp, new_temp, delta) tuples --
    shared formatting for both the tier-upgrade deltas fetch_models()
    returns and the anomaly-repair deltas backfill_anomalies() returns.
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


def main():
    parser = argparse.ArgumentParser(
        description="Gold-standard URMA/RTMA sweep, deep backfill, anomaly repair.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    env_lat, env_lon = env_lat_lon()
    parser.add_argument("--lat", type=float, default=env_lat,
                        help="Latitude (defaults to $WEATHER_LAT env var)")
    parser.add_argument("--lon", type=float, default=env_lon,
                        help="Longitude (defaults to $WEATHER_LON env var)")
    parser.add_argument("--days", type=float, default=None,
                        help="Lookback window in DAYS from now.")
    parser.add_argument("--hours", type=float, default=48.0,
                        help="Lookback window in HOURS from now (default: 48).")
    parser.add_argument("--start-date", type=str, default=None,
                        help="Start date (inclusive). Flexible parsing.")
    parser.add_argument("--end-date", type=str, default=None,
                        help="End date (inclusive). Defaults to now.")
    parser.add_argument("--force", action="store_true",
                        help="Re-download and overwrite ALL hours in range.")
    parser.add_argument("--skip-backfill", action="store_true",
                        help="Skip the anomaly detection/repair pass.")
    parser.add_argument("--backfill-only", action="store_true",
                        help="Skip urma/rtma collection; run only the anomaly repair pass.")
    parser.add_argument("--backfill-reverse", action="store_true",
                        help=("Deep backfill: work BACKWARDS from now towards "
                              "--backfill-until, urma/rtma only."))
    parser.add_argument("--backfill-until", type=str, default="2025-01-01",
                        help="Floor date for --backfill-reverse (default: 2025-01-01).")
    parser.add_argument("--vacuum", action="store_true",
                        help=("MANUAL MAINTENANCE: run VACUUM to reclaim "
                              "space from overwritten rows. Standalone -- "
                              "ignores other flags and does not run the "
                              "normal fetch/backfill. Not safe to run on a "
                              "tight cron schedule (needs an exclusive DB "
                              "lock); run this one manually/occasionally."))
    parser.add_argument("--backup", action="store_true",
                        help=("MANUAL MAINTENANCE: snapshot + gzip the DB "
                              "for cold storage (~13x smaller in practice). "
                              "Standalone, safe to run alongside other cron "
                              "jobs -- uses sqlite3's backup API rather "
                              "than a raw file copy."))
    parser.add_argument("--parallel", type=int, default=1, metavar="N",
                        help=("Fetch N hours concurrently instead of one at "
                              "a time (default: 1, fully serial, unchanged "
                              "behavior). Only applies to hours with no "
                              "existing row -- tier-upgrade comparisons "
                              "stay serial regardless. UNTESTED against a "
                              "real network/eccodes install (this was "
                              "written without either available) -- start "
                              "small (3-4) and watch closely for garbled/"
                              "inconsistent values or crashes before "
                              "increasing it. If you see corruption, this "
                              "is the first thing to suspect; --parallel 1 "
                              "disables it entirely."))
    args = parser.parse_args()

    if args.lat is None or args.lon is None:
        parser.error(
            "Latitude/longitude not set. Pass --lat/--lon, or set "
            "WEATHER_LAT/WEATHER_LON in your environment or .env file."
        )

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.backup:
        backup_and_compress_db(conn)
        conn.close()
        sys.exit(0)

    if args.vacuum:
        vacuum_db(conn)
        conn.close()
        sys.exit(0)

    if args.backfill_reverse:
        until_date = parse_date_arg(args.backfill_until)
        print("=== URMA/RTMA REVERSE BACKFILL ===")
        print(f"  Working backwards from NOW to "
              f"{until_date.strftime('%Y-%m-%d')} UTC (urma/rtma only)")
        fetch_models_reverse(conn, args.lat, args.lon, until_date,
                             force=args.force, tiers=URMA_RTMA_TIERS,
                             max_workers=args.parallel)

        if not args.skip_backfill:
            backfill_anomalies(conn, args.lat, args.lon)  # full cascade, best-effort repair

        conn.close()
        print("Reverse backfill complete. Data stored in", DB_PATH)
        sys.exit(0)

    now_utc = datetime.now(timezone.utc)
    if args.start_date:
        start_time = parse_date_arg(args.start_date)
        end_time = parse_date_arg(args.end_date) if args.end_date else now_utc
        if end_time > now_utc:
            end_time = now_utc
    else:
        lookback = (timedelta(days=args.days) if args.days is not None
                    else timedelta(hours=args.hours))
        end_time = now_utc
        start_time = now_utc - lookback

    if not args.backfill_only:
        print(f"Fetching URMA/RTMA for {start_time.strftime('%Y-%m-%d %H:%M')} -> "
              f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")
        fetch_models(
            conn, args.lat, args.lon, start_time, end_time,
            force=args.force,
            extend_future=False,  # future hours can only ever be hrrr — not this script's job
            tiers=URMA_RTMA_TIERS,
        )

    if not args.skip_backfill:
        backfill_anomalies(conn, args.lat, args.lon)  # full cascade, best-effort repair

    conn.close()
    print("\nURMA/RTMA collection complete. Data stored in", DB_PATH)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
