#!/usr/bin/env python3
"""
consecutive_days.py — Find runs of consecutive calendar days meeting a
temperature threshold (>= or <=), for any station in the archive,
including G6964.

Connected to the rest of the pipeline rather than a standalone script:
reuses load_daily_summary()/discover_stations()/DB_PATH/DEFAULT_TZ from
climate_normals.py directly, so this shares the exact same daily
TMAX/TMIN/TAVG aggregation (local calendar days, TAVG=(TMAX+TMIN)/2) as
the normals/compare/monthly/threshold reports there, instead of
reimplementing daily aggregation a second time with its own quirks.

STATIONS NOT IN THE LOCAL DB: rather than erroring out, this checks
whether the station is registered in the ACIS network (data.rcc-acis.org)
and, if so, pulls daily maxt/mint directly from ACIS's StnData webservice
for the requested date range, computing tavg=(tmax+tmin)/2 to match the
same local-aggregation convention climate_normals.py uses -- so a station
missing locally (or only partially archived) still gets a real answer
instead of "no data found." This mirrors the same ACIS-registration check
used in station_normals_vs_observed.py.

A "run" requires an actual data point on every day in between -- a
missing day (sensor gap, station offline) breaks the streak, same as a
day that simply fails the threshold. This applies uniformly regardless
of station, so G6964's shorter history and NWS/IEM stations' longer
history are both handled the same way.

Usage:
    python consecutive_days.py G6964 --field tmax --above 90
    python consecutive_days.py KSGF --field tmin --below 32
    python consecutive_days.py KSGF --field tavg --at-or-above 75 --start-date 2020-01-01
    python consecutive_days.py G6964 --field tmin --below 32 --top 5

    # Station not in the local db -- automatically pulled from ACIS:
    python consecutive_days.py KMCI --field tmax --above 100 --start-date 2000-01-01
"""

import argparse
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import requests

from climate_normals import (
    load_daily_summary, discover_stations, DB_PATH, DEFAULT_TZ,
)

ACIS_STNDATA_URL = "http://data.rcc-acis.org/StnData"


def station_exists_in_acis(station):
    """
    Check whether `station` is a registered ACIS station (vs. a personal
    station id like 'G6964' that only lives in your local db, or a typo).
    """
    try:
        resp = requests.post(
            "http://data.rcc-acis.org/StnMeta",
            json={"sids": station, "meta": ["name", "sids"]},
            timeout=30,
        )
        resp.raise_for_status()
        return bool(resp.json().get("meta"))
    except Exception as e:
        print(f"Warning: could not check ACIS registration for {station!r} ({e}).")
        return False


