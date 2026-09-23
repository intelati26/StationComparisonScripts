#!/usr/bin/env python3
"""
station_compare.py -- Compare stations against a reference station, or
against gridded analysis (URMA / RTMA / HRRR f00 / gladstone) at each station.

For each comparison, draws:
  * a connected scatter: reference on x, station on y, points joined in time
    order (line breaks at data gaps), colored by local hour or date,
    with a 1:1 line and a Deming (orthogonal) fit
  * a Bland-Altman panel: difference (station - reference) vs. mean of the
    pair, with mean bias and 95% limits of agreement (bias +/- 1.96 SD)
and prints n / bias / MAE / RMSE / r / Deming slope+intercept / LoA.

Station mode: the FIRST station in --station is the reference unless --ref.
Analysis mode (--analysis): every station is compared to the analysis value
at that station; one figure per analysis source.

Figures are written to ./results/ by default (--out-dir / --out to change).

Examples:
  python station_compare.py --station KSGF,AMBIENT --start 2026-09-01 --end 2026-09-07
  python station_compare.py --station KSGF,AMBIENT,KBBG --analysis urma,rtma \\
      --start 2026-09-01 --end 2026-09-07
  python station_compare.py --station KSGF,AMBIENT,KBBG,WU:KMOJOPLI124 \\
      --start 2026-06-01 --end 2026-08-31 --mode daily --daily-stat max
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

# Column auto-detection candidates (first one that exists AND is populated wins)
STATION_CANDIDATES = ["station", "station_id", "stid"]
TIME_CANDIDATES = ["obs_time", "valid_time", "valid", "timestamp", "time", "datetime", "ts"]
VALUE_CANDIDATES = ["temp_f", "tempf", "tmpf", "t2m_f", "tmp_f", "temp", "temperature", "t2m", "tmp_2m",
                    "temp_c", "t2m_c", "tmp_c", "temp_k", "t2m_k", "tmp_k"]
MODEL_CANDIDATES = ["model", "source", "model_name"]
LEAD_CANDIDATES = ["lead", "lead_hour", "fxx", "forecast_hour", "fhour"]


# ----------------------------------------------------------------------------
# Database helpers
# ----------------------------------------------------------------------------
def connect_ro(db_path):
    p = Path(db_path)
    if not p.is_file():
        sys.exit(f"[error] database not found: {p}  (refusing to let sqlite create an empty one)")
    con = sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def list_tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def table_columns(con, table, required=True):
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    if not cols and required:
        sys.exit(f"[error] table '{table}' not found. Tables: {', '.join(list_tables(con))}")
    return cols


def pick_column(con, table, cols, explicit, candidates, role, where="", params=(), required=True):
    """Use the explicit column if given; otherwise the first candidate that
    exists and actually has non-null rows (schema presence isn't enough)."""
    if explicit:
        if explicit not in cols:
            sys.exit(f"[error] --{role}-col '{explicit}' not in {table}. Columns: {', '.join(cols)}")
        return explicit
    for c in candidates:
        if c in cols:
            n = con.execute(f'SELECT COUNT("{c}") FROM "{table}" {where}', params).fetchone()[0]
            if n > 0:
                return c
            print(f"[schema] {table}: {role} candidate '{c}' exists but has 0 non-null rows -- skipping")
    if not required:
        return None
    sys.exit(f"[error] could not auto-detect the {role} column in {table}. "
             f"Pass --{role}-col. Columns: {', '.join(cols)}")


def detect_time_storage(con, table, tcol):
    row = con.execute(f'SELECT typeof("{tcol}"), "{tcol}" FROM "{table}" '
                      f'WHERE "{tcol}" IS NOT NULL LIMIT 1').fetchone()
    if row is None:
        sys.exit(f"[error] {table}.{tcol} has no data")
    kind, sample = row
    if kind in ("integer", "real"):
        return ("epoch_ms" if float(sample) > 1e11 else "epoch_s"), sample
    return "text", sample


def parse_times(series, storage, db_tz):
    if storage == "epoch_s":
        return pd.to_datetime(series, unit="s", utc=True)
    if storage == "epoch_ms":
        return pd.to_datetime(series, unit="ms", utc=True)
    # Different collectors may write different formats into the same column
    # ('2026-09-01 12:53:00', '2026-09-01T12:55:00Z', '...+00:00'), so parse
    # offset-bearing and naive strings separately.
    s = series.astype(str).str.strip()
    has_offset = s.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns, UTC]")
    if has_offset.any():
        out[has_offset] = pd.to_datetime(s[has_offset], errors="coerce", utc=True, format="mixed")
    if (~has_offset).any():
        naive = pd.to_datetime(s[~has_offset], errors="coerce", format="mixed")
        out[~has_offset] = (naive.dt.tz_localize(db_tz, ambiguous="NaT", nonexistent="NaT")
                                 .dt.tz_convert("UTC"))
    return out


