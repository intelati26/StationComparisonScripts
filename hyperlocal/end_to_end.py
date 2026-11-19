#!/usr/bin/env python3
"""The equation on the inputs a forecast really has (the 22Z NBM run for temperature, dewpoint, cloud; the 22Z HRRR run for the 925 hPa wind), scored on the night's lowest hourly temperature at the station.
  python end_to_end.py"""
import json, math, os, statistics as st, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coldpool as c

cfg = c.load_config()
obs, mod = c.load_db(cfg)
c.merge_cache(obs, mod)
hr = json.load(open(c.LOG / "hrrr925_run22.json"))
nb = json.load(open(c.LOG / "nbm_run22.json"))
fc = dict(cfg, **c.FORECAST_CFG)
nights = {}
for k in sorted(set(obs) & set(mod)):
    if c.night_factor(k, cfg["lat"], cfg["lon"]) >= 1.0 and len(obs[k]) > 2 and obs[k][2] is not None:
        nights.setdefault((k - timedelta(hours=13)).date(), []).append(k)
rows = []
for d, ks in nights.items():
    key = f"{d}T22"
    if len(ks) < 9 or key not in hr or key not in nb:
        continue
    r0 = datetime(d.year, d.month, d.day, 22, tzinfo=timezone.utc)
    lead = {k: str(int((k - r0).total_seconds() // 3600)) for k in ks}
    if any(lead[k] not in hr[key] or lead[k] not in nb[key] for k in ks):
        continue
    u = {k: hr[key][lead[k]] for k in ks}
    T = {k: nb[key][lead[k]][0] for k in ks}; Td = {k: nb[key][lead[k]][1] for k in ks}; W10 = {k: nb[key][lead[k]][2] for k in ks}; C = {k: nb[key][lead[k]][3] for k in ks}
    rows.append((d, ks, u, T, Td, W10, C))
print(len(rows), "nights with every input")
actual = lambda ks: min(obs[k][0] for k in ks)


def low(ks, u, T, cloud, td_of, wind=None):
    preds = []
    for i, k in enumerate(ks):
        drop = c.model(fc, {"t": k, "cloud": cloud[k], "wind_mph": (wind or u)[k], "dep": T[k] - td_of(i, k)}, 0.2)[0]
        preds.append(T[k] - drop)
    return min(preds)


variants = {}
def add(name, fn):
    variants[name] = [fn(*r) - actual(r[1]) for r in rows]
add("NBM low alone (the forecast)", lambda d, ks, u, T, Td, W, C: min(T.values()))
add("equation: NBM T and cloud, 925 wind, evening dewpoint held", lambda d, ks, u, T, Td, W, C: low(ks, u, T, C, lambda i, k: obs[ks[0]][2]))
add("equation: ... dewpoint falling 0.3 F/h", lambda d, ks, u, T, Td, W, C: low(ks, u, T, C, lambda i, k: obs[ks[0]][2] - 0.3 * i))
add("equation: ... NBM hourly dewpoint", lambda d, ks, u, T, Td, W, C: low(ks, u, T, C, lambda i, k: Td[k]))
add("equation: ... NBM dewpoint shifted to the station's evening", lambda d, ks, u, T, Td, W, C: low(ks, u, T, C, lambda i, k: Td[k] + (obs[ks[0]][2] - Td[ks[0]])))
add("equation: NBM 10 m wind (threshold 10 mph) instead of 925", lambda d, ks, u, T, Td, W, C: min(T[k] - c.model(dict(fc, wind_u0=10, max_deficit_f=9), {"t": k, "cloud": C[k], "wind_mph": W[k], "dep": T[k] - (obs[ks[0]][2] - 0.3 * i)}, 0.2)[0] for i, k in enumerate(ks)))
add("equation with the analysis inputs (URMA T and cloud, actual hourly dewpoint)", lambda d, ks, u, T, Td, W, C: min(mod[k][0] - c.model(fc, {"t": k, "cloud": mod[k][1], "wind_mph": u[k], "dep": mod[k][0] - obs[k][2]}, 0.2)[0] for k in ks))


def mental(d, ks, u, T, Td, W, C):
    nws = min(T.values()); cl = st.mean(C.values()); um = max(u.values()); td = obs[ks[0]][2]
    return min(nws, td) if (cl < 20 and um < 20) else (nws - 1 if (um >= 30 or cl >= 60) else nws - 3)
add("mental rule (NBM low, cloud, 925 wind, evening dewpoint)", mental)
print(f"\n{'prediction of the night low, scored against the station':<82}{'MAE':>6}{'bias':>7}{'rmse':>7}")
for n, e in variants.items():
    print(f"{n:<82}{st.mean(abs(x) for x in e):>5.1f}F{st.mean(e):>+6.1f}{math.sqrt(st.mean(x * x for x in e)):>6.1f}F")
# the forecast inputs themselves
tn = [T_ - mod[k][0] for d, ks, u, T, Td, W, C in rows for k in ks for T_ in [T[k]]]
print(f"\nNBM temperature minus URMA at the night hours: mean {st.mean(tn):+.1f}F, rmse {math.sqrt(st.mean(x * x for x in tn)):.1f}F;  NBM dewpoint minus the station's: mean {st.mean(Td[k] - obs[k][2] for d, ks, u, T, Td, W, C in rows for k in ks):+.1f}F")
print(f"NBM cloud minus URMA cloud: mean {st.mean(C[k] - mod[k][1] for d, ks, u, T, Td, W, C in rows for k in ks):+.0f} points")
