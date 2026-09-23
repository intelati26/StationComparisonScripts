#!/usr/bin/env python3
"""
pair_matrix.py -- Every pairwise combination, C(N,2), of stations and analysis.

Sources = the stations in --station, plus each --analysis sampled at one
station location (--analysis-at; default the first station) or at its single
grid point if the analysis table has no station column.

Outputs (in ./results/ by default; --only to pick a subset):
  scatter  N x N matrix. Lower triangle: connected scatter, row vs. column,
           1:1 line. Upper triangle: Bland-Altman, row - column. Diagonal:
           each source's distribution.
  summary  Heatmaps of bias (row - column), RMSE and pair count, plus the
           N-cornered-hat estimate of each source's OWN random error (sigma),
           solved by least squares from all pairwise difference variances.
  lag      Best time lag per pair from cross-correlation of first differences
           (detects clock drift / UTC-vs-local mistakes) and its correlation.
  diurnal  Each source minus the leave-one-out median of the others, by hour.
  drift    Same difference as a daily series with a rolling mean (--drift-window).
  CSV      *_pairs.csv (all pair stats + lag) and *_sources.csv (hat sigma).

Requires station_compare.py in the same directory (shared loaders/stats).

Examples:
  python pair_matrix.py --station KSGF,KBBG,AMBIENT,WU:KMOJOPLI124 --start 2026-08-01 --end 2026-09-15
  python pair_matrix.py --station KSGF,KBBG,AMBIENT --analysis urma,rtma,hrrr \\
      --analysis-at all --start 2026-08-01 --end 2026-09-15
"""
import argparse
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

try:
    import station_compare as sc
except ImportError:
    sys.exit("[error] pair_matrix.py needs station_compare.py in the same directory")