def load_rows(con, table, tcol, vcol, key_cols, where_sql, where_params,
              start_local, end_local, db_tz, tz):
    """Load [key cols..., time, v] from one table for the local-time window.
    key_cols: {alias: column} selected alongside time/value (e.g. station, model)."""
    storage, sample = detect_time_storage(con, table, tcol)
    keys_txt = " ".join(f"{a}='{c}'" for a, c in key_cols.items())
    print(f"[schema] {table}: {keys_txt} time='{tcol}' ({storage}, e.g. {sample!r}) value='{vcol}'")

    # Pad the query window by a day on each side, then filter exactly in local time
    lo_utc = (start_local - pd.Timedelta(days=1)).tz_convert("UTC")
    hi_utc = (end_local + pd.Timedelta(days=1)).tz_convert("UTC")
    if storage == "text":
        lo, hi = lo_utc.strftime("%Y-%m-%d"), hi_utc.strftime("%Y-%m-%d")
    else:
        scale = 1000 if storage == "epoch_ms" else 1
        lo, hi = int(lo_utc.timestamp()) * scale, int(hi_utc.timestamp()) * scale

    sel = "".join(f'"{c}" AS "{a}", ' for a, c in key_cols.items())
    conds = ([where_sql] if where_sql else []) + [
        f'"{tcol}" >= ?', f'"{tcol}" < ?', f'"{vcol}" IS NOT NULL']
    q = f'SELECT {sel}"{tcol}" AS t, "{vcol}" AS v FROM "{table}" WHERE ' + " AND ".join(conds)
    df = pd.read_sql_query(q, con, params=[*where_params, lo, hi])

    df["time"] = parse_times(df["t"], storage, db_tz).dt.tz_convert(tz)
    df["v"] = pd.to_numeric(df["v"], errors="coerce")
    bad_t = df["time"].isna().sum()
    if bad_t:
        print(f"[warn] {table}: {bad_t} rows had unparseable timestamps -- dropped")
    df = df.dropna(subset=["time", "v"])
    df = df[(df["time"] >= start_local) & (df["time"] < end_local)]
    try:
        df["time"] = df["time"].dt.as_unit("ns")
    except AttributeError:
        pass
    # Collapse exact duplicate timestamps per key
    return df.groupby([*key_cols, "time"], as_index=False)["v"].mean()


# ----------------------------------------------------------------------------
# Analysis (gridded) source
# ----------------------------------------------------------------------------
def find_analysis_table(con, explicit, explicit_mcol, analyses):
    """Find the table holding the requested analysis rows (model column match)."""
    tables = [explicit] if explicit else list_tables(con)
    seen = {}
    for t in tables:
        cols = table_columns(con, t, required=bool(explicit))
        mcol = explicit_mcol if explicit_mcol in cols else next(
            (c for c in MODEL_CANDIDATES if c in cols), None)
        if not mcol:
            continue
        ph = ",".join("?" * len(analyses))
        n = con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE LOWER("{mcol}") IN ({ph})',
                        analyses).fetchone()[0]
        if n > 0:
            return t, mcol, cols
        seen[t] = [r[0] for r in con.execute(
            f'SELECT DISTINCT "{mcol}" FROM "{t}" ORDER BY 1 LIMIT 30')]
    detail = "; ".join(f"{t}: {', '.join(map(str, m))}" for t, m in seen.items()) or "none found"
    sys.exit(f"[error] no rows for --analysis {','.join(analyses)}. "
             f"Model values by table -> {detail}")


