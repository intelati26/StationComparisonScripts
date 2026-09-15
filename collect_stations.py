#!/usr/bin/env python3
"""
collect_stations.py — Fetch NWS station obs (KSGF, KBBG) and Ambient
Weather obs into the shared archive. No model/GRIB fetching, so
this has no herbie/numpy dependency and is safe to run frequently
(every few minutes) on its own cron schedule.

Also supports a one-time bulk import of a manually-downloaded Ambient
Weather dashboard CSV export (--import-csv), for historical data that
predates or falls outside the live API's pagination range. This is a
standalone operation -- it does not run the normal fetch loop.

Also supports backfilling NWS station history from the Iowa Environmental
Mesonet's ASOS archive (--iem-backfill), which holds far more history than
the live NWS API's rolling window -- default 48h, an explicit range via
--start-date/--end-date, or a station's entire history via --full-history.
This also populates wind_speed for NWS stations, which the live-API path
(fetch_nws_station) does not.

Usage:
  python collect_stations.py --station KSGF,KBBG          # Last 48h, skip existing
  python collect_stations.py --station KSGF --days 60            # Last 60 days
  python collect_stations.py --station KBBG --hours 72 --force  # Overwrite last 72h
  python collect_stations.py --station KSGF,KBBG --start-date 2026-06-01 --end-date 2026-07-01
  python collect_stations.py --ambient-force --days 30   # Re-pull G6964 only
  python collect_stations.py --backfill-fields --days 30 # Fill missing G6964 fields
  python collect_stations.py --import-csv export.csv     # One-time CSV bulk import
  python collect_stations.py --iem-backfill KSGF                      # Last 48h via IEM
  python collect_stations.py --iem-backfill KSGF --days 365            # 1 year via IEM
  python collect_stations.py --iem-backfill KSGF --start-date 2020-01-01 --end-date 2024-01-01
  python collect_stations.py --iem-backfill KSGF --full-history         # Entire station history
"""

import sys
import argparse
import time
import sqlite3
from datetime import datetime, timezone, timedelta

