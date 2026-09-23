#!/usr/bin/env python3

import argparse
import csv
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from weather_common import init_db


BASE_URL = "https://api.weather.com/v2/pws"
SOURCE_PREFIX = "WU:"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect Weather Underground PWS observations into station_obs."
    )

    parser.add_argument(
        "--station",
        required=True,
        help="Weather Underground station ID, e.g. KMOJOPLI124",
    )

    parser.add_argument(
        "--db",
        default="weather_archive.db",
        help="SQLite database path",
    )

    parser.add_argument(
        "--start",
        help="Start time, ISO-8601 UTC, e.g. 2026-09-20T00:00:00Z",
    )

    parser.add_argument(
        "--end",
        help="End time, ISO-8601 UTC, e.g. 2026-09-23T00:00:00Z",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite observations that already exist",
    )

    parser.add_argument(
        "--pause",
        type=float,
        default=1.0,
        help="Seconds to wait between API requests",
    )

    return parser.parse_args()


def parse_timestamp(value):
    """Parse a WU ISO timestamp and return an aware UTC datetime."""
    if not value:
        return None

    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)

        return ts

    except (ValueError, TypeError):
        return None


def request_json(session, endpoint, params):
    response = session.get(
        endpoint,
        params=params,
        timeout=60,
    )

    print(f"HTTP: {response.status_code}")

    response.raise_for_status()

    return response.json()


def load_existing_timestamps(conn, station):
    rows = conn.execute(
        """
        SELECT timestamp
        FROM station_obs
        WHERE station = ?
        """,
        (station,),
    ).fetchall()

    return {row[0] for row in rows}