def analysis_units(vcol, values, forced):
    """Return the unit the analysis column is stored in (F, C or K)."""
    if forced != "auto":
        return forced
    name = vcol.lower()
    for suf, u in (("_k", "K"), ("_c", "C"), ("_f", "F")):
        if name.endswith(suf):
            return u
    if any(k in name for k in ("temp", "tmp", "t2m")) and len(values) and np.nanmedian(values) > 200:
        return "K"
    return "F"


def to_f(v, unit):
    if unit == "K":
        return (v - 273.15) * 9 / 5 + 32
    if unit == "C":
        return v * 9 / 5 + 32
    return v


# ----------------------------------------------------------------------------
# Pairing
# ----------------------------------------------------------------------------
def daily_series(s, stat, min_hours):
    agg = s.resample("1D").agg(stat)
    hours = pd.Series(1, index=s.index.floor("h").unique())
    cov = hours.resample("1D").sum().reindex(agg.index, fill_value=0)
    return agg.where(cov >= min_hours)


def pair(ref_df, tst_df, mode, tol_min, daily_stat, min_hours):
    """Return DataFrame[time, ref, tst] of time-matched pairs."""
    if mode == "instant":
        r = ref_df[["time", "v"]].rename(columns={"v": "ref"}).sort_values("time")
        t = tst_df[["time", "v"]].rename(columns={"v": "tst"}).sort_values("time")
        p = pd.merge_asof(r, t, on="time", direction="nearest",
                          tolerance=pd.Timedelta(minutes=tol_min))
        return p.dropna().reset_index(drop=True)

    rs = ref_df.set_index("time")["v"].sort_index()
    ts = tst_df.set_index("time")["v"].sort_index()
    if mode == "hourly":
        rs, ts = rs.resample("1h").mean(), ts.resample("1h").mean()
    else:  # daily
        rs, ts = daily_series(rs, daily_stat, min_hours), daily_series(ts, daily_stat, min_hours)
    p = pd.concat({"ref": rs, "tst": ts}, axis=1, join="inner").dropna()
    return p.rename_axis("time").reset_index()


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------
def compare_stats(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    d = y - x
    n = len(d)
    out = dict(n=n, bias=np.nan, mae=np.nan, rmse=np.nan, sd=np.nan, r=np.nan,
               slope=np.nan, intercept=np.nan, loa_lo=np.nan, loa_hi=np.nan)
    if n == 0:
        return out
    out.update(bias=d.mean(), mae=np.abs(d).mean(), rmse=np.sqrt((d ** 2).mean()))
    if n >= 3:
        out["sd"] = d.std(ddof=1)
        out["loa_lo"] = out["bias"] - 1.96 * out["sd"]
        out["loa_hi"] = out["bias"] + 1.96 * out["sd"]
        if x.std() > 0 and y.std() > 0:
            out["r"] = np.corrcoef(x, y)[0, 1]
        # Deming regression, error-variance ratio = 1 (orthogonal regression):
        # both stations carry measurement error, so OLS would bias the slope.
        sxx, syy = np.var(x, ddof=1), np.var(y, ddof=1)
        sxy = np.cov(x, y, ddof=1)[0, 1]
        if sxy != 0:
            slope = (syy - sxx + np.sqrt((syy - sxx) ** 2 + 4 * sxy ** 2)) / (2 * sxy)
            out.update(slope=slope, intercept=y.mean() - slope * x.mean())
    return out


# ----------------------------------------------------------------------------
# Plotting
# ----------------------------------------------------------------------------
def color_values(times, color_by):
    import matplotlib.dates as mdates
    if color_by == "hour":
        return (times.dt.hour + times.dt.minute / 60.0).to_numpy(), "twilight", (0, 24)
    # Date coloring: plot in naive local wall time so tick labels read as local dates
    num = mdates.date2num(times.dt.tz_localize(None))
    return num, "viridis", (num.min(), num.max())


def add_colorbar(fig, sc, ax, color_by):
    import matplotlib.dates as mdates
    cb = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.03)
    if color_by == "hour":
        cb.set_ticks([0, 6, 12, 18, 24])
        cb.set_label("Local hour")
    else:
        cb.ax.yaxis.set_major_locator(mdates.AutoDateLocator())
        cb.ax.yaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        cb.set_label("Date")


def connected_segments(p, gap):
    """Yield contiguous runs so the connecting line doesn't jump across gaps."""
    seg_id = (p["time"].diff() > gap).cumsum()
    for _, seg in p.groupby(seg_id):
        if len(seg) > 1:
            yield seg