OUTPUTS = ["scatter", "summary", "lag", "diurnal", "drift"]


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def load_all(args, stations, analyses, start_local, end_local):
    """Return ({station: Series}, {(model, loc|None): Series}, has_station_col)."""
    con = sc.connect_ro(args.db)
    cols = sc.table_columns(con, args.table)
    scol = sc.pick_column(con, args.table, cols, args.station_col, sc.STATION_CANDIDATES, "station")
    ph = ",".join("?" * len(stations))
    where = f'WHERE "{scol}" IN ({ph})'
    tcol = sc.pick_column(con, args.table, cols, args.time_col, sc.TIME_CANDIDATES, "time", where, stations)
    vcol = sc.pick_column(con, args.table, cols, args.value_col, sc.VALUE_CANDIDATES, "value", where, stations)
    obs = sc.load_rows(con, args.table, tcol, vcol, {"station": scol}, f'"{scol}" IN ({ph})',
                       stations, start_local, end_local, args.db_tz, args.tz)
    counts = obs["station"].value_counts()
    for s in stations:
        print(f"[load] {s:<18} {counts.get(s, 0):>8,} obs")
    missing = [s for s in stations if counts.get(s, 0) == 0]
    if missing:
        known = [r[0] for r in con.execute(
            f'SELECT DISTINCT "{scol}" FROM "{args.table}" ORDER BY 1 LIMIT 60')]
        sys.exit(f"[error] no '{vcol}' data in window for: {', '.join(missing)}\n"
                 f"        stations in table: {', '.join(map(str, known))}")
    st = {s: obs[obs["station"] == s].set_index("time")["v"].sort_index() for s in stations}

    ana, has_scol = {}, False
    if analyses:
        atable, mcol, acols = sc.find_analysis_table(con, args.analysis_table,
                                                     args.analysis_model_col, analyses)
        ph_a = ",".join("?" * len(analyses))
        a_where = f'WHERE LOWER("{mcol}") IN ({ph_a})'
        ascol = sc.pick_column(con, atable, acols, args.analysis_station_col, sc.STATION_CANDIDATES,
                               "analysis-station", a_where, analyses, required=False)
        atcol = sc.pick_column(con, atable, acols, args.analysis_time_col, sc.TIME_CANDIDATES,
                               "analysis-time", a_where, analyses)
        avcol = sc.pick_column(con, atable, acols, args.analysis_value_col, sc.VALUE_CANDIDATES,
                               "analysis-value", a_where, analyses)
        alcol = sc.pick_column(con, atable, acols, args.analysis_lead_col, sc.LEAD_CANDIDATES,
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
        a = sc.load_rows(con, atable, atcol, avcol, keys, " AND ".join(conds), params,
                         start_local, end_local, args.db_tz, args.tz)
        a["model"] = a["model"].astype(str).str.lower()
        has_scol = bool(ascol)
        for m in analyses:
            a_m = a[a["model"] == m].copy()
            if a_m.empty:
                print(f"[warn] {m}: no rows in window -- skipped")
                continue
            unit = sc.analysis_units(avcol, a_m["v"].to_numpy(), args.analysis_units)
            a_m["v"] = sc.to_f(a_m["v"], unit)
            print(f"[units] {m}: {avcol} treated as {unit}"
                  f"{' -> F' if unit != 'F' else ''} (median {a_m['v'].median():.1f})")
            if has_scol:
                for loc, g in a_m.groupby("station"):
                    ana[(m, str(loc))] = g.set_index("time")["v"].sort_index()
            else:
                ana[(m, None)] = a_m.set_index("time")["v"].sort_index()
    con.close()
    return st, ana, has_scol


def build_runs(args, stations, analyses, st, ana, has_scol):
    """Each run: (tag, {label: Series}, set_of_analysis_labels)."""
    if not analyses:
        return [("stations", dict(st), set())]
    if not has_scol:
        print("[warn] analysis table has no station column: each analysis is ONE grid point, "
              "compared with every station (only co-located pairs measure analysis error).")
        srcs, al = dict(st), set()
        for m in analyses:
            if (m, None) in ana:
                srcs[m.upper()] = ana[(m, None)]
                al.add(m.upper())
        return [("analysis-point", srcs, al)]
    at = args.analysis_at or stations[0]
    locs = stations if at.lower() == "all" else sc.split_list(at)
    bad = [l for l in locs if l not in stations]
    if bad:
        sys.exit(f"[error] --analysis-at {','.join(bad)} not in --station")
    runs = []
    for loc in locs:
        srcs, al = dict(st), set()
        for m in analyses:
            key = (m, loc)
            if key in ana:
                lab = f"{m.upper()}@{loc}"
                srcs[lab] = ana[key]
                al.add(lab)
            else:
                print(f"[warn] {m.upper()}: no values at {loc} -- left out of the {loc} run")
        if not al:
            print(f"[warn] no analysis values at {loc} -- run skipped")
            continue
        runs.append((f"at-{sc.safe(loc)}", srcs, al))
    return runs


# ----------------------------------------------------------------------------
# Alignment
# ----------------------------------------------------------------------------
def build_wide(sources, args, start_local, end_local):
    """Align all sources on one time axis. Returns (wide DataFrame, note)."""
    if args.mode == "instant":
        gm = args.grid_minute
        if gm is None:  # anchor the grid on the sparsest source's usual minute (ASOS -> :53)
            sparsest = min(sources, key=lambda k: len(sources[k]))
            mins = pd.Series(sources[sparsest].index.minute)
            gm = int(mins.value_counts().idxmax()) if len(mins) else 0
        grid = pd.date_range(start_local.floor("h") - pd.Timedelta(hours=1), end_local,
                             freq="1h") + pd.Timedelta(minutes=gm)
        grid = grid[(grid >= start_local) & (grid < end_local)]
        base = pd.DataFrame({"time": grid})
        try:
            base["time"] = base["time"].dt.as_unit("ns")
        except AttributeError:
            pass
        out = {}
        for k, s in sources.items():
            d = s.rename("v").rename_axis("time").reset_index()
            m = pd.merge_asof(base, d, on="time", direction="nearest",
                              tolerance=pd.Timedelta(minutes=args.tol_min))
            out[k] = m["v"].to_numpy()
        wide = pd.DataFrame(out, index=grid)
        note = f"hourly grid at :{gm:02d}, nearest obs within ±{args.tol_min:g} min"
    elif args.mode == "hourly":
        wide = pd.concat({k: s.resample("1h").mean() for k, s in sources.items()}, axis=1)
        note = "hourly means"
    else:
        wide = pd.concat({k: sc.daily_series(s, args.daily_stat, args.min_hours)
                          for k, s in sources.items()}, axis=1)
        note = f"daily {args.daily_stat}, ≥{args.min_hours} h coverage"
    wide = wide[wide.notna().sum(axis=1) >= 2]
    if args.pairs == "common":
        wide = wide.dropna()
        note += "; common times only"
    else:
        note += "; pairwise overlap"
    return wide, note


def hourly_table(sources):
    return pd.concat({k: s.resample("1h").mean() for k, s in sources.items()}, axis=1)


# ----------------------------------------------------------------------------
# Analyses
# ----------------------------------------------------------------------------
def pair_stats(wide, labels):
    """stats[(i, j)] for i < j: y = row i, x = column j (bias = row - column)."""
    stats, subs = {}, {}
    for i, j in combinations(range(len(labels)), 2):
        a, b = labels[i], labels[j]
        sub = wide[[a, b]].dropna()
        subs[(i, j)] = sub
        stats[(i, j)] = sc.compare_stats(sub[b], sub[a])
    return stats, subs


def n_cornered_hat(wide, labs):
    """Least-squares solve var(Xi - Xj) = s_i + s_j over all pairs.
    Returns {label: (sigma, raw_variance)} or None if underdetermined."""
    if len(labs) < 3:
        return None
    A, rhs = [], []
    for p, q in combinations(range(len(labs)), 2):
        d = (wide[labs[p]] - wide[labs[q]]).dropna()
        if len(d) < 10:
            continue
        row = np.zeros(len(labs))
        row[[p, q]] = 1
        A.append(row)
        rhs.append(d.var(ddof=1))
    if not A:
        return None
    A = np.array(A)
    if np.linalg.matrix_rank(A) < len(labs):
        return None
    s, *_ = np.linalg.lstsq(A, np.array(rhs), rcond=None)
    return {lab: (np.sqrt(max(v, 0.0)), v) for lab, v in zip(labs, s)}


def lag_matrix(sources, labels, max_lag, step):
    """Best lag (minutes) of row vs column: positive = row LAGS column."""
    fine = {}
    for k in labels:
        r = sources[k].resample(f"{step}min").mean()
        r = r.interpolate(method="time", limit=int(np.ceil(75 / step)), limit_area="inside")
        # 1-hour change (not per-step): hourly ASOS/analysis are linear between obs,
        # so per-step differences are mostly noise; hourly change carries the signal
        fine[k] = r.diff(max(1, int(round(60 / step))))
    K = int(max_lag // step)
    N = len(labels)
    L, C = np.full((N, N), np.nan), np.full((N, N), np.nan)
    C0 = np.full((N, N), np.nan)
    for i, j in combinations(range(N), 2):
        x, y = fine[labels[i]], fine[labels[j]]
        best_k, best_c = None, -np.inf
        for k in range(-K, K + 1):
            pr = pd.concat([x, y.shift(k)], axis=1, sort=True).dropna()
            if len(pr) < 50:
                continue
            c = pr.iloc[:, 0].corr(pr.iloc[:, 1])
            if k == 0:
                C0[i, j] = C0[j, i] = c
            if np.isfinite(c) and c > best_c:
                best_k, best_c = k, c
        if best_k is not None:
            L[i, j], L[j, i] = best_k * step, -best_k * step
            C[i, j] = C[j, i] = best_c
    return L, C, C0


def loo_differences(H, labels):
    """Each source minus the median of the OTHER sources at that hour."""
    min_others = 1 if len(labels) == 2 else 2
    out = {}
    for c in labels:
        others = H.drop(columns=c)
        cons = others.median(axis=1).where(others.notna().sum(axis=1) >= min_others)
        out[c] = (H[c] - cons).dropna()
    return out


# ----------------------------------------------------------------------------
# Plotting
# ----------------------------------------------------------------------------
def src_colors(labels):
    import matplotlib.pyplot as plt
    cyc = plt.get_cmap("tab10")
    return {lab: cyc(i % 10) for i, lab in enumerate(labels)}


def heatmap(fig, ax, M, labels, title, cmap_name, vmin, vmax, fmt, cbar_label):
    import matplotlib.pyplot as plt
    cmap = plt.get_cmap(cmap_name).copy()
    cmap.set_bad("0.92")
    im = ax.imshow(np.ma.masked_invalid(M), cmap=cmap, vmin=vmin, vmax=vmax)
    N = len(labels)
    ax.set_xticks(range(N))
    ax.set_yticks(range(N))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_yticklabels(labels, fontsize=8)
    for i in range(N):
        for j in range(N):
            v = M[i, j]
            if np.isfinite(v):
                r, g, b, _ = cmap(im.norm(v))
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                ax.text(j, i, fmt(v), ha="center", va="center", fontsize=7.5,
                        color="white" if lum < 0.5 else "black")
    ax.set_title(title, fontsize=10, loc="left")
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
    cb.set_label(cbar_label, fontsize=8)


def plot_scatter_matrix(plt, args, wide, labels, subs, title, out):
    N = len(labels)
    if N > 9:
        print(f"[warn] {N} sources -> {N*N} panels; the scatter matrix will be dense")
    fig, axes = plt.subplots(N, N, figsize=(2.45 * N + 1.2, 2.45 * N), squeeze=False)
    vals = wide[labels].to_numpy(dtype=float)
    lo, hi = np.nanmin(vals), np.nanmax(vals)
    pad = max((hi - lo) * 0.04, 0.5)
    lim = (lo - pad, hi + pad)
    diffs = [np.abs(sub.iloc[:, 0] - sub.iloc[:, 1]).quantile(0.995)
             for sub in subs.values() if len(sub) >= 3]
    D = max(max(diffs) * 1.1 if diffs else 1.0, 1.0)
    colors = src_colors(labels)
    gap_base = pd.Timedelta(days=1) if args.mode == "daily" else pd.Timedelta(hours=1)
    mappable = None

    for i in range(N):
        for j in range(N):
            ax = axes[i, j]
            ax.tick_params(labelsize=6.5)
            ax.set_box_aspect(1)
            if i == j:
                v = wide[labels[i]].dropna()
                ax.hist(v, bins=30, color=colors[labels[i]], alpha=0.8)
                ax.set_xlim(lim)
                ax.set_yticks([])
                ax.text(0.5, 0.92, labels[i], transform=ax.transAxes, ha="center", va="top",
                        fontsize=8, weight="bold")
            else:
                key = (min(i, j), max(i, j))
                sub = subs[key]
                if len(sub) < 3:
                    ax.text(0.5, 0.5, f"n={len(sub)}", transform=ax.transAxes,
                            ha="center", va="center", fontsize=8, color="0.5")
                    ax.set_xticks([]); ax.set_yticks([])
                else:
                    y = sub[labels[i]].to_numpy()
                    x = sub[labels[j]].to_numpy()
                    times = pd.Series(sub.index)
                    c, cmap, (vmin, vmax) = sc.color_values(times, args.color_by)
                    if i > j:   # lower: connected scatter, row (y) vs column (x)
                        if len(sub) <= args.connect_max:
                            p = pd.DataFrame({"time": sub.index, "ref": x, "tst": y})
                            step = p["time"].diff().median()
                            gap = (max(gap_base, step) if pd.notna(step) else gap_base) * 2.5
                            for seg in sc.connected_segments(p, gap):
                                ax.plot(seg["ref"], seg["tst"], "-", lw=0.4, color="0.7", zorder=1)
                        mappable = ax.scatter(x, y, c=c, cmap=cmap, vmin=vmin, vmax=vmax,
                                              s=5, edgecolor="none", zorder=2)
                        ax.axline((lim[0], lim[0]), slope=1, color="k", ls="--", lw=0.6)
                        ax.set_xlim(lim); ax.set_ylim(lim)
                        st = sc.compare_stats(x, y)
                        ax.text(0.04, 0.96, f"{st['bias']:+.2f}\n{st['rmse']:.2f}\nn={st['n']}",
                                transform=ax.transAxes, va="top", fontsize=6.3, family="monospace",
                                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="0.8", alpha=0.85))
                    else:       # upper: Bland-Altman, row - column
                        d, m = y - x, (x + y) / 2
                        st = sc.compare_stats(x, y)
                        ax.scatter(m, d, c=c, cmap=cmap, vmin=vmin, vmax=vmax, s=5, edgecolor="none")
                        ax.axhline(0, color="0.5", lw=0.6)
                        ax.axhline(st["bias"], color="tab:red", lw=0.9)
                        if np.isfinite(st["loa_lo"]):
                            ax.axhline(st["loa_lo"], color="tab:red", ls="--", lw=0.6)
                            ax.axhline(st["loa_hi"], color="tab:red", ls="--", lw=0.6)
                        ax.set_xlim(lim); ax.set_ylim(-D, D)
                        ax.yaxis.tick_right()
                        if j != N - 1:
                            ax.set_yticklabels([])
                    ax.grid(alpha=0.25, lw=0.5)
            # edge labels only
            if i == N - 1:
                ax.set_xlabel(labels[j], fontsize=8)
            elif i >= j:
                ax.set_xticklabels([])
            if i < j:
                ax.set_xticklabels([])
            if j == 0 and i != 0:
                ax.set_ylabel(labels[i], fontsize=8)
            elif i > j:
                ax.set_yticklabels([])

    fig.subplots_adjust(left=0.07, right=0.9, bottom=0.08, top=0.92, wspace=0.12, hspace=0.12)
    if mappable is not None:
        cax = fig.add_axes([0.925, 0.3, 0.012, 0.4])
        cb = fig.colorbar(mappable, cax=cax)
        if args.color_by == "hour":
            cb.set_ticks([0, 6, 12, 18, 24]); cb.set_label("Local hour")
        else:
            import matplotlib.dates as mdates
            cb.ax.yaxis.set_major_locator(mdates.AutoDateLocator())
            cb.ax.yaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
            cb.set_label("Date")
    fig.suptitle(f"{title}\nlower: row vs column (1:1 dashed; bias/RMSE/n)   ·   "
                 f"upper: Bland–Altman row − column (right axis)   ·   diagonal: distribution",
                 fontsize=10)
    fig.savefig(out, dpi=args.dpi)
    print(f"[saved] {out.resolve()}")
    return fig


def plot_summary(plt, args, labels, stats, hat_all, hat_st, analysis_labels, title, out):
    N = len(labels)
    B, R, Nn = (np.full((N, N), np.nan) for _ in range(3))
    for (i, j), st in stats.items():
        if st["n"] >= 3:
            B[i, j], B[j, i] = st["bias"], -st["bias"]
            R[i, j] = R[j, i] = st["rmse"]
        Nn[i, j] = Nn[j, i] = st["n"]
    fig, axes = plt.subplots(2, 2, figsize=(max(12, 1.2 * N + 7), max(10, 1.1 * N + 6)))
    bmax = max(np.nanmax(np.abs(B)) if np.isfinite(B).any() else 0.5, 0.5)
    heatmap(fig, axes[0, 0], B, labels, "Bias: row − column", "RdBu_r", -bmax, bmax,
            lambda v: f"{v:+.2f}", args.label)
    heatmap(fig, axes[0, 1], R, labels, "RMSE", "magma_r", 0,
            np.nanmax(R) if np.isfinite(R).any() else 1, lambda v: f"{v:.2f}", args.label)
    heatmap(fig, axes[1, 0], Nn, labels, "Matched pairs (n)", "Blues", 0,
            np.nanmax(Nn) if np.isfinite(Nn).any() else 1, lambda v: f"{int(v):,}", "pairs")

    ax = axes[1, 1]
    colors = src_colors(labels)
    if hat_all:
        x = np.arange(N)
        w = 0.38 if hat_st else 0.7
        s_all = [hat_all[l][0] if l in hat_all else np.nan for l in labels]
        bars = ax.bar(x - (w / 2 if hat_st else 0), s_all, w,
                      color=[colors[l] for l in labels], label="all sources")
        for b, l in zip(bars, labels):
            if l in hat_all and hat_all[l][1] < 0:
                b.set_hatch("///"); b.set_edgecolor("k")
        if hat_st:
            s_st = [hat_st[l][0] if l in hat_st else np.nan for l in labels]
            ax.bar(x + w / 2, s_st, w, color=[colors[l] for l in labels], alpha=0.45,
                   edgecolor="k", lw=0.5, label="stations only")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax.set_ylabel(f"estimated own random error σ  {args.label}", fontsize=8)
        note = "hatched = negative variance solved (errors correlated / not independent)"
        if analysis_labels:
            note += "\nanalyses assimilate ASOS obs, so their errors correlate with those stations"
        ax.text(0.01, 0.99, note, transform=ax.transAxes, va="top", fontsize=7, color="0.3")
        ax.set_ylim(0, np.nanmax([v[0] for v in hat_all.values()]) * 1.25)
        ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(0, 0.9))
        ax.grid(axis="y", alpha=0.3)
    else:
        ax.text(0.5, 0.5, "N-cornered hat needs ≥3 sources\nwith enough overlapping pairs",
                ha="center", va="center", transform=ax.transAxes, color="0.4")
        ax.set_axis_off()
    ax.set_title("N-cornered hat: each source's own error (bias removed)", fontsize=10, loc="left")
    fig.suptitle(title, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out, dpi=args.dpi)
    print(f"[saved] {out.resolve()}")
    return fig


