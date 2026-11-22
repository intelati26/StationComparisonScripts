#!/usr/bin/env python3
"""
calibrate_clearsky.py

Fit an empirical clear-sky correction for a station's solar radiation
sensor, binned by solar elevation angle, and store it in the
clearsky_calibration table (see weather_common.SCHEMA) for plotter.py's
cloudiness panel to apply on top of the generic Haurwitz formula.

WHY: the Haurwitz model (clear_sky_ghi() in plotter.py) is a single
formula fit to average atmospheric clarity -- it doesn't know this
site's elevation, typical haze, or this specific sensor's cosine
response at low sun angles. All three show up the same way in the
data: "measured solar runs consistently high or low relative to
Haurwitz at a given sun angle." This script measures that directly
from station_ids import AMBIENT_ID
from the station's own history instead of guessing at it.

METHOD (--method clear-days, the default; --method percentile is the original):
    clear-days: readings are thinned to every 15 minutes, days are classified clear when they are bright and SMOOTH (a day's mean |second
    difference| over its peak is small; cloud makes the curve jagged), and each elevation bin takes the MEDIAN clearness index of the clear days
    only. The original 97th percentile over ALL readings picks the brightest instants (cloud-edge enhancement, the clearest air of the whole
    history) and so sat about 14% above an ordinary clear day: on a clear day the measured curve then never reached the clear-sky line and the
    cloudiness panel showed a grey "cloudier" band under a clear sky.

    percentile (original):
    1. For every solar_radiation reading, compute solar elevation and
       the raw (uncorrected) Haurwitz estimate for that moment.
    2. clearness index kt = measured / haurwitz_raw.
    3. Bin readings by elevation angle (--bin-size degrees each).
    4. Per bin, take a high percentile (--percentile, default 97th) of
       kt -- NOT the mean, since clouds only ever pull kt down from the
       true clear-sky ceiling, essentially never push it above. The
       high percentile approximates "what this bin looks like on a
       clear reading", filtered for the occasional sensor-glitch spike
       by not using the bare max.
    5. Bins with fewer than --min-samples readings are skipped (left
       out of the table entirely) rather than recorded on thin data --
       plotter.py's interpolation just spans the gap.

This is a per-station GLOBAL fit across the whole history queried, not
split by season -- a full year+ of data averages seasonal turbidity
swings into one curve. If that stops being good enough, the next step
would be splitting by month-of-year, but that's not built here.

Default behavior is a no-op if calibration rows already exist for the
station (prints the existing table and exits) -- pass --refresh to
recompute and overwrite.

Example:
    python calibrate_clearsky.py \
        --db /path/to/weather_archive.db \
        --station $AMBIENT_STATION_ID \
        --refresh
"""

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from astral import Observer
from astral.sun import elevation

from weather_common import init_db, env_lat_lon, parse_date_arg


# ---------------------------------------------------------------------------
# Haurwitz clear-sky formula (kept in sync with plotter.py's clear_sky_ghi --
# duplicated rather than imported so this script and plotter.py each stay
# standalone; see plotter.py's own copy for the "why duplicated" note).
# ---------------------------------------------------------------------------

def haurwitz_raw(elevation_deg):
    """Raw (uncorrected) clear-sky GHI estimate (W/m^2) from solar
    elevation alone."""
    if elevation_deg is None or elevation_deg <= 0:
        return 0.0
    cos_z = math.cos(math.radians(90.0 - elevation_deg))
    if cos_z <= 0:
        return 0.0
    return max(1098.0 * cos_z * math.exp(-0.057 / cos_z), 0.0)


# ---------------------------------------------------------------------------
# Existing-calibration report (default, no-op path)
# ---------------------------------------------------------------------------