def plot_row(fig, ax_sc, ax_ba, p, st, ref, tst, args, gap, connect):
    x, y = p["ref"].to_numpy(), p["tst"].to_numpy()
    c, cmap, (vmin, vmax) = color_values(p["time"], args.color_by)

    # --- connected scatter ---
    if connect:
        for seg in connected_segments(p, gap):
            ax_sc.plot(seg["ref"], seg["tst"], "-", lw=0.6, color="0.65", zorder=1)
    sc = ax_sc.scatter(x, y, c=c, cmap=cmap, vmin=vmin, vmax=vmax, s=14,
                       edgecolor="none", zorder=2)
    lo, hi = min(x.min(), y.min()), max(x.max(), y.max())
    pad = max((hi - lo) * 0.04, 0.5)
    lim = (lo - pad, hi + pad)
    ax_sc.set_xlim(lim)
    ax_sc.set_ylim(lim)
    ax_sc.set_aspect("equal", adjustable="box")
    ax_sc.axline((lim[0], lim[0]), slope=1, color="k", ls="--", lw=0.8, label="1:1")
    if np.isfinite(st["slope"]):
        ax_sc.axline((0, st["intercept"]), slope=st["slope"], color="tab:red", lw=1.0,
                     label=f"Deming  y = {st['slope']:.3f}x {st['intercept']:+.2f}")
    ax_sc.set_xlabel(f"{ref}  {args.label}")
    ax_sc.set_ylabel(f"{tst}  {args.label}")
    ax_sc.grid(alpha=0.3)
    ax_sc.legend(loc="lower right", fontsize=8, framealpha=0.85)
    txt = (f"n = {st['n']:,}\nbias = {st['bias']:+.2f}\nMAE = {st['mae']:.2f}\n"
           f"RMSE = {st['rmse']:.2f}\nr = {st['r']:.3f}")
    ax_sc.text(0.03, 0.97, txt, transform=ax_sc.transAxes, va="top", ha="left",
               fontsize=8.5, family="monospace",
               bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.9))
    add_colorbar(fig, sc, ax_sc, args.color_by)

    # --- Bland-Altman ---
    m, d = (x + y) / 2.0, y - x
    sc2 = ax_ba.scatter(m, d, c=c, cmap=cmap, vmin=vmin, vmax=vmax, s=14, edgecolor="none")
    ax_ba.axhline(0, color="0.5", lw=0.8)
    ax_ba.axhline(st["bias"], color="tab:red", lw=1.2, label=f"bias {st['bias']:+.2f}")
    if np.isfinite(st["loa_lo"]):
        for v in (st["loa_lo"], st["loa_hi"]):
            ax_ba.axhline(v, color="tab:red", ls="--", lw=0.9)
        ax_ba.axhspan(st["loa_lo"], st["loa_hi"], color="tab:red", alpha=0.06,
                      label=f"95% LoA [{st['loa_lo']:+.2f}, {st['loa_hi']:+.2f}]")
    ax_ba.set_xlabel(f"Mean of {tst} & {ref}  {args.label}")
    ax_ba.set_ylabel(f"{tst} − {ref}")
    ax_ba.grid(alpha=0.3)
    ax_ba.legend(loc="upper right", fontsize=8, framealpha=0.85)
    add_colorbar(fig, sc2, ax_ba, args.color_by)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        description="Connected scatter + Bland-Altman comparison of stations vs. a reference "
                    "station or vs. gridded analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--station", required=True,
                    help="Comma-separated stations, e.g. KSGF,AMBIENT,WU:KMOJOPLI124. "
                         "Station mode: first one is the reference unless --ref.")
    ap.add_argument("--ref", help="Reference station (station mode only; must be in --station)")
    ap.add_argument("--analysis",
                    help="Comma-separated analysis sources to use as the reference instead of a "
                         "station, e.g. urma or urma,rtma,gladstone (matched case-insensitively "
                         "against the model column)")
    ap.add_argument("--start", required=True, help="Start date (local), YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="End date (local, inclusive), YYYY-MM-DD")
    ap.add_argument("--db", default="weather_archive.db", help="SQLite database path")

    g = ap.add_argument_group("station table")
    g.add_argument("--table", default="station_obs")
    g.add_argument("--station-col", help="Station column (auto-detected if omitted)")
    g.add_argument("--time-col", help="Time column (auto-detected if omitted)")
    g.add_argument("--value-col", help="Value column (auto-detected temperature if omitted)")

    g = ap.add_argument_group("analysis table (auto-detected if omitted)")
    g.add_argument("--analysis-table", help="Table holding analysis rows")
    g.add_argument("--analysis-model-col", help="Model/source column")
    g.add_argument("--analysis-station-col", help="Station column (point sampled per station)")
    g.add_argument("--analysis-time-col", help="Valid-time column")
    g.add_argument("--analysis-value-col", help="Value column")
    g.add_argument("--analysis-lead-col", help="Forecast lead column, if the table has one")
    g.add_argument("--analysis-lead", default="0",
                   help="Lead hour to keep when a lead column exists (default 0 = analysis; "
                        "rows with NULL lead are always kept). 'any' disables the filter.")
    g.add_argument("--analysis-units", choices=["auto", "F", "C", "K"], default="auto",
                   help="Units of the analysis value column (auto: column-name suffix, "
                        "or Kelvin if a temperature column's median > 200; else F)")

    g = ap.add_argument_group("pairing / plotting")
    g.add_argument("--label", default="Temperature (°F)", help="Axis label for the value")
    g.add_argument("--tz", default="America/Chicago", help="Local timezone for days/hours")
    g.add_argument("--db-tz", default="UTC", help="Zone of naive timestamps stored in the DB")
    g.add_argument("--mode", choices=["instant", "hourly", "daily"], default="instant",
                   help="instant: nearest-obs match to reference times (default); "
                        "hourly: hourly means; daily: local-day aggregate")
    g.add_argument("--tol-min", type=float,
                   help="instant mode: max minutes between paired obs "
                        "(default 5 vs a station, 10 vs analysis so :53 ASOS obs reach :00 grids)")
    g.add_argument("--daily-stat", choices=["max", "min", "mean"], default="mean")
    g.add_argument("--min-hours", type=int, default=20,
                   help="daily mode: min distinct hours with data for a day to count (default 20)")
    g.add_argument("--color-by", choices=["auto", "hour", "date"], default="auto",
                   help="auto = hour for instant/hourly, date for daily")
    g.add_argument("--connect", choices=["auto", "always", "never"], default="auto",
                   help="auto connects when a panel has <= --connect-max points")
    g.add_argument("--connect-max", type=int, default=400)

    g = ap.add_argument_group("output")
    g.add_argument("--out-dir", default="results",
                   help="Directory for figures (default ./results, created if missing)")
    g.add_argument("--out", help="Exact output PNG path (overrides --out-dir); with several "
                                 "analyses, the analysis name is appended to the stem")
    g.add_argument("--dpi", type=int, default=130)
    g.add_argument("--show", action="store_true", help="Show interactively as well as saving")
    return ap


def split_list(s):
    return list(dict.fromkeys(x.strip() for x in s.split(",") if x.strip()))


def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s)