def plot_lag(plt, args, labels, L, C, title, out):
    N = len(labels)
    fig, axes = plt.subplots(1, 2, figsize=(max(12, 1.2 * N + 7), max(5.5, 0.6 * N + 3.5)))
    lmax = max(np.nanmax(np.abs(L)) if np.isfinite(L).any() else args.lag_step, args.lag_step)
    heatmap(fig, axes[0], L, labels, "Best lag (min): + = row LAGS column", "RdBu_r",
            -lmax, lmax, lambda v: f"{v:+.0f}", "minutes")
    heatmap(fig, axes[1], C, labels, "Peak correlation of 1-h changes", "viridis",
            0, 1, lambda v: f"{v:.2f}", "r")
    fig.suptitle(f"{title}\n{args.lag_step}-min grid, ±{args.max_lag_min} min search; "
                 f"lags within ±{2 * args.lag_step} min are within resolution", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(out, dpi=args.dpi)
    print(f"[saved] {out.resolve()}")
    return fig


def plot_diurnal(plt, args, loo, labels, title, out):
    colors = src_colors(labels)
    fig, ax = plt.subplots(figsize=(11, 5.5))
    min_n = args.diurnal_min_days
    any_line, max_count = False, 0
    for lab in labels:
        d = loo[lab]
        if d.empty:
            print(f"[diurnal] {lab}: no hours with enough other sources for a consensus -- not drawn")
            continue
        g = d.groupby(d.index.hour).agg(["mean", "std", "count"]).reindex(range(24))
        max_count = max(max_count, int(g["count"].max(skipna=True) or 0))
        good = g["count"] >= min_n
        c = colors[lab]
        # 95% CI of the hourly mean (needs >= 2 samples)
        se = g["std"] / np.sqrt(g["count"])
        band = g["count"] >= 2
        ax.fill_between(g.index[band], (g["mean"] - 1.96 * se)[band], (g["mean"] + 1.96 * se)[band],
                        color=c, alpha=0.12, lw=0)
        # full curve (thin) so short windows still show a shape; reliable hours solid on top
        ax.plot(g.index, g["mean"], "-", lw=0.8, color=c, alpha=0.6)
        ax.plot(g.index[good], g["mean"][good], "o", ms=4, color=c,
                label=lab if good.any() else None)
        ax.plot(g.index[~good & g["count"].notna()], g["mean"][~good & g["count"].notna()], "o",
                ms=4, mfc="white", mec=c, label=None if good.any() else f"{lab} (few days)")
        any_line = any_line or g["count"].notna().any()
    ax.axhline(0, color="0.4", lw=0.8)
    ax.set_xticks(range(0, 24, 3))
    ax.set_xlim(-0.5, 23.5)
    ax.set_xlabel(f"Local hour ({args.tz})")
    ax.set_ylabel(f"source − median of others  {args.label}")
    ax.grid(alpha=0.3)
    if any_line:
        ax.legend(fontsize=8, ncol=2)
    if max_count < min_n:
        print(f"[diurnal] at most {max_count} days per hour (< --diurnal-min-days {min_n}): "
              f"curve drawn with hollow markers; widen the window for a reliable diurnal shape")
    ax.set_title(f"{title}\nDiurnal bias vs. leave-one-out consensus (hourly means); "
                 f"shading = 95% CI; hollow = fewer than {min_n} days",
                 fontsize=10, loc="left")
    fig.tight_layout()
    fig.savefig(out, dpi=args.dpi)
    print(f"[saved] {out.resolve()}")
    return fig


def plot_drift(plt, args, loo, labels, title, out):
    colors = src_colors(labels)
    w = args.drift_window
    fig, ax = plt.subplots(figsize=(12, 5.5))
    plotted = False
    for lab in labels:
        d = loo[lab]
        if d.empty:
            continue
        daily = d.resample("1D").agg(["mean", "count"])
        dm = daily["mean"].where(daily["count"] >= 12)
        roll = dm.rolling(w, min_periods=max(3, w // 2), center=True).mean()
        idx = dm.index.tz_localize(None)
        ax.plot(idx, dm.to_numpy(), ".", ms=3, color=colors[lab], alpha=0.35)
        ax.plot(idx, roll.to_numpy(), "-", lw=1.8, color=colors[lab], label=lab)
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    ax.axhline(0, color="0.4", lw=0.8)
    ax.set_ylabel(f"daily mean: source − median of others  {args.label}")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, ncol=2)
    ax.set_title(f"{title}\nDrift: daily difference vs. consensus, {w}-day centered rolling mean",
                 fontsize=10, loc="left")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out, dpi=args.dpi)
    print(f"[saved] {out.resolve()}")
    return fig


# ----------------------------------------------------------------------------
# Run one source set
# ----------------------------------------------------------------------------
def run(plt, args, tag, sources, analysis_labels, start_local, end_local, only):
    labels = list(sources)
    print(f"\n==================== {tag}: {len(labels)} sources, "
          f"{len(labels) * (len(labels) - 1) // 2} pairs ====================")
    if len(labels) < 2:
        print("[warn] fewer than 2 sources -- skipped")
        return
    wide, note = build_wide(sources, args, start_local, end_local)
    title = f"{args.label} — {args.start} to {args.end} — {note}"
    stem = f"pair_matrix_{tag}_{args.start}_{args.end}_{args.mode}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stats, subs = pair_stats(wide, labels)
    L = C = C0 = None
    if "lag" in only:
        L, C, C0 = lag_matrix(sources, labels, args.max_lag_min, args.lag_step)

    # ---- console: pair table ----
    rows = []
    for (i, j), st in stats.items():
        rows.append(dict(a=labels[i], b=labels[j], n=st["n"], bias_a_minus_b=st["bias"],
                         mae=st["mae"], rmse=st["rmse"], r=st["r"], sd_diff=st["sd"],
                         deming_slope=st["slope"], deming_intercept=st["intercept"],
                         loa_lo=st["loa_lo"], loa_hi=st["loa_hi"],
                         lag_min_a_vs_b=L[i, j] if L is not None else np.nan,
                         lag_corr=C[i, j] if C is not None else np.nan))
    ptab = pd.DataFrame(rows).sort_values("rmse", na_position="last")
    print(f"\n{'pair':<34}{'n':>7}{'bias':>8}{'RMSE':>7}{'r':>7}{'lag':>6}")
    for _, r in ptab.iterrows():
        lag = f"{r.lag_min_a_vs_b:+.0f}" if np.isfinite(r.lag_min_a_vs_b) else "  -"
        print(f"{r.a + ' − ' + r.b:<34}{int(r.n):>7,}{r.bias_a_minus_b:>+8.2f}"
              f"{r.rmse:>7.2f}{r.r:>7.3f}{lag:>6}")
    ptab.to_csv(out_dir / f"{stem}_pairs.csv", index=False, float_format="%.4f")

    # ---- N-cornered hat ----
    hat_all = n_cornered_hat(wide, labels)
    st_labels = [l for l in labels if l not in analysis_labels]
    hat_st = n_cornered_hat(wide, st_labels) if analysis_labels and len(st_labels) >= 3 else None
    if hat_all:
        min_n = min(st["n"] for st in stats.values())
        print("\nN-cornered hat (own random error σ, bias removed):")
        if min_n < 30:
            print(f"  [warn] smallest pair has only n={min_n}; σ estimates are unreliable "
                  f"(variance of a variance is large) -- widen the window")
        for l in labels:
            s, v = hat_all[l]
            extra = f"   stations-only {hat_st[l][0]:.2f}" if hat_st and l in hat_st else ""
            flag = "   (negative variance -> correlated errors)" if v < 0 else ""
            print(f"  {l:<22} σ = {s:5.2f}{extra}{flag}")
    else:
        print("\n[hat] needs ≥3 sources with overlapping pairs -- skipped")
    src_rows = [dict(source=l, n_obs=int(sources[l].size),
                     n_aligned=int(wide[l].notna().sum()),
                     hat_sigma=hat_all[l][0] if hat_all else np.nan,
                     hat_var_raw=hat_all[l][1] if hat_all else np.nan,
                     hat_sigma_stations_only=hat_st[l][0] if hat_st and l in hat_st else np.nan,
                     is_analysis=l in analysis_labels) for l in labels]
    pd.DataFrame(src_rows).to_csv(out_dir / f"{stem}_sources.csv", index=False, float_format="%.4f")
    print(f"[saved] {(out_dir / f'{stem}_pairs.csv').resolve()}")
    print(f"[saved] {(out_dir / f'{stem}_sources.csv').resolve()}")

    if L is not None:
        # Per-source clock offset: a source with a bad clock lags EVERY other source by the
        # same amount, so the median of its row isolates it (pairwise lags alone can't say
        # which of the two is wrong).
        offsets = {}
        if len(labels) >= 3:
            for i, lab in enumerate(labels):
                row = L[i][np.isfinite(L[i])]
                if len(row) >= 2:
                    offsets[lab] = float(np.median(row))
            print("\nPer-source timing offset (median lag vs. all others; + = runs late):")
            for lab, o in offsets.items():
                flag = "   <-- check timestamps/timezone" if abs(o) > 2 * args.lag_step else ""
                print(f"  {lab:<22} {o:+5.0f} min{flag}")
            for r in src_rows:
                r["timing_offset_min"] = offsets.get(r["source"], np.nan)
            pd.DataFrame(src_rows).to_csv(out_dir / f"{stem}_sources.csv", index=False,
                                          float_format="%.4f")
        # Pairwise: flag only offsets beyond grid resolution that clearly beat zero lag
        for i, j in combinations(range(len(labels)), 2):
            if not (np.isfinite(L[i, j]) and np.isfinite(C0[i, j])):
                continue
            gain = C[i, j] - C0[i, j]
            if abs(L[i, j]) > 2 * args.lag_step and gain >= args.lag_min_gain:
                print(f"[lag] {labels[i]} vs {labels[j]}: best lag {L[i, j]:+.0f} min "
                      f"(r {C0[i, j]:.3f} -> {C[i, j]:.3f}) -- check timestamps/timezone")

    figs = []
    if "scatter" in only:
        figs.append(plot_scatter_matrix(plt, args, wide, labels, subs, title,
                                        out_dir / f"{stem}_scatter.png"))
    if "summary" in only:
        figs.append(plot_summary(plt, args, labels, stats, hat_all, hat_st, analysis_labels,
                                 title, out_dir / f"{stem}_summary.png"))
    if "lag" in only:
        figs.append(plot_lag(plt, args, labels, L, C, title, out_dir / f"{stem}_lag.png"))
    if ("diurnal" in only or "drift" in only):
        loo = loo_differences(hourly_table(sources), labels)
        if "diurnal" in only:
            figs.append(plot_diurnal(plt, args, loo, labels, title, out_dir / f"{stem}_diurnal.png"))
        if "drift" in only:
            span = (end_local - start_local).days
            if span < args.drift_window:
                print(f"[drift] window is {span} days < --drift-window {args.drift_window} -- skipped")
            else:
                figs.append(plot_drift(plt, args, loo, labels, title, out_dir / f"{stem}_drift.png"))
    if not args.show:
        for f in figs:
            if f is not None:
                plt.close(f)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        description="All-pairs (C(N,2)) comparison of stations and gridded analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--station", required=True, help="Comma-separated stations, e.g. KSGF,KBBG,AMBIENT")
    ap.add_argument("--analysis", help="Comma-separated analysis sources, e.g. urma,rtma,hrrr")
    ap.add_argument("--analysis-at",
                    help="Station(s) whose location the analysis is sampled at: one, a comma list "
                         "(one full run each), or 'all'. Default: first --station.")
    ap.add_argument("--start", required=True, help="Start date (local), YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="End date (local, inclusive), YYYY-MM-DD")
    ap.add_argument("--db", default="weather_archive.db")

    g = ap.add_argument_group("station table")
    g.add_argument("--table", default="station_obs")
    g.add_argument("--station-col"); g.add_argument("--time-col"); g.add_argument("--value-col")

    g = ap.add_argument_group("analysis table (auto-detected if omitted)")
    g.add_argument("--analysis-table"); g.add_argument("--analysis-model-col")
    g.add_argument("--analysis-station-col"); g.add_argument("--analysis-time-col")
    g.add_argument("--analysis-value-col"); g.add_argument("--analysis-lead-col")
    g.add_argument("--analysis-lead", default="0", help="Lead to keep (default 0; 'any' = no filter)")
    g.add_argument("--analysis-units", choices=["auto", "F", "C", "K"], default="auto")

    g = ap.add_argument_group("alignment")
    g.add_argument("--label", default="Temperature (°F)")
    g.add_argument("--tz", default="America/Chicago")
    g.add_argument("--db-tz", default="UTC", help="Zone of naive timestamps stored in the DB")
    g.add_argument("--mode", choices=["instant", "hourly", "daily"], default="instant",
                   help="instant: hourly grid, nearest obs within --tol-min (default); "
                        "hourly: hourly means; daily: local-day aggregate")
    g.add_argument("--tol-min", type=float, default=10.0, help="instant mode tolerance (default 10)")
    g.add_argument("--grid-minute", type=int,
                   help="instant mode grid minute (default: modal minute of the sparsest source, "
                        "e.g. 53 for ASOS)")
    g.add_argument("--daily-stat", choices=["max", "min", "mean"], default="mean")
    g.add_argument("--min-hours", type=int, default=20)
    g.add_argument("--pairs", choices=["pairwise", "common"], default="pairwise",
                   help="pairwise: each pair uses its own overlap (default); "
                        "common: only times where EVERY source reports")

    g = ap.add_argument_group("analyses / plotting")
    g.add_argument("--only", help=f"Comma subset of outputs: {','.join(OUTPUTS)} (default all)")
    g.add_argument("--color-by", choices=["auto", "hour", "date"], default="auto")
    g.add_argument("--connect-max", type=int, default=250,
                   help="connect scatter points in a panel only if it has <= this many (default 250)")
    g.add_argument("--max-lag-min", type=int, default=180, help="lag search range ± minutes")
    g.add_argument("--lag-step", type=int, default=10, help="lag grid resolution, minutes")
    g.add_argument("--lag-min-gain", type=float, default=0.03,
                   help="flag a lag only if its correlation beats zero lag by this much")
    g.add_argument("--diurnal-min-days", type=int, default=3,
                   help="hours with fewer samples than this are drawn hollow (default 3)")
    g.add_argument("--drift-window", type=int, default=30, help="rolling window, days")

    g = ap.add_argument_group("output")
    g.add_argument("--out-dir", default="results")
    g.add_argument("--dpi", type=int, default=130)
    g.add_argument("--show", action="store_true")
    return ap


def main():
    args = build_parser().parse_args()
    if not args.show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stations = sc.split_list(args.station)
    analyses = [a.lower() for a in sc.split_list(args.analysis)] if args.analysis else []
    if len(stations) + len(analyses) < 2 or not stations:
        sys.exit("[error] need at least two sources (e.g. --station KSGF,AMBIENT, "
                 "or --station KSGF --analysis urma)")
    only = set(sc.split_list(args.only)) if args.only else set(OUTPUTS)
    bad = only - set(OUTPUTS)
    if bad:
        sys.exit(f"[error] --only: unknown {', '.join(bad)}; choose from {', '.join(OUTPUTS)}")
    if args.color_by == "auto":
        args.color_by = "date" if args.mode == "daily" else "hour"
    if args.mode == "daily" and "diurnal" in only:
        print("[note] diurnal panel always uses hourly means, independent of --mode daily")

    try:
        start_local = pd.Timestamp(args.start).tz_localize(args.tz)
        end_local = pd.Timestamp(args.end).tz_localize(args.tz) + pd.Timedelta(days=1)
    except Exception as e:
        sys.exit(f"[error] bad --start/--end/--tz: {e}")
    if end_local <= start_local:
        sys.exit("[error] --end must be on or after --start")

    st, ana, has_scol = load_all(args, stations, analyses, start_local, end_local)
    runs = build_runs(args, stations, analyses, st, ana, has_scol)
    if not runs:
        sys.exit("[error] nothing to compare")
    for tag, sources, al in runs:
        run(plt, args, tag, sources, al, start_local, end_local, only)
    if args.show:
        plt.show()


if __name__ == "__main__":
    main()