def load_daily_from_acis(station, start_date, end_date):
    """
    Pull daily maxt/mint for `station` directly from ACIS StnData and shape
    it to match load_daily_summary()'s output: columns date/tmax/tmin/tavg,
    with tavg=(tmax+tmin)/2 -- the same convention climate_normals.py uses
    for local calendar-day aggregation, so downstream run-finding logic
    doesn't need to know or care which source the data came from.

    NOTE: this fetches the full requested range in one call. For very wide
    ranges (e.g. the script's default --start-date 1900-01-01) that's a
    large one-time pull; ACIS handles a start date before a station's
    actual period of record gracefully (it just returns from when the
    station's record actually begins).
    """
    payload = {
        "sid": station,
        "sdate": start_date,
        "edate": end_date,
        "elems": ["maxt", "mint"],
        "meta": ["name"],
    }
    resp = requests.post(ACIS_STNDATA_URL, json=payload, timeout=120)
    resp.raise_for_status()
    result = resp.json()

    if "data" not in result or not result["data"]:
        return pd.DataFrame(columns=["date", "tmax", "tmin", "tavg"])

    df = pd.DataFrame(result["data"], columns=["date", "tmax", "tmin"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["tmax"] = pd.to_numeric(df["tmax"], errors="coerce")  # 'M' (missing) -> NaN
    df["tmin"] = pd.to_numeric(df["tmin"], errors="coerce")
    df["tavg"] = (df["tmax"] + df["tmin"]) / 2
    return df[["date", "tmax", "tmin", "tavg"]]


def load_daily_any_source(conn, station, local_stations, start_date, end_date, tzname):
    """
    Route to the local db for stations already archived there, or fall
    back to a live ACIS pull for stations that are ACIS-registered but not
    (or not fully) present locally.
    """
    if station in local_stations:
        return load_daily_summary(conn, [station], start_date, end_date, tzname)

    if station_exists_in_acis(station):
        print(f"'{station}' not found in local db -- pulling daily obs from "
              f"ACIS ({start_date} to {end_date})...")
        return load_daily_from_acis(station, start_date, end_date)

    print(f"Warning: station '{station}' not found in local db or in the "
          f"ACIS network. Available locally: {local_stations}")
    return pd.DataFrame(columns=["date", "tmax", "tmin", "tavg"])


def find_missing_days(daily, field, start_date, end_date):
    """Calendar dates in [start_date, end_date] with NO observation at all
    for `field` -- distinct from a day that has data but simply fails the
    threshold. xmACIS2 surfaces this explicitly because a streak broken by
    a genuine missing day is a different (weaker) claim than one broken by
    a day that actually disproved the condition: the missing day's real
    value is simply unknown, not confirmed to have failed.
    """
    present = set(daily.dropna(subset=[field])["date"].dt.date)
    all_days = pd.date_range(start_date, end_date, freq="D")
    return [d.date() for d in all_days if d.date() not in present]


def find_consecutive_runs(daily, field, comparison_fn):
    """Find all runs of consecutive calendar days (day-to-day gap of
    exactly 1) where comparison_fn(value) is True for `field`. A day
    missing from `daily` entirely (no data, not just a non-matching
    value) breaks the run, since the gap check is purely date-arithmetic
    on whatever rows actually exist.

    Returns a list of dicts: start_date, end_date, length, values
    (the field's value on each day in the run, in date order).
    """
    daily = daily.dropna(subset=[field]).sort_values("date").reset_index(drop=True)
    if daily.empty:
        return []

    runs = []
    current_run = []

    for _, row in daily.iterrows():
        date = row["date"]
        value = row[field]
        matches = comparison_fn(value)

        if matches:
            if current_run and (date - current_run[-1][0]).days == 1:
                current_run.append((date, value))
            else:
                if current_run:
                    runs.append(current_run)
                current_run = [(date, value)]
        else:
            if current_run:
                runs.append(current_run)
                current_run = []

    if current_run:
        runs.append(current_run)

    result = []
    for run in runs:
        dates = [d for d, v in run]
        values = [v for d, v in run]
        result.append({
            "start_date": dates[0],
            "end_date": dates[-1],
            "length": len(dates),
            "values": values,
        })
    return result


def report_consecutive_days(daily, station, field, direction, threshold,
                            start_date, end_date, top_n=10,
                            min_length=None):
    label = {"above": f"> {threshold}", "below": f"< {threshold}",
             "at_or_above": f">= {threshold}", "at_or_below": f"<= {threshold}"}[direction]
    comparison_fns = {
        "above": lambda v: v > threshold,
        "below": lambda v: v < threshold,
        "at_or_above": lambda v: v >= threshold,
        "at_or_below": lambda v: v <= threshold,
    }
    comparison_fn = comparison_fns[direction]

    def format_streak_line(r, missing_set):
        extreme = max(r["values"]) if direction in ("above", "at_or_above") else min(r["values"])
        date_range = (r["start_date"].strftime("%Y-%m-%d") if r["length"] == 1
                     else f"{r['start_date'].strftime('%Y-%m-%d')} -> "
                          f"{r['end_date'].strftime('%Y-%m-%d')}")
        # Defensive wrap, consistent with the same fix in plotter.py/
        # tempAnalysis.py: this traces back to .iterrows() (confirmed
        # via testing to box values as proper pandas.Timestamp, unlike
        # the .items() pattern that was actually vulnerable), but the
        # explicit wrap costs nothing and removes any doubt.
        day_before = (pd.Timestamp(r["start_date"]) - pd.Timedelta(days=1)).date()
        day_after = (pd.Timestamp(r["end_date"]) + pd.Timedelta(days=1)).date()
        notes = []
        if day_before in missing_set:
            notes.append(f"data missing {day_before} -- may extend earlier")
        if day_after in missing_set:
            notes.append(f"data missing {day_after} -- may extend later")
        note_str = f"  [{'; '.join(notes)}]" if notes else ""
        return (f"{r['length']} day(s): {date_range}  "
               f"(most extreme {field}: {extreme:.1f}){note_str}")

    print(f"\n{'=' * 70}")
    print(f"  CONSECUTIVE DAYS: {station}, {field} {label}")
    print(f"  Range checked: {start_date} -> {end_date}")
    print(f"{'=' * 70}")

    if daily.empty or field not in daily.columns:
        print("  No data found for this station/field/range.")
        return []

    missing_days = find_missing_days(daily, field, start_date, end_date)
    total_days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days + 1
    completeness = 100.0 * (total_days - len(missing_days)) / total_days if total_days else 0.0
    print(f"\n  Missing days: {len(missing_days)} of {total_days} "
         f"({completeness:.1f}% complete)")
    if 0 < len(missing_days) <= 10:
        print(f"    {', '.join(d.strftime('%Y-%m-%d') for d in missing_days)}")
    elif len(missing_days) > 10:
        print(f"    (too many to list -- first 5: "
             f"{', '.join(d.strftime('%Y-%m-%d') for d in missing_days[:5])} ...)")

    missing_set = set(missing_days)

    runs = find_consecutive_runs(daily, field, comparison_fn)
    if not runs:
        print(f"\n  No runs found where {field} {label}.")
        return []

    runs.sort(key=lambda r: r["length"], reverse=True)

    # Is the single most recent day in this station's record part of an
    # ongoing streak? Useful context distinct from the historical top-N.
    last_date = daily["date"].max()
    current_streak = next(
        (r for r in runs if r["end_date"] == last_date), None)

    print(f"\n  Top {min(top_n, len(runs))} longest run(s):")
    for i, r in enumerate(runs[:top_n], 1):
        print(f"    {i}. {format_streak_line(r, missing_set)}")

    # "Streaks of at least X days" is a different question from "top N
    # longest" -- this is a THRESHOLD count (e.g. "how many 5+ day heat
    # waves"), which can return many more (or fewer) results than top_n,
    # and answers "how often has this happened" rather than "what was
    # the biggest one."
    if min_length is not None:
        qualifying = [r for r in runs if r["length"] >= min_length]
        print(f"\n  Streaks of at least {min_length} day(s): "
             f"{len(qualifying)} found")
        if len(qualifying) <= 25:
            for i, r in enumerate(qualifying, 1):
                print(f"    {i}. {format_streak_line(r, missing_set)}")
        else:
            print(f"    (too many to list individually -- showing the 10 longest)")
            for i, r in enumerate(qualifying[:10], 1):
                print(f"    {i}. {format_streak_line(r, missing_set)}")

    if current_streak:
        print(f"\n  Currently ONGOING as of the archive's last day "
             f"({last_date.strftime('%Y-%m-%d')}): {current_streak['length']} "
             f"day(s) so far, starting {current_streak['start_date'].strftime('%Y-%m-%d')}.")
    else:
        print(f"\n  No streak currently active as of the archive's last "
             f"day ({last_date.strftime('%Y-%m-%d')}).")

    return runs


def main():
    ap = argparse.ArgumentParser(
        description="Find runs of consecutive days meeting a temperature "
                    "threshold, for any station (including G6964). Stations "
                    "not in the local db are automatically pulled from ACIS "
                    "if registered there.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("station")
    ap.add_argument("--field", choices=["tmax", "tmin", "tavg"], default="tmax")
    ap.add_argument("--above", type=float, default=None)
    ap.add_argument("--below", type=float, default=None)
    ap.add_argument("--at-or-above", type=float, default=None)
    ap.add_argument("--at-or-below", type=float, default=None)
    ap.add_argument("--start-date", type=str, default="1900-01-01")
    ap.add_argument("--end-date", type=str, default=None)
    ap.add_argument("--top", type=int, default=10,
                    help="Number of longest runs to show (default: 10)")
    ap.add_argument("--min-length", type=int, default=None,
                    help="Also list ALL streaks of at least this many days "
                         "(a count/threshold question -- 'how many 5+ day "
                         "heat waves' -- distinct from --top's 'what were "
                         "the biggest ones')")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--tz", default=DEFAULT_TZ)
    args = ap.parse_args()

    conditions = [("above", args.above), ("below", args.below),
                 ("at_or_above", args.at_or_above), ("at_or_below", args.at_or_below)]
    active = [(name, val) for name, val in conditions if val is not None]
    if len(active) != 1:
        ap.error("Specify exactly one of --above/--below/--at-or-above/--at-or-below")
    direction, threshold = active[0]

    end_date = args.end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not __import__("os").path.exists(args.db):
        print(f"Database not found: {args.db}")
        return
    conn = sqlite3.connect(args.db)

    local_stations = discover_stations(conn)
    daily = load_daily_any_source(conn, args.station, local_stations,
                                   args.start_date, end_date, args.tz)

    report_consecutive_days(daily, args.station, args.field, direction,
                            threshold, args.start_date, end_date,
                            top_n=args.top, min_length=args.min_length)
    conn.close()


if __name__ == "__main__":
    main()