def run_group(plt, args, group_name, ref_name, comparisons, mode_txt, out_path):
    """comparisons: list of (ref_label, tst_label, ref_df, tst_df). One figure."""
    results = []
    for ref_label, tst_label, ref_df, tst_df in comparisons:
        p = pair(ref_df, tst_df, args.mode, args.tol_min, args.daily_stat, args.min_hours)
        if len(p) < 3:
            print(f"[warn] {tst_label}: only {len(p)} matched pairs with {ref_label} -- skipped")
            continue
        results.append((ref_label, tst_label, p, compare_stats(p["ref"], p["tst"])))
    if not results:
        print(f"[warn] {group_name}: no comparison had enough matched pairs "
              f"(try a wider --tol-min, --mode hourly, or a different window)")
        return False

    stat_note = f" ({args.daily_stat})" if args.mode == "daily" else ""
    print(f"\nReference: {ref_name}   mode: {args.mode}{stat_note}")
    hdr = (f"{'station':<18}{'vs':<14}{'n':>7}{'bias':>8}{'MAE':>7}{'RMSE':>7}{'r':>7}"
           f"{'slope':>8}{'icpt':>8}{'LoA 95%':>18}")
    print(hdr)
    print("-" * len(hdr))
    for ref_label, tst_label, _, st in results:
        loa = f"[{st['loa_lo']:+.2f},{st['loa_hi']:+.2f}]"
        print(f"{tst_label:<18}{ref_label:<14}{st['n']:>7,}{st['bias']:>+8.2f}{st['mae']:>7.2f}"
              f"{st['rmse']:>7.2f}{st['r']:>7.3f}{st['slope']:>8.3f}{st['intercept']:>+8.2f}{loa:>18}")

    gap_base = pd.Timedelta(days=1) if args.mode == "daily" else pd.Timedelta(hours=1)
    nrows = len(results)
    fig, axes = plt.subplots(nrows, 2, figsize=(13, 5.6 * nrows), squeeze=False)
    for i, (ref_label, tst_label, p, st) in enumerate(results):
        step = p["time"].diff().median()
        gap = max(gap_base, step) * 2.5 if pd.notna(step) else gap_base * 2.5
        connect = (args.connect == "always" or
                   (args.connect == "auto" and len(p) <= args.connect_max))
        plot_row(fig, axes[i, 0], axes[i, 1], p, st, ref_label, tst_label, args, gap, connect)
        axes[i, 0].set_title(f"{tst_label} vs {ref_label}" +
                             ("" if connect else "  (unconnected: many points)"),
                             fontsize=10, loc="left")
        axes[i, 1].set_title("Bland-Altman", fontsize=10, loc="left")

    fig.suptitle(f"{args.label} — reference {ref_name} — {args.start} to {args.end} ({mode_txt})",
                 fontsize=12, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.985))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=args.dpi)
    print(f"[saved] {out_path.resolve()}")
    if not args.show:
        plt.close(fig)
    return True