from weather_common import (
    DB_PATH, init_db, parse_date_arg,
    fetch_nws_station, fetch_ambient_g6964, import_ambient_csv,
    fetch_iem_asos, fetch_iem_asos_1min, IEM_FULL_HISTORY_START,
)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch NWS + Ambient Weather station observations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=float, default=None,
                        help="Lookback window in DAYS from now.")
    parser.add_argument("--hours", type=float, default=48.0,
                        help="Lookback window in HOURS from now (default: 48).")
    parser.add_argument("--start-date", type=str, default=None,
                        help="Start date (inclusive). Flexible parsing.")
    parser.add_argument("--end-date", type=str, default=None,
                        help="End date (inclusive). Defaults to now.")
    parser.add_argument("--force", action="store_true",
                        help="Re-download and overwrite ALL hours for NWS stations.")
    parser.add_argument("--ambient-force", action="store_true",
                        help=("Force re-download ALL G6964 data with full "
                              "fields, overwriting existing rows."))
    parser.add_argument("--backfill-fields", action="store_true",
                        help=("Re-paginate the Ambient Weather API to backfill "
                              "missing fields into existing G6964 rows, "
                              "without inserting new rows."))
    parser.add_argument("--import-csv", type=str, default=None,
                        help=("ONE-TIME: bulk import an Ambient Weather "
                              "dashboard CSV export. Overwrites any existing "
                              "rows for matching (station, timestamp). Runs "
                              "standalone -- ignores --days/--hours/--force "
                              "and does not run the normal fetch loop."))
    parser.add_argument("--import-station", type=str, default="G6964",
                        help="Station code to tag imported CSV rows with "
                             "(default: G6964).")
    parser.add_argument("--iem-backfill", type=str, default=None, metavar="STATION",
                        help=("Fetch station history from IEM Mesonet's ASOS "
                              "archive (hourly + special METAR reports) "
                              "instead of the live NWS API. Accepts a "
                              "comma-separated list (e.g. KSGF,KBBG) -- "
                              "processed SERIALLY with a courtesy pause "
                              "between stations, never concurrently, out "
                              "of respect for IEM's own documented 1-"
                              "second-per-IP throttle. Combine with "
                              "--days/--hours/--start-date/--end-date for a "
                              "specific range, or --full-history for the "
                              "entire archive. Standalone -- does not run "
                              "the normal fetch loop."))
    parser.add_argument("--iem-1min-backfill", type=str, default=None, metavar="STATION",
                        help=("Fetch HIGH-FREQUENCY (~1-4min cadence) "
                              "history from IEM's separate one-minute ASOS "
                              "archive. Also accepts a comma-separated list, "
                              "processed serially like --iem-backfill. "
                              "PROVISIONAL: this format is described by IEM "
                              "itself as best-guess/undocumented, delayed "
                              "~24-36h, no relative humidity field, "
                              "coverage varies by station. Same range "
                              "flags as --iem-backfill."))
    parser.add_argument("--iem-pause", type=float, default=3.0,
                        help="Seconds to wait between stations when "
                             "--iem-backfill/--iem-1min-backfill gets "
                             "multiple stations (default: 3.0). IEM's own "
                             "docs mention a 1s-per-IP throttle; this "
                             "default sits comfortably above that rather "
                             "than exactly at it.")
    parser.add_argument("--full-history", action="store_true",
                        help="With --iem-backfill: fetch the station's "
                             "entire available history instead of a "
                             "bounded range.")
    parser.add_argument("--iem-force", action="store_true",
                        help="With --iem-backfill: overwrite existing rows "
                             "instead of skipping them.")
    parser.add_argument("--station", type=str, default=None,
                        help="Comma-separated station code(s) to fetch "
                             "(e.g. KJLN,KSGF,KBBG). REQUIRED for a normal "
                             "fetch run -- weather_common.py no longer "
                             "defines a default station list, so this "
                             "can't silently fetch stations you didn't "
                             "ask for. Not needed with --import-csv/"
                             "--iem-backfill/--iem-1min-backfill/"
                             "--ambient-force/--backfill-fields, which "
                             "already specify their own target station(s).")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.iem_backfill or args.iem_1min_backfill:
        raw_stations = args.iem_backfill or args.iem_1min_backfill
        target_stations = [s.strip() for s in raw_stations.split(",") if s.strip()]
        is_1min = bool(args.iem_1min_backfill)
        label = "IEM MESONET ASOS 1-MINUTE BACKFILL" if is_1min else "IEM MESONET ASOS BACKFILL"

        now_utc = datetime.now(timezone.utc)
        if args.full_history:
            start_time = IEM_FULL_HISTORY_START
            end_time = now_utc
        elif args.start_date:
            start_time = parse_date_arg(args.start_date)
            end_time = parse_date_arg(args.end_date) if args.end_date else now_utc
            if end_time > now_utc:
                end_time = now_utc
        else:
            lookback = (timedelta(days=args.days) if args.days is not None
                       else timedelta(hours=args.hours))
            end_time = now_utc
            start_time = now_utc - lookback

        print(f"\n=== {label} ({', '.join(target_stations)}) ===")
        if len(target_stations) > 1:
            print(f"  {len(target_stations)} stations, processed SERIALLY "
                  f"with a {args.iem_pause:.1f}s pause between each -- "
                  f"never concurrent requests.")
        if args.full_history:
            print("  Mode: FULL HISTORY (from "
                  f"{start_time.strftime('%Y-%m-%d')})")
            if is_1min:
                print("  WARNING: full-history + 1-minute cadence could be "
                      "a very large request per station. Consider a "
                      "bounded range (--start-date/--end-date) instead "
                      "for this source.")

        for i, station in enumerate(target_stations):
            if i > 0:
                print(f"\n  (pausing {args.iem_pause:.1f}s before next "
                      f"station...)")
                time.sleep(args.iem_pause)
            print(f"\n--- Station {i+1}/{len(target_stations)}: {station} ---")
            if is_1min:
                fetch_iem_asos_1min(conn, station, start_time, end_time,
                                    force=args.iem_force)
            else:
                fetch_iem_asos(conn, station, start_time, end_time,
                               force=args.iem_force)
        conn.close()
        print("\nIEM backfill complete. Data stored in", DB_PATH)
        sys.exit(0)

    if args.import_csv:
        print("\n=== ONE-TIME CSV IMPORT ===")
        import_ambient_csv(conn, args.import_csv, station=args.import_station)
        conn.close()
        print("\nCSV import complete. Data stored in", DB_PATH)
        sys.exit(0)

    now_utc = datetime.now(timezone.utc)
    if args.start_date:
        start_time = parse_date_arg(args.start_date)
        end_time = parse_date_arg(args.end_date) if args.end_date else now_utc
        if end_time > now_utc:
            end_time = now_utc
        print(f"Date range: {start_time.strftime('%Y-%m-%d %H:%M')} -> "
              f"{end_time.strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        if args.days is not None:
            lookback = timedelta(days=args.days)
            print(f"Lookback: {args.days} days")
        else:
            lookback = timedelta(hours=args.hours)
            print(f"Lookback: {args.hours:.0f} hours ({args.hours / 24:.1f} days)")
        end_time = now_utc
        start_time = now_utc - lookback

    if args.ambient_force:
        print("\n=== AMBIENT FORCE BACKFILL (G6964 only) ===")
        fetch_ambient_g6964(conn, start_time, end_time, force=True, fields_only=False)
    elif args.backfill_fields:
        print("\n=== AMBIENT FIELDS BACKFILL (existing rows only) ===")
        fetch_ambient_g6964(conn, start_time, end_time, force=False, fields_only=True)
    else:
        if not args.station:
            parser.error(
                "--station is required for a normal fetch run (e.g. "
                "--station KJLN,KSGF,KBBG) -- weather_common.py no "
                "longer defines a default station list, so this won't "
                "silently fetch stations you didn't ask for."
            )
        stations = [s.strip() for s in args.station.split(",") if s.strip()]
        for st in stations:
            if st == "G6964":
                fetch_ambient_g6964(conn, start_time, end_time, force=args.force)
            else:
                fetch_nws_station(conn, st, start_time, end_time, force=args.force)

    conn.close()
    print("\nStation collection complete. Data stored in", DB_PATH)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
