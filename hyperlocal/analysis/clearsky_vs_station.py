#!/usr/bin/env python3
"""The station's solar radiation against clear-sky estimates, DAY BY DAY across the whole record, with and without a humidity (dewpoint) adjustment.
Models, per day:  A raw Haurwitz | B calibrated by sun elevation (median clearness of clear days) | C = B x (1 + b (dewpoint - reference)) | D0 physical (pvlib simplified Solis) x one sensor factor, water vapour fixed | D1 the same with water vapour from the station's own temperature and humidity.
Each month is scored with a fit made from the OTHER months only (leave-one-month-out), so nothing is tuned to the days it is judged on.  Clear days = bright and smooth (calibrate_clearsky.py's test).
  python analysis/clearsky_vs_station.py [--db FILE] [--station AMBIENT] [--csv OUT.csv] [--png OUT.png]     (the plotter's database with station_obs; WEATHER_LAT / WEATHER_LON from .env)
Needs numpy, pandas, pvlib (pip install pvlib)."""
import argparse, os, sqlite3, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c
import pvlib

ap = argparse.ArgumentParser(); ap.add_argument("--db"); ap.add_argument("--station", default="AMBIENT"); ap.add_argument("--csv"); ap.add_argument("--png"); ap.add_argument("--month", help="YYYY-MM for a small-multiples sheet of that month")
ap.add_argument("--envelope", action="store_true", help="also use the clear STRETCHES of partly cloudy days (the smoothed upper envelope) and compare the fits")
ap.add_argument("--sheet", help="PNG: a sheet of NON-clear days (measured against the calibrated curve with and without the dewpoint adjustment), spread over the dewpoint range")
ap.add_argument("--days", help="with --sheet: comma-separated dates (YYYY-MM-DD) instead of the automatic pick")
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
MIN_EL = float(os.environ.get("MIN_EL", "25"))            # the fits of the humidity term use only readings with the sun at least this high
def fit(train):                                  # everything the models need, from the training days only
    t = train[train.clear]; b = np.digitize(t.el, EDGES) - 1; tab = {i: float(np.median(t.kt[b == i])) for i in set(b) if (b == i).sum() >= 10}
    mid = np.array([EDGES[i] + 2.5 for i in sorted(tab)]); val = np.array([tab[i] for i in sorted(tab)])
    cal = lambda el: np.interp(el, mid, val)
    hi = t[t.el >= MIN_EL]; r = hi.kt / cal(hi.el); td0 = float(hi.td.median()); bco = float(np.polyfit(hi.td - td0, r, 1)[0])      # relative change of the ceiling per F of dewpoint
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
_hi = u[u.clear & (u.el >= MIN_EL)].copy(); _hi["rr"] = _hi.kt / _cal(_hi.el)
print(f"  sample: station record {df['time'].min().date()} to {df['time'].max().date()} ({(df['time'].max() - df['time'].min()).days / 365.25:.1f} years); {u.day.nunique()} days with solar, humidity and dewpoint; {int(days.clear.sum())} clear days; the fit rests on {len(_hi)} clear-day readings (sun 25+ deg) from {_hi.day.nunique()} days, dewpoints {_hi.td.min():.0f} to {_hi.td.max():.0f} F")
_sea = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
print("  the same fit INSIDE each season alone (does the effect hold without the season-to-season swing?):")
for _s in ("winter", "spring", "summer", "autumn"):
    _g = _hi[_hi.time.dt.month.map(_sea) == _s]
    if len(_g) < 30:
        print(f"    {_s:<7} too few readings at this sun angle ({len(_g)})"); continue
    print(f"    {_s:<7} {np.polyfit(_g.td - _td0, _g.rr, 1)[0] * 1000:+6.2f} % per +10 F   ({len(_g):>4} readings, {_g.day.nunique():>2} days, dewpoint {_g.td.min():.0f}-{_g.td.max():.0f} F, corr {np.corrcoef(_g.td, _g.rr)[0, 1]:+.2f})")
_y1 = _hi[_hi.time < pd.Timestamp("2026-01-01", tz="UTC")]; _y2 = _hi[_hi.time >= pd.Timestamp("2026-01-01", tz="UTC")]
print(f"  by calendar year: 2025 {np.polyfit(_y1.td - _td0, _y1.rr, 1)[0] * 1000:+.2f} % per +10 F ({_y1.day.nunique()} days) | 2026 {np.polyfit(_y2.td - _td0, _y2.rr, 1)[0] * 1000:+.2f} % per +10 F ({_y2.day.nunique()} days)")
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