def main():
    args = build_parser().parse_args()
    if not args.show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stations = split_list(args.station)
    analyses = [a.lower() for a in split_list(args.analysis)] if args.analysis else []
    if analyses:
        if args.ref:
            sys.exit("[error] --ref is for station mode; with --analysis the analysis is the reference")
        if not stations:
            sys.exit("[error] --station needs at least one station")
    elif len(stations) < 2:
        sys.exit("[error] --station needs at least two stations (e.g. --station KSGF,AMBIENT), "
                 "or one or more stations plus --analysis")
    ref = None
    if not analyses:
        ref = args.ref or stations[0]
        if ref not in stations:
            sys.exit(f"[error] --ref {ref} is not in --station {','.join(stations)}")
    if args.tol_min is None:
        args.tol_min = 10.0 if analyses else 5.0
    if args.color_by == "auto":
        args.color_by = "date" if args.mode == "daily" else "hour"

    try:
        start_local = pd.Timestamp(args.start).tz_localize(args.tz)
        end_local = pd.Timestamp(args.end).tz_localize(args.tz) + pd.Timedelta(days=1)
    except Exception as e:
        sys.exit(f"[error] bad --start/--end/--tz: {e}")
    if end_local <= start_local:
        sys.exit("[error] --end must be on or after --start")

    con = connect_ro(args.db)

    # ---- stations ----
    cols = table_columns(con, args.table)
    scol = pick_column(con, args.table, cols, args.station_col, STATION_CANDIDATES, "station")
    ph = ",".join("?" * len(stations))
    where = f'WHERE "{scol}" IN ({ph})'
    tcol = pick_column(con, args.table, cols, args.time_col, TIME_CANDIDATES, "time", where, stations)
    vcol = pick_column(con, args.table, cols, args.value_col, VALUE_CANDIDATES, "value", where, stations)
    obs = load_rows(con, args.table, tcol, vcol, {"station": scol}, f'"{scol}" IN ({ph})',
                    stations, start_local, end_local, args.db_tz, args.tz)
    counts = obs["station"].value_counts()
    for s in stations:
        print(f"[load] {s:<18} {counts.get(s, 0):>8,} obs  ({args.start} .. {args.end}, {args.tz})")
    missing = [s for s in stations if counts.get(s, 0) == 0]
    if missing:
        known = [r[0] for r in con.execute(
            f'SELECT DISTINCT "{scol}" FROM "{args.table}" ORDER BY 1 LIMIT 60')]
        sys.exit(f"[error] no '{vcol}' data in window for: {', '.join(missing)}\n"
                 f"        stations in table: {', '.join(map(str, known))}")

    mode_txt = {"instant": f"instantaneous pairs, ±{args.tol_min:g} min",
                "hourly": "hourly means",
                "daily": f"daily {args.daily_stat}, ≥{args.min_hours} h coverage"}[args.mode]
    out_dir = Path(args.out_dir)

    def out_for(name, n_groups):
        if args.out:
            o = Path(args.out)
            return o if n_groups == 1 else o.with_name(f"{o.stem}_{safe(name)}{o.suffix or '.png'}")
        return out_dir / f"station_compare_{safe(name)}_{args.start}_{args.end}_{args.mode}.png"

    # ---- station mode ----
    if not analyses:
        con.close()
        ref_df = obs[obs["station"] == ref]
        comps = [(ref, s, ref_df, obs[obs["station"] == s]) for s in stations if s != ref]
        ok = run_group(plt, args, ref, ref, comps, mode_txt, out_for(ref, 1))
        if not ok:
            sys.exit(1)
        if args.show:
            plt.show()
        return

    # ---- analysis mode ----
    atable, mcol, acols = find_analysis_table(con, args.analysis_table,
                                              args.analysis_model_col, analyses)
    ph_a = ",".join("?" * len(analyses))
    a_where = f'WHERE LOWER("{mcol}") IN ({ph_a})'
    ascol = pick_column(con, atable, acols, args.analysis_station_col, STATION_CANDIDATES,
                        "analysis-station", a_where, analyses, required=False)
    atcol = pick_column(con, atable, acols, args.analysis_time_col, TIME_CANDIDATES,
                        "analysis-time", a_where, analyses)
    avcol = pick_column(con, atable, acols, args.analysis_value_col, VALUE_CANDIDATES,
                        "analysis-value", a_where, analyses)
    alcol = pick_column(con, atable, acols, args.analysis_lead_col, LEAD_CANDIDATES,
                        "analysis-lead", a_where, analyses, required=False)

    conds, params = [f'LOWER("{mcol}") IN ({ph_a})'], list(analyses)
    if ascol:
        conds.append(f'"{ascol}" IN ({ph})')
        params += stations
    if alcol and args.analysis_lead.lower() != "any":
        conds.append(f'("{alcol}" = ? OR "{alcol}" IS NULL)')
        params.append(float(args.analysis_lead))
        print(f"[schema] {atable}: keeping {alcol} = {args.analysis_lead} (or NULL) only")
    keys = {"model": mcol, **({"station": ascol} if ascol else {})}
    ana = load_rows(con, atable, atcol, avcol, keys, " AND ".join(conds), params,
                    start_local, end_local, args.db_tz, args.tz)
    con.close()
    ana["model"] = ana["model"].astype(str).str.lower()

    if not ascol:
        print(f"[warn] {atable} has no station column: each analysis is treated as ONE grid point "
              f"and compared against every station. That's only meaningful for the station "
              f"co-located with that point.")

    groups = 0
    for m in analyses:
        a_m = ana[ana["model"] == m].copy()
        if a_m.empty:
            print(f"[warn] {m}: no rows in window -- skipped")
            continue
        unit = analysis_units(avcol, a_m["v"].to_numpy(), args.analysis_units)
        a_m["v"] = to_f(a_m["v"], unit)
        print(f"[units] {m}: {avcol} treated as {unit}"
              f"{' -> converted to F' if unit != 'F' else ''} "
              f"(median after conversion {a_m['v'].median():.1f})")

        M = m.upper()
        comps = []
        for s in stations:
            if ascol:
                ref_df = a_m[a_m["station"] == s]
                if ref_df.empty:
                    have = ", ".join(sorted(a_m["station"].astype(str).unique())) or "none"
                    print(f"[warn] {M}: no values at {s} in window (has: {have}) -- skipped")
                    continue
                comps.append((f"{M}@{s}", s, ref_df, obs[obs["station"] == s]))
            else:
                comps.append((M, s, a_m, obs[obs["station"] == s]))
        if comps and run_group(plt, args, M, M, comps, mode_txt, out_for(m, len(analyses))):
            groups += 1
    if not groups:
        sys.exit(1)
    if args.show:
        plt.show()


if __name__ == "__main__":
    main()