def print_existing_calibration(cursor, station):
    rows = cursor.execute(
        "SELECT elevation_bin_low, elevation_bin_high, correction_factor, "
        "n_samples, percentile, computed_at FROM clearsky_calibration "
        "WHERE station = ? ORDER BY elevation_bin_low",
        (station,),
    ).fetchall()

    print(f"Existing calibration for {station}: {len(rows)} bins")
    print(f"{'Elevation':>14}  {'Factor':>7}  {'N':>7}  {'Pctile':>7}  Computed")
    for lo, hi, factor, n, pctile, computed_at in rows:
        print(f"{lo:5.1f}-{hi:5.1f} deg  {factor:7.3f}  {n:7d}  "
              f"{pctile:7.1f}  {computed_at}")
    print("\nRun with --refresh to recompute from current data.")


# ---------------------------------------------------------------------------
# Main fit
# ---------------------------------------------------------------------------

def fit_calibration(conn, station, lat, lon, bin_size, percentile,
                     min_samples, min_elevation, start_dt, end_dt,
                     method="percentile", clear_kmax=0.6, clear_kmean=0.5, clear_rough=0.04, show_days=False):
    cursor = conn.cursor()

    query = (
        "SELECT timestamp, solar_radiation FROM station_obs "
        "WHERE station = ? AND solar_radiation IS NOT NULL "
        "AND solar_radiation >= 0"
    )
    params = [station]
    if start_dt is not None:
        query += " AND timestamp >= ?"
        params.append(start_dt.isoformat())
    if end_dt is not None:
        query += " AND timestamp <= ?"
        params.append(end_dt.isoformat())
    query += " ORDER BY timestamp"

    df = pd.read_sql(query, conn, params=params, parse_dates=["timestamp"])

    if df.empty:
        print(f"No solar_radiation rows found for station '{station}'. "
              f"Nothing to calibrate.")
        return None

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    if method == "clear-days":
        df = df[df["timestamp"].dt.minute % 15 == 0].reset_index(drop=True)      # 15-minute steps: plenty for a ceiling, and the roughness test is defined on them
    print(f"Loaded {len(df)} solar_radiation readings for {station}, "
          f"{df['timestamp'].min()} -> {df['timestamp'].max()}")

    observer = Observer(latitude=lat, longitude=lon)

    print("Computing solar elevation + Haurwitz baseline per reading "
          "(this can take a while on a large archive)...")
    elevations = np.empty(len(df))
    haurwitz = np.empty(len(df))
    n = len(df)
    for i, ts in enumerate(df["timestamp"]):
        try:
            elev = elevation(observer, ts.to_pydatetime())
        except Exception:
            elev = None
        elevations[i] = elev if elev is not None else np.nan
        haurwitz[i] = haurwitz_raw(elev)
        if (i + 1) % 20000 == 0 or (i + 1) == n:
            print(f"  {i + 1}/{n}", flush=True)

    df["elevation"] = elevations
    df["haurwitz"] = haurwitz

    # Drop night, near-horizon (noisy/div-by-small-cos_z), and anywhere
    # Haurwitz itself came out ~0 (would blow up the ratio).
    usable = df[(df["elevation"] >= min_elevation) & (df["haurwitz"] > 1.0)].copy()
    dropped = len(df) - len(usable)
    print(f"  {len(usable)} readings usable (elevation >= {min_elevation} deg); "
          f"{dropped} dropped (night / near-horizon / degenerate Haurwitz).")

    if usable.empty:
        print("Nothing usable after filtering. Aborting.")
        return None

    usable["kt"] = usable["solar_radiation"] / usable["haurwitz"]
    # Defensive cap: a kt far above 1 is a sensor glitch, not a real
    # clear-sky reading (the atmosphere does not amplify sunlight).
    # Capping (rather than dropping) keeps the row's elevation bin from
    # losing a sample outright over one bad point.
    usable["kt"] = usable["kt"].clip(upper=1.3)

    bin_edges = np.arange(min_elevation, 90.0 + bin_size, bin_size)
    usable["bin_idx"] = np.digitize(usable["elevation"], bin_edges) - 1

    if method == "clear-days":
        # the solar day (local apparent time: the station's longitude shifts the UTC date) so a day is not cut at UTC midnight
        usable["day"] = (usable["timestamp"] + pd.to_timedelta(lon / 15.0, unit="h")).dt.floor("D")

        def _rough(g):
            v = g.sort_values("timestamp")["solar_radiation"].to_numpy()
            return np.nan if len(v) < 10 else float(np.mean(np.abs(np.diff(v, 2))) / max(v.max(), 1.0))
        days = usable.groupby("day").agg(n=("kt", "size"), kmax=("kt", "max"), kmean=("kt", "mean"))
        days["rough"] = usable.groupby("day").apply(_rough, include_groups=False)
        ok = days[(days["n"] >= 14) & (days["kmax"] >= clear_kmax) & (days["kmean"] >= clear_kmean) & (days["rough"] < clear_rough)].index
        print(f"  {len(ok)} of {len(days)} days classified clear (bright and smooth); fitting the median clearness of those days per elevation bin.")
        if show_days:
            sel = days.loc[ok]
            print("  clear days used (month: date meanKT/roughness):")
            for mon, g in sel.groupby(sel.index.to_period("M")):
                print(f"    {mon}: " + "  ".join(f"{d.day:02d} {r.kmean:.2f}/{r.rough:.3f}" for d, r in g.iterrows()))
        if len(ok) < 5:
            print("Too few clear days to fit a ceiling. Loosen --clear-kmax / --clear-rough or use --method percentile.")
            return None
        usable = usable[usable["day"].isin(ok)]
        min_samples = min(min_samples, 10)

    results = []
    n_sparse = 0
    for bin_idx in range(len(bin_edges) - 1):
        bin_vals = usable.loc[usable["bin_idx"] == bin_idx, "kt"]
        lo, hi = bin_edges[bin_idx], bin_edges[bin_idx + 1]
        if len(bin_vals) < min_samples:
            if len(bin_vals) > 0:
                print(f"  [{lo:5.1f}-{hi:5.1f} deg] SPARSE: only "
                      f"{len(bin_vals)} samples (< {min_samples}) -- skipped.")
                n_sparse += 1
            continue
        factor = float(np.median(bin_vals)) if method == "clear-days" else float(np.percentile(bin_vals, percentile))
        results.append((lo, hi, factor, len(bin_vals)))

    if not results:
        print("Every elevation bin was sparse -- no calibration produced. "
              "Try a larger --bin-size or lower --min-samples.")
        return None

    if n_sparse:
        print(f"  {n_sparse} bin(s) skipped for insufficient data.")

    return results


