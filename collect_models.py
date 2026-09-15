#!/usr/bin/env python3
"""
collect_models.py — Fetch the fast-turnaround model tiers only: HRRR
(analysis + forecast leads) and, optionally, RRFS. Deliberately never
attempts URMA/RTMA — those have ~1-6h latency and belong to
collect_urma.py's slower sweep, so this script never wastes a request
waiting on them. Safe to run every ~45min-1h.

Usage:
  python collect_models.py                       # Last 48h, skip existing
  python collect_models.py --hours 6              # Just the last 6h
  python collect_models.py --force                # Overwrite existing rows
    python collect_models.py --hrrr-max-lead 6       # Walk back up to 6 forecast hours (default)
  python collect_models.py --enable-rrfs           # Also attempt RRFS (experimental)
"""

import sys
import argparse
import sqlite3
from datetime import datetime, timezone, timedelta

from weather_common import (
    DB_PATH, HRRR_MAX_LEAD, init_db, fetch_models, env_lat_lon,
)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch fast-turnaround model tiers (HRRR/RRFS) only.",
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
    parser.add_argument("--hrrr-max-lead", type=int, default=6,
                        help=("Maximum HRRR forecast lead hours to walk back, "
                              "and (since the extend_future fix) how far into "
                              "the future the forecast extension reaches "
                              "(default: 6)."))
    parser.add_argument("--enable-rrfs", action="store_true",
                        help=("EXPERIMENTAL: also attempt RRFS. See RRFS note "
                              "in weather_common.py's module docstring."))
    args = parser.parse_args()

    if args.lat is None or args.lon is None:
        parser.error(
            "Latitude/longitude not set. Pass --lat/--lon, or set "
            "WEATHER_LAT/WEATHER_LON in your environment or .env file."
        )

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    now_utc = datetime.now(timezone.utc)
    if args.start_date:
        from weather_common import parse_date_arg
        start_time = parse_date_arg(args.start_date)
        end_time = parse_date_arg(args.end_date) if args.end_date else now_utc
        if end_time > now_utc:
            end_time = now_utc
    else:
        lookback = (timedelta(days=args.days) if args.days is not None
                    else timedelta(hours=args.hours))
        end_time = now_utc
        start_time = now_utc - lookback

    print(f"Fetching HRRR{' + RRFS (experimental)' if args.enable_rrfs else ''} "
          f"for {start_time.strftime('%Y-%m-%d %H:%M')} -> "
          f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")

    tiers = {"hrrr"} | ({"rrfs"} if args.enable_rrfs else set())

    fetch_models(
        conn, args.lat, args.lon, start_time, end_time,
        force=args.force,
        extend_future=(args.end_date is None and args.start_date is None),
        enable_rrfs=args.enable_rrfs,
        tiers=tiers,
        hrrr_max_lead=args.hrrr_max_lead,
    )

    conn.close()
    print("\nFast-model collection complete. Data stored in", DB_PATH)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
