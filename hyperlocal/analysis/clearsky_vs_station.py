#!/usr/bin/env python3
"""The station's solar radiation against clear-sky estimates, DAY BY DAY across the whole record, with and without a humidity (dewpoint) adjustment.
Models, per day:  A raw Haurwitz | B calibrated by sun elevation (median clearness of clear days) | C = B x (1 + b (dewpoint - reference)) | D0 physical (pvlib simplified Solis) x one sensor factor, water vapour fixed | D1 the same with water vapour from the station's own temperature and humidity.
Each month is scored with a fit made from the OTHER months only (leave-one-month-out), so nothing is tuned to the days it is judged on.  Clear days = bright and smooth (calibrate_clearsky.py's test).
  python analysis/clearsky_vs_station.py [--db FILE] [--station G6964] [--csv OUT.csv] [--png OUT.png]     (the plotter's database with station_obs; WEATHER_LAT / WEATHER_LON from .env)
Needs numpy, pandas, pvlib (pip install pvlib)."""
import argparse, os, sqlite3, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c
import pvlib

ap = argparse.ArgumentParser(); ap.add_argument("--db"); ap.add_argument("--station", default="G6964"); ap.add_argument("--csv"); ap.add_argument("--png"); ap.add_argument("--month", help="YYYY-MM for a small-multiples sheet of that month")
a = ap.parse_args(); c.load_env(); cfg = c.load_config()
db = a.db or os.environ.get("WEATHER_DB_PATH", "weather_archive.db"); lat, lon, alt = cfg["lat"], cfg["lon"], cfg["valley_elevation_ft"] * 0.3048
q = ("SELECT timestamp, solar_radiation sr, dewpoint td, temperature t, relative_humidity rh FROM station_obs WHERE station=? AND solar_radiation IS NOT NULL AND solar_radiation>=0 "
     "AND dewpoint IS NOT NULL AND relative_humidity IS NOT NULL AND temperature IS NOT NULL ORDER BY timestamp")
df = pd.read_sql(q, sqlite3.connect(db), params=[a.station]); df["time"] = pd.to_datetime(df["timestamp"], utc=True); df = df[df["time"].dt.minute % 15 == 0].reset_index(drop=True)
sp = pvlib.solarposition.get_solarposition(pd.DatetimeIndex(df["time"]), lat, lon, altitude=alt)
df["el"] = sp["apparent_elevation"].to_numpy(); df["az"] = sp["azimuth"].to_numpy()
cosz = np.cos(np.radians(90 - df["el"].clip(lower=0))); df["haur"] = np.where(df["el"] > 0, 1098 * cosz * np.exp(-0.057 / cosz.clip(lower=1e-6)), 0.0)
u = df[(df["el"] >= 8) & (df["haur"] > 5)].copy(); u["kt"] = (u["sr"] / u["haur"]).clip(upper=1.3)
u["day"] = (u["time"] + pd.to_timedelta(lon / 15.0, unit="h")).dt.floor("D"); u["month"] = u["time"].dt.to_period("M")
# --- clear days (calibrate_clearsky.py's rule)
def rough(g):
    v = g.sort_values("time")["sr"].to_numpy(); return np.nan if len(v) < 10 else float(np.mean(np.abs(np.diff(v, 2))) / max(v.max(), 1.0))
days = u.groupby("day").agg(n=("kt", "size"), kmax=("kt", "max"), kmean=("kt", "mean")); days["rough"] = u.groupby("day").apply(rough, include_groups=False)
days["clear"] = (days.n >= 14) & (days.kmax >= 0.6) & (days.kmean >= 0.5) & (days.rough < 0.04); u["clear"] = u["day"].map(days["clear"]).fillna(False)
# --- physical model: simplified Solis x one factor; water vapour fixed or from the station's own temperature and humidity
dni_extra = pvlib.irradiance.get_extra_radiation(pd.DatetimeIndex(u["time"])).to_numpy(); pres = pvlib.atmosphere.alt2pres(alt)
pw1 = pvlib.atmosphere.gueymard94_pw(((u["t"] - 32) / 1.8).to_numpy(), u["rh"].to_numpy()).clip(0.2, 6)
def solis(pw): return np.asarray(pvlib.clearsky.simplified_solis(u["el"].clip(lower=0.5).to_numpy(), aod700=0.08, precipitable_water=pw, pressure=pres, dni_extra=dni_extra)["ghi"])
u["solis0"] = solis(np.full(len(u), float(np.median(pw1[u["clear"].to_numpy()])))); u["solis1"] = solis(pw1)
EDGES = np.arange(8, 90, 5.0)
def fit(train):                                  # everything the models need, from the training days only
    t = train[train.clear]; b = np.digitize(t.el, EDGES) - 1; tab = {i: float(np.median(t.kt[b == i])) for i in set(b) if (b == i).sum() >= 10}
    mid = np.array([EDGES[i] + 2.5 for i in sorted(tab)]); val = np.array([tab[i] for i in sorted(tab)])
    cal = lambda el: np.interp(el, mid, val)
    hi = t[t.el >= 25]; r = hi.kt / cal(hi.el); td0 = float(hi.td.median()); bco = float(np.polyfit(hi.td - td0, r, 1)[0])      # relative change of the ceiling per F of dewpoint
    k0 = float(np.median(hi.sr / hi.solis0)); k1 = float(np.median(hi.sr / hi.solis1))
    return cal, td0, bco, k0, k1