def write_calibration(conn, station, results, percentile):
    cursor = conn.cursor()
    computed_at = datetime.now(timezone.utc).isoformat()

    with conn:
        cursor.execute(
            "DELETE FROM clearsky_calibration WHERE station = ?", (station,)
        )
        cursor.executemany(
            "INSERT INTO clearsky_calibration "
            "(station, elevation_bin_low, elevation_bin_high, "
            "correction_factor, n_samples, percentile, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(station, lo, hi, factor, n, percentile, computed_at)
             for lo, hi, factor, n in results],
        )

    print(f"\nWrote {len(results)} calibration bins for {station}.")
    print(f"{'Elevation':>14}  {'Factor':>7}  {'N':>7}")
    for lo, hi, factor, n in results:
        flag = ""
        if factor < 0.85 or factor > 1.15:
            flag = "  <- large correction, worth a sanity check"
        print(f"{lo:5.1f}-{hi:5.1f} deg  {factor:7.3f}  {n:7d}{flag}")

    mean_factor = sum(r[2] for r in results) / len(results)
    print(f"\nMean correction factor across bins: {mean_factor:.3f} "
          f"({'raw Haurwitz runs high' if mean_factor < 1 else 'raw Haurwitz runs low'} "
          f"relative to this station by roughly "
          f"{abs(1 - mean_factor) * 100:.0f}% on average).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Fit an empirical, elevation-binned clear-sky correction "
            "for a station's solar radiation sensor."
        )
    )
    parser.add_argument("--db", required=True,
                        help="Path to the SQLite weather database")
    parser.add_argument("--station", default=AMBIENT_ID,
                        help="Station to calibrate (default: AMBIENT_STATION_ID)")
    parser.add_argument("--lat", type=float, default=None,
                        help="Latitude (default: WEATHER_LAT env var)")
    parser.add_argument("--lon", type=float, default=None,
                        help="Longitude (default: WEATHER_LON env var)")
    parser.add_argument("--bin-size", type=float, default=5.0,
                        help="Elevation bin width in degrees (default: 5)")
    parser.add_argument("--percentile", type=float, default=97.0,
                        help="Clearness-index percentile per bin, "
                             "approximating the clear-sky ceiling "
                             "(default: 97)")
    parser.add_argument("--min-samples", type=int, default=30,
                        help="Minimum readings required to keep a bin "
                             "(default: 30)")
    parser.add_argument("--min-elevation", type=float, default=3.0,
                        help="Lowest sun angle to include -- avoids "
                             "noisy near-horizon division (default: 3)")
    parser.add_argument("--start", type=str, default=None,
                        help="Only use readings on/after this date "
                             "(default: full history)")
    parser.add_argument("--end", type=str, default=None,
                        help="Only use readings on/before this date "
                             "(default: full history)")
    parser.add_argument("--method", choices=("clear-days", "percentile"), default="clear-days",
                        help="clear-days (default): the median clearness of clear, smooth days per elevation bin; "
                             "percentile: the original high percentile of all readings")
    parser.add_argument("--clear-kmax", type=float, default=0.6,
                        help="clear-days: a clear day's best clearness index is at least this (default 0.6)")
    parser.add_argument("--clear-kmean", type=float, default=0.5,
                        help="clear-days: a clear day's mean clearness index is at least this (default 0.5)")
    parser.add_argument("--clear-rough", type=float, default=0.04,
                        help="clear-days: a clear day's roughness (mean |second difference| / peak, at 15-minute steps) is below this (default 0.04)")
    parser.add_argument("--show-days", action="store_true",
                        help="clear-days: also print every day that was classified clear (date, mean clearness, roughness), grouped by month, "
                             "so the choice can be checked by eye against the plot")
    parser.add_argument("--refresh", action="store_true",
                        help="Recompute and overwrite any existing "
                             "calibration for this station. Without "
                             "this flag, an existing calibration is "
                             "left untouched and just printed.")

    args = parser.parse_args()

    lat, lon = args.lat, args.lon
    if lat is None or lon is None:
        env_lat, env_lon = env_lat_lon()
        lat = lat if lat is not None else env_lat
        lon = lon if lon is not None else env_lon
    if lat is None or lon is None:
        parser.error(
            "Latitude/longitude required: pass --lat/--lon or set "
            "WEATHER_LAT/WEATHER_LON."
        )

    start_dt = parse_date_arg(args.start) if args.start else None
    end_dt = parse_date_arg(args.end) if args.end else None

    conn = sqlite3.connect(args.db)
    try:
        init_db(conn)
        cursor = conn.cursor()

        existing = cursor.execute(
            "SELECT COUNT(*) FROM clearsky_calibration WHERE station = ?",
            (args.station,),
        ).fetchone()[0]

        if existing and not args.refresh:
            print_existing_calibration(cursor, args.station)
            return

        results = fit_calibration(
            conn, args.station, lat, lon, args.bin_size, args.percentile,
            args.min_samples, args.min_elevation, start_dt, end_dt,
            method=args.method, clear_kmax=args.clear_kmax, clear_kmean=args.clear_kmean, clear_rough=args.clear_rough,
            show_days=args.show_days,
        )
        if results is None:
            sys.exit(1)

        write_calibration(conn, args.station, results, 50.0 if args.method == "clear-days" else args.percentile)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