def save_observations(
    conn,
    station,
    observations,
    range_start,
    range_end,
    existing,
    force=False,
):
    """
    Save WU observations into station_obs.

    WU's numeric weather measurements are contained in the
    observation's "imperial" object.
    """

    new_count = 0
    overwritten_count = 0
    skipped_count = 0

    with conn:
        for obs in observations:

            ts_str = obs.get("obsTimeUtc")

            if not ts_str:
                continue

            ts = parse_timestamp(ts_str)

            if ts is None:
                continue

            if ts < range_start or ts > range_end:
                continue

            iso_ts = ts.isoformat()

            was_existing = iso_ts in existing

            if was_existing and not force:
                skipped_count += 1
                continue

            imperial = obs.get("imperial") or {}

            # ---------------------------------------------------------
            # Weather Underground field mapping
            # ---------------------------------------------------------

            temperature = imperial.get("tempAvg")

            dewpoint = imperial.get("dewptAvg")

            relative_humidity = obs.get("humidityAvg")

            wind_speed = imperial.get("windspeedAvg")

            wind_gust = imperial.get("windgustAvg")

            wind_dir = obs.get("winddirAvg")

            # Use wind chill when available; otherwise heat index.
            feels_like = imperial.get("windchillAvg")

            if feels_like is None:
                feels_like = imperial.get("heatindexAvg")

            # precipRate is the precipitation rate reported by WU.
            rain_hourly = imperial.get("precipRate")

            # These fields are intentionally left NULL until their
            # semantics are confirmed against the existing schema.
            pressure_rel = None
            rain_event = None

            conn.execute(
                """
                INSERT OR REPLACE INTO station_obs (
                    station,
                    timestamp,
                    temperature,
                    dewpoint,
                    relative_humidity,
                    feels_like,
                    wind_speed,
                    wind_gust,
                    wind_dir,
                    pressure_rel,
                    rain_hourly,
                    rain_event
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    station,
                    iso_ts,
                    temperature,
                    dewpoint,
                    relative_humidity,
                    feels_like,
                    wind_speed,
                    wind_gust,
                    wind_dir,
                    pressure_rel,
                    rain_hourly,
                    rain_event,
                ),
            )

            if was_existing:
                overwritten_count += 1
            else:
                new_count += 1

            existing.add(iso_ts)

    return new_count, overwritten_count, skipped_count


def download_history_day(
    session,
    conn,
    station,
    api_station,
    api_key,
    date,
    range_start,
    range_end,
    existing,
    force,
):
    """Download one WU history chunk."""

    print(f"[{date}] Downloading history...")

    endpoint = f"{BASE_URL}/history/all"

    params = {
        "stationId": api_station,
        "format": "json",
        "units": "e",
        "numericPrecision": "decimal",
        "date": date,
        "apiKey": api_key,
    }

    data = request_json(session, endpoint, params)

    observations = data.get("observations", [])

    print(f"  {len(observations)} observations")

    if observations:
        first = observations[0].get("obsTimeUtc")
        last = observations[-1].get("obsTimeUtc")

        print(f"  First: {first}")
        print(f"  Last:  {last}")

    result = save_observations(
        conn,
        station,
        observations,
        range_start,
        range_end,
        existing,
        force,
    )

    new_count, overwritten_count, skipped_count = result

    print(
        f"  Saved: {new_count}, "
        f"overwritten: {overwritten_count}, "
        f"skipped: {skipped_count}"
    )

    return result


def download_recent(
    session,
    conn,
    station,
    api_station,
    api_key,
    range_start,
    range_end,
    existing,
    force,
):
    """Download the rolling 24-hour rapid-frequency observations."""

    print("[recent] Downloading 1-day rapid observations...")

    endpoint = f"{BASE_URL}/observations/all/1day"

    params = {
        "stationId": api_station,
        "format": "json",
        "units": "e",
        "numericPrecision": "decimal",
        "apiKey": api_key,
    }

    data = request_json(session, endpoint, params)

    observations = data.get("observations", [])

    print(f"  {len(observations)} observations")

    if observations:
        first = observations[0].get("obsTimeUtc")
        last = observations[-1].get("obsTimeUtc")

        print(f"  First: {first}")
        print(f"  Last:  {last}")

    result = save_observations(
        conn,
        station,
        observations,
        range_start,
        range_end,
        existing,
        force,
    )

    new_count, overwritten_count, skipped_count = result

    print(
        f"  Saved: {new_count}, "
        f"overwritten: {overwritten_count}, "
        f"skipped: {skipped_count}"
    )

    return result


def date_range(start_date, end_date):
    """Yield YYYYMMDD strings for an inclusive date range."""

    current = start_date

    while current <= end_date:
        yield current.strftime("%Y%m%d")
        current += timedelta(days=1)


def collect_last_72_hours(
    session,
    conn,
    station,
    api_station,
    api_key,
    existing,
    force,
    pause,
):
    """
    Collect a complete rolling 72-hour window.

    We intentionally fetch history through the newest history chunk
    and ALSO fetch the rolling 24-hour endpoint. Duplicate timestamps
    are handled by station/timestamp and --force.
    """

    now = datetime.now(timezone.utc)

    range_start = now - timedelta(hours=72)
    range_end = now

    print()
    print("72-hour collection")
    print(f"Start: {range_start.isoformat()}")
    print(f"End:   {range_end.isoformat()}")
    print()

    # -------------------------------------------------------------
    # History
    #
    # Fetch every calendar day touched by the 72-hour window.
    # An extra day on either side is harmless because observations
    # are filtered against range_start/range_end.
    # -------------------------------------------------------------

    history_first_date = (range_start - timedelta(days=1)).date()
    history_last_date = range_end.date()

    for date in date_range(history_first_date, history_last_date):

        download_history_day(
            session,
            conn,
            station,
            api_station,
            api_key,
            date,
            range_start,
            range_end,
            existing,
            force,
        )

        time.sleep(pause)

    # -------------------------------------------------------------
    # Recent rapid observations
    #
    # This is important because the history endpoint does not
    # necessarily contain the newest observations.
    # -------------------------------------------------------------

    download_recent(
        session,
        conn,
        station,
        api_station,
        api_key,
        range_start,
        range_end,
        existing,
        force,
    )


def collect_explicit_range(
    session,
    conn,
    station,
    api_station,
    api_key,
    range_start,
    range_end,
    existing,
    force,
    pause,
):
    """Collect an explicitly requested time range."""

    print()
    print("Explicit collection")
    print(f"Start: {range_start.isoformat()}")
    print(f"End:   {range_end.isoformat()}")
    print()

    first_date = (range_start - timedelta(days=1)).date()
    last_date = range_end.date()

    for date in date_range(first_date, last_date):

        download_history_day(
            session,
            conn,
            station,
            api_station,
            api_key,
            date,
            range_start,
            range_end,
            existing,
            force,
        )

        time.sleep(pause)

    # The rapid endpoint is useful when the requested range reaches
    # the present, so fetch it as well. The timestamp filtering will
    # discard anything outside the requested range.
    now = datetime.now(timezone.utc)

    if range_end >= now - timedelta(days=1):

        download_recent(
            session,
            conn,
            station,
            api_station,
            api_key,
            range_start,
            range_end,
            existing,
            force,
        )


def main():
    load_dotenv()

    args = parse_args()

    api_key = os.environ.get("WUNDERGROUND_API_KEY")

    if not api_key:
        raise RuntimeError(
            "WUNDERGROUND_API_KEY was not found in the environment or .env"
        )

    station = args.station
    db_station = f"{SOURCE_PREFIX}{station}"

    # -------------------------------------------------------------
    # Parse explicit dates if supplied
    # -------------------------------------------------------------

    if bool(args.start) != bool(args.end):
        raise SystemExit(
            "--start and --end must either both be supplied or both omitted."
        )

    range_start = None
    range_end = None

    if args.start and args.end:
        range_start = parse_timestamp(args.start)
        range_end = parse_timestamp(args.end)

        if range_start is None:
            raise SystemExit(f"Invalid --start timestamp: {args.start}")

        if range_end is None:
            raise SystemExit(f"Invalid --end timestamp: {args.end}")

        if range_end < range_start:
            raise SystemExit("--end cannot be earlier than --start")

    # -------------------------------------------------------------
    # Database
    # -------------------------------------------------------------

    conn = sqlite3.connect(args.db)

    try:
        init_db(conn)

        existing = load_existing_timestamps(
            conn,
            db_station,
        )

        print(f"Database: {args.db}")
        print(f"Station:  {db_station}")
        print(f"Existing timestamps: {len(existing)}")

        session = requests.Session()

        if range_start is None:
            collect_last_72_hours(
                session,
                conn,
                db_station,
                station,
                api_key,
                existing,
                args.force,
                args.pause,
            )
        else:
            collect_explicit_range(
                session,
                conn,
                db_station,
                station,
                api_key,
                range_start,
                range_end,
                existing,
                args.force,
                args.pause,
            )

        # ---------------------------------------------------------
        # Final database check
        # ---------------------------------------------------------

        row = conn.execute(
            """
            SELECT
                COUNT(*),
                MIN(timestamp),
                MAX(timestamp)
            FROM station_obs
            WHERE station = ?
            """,
            (db_station,),
        ).fetchone()

        print()
        print("Database result:")
        print(f"  Observations: {row[0]}")
        print(f"  First:       {row[1]}")
        print(f"  Last:        {row[2]}")

        # Show the newest weather values.
        rows = conn.execute(
            """
            SELECT
                timestamp,
                temperature,
                dewpoint,
                relative_humidity,
                wind_speed,
                wind_gust,
                wind_dir
            FROM station_obs
            WHERE station = ?
            ORDER BY timestamp DESC
            LIMIT 5
            """,
            (db_station,),
        ).fetchall()

        print()
        print("Newest observations:")

        for row in rows:
            print(
                f"  {row[0]}  "
                f"temp={row[1]}  "
                f"dew={row[2]}  "
                f"RH={row[3]}  "
                f"wind={row[4]}  "
                f"gust={row[5]}  "
                f"dir={row[6]}"
            )

    finally:
        conn.close()


if __name__ == "__main__":
    main()