out = []; folds = []
for m in sorted(u.month.unique()):
    te = u[u.month == m]; tr = u[u.month != m]
    if tr.clear.sum() < 200 or len(te) == 0: continue
    cal, td0, bco, k0, k1 = fit(tr); folds.append((str(m), td0, bco)); d = te.copy()
    d["A"] = d.haur; d["B"] = d.haur * cal(d.el); d["C"] = d.B * (1 + bco * (d.td - td0)); d["D0"] = k0 * d.solis0; d["D1"] = k1 * d.solis1; out.append(d)
r = pd.concat(out); M = ["A", "B", "C", "D0", "D1"]
LAB = {"A": "A raw Haurwitz", "B": "B calibrated, no humidity", "C": "C calibrated + dewpoint term", "D0": "D0 physical x factor, vapour fixed", "D1": "D1 physical x factor, vapour from T and RH"}
for mname in M: r["r_" + mname] = r.sr / r[mname].clip(lower=5)
cl = r[r.clear & (r.el >= 15)]
print(f"{r.day.nunique()} days, {int(days.clear.sum())} clear; each month scored with a fit from the other months.\n")
_cal, _td0, _bco, _k0, _k1 = fit(u)
print(f"THE ADJUSTMENT (fit on all months): clear_sky(reading) = table(sun elevation) x (1 + {_bco:+.5f} x (dewpoint F - {_td0:.1f}))   i.e. {_bco*10*100:+.2f}% per +10 F of dewpoint")
print(f"  across the {len(folds)} leave-one-month-out fits: coefficient {min(f[2] for f in folds):+.5f} to {max(f[2] for f in folds):+.5f}, reference dewpoint {min(f[1] for f in folds):.1f} to {max(f[1] for f in folds):.1f} F")
print("  e.g. the ceiling is x" + ", x".join(f"{1 + _bco * (td - _td0):.3f} at a {td} F dewpoint" for td in (10, 30, 50, 70)) + "\n")
print("CLEAR days, readings with the sun 15+ degrees up: measured / clear-sky model (1.00 = on the line)")
print(f"{'model':<44}{'median':>7}{'10%':>7}{'90%':>7}{'mean abs err':>14}{'abs err W/m2':>14}")
for mname in M:
    x = cl["r_" + mname]; e = (cl.sr - cl[mname]).abs()
    print(f"{LAB[mname]:<44}{x.median():>7.3f}{x.quantile(.1):>7.3f}{x.quantile(.9):>7.3f}{np.mean(np.abs(x - 1)):>14.3f}{e.mean():>14.1f}")
print("\nthe same, by season (mean abs error of the ratio on clear days):")
seas = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
cl = cl.assign(season=cl.time.dt.month.map(seas)); print(f"{'':<8}" + "".join(f"{m:>8}" for m in M) + "     n")
for s in ("winter", "spring", "summer", "autumn"):
    g = cl[cl.season == s]; print(f"{s:<8}" + "".join(f"{np.mean(np.abs(g['r_' + m] - 1)):>8.3f}" for m in M) + f"  {len(g):>5}")
print("\nand the drift with dewpoint on clear days (median ratio by dewpoint band; a flat line = the model already accounts for humidity):")
bands = [(-20, 25), (25, 40), (40, 55), (55, 80)]; print(f"{'dewpoint F':<12}" + "".join(f"{m:>8}" for m in M) + "     n")
for lo, hi in bands:
    g = cl[(cl.td >= lo) & (cl.td < hi)]; print(f"{lo:>4}-{hi:<7}" + "".join(f"{g['r_' + m].median():>8.3f}" for m in M) + f"  {len(g):>5}")
# per-day table: the noon (sun highest) ratio for each model
rows = []
for day, g in r.groupby("day"):
    g = g.sort_values("time"); i = int(g.el.values.argmax()); w = g.iloc[max(0, i - 2):i + 3]
    rows.append(dict(day=day.date(), clear=bool(g.clear.iloc[0]), n=len(g), noon_el=round(float(g.el.max()), 1), dewpoint=round(float(g.td.mean()), 1), measured_noon=round(float(w.sr.median()), 1), **{"ratio_" + m: round(float(w.sr.median() / max(w[m].median(), 5)), 3) for m in M}))
tab = pd.DataFrame(rows)
if a.csv: tab.to_csv(a.csv, index=False); print("\nper-day table ->", a.csv)
if a.png:
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    fig, ax = plt.subplots(2, 1, figsize=(13, 8.5), dpi=120)
    cd = tab[tab.clear]; x = pd.to_datetime(cd.day)
    for m, col in (("B", "#d95926"), ("C", "#1f4fd8"), ("D1", "#1baf7a")): ax[0].plot(x, cd["ratio_" + m], "o-", ms=3, lw=0.8, color=col, label=LAB[m], alpha=.85)
    ax[0].axhline(1, color="gray", lw=.8); ax[0].set_ylim(0.8, 1.25); ax[0].set_ylabel("noon: measured / model, clear days"); ax[0].legend(fontsize=8, frameon=False, ncol=3); ax[0].grid(alpha=.25)
    ax[0].set_title("Clear-day noon ratio through the record: a model that handles humidity stays on 1.0 in every season", fontsize=10)
    for m, col in (("B", "#d95926"), ("C", "#1f4fd8")): ax[1].hist(cl["r_" + m], bins=np.arange(0.5, 1.6, 0.02), alpha=.55, color=col, label=LAB[m])
    ax[1].axvline(1, color="gray", lw=.8); ax[1].set_xlabel("measured / model, every clear-day reading with the sun 15+ degrees up"); ax[1].set_ylabel("readings"); ax[1].legend(fontsize=8, frameon=False); ax[1].grid(alpha=.25)
    fig.tight_layout(); fig.savefig(a.png); print("figure ->", a.png)