if a.sheet:
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    r["local"] = r["time"] + pd.to_timedelta(lon / 15.0, unit="h"); r["ldate"] = r["local"].dt.date
    daily = r.groupby("ldate").agg(td=("td", "mean"), n=("sr", "size"), kmean=("kt", "mean"), clear=("clear", "first"), eB=("B", "sum"), eC=("C", "sum"), eM=("sr", "sum"))
    daily["cloudy_B"] = 1 - daily.eM / daily.eB; daily["cloudy_C"] = 1 - daily.eM / daily.eC
    nc = daily[(~daily.clear) & (daily.n >= 40) & (daily.kmean.between(0.15, 0.55))].sort_values("td")
    if a.days: pick = [pd.Timestamp(x).date() for x in a.days.split(",")]
    else: pick = [nc.index[int(q * (len(nc) - 1))] for q in (0.04, 0.2, 0.4, 0.6, 0.8, 0.97)]
    print("\nNON-clear days on the sheet: dewpoint, and the cloudiness each curve implies (1 - measured energy / clear-sky energy over the day):")
    print(f"{'day':<12}{'dewpoint F':>11}{'cloudy, no humidity':>21}{'cloudy, dewpoint-adjusted':>27}{'difference':>12}")
    fig, axs = plt.subplots(2, 3, figsize=(15, 7.2), dpi=120, sharey=False); axs = axs.ravel()
    for ax_, day in zip(axs, pick):
        g = r[r.ldate == day].sort_values("local"); dd = daily.loc[day]
        ax_.plot(g.local.dt.hour + g.local.dt.minute / 60, g.sr, color="#ff8c00", lw=1.2, label="measured"); ax_.plot(g.local.dt.hour + g.local.dt.minute / 60, g.B, color="#d95926", lw=1.1, ls="--", label="clear-sky, no humidity")
        ax_.plot(g.local.dt.hour + g.local.dt.minute / 60, g.C, color="#1f4fd8", lw=1.3, label="clear-sky, dewpoint-adjusted"); ax_.fill_between(g.local.dt.hour + g.local.dt.minute / 60, g.sr, g.C, where=g.C > g.sr, color="gray", alpha=.25)
        ax_.set_title(f"{day}   dewpoint {dd.td:.0f} F   cloudy {dd.cloudy_C * 100:.0f}% (was {dd.cloudy_B * 100:.0f}%)", fontsize=9); ax_.grid(alpha=.25); ax_.set_xlabel("local solar hour"); ax_.set_ylabel("W/m^2")
        print(f"{str(day):<12}{dd.td:>11.0f}{dd.cloudy_B * 100:>20.1f}%{dd.cloudy_C * 100:>26.1f}%{(dd.cloudy_C - dd.cloudy_B) * 100:>+11.1f}")
    axs[0].legend(fontsize=7, frameon=False, loc="upper left"); fig.suptitle("Non-sunny days against the clear-sky line: the dewpoint adjustment moves the line most on the driest and wettest days (grey = what the clouds took)", fontsize=10)
    fig.tight_layout(); fig.savefig(a.sheet); print("sheet ->", a.sheet)
    print(f"\nOver all {len(nc)} partly cloudy days (kmean 0.15-0.55): the adjustment changes the implied cloudiness by {np.mean(np.abs(nc.cloudy_C - nc.cloudy_B)) * 100:.1f} points on average, up to {np.max(np.abs(nc.cloudy_C - nc.cloudy_B)) * 100:.1f}.")
    for lo, hi in ((-20, 30), (30, 45), (45, 60), (60, 80)):
        g = nc[(nc.td >= lo) & (nc.td < hi)]
        if len(g): print(f"  dewpoint {lo:>3}-{hi:<3} F: {len(g):>3} days, mean change {(g.cloudy_C - g.cloudy_B).mean() * 100:+.1f} points")

if a.envelope:
    ENV_MIN = float(os.environ.get("ENV_MIN", "0.85"))
    cal_all, td0_all, b_all, _, _ = fit(u)
    u["kt_cal"] = u.kt / cal_all(u.el)                                     # clearness relative to the calibrated line (1 = on it)
    u["env"] = u.sort_values("time").groupby("day")["kt_cal"].transform(lambda x: x.rolling(7, center=True, min_periods=4).quantile(0.9))
    u["stretch"] = (u.kt_cal >= 0.95 * u.env) & (u.env >= ENV_MIN) & (u.el >= MIN_EL)              # on the upper envelope, and the envelope itself is near the clear-sky line
    sd = u.groupby("day").agg(env=("env", "median"), n=("el", "size")); sd = sd[sd.n >= 14]
    print("\nHow close does each day's smoothed upper envelope read to the calibrated clear-sky line? (all days, envelope median / line; dewpoint-adjusted line)")
    dmean = u.groupby("day").td.mean(); sd["td"] = dmean; sd["env_adj"] = sd.env / (1 + b_all * (sd.td - td0_all))
    for lab, col in (("no humidity", "env"), ("dewpoint-adjusted", "env_adj")):
        x = sd[col]; print(f"  {lab:<18} median {x.median():.3f}   within 5% of 1: {np.mean(np.abs(x - 1) <= .05):.0%}   within 10%: {np.mean(np.abs(x - 1) <= .10):.0%}   ({len(sd)} days)")
    hi = u[u.stretch].copy(); hi["rr"] = hi.kt_cal
    print(f"\nthe dewpoint fit from the clear STRETCHES of all days: {len(hi)} readings from {hi.day.nunique()} days (clear days only: {int((u.clear & (u.el >= MIN_EL)).sum())} readings from {int(days.clear.sum())} days)")
    p = np.polyfit(hi.td - td0_all, hi.rr, 1); print(f"  all seasons: {p[0] * 1000:+.2f} % per +10 F (clear days only: {b_all * 1000:+.2f})")
    for sname in ("winter", "spring", "summer", "autumn"):
        g = hi[hi.time.dt.month.map(seas) == sname]
        if len(g) > 30: print(f"  {sname:<7} {np.polyfit(g.td - td0_all, g.rr, 1)[0] * 1000:+6.2f} % per +10 F   ({len(g):>4} readings, {g.day.nunique():>3} days, dewpoint {g.td.min():.0f}-{g.td.max():.0f} F, corr {np.corrcoef(g.td, g.rr)[0, 1]:+.2f})")
