#!/usr/bin/env python3
"""Does the sky view from lidar (skyview_lidar.py) explain the stations' amplitudes?  36 Wunderground stations: amplitude b and offset a (station_amps.py), coordinates from log/wu_nearby*.json.
  python sky_regress.py --run        lidar sky view for each station (sky view over +-10 m shifts of the point: the coordinates are only good to that) -> log/sky_stations.json, resumable, about 30 s each
  python sky_regress.py              the regression report (and maps/sky_regress.png)"""
import glob, json, os, statistics as st, sys, time
import numpy as np
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
import coldpool as c
import skyview_lidar as S
UNDER = "--under" in sys.argv          # keep shifted points that fall under a crown (a sensor can sit under trees); the default skips points under 2 m+ of canopy or a roof
OUT = c.LOG / ("sky_stations_under.json" if UNDER else "sky_stations.json"); GRID = c.LOG / "skyview_grids"
amps = {r["sid"]: r for r in json.load(open(c.LOG / "station_amps.json"))}
coords = {}
for f in glob.glob(str(c.LOG / "wu_nearby*.json")):
    for d, sid, city, la, lo, *_ in json.load(open(f))["stations"]: coords[sid] = (la, lo)
home = json.load(open(c.LOG / "home_stations.json")); cfg = c.load_config()
if home.get("home_wu_id") and home["home_wu_id"] in amps: coords.setdefault(home["home_wu_id"], coords.get(home["home_wu_id"]))
res = json.load(open(OUT)) if OUT.exists() else {}
if "--run" in sys.argv:
    os.makedirs(GRID, exist_ok=True)
    for sid, r in amps.items():
        if sid in res or sid not in coords or coords[sid] is None: continue
        la, lo = coords[sid]; f = GRID / f"{sid}.npz"
        try:
            if f.exists(): z = np.load(f); dsm, dtm = z["dsm"], z["dtm"]; meta = {"project": str(z["project"])}
            else:
                out = S.load(la, lo, 130)
                if isinstance(out, dict): res[sid] = {"error": out["error"]}; OUT.write_text(json.dumps(res)); print(sid, out["error"], flush=True); continue
                dsm, dtm, meta = out; np.savez_compressed(f, dsm=dsm, dtm=dtm, project=meta["project"])
            sv = []; qs = []; n = dsm.shape[0]
            for dn in (-10, -5, 0, 5, 10):
                for de in (-10, -5, 0, 5, 10):
                    cy, cx = int(round(n // 2 - dn)), int(round(n // 2 + de))
                    if not UNDER and dsm[cy, cx] - dtm[cy, cx] > 2.0: continue          # a sensor is not on a roof or in a crown
                    v = S.look(dsm, dtm, 1.5, 100, dn, de); sv.append(v["svf_cos2"]); qs.append(v["mean_horizon_deg"])
            if not sv: res[sid] = {"error": "every shifted point is under a roof or crown"}; OUT.write_text(json.dumps(res)); print(sid, res[sid]["error"], flush=True); continue
            v0 = S.look(dsm, dtm, 1.5, 100)
            res[sid] = {"project": meta["project"], "svf0": v0["svf_cos2"], "svf_med": float(np.median(sv)), "svf_min": min(sv), "svf_max": max(sv), "hor_med": float(np.median(qs)), "obst0": v0["obstruction_height_mean_m"], "sectors0": v0["sector_horizon_deg"]}
        except Exception as e: res[sid] = {"error": repr(e)[:120]}
        OUT.write_text(json.dumps(res)); print(sid, res[sid].get("svf_med", res[sid].get("error")), flush=True)
    sys.exit()
print('mode:', 'under-crown points kept' if UNDER else 'under-crown points skipped')
rows = [(amps[s], v) for s, v in res.items() if "svf_med" in v and s in amps]
print(len(rows), "stations with lidar of", len(amps), "; errors:", sum("error" in v for v in res.values()))
def corr(x, y): return float(np.corrcoef(x, y)[0, 1]) if len(x) > 2 else float("nan")
print(f"\n{'':<10}{'n':>3} | corr(b, svf)  slope (F of b per 0.1 svf) | corr(a, svf) | svf mean (range of the +-10 m scatter)")
for place in [None] + sorted({r["place"] for r, v in rows}):
    rs = [(r, v) for r, v in rows if place is None or r["place"] == place]
    if len(rs) < 4: continue
    b = np.array([r["b"] for r, v in rs]); a = np.array([r["a"] for r, v in rs]); sv = np.array([v["svf_med"] for r, v in rs])
    if place is None:                                    # pooled, centred within each place
        for pl in {r["place"] for r, v in rs}:
            idx = [i for i, (r, v) in enumerate(rs) if r["place"] == pl]; b[idx] -= b[idx].mean(); a[idx] -= a[idx].mean(); sv[idx] -= sv[idx].mean()
    sl = float(np.polyfit(sv, b, 1)[0]) * 0.1 if sv.std() > 0 else float("nan")
    print(f"{place or 'pooled*':<10}{len(rs):>3} | {corr(b, sv):>+8.2f}   {sl:>+8.2f}                 | {corr(a, sv):>+8.2f}   | {np.mean([v['svf_med'] for r, v in rs]):.2f} ({np.mean([v['svf_max']-v['svf_min'] for r, v in rs]):.2f} typical scatter)")
print("(* pooled = each place centred on its own mean)")
