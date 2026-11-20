#!/usr/bin/env python3
"""Order of magnitude of the snow effect at the station:  python snow_effect.py AIRPORT   (the airport code, or HOME_AIRPORT in .env)
Snow nights (the airport reported snow in the previous 72 h and stayed below 40 F) against control nights (no snow for 7 days), winter 2025-26.
URMA at the station (AWS GRIB, free) and the station's own hourly data (Ambient, paged as StationComparisonScripts does)."""
import json, math, os, random, statistics as st, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c
from concurrent.futures import ThreadPoolExecutor

ap = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HOME_AIRPORT") or sys.exit("give the airport code (or set HOME_AIRPORT in .env)")
c.load_env()
cfg = c.load_config()
nights = json.load(open(c.LOG / f"snow_nights_{ap}.json"))
snow = [n["night"] for n in nights if n["kind"] == "snow"]
ctl = [n["night"] for n in nights if n["kind"] == "control"]
random.Random(7).shuffle(ctl)
ctl = [] if os.environ.get("SNOW_ONLY") else sorted(ctl[:30])      # SNOW_ONLY=1: just the snow nights; the autumn equation (fitted on no-snow nights) is the control
chosen = [(d, "snow") for d in snow] + [(d, "control") for d in ctl]
print(len(snow), "snow nights,", len(ctl), "control nights", flush=True)

f = c.LOG / f"snow_effect_{ap}.json"
have = json.loads(f.read_text()) if f.exists() else {"urma": {}, "obs": {}}
hours = []
for d, _ in chosen:
    day = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
    t = day + timedelta(hours=12)
    while t < day + timedelta(hours=40):
        if c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0 and t.isoformat() not in have["urma"]:
            hours.append(t)
        t += timedelta(hours=1)
print(len(hours), "URMA hours to fetch", flush=True)


def one(h):
    time.sleep(0.2)
    return h, c.grib_urma_hour(cfg, h)


n = 0
with ThreadPoolExecutor(3) as ex:
    for h, v in ex.map(one, hours):
        if v:
            have["urma"][h.isoformat()] = list(v)
        n += 1
        if n % 60 == 0:
            f.write_text(json.dumps(have)); print(f"URMA {n}/{len(hours)}", flush=True)
f.write_text(json.dumps(have))
for d, _ in chosen:
    day = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
    if (day + timedelta(hours=12)).isoformat() in have["obs"]:
        continue
    for k, v in c.ambient_history(day + timedelta(hours=12), day + timedelta(hours=40)).items():
        have["obs"][k.isoformat()] = v
    f.write_text(json.dumps(have))
print("data in", len(have["urma"]), "URMA hours,", len(have["obs"]), "station hours", flush=True)

fit = json.load(open(c.LOG / "fit.json"))
D = 1 + (cfg["rim_elevation_ft"] - cfg["valley_elevation_ft"]) / 3000
rows = []
for d, kind in chosen:
    day = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
    xs = []
    t = day + timedelta(hours=12)
    while t < day + timedelta(hours=40):
        a, o = have["urma"].get(t.isoformat()), have["obs"].get(t.isoformat())
        if a and o and o[2] is not None and c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0:
            drop = fit["max_deficit_f"] * D * math.exp(-(a[2] / fit["wind_u0"]) ** fit["wind_p"]) * (1 - fit["cloud_weight"] * a[1] / 100) * min(1.0, max(0.0, a[0] - o[2]) / fit["dep_scale"])
            xs.append((a[0] - o[0], a[1], a[2], o[1], drop, o[0]))
        t += timedelta(hours=1)
    if len(xs) >= 6:
        rows.append(dict(night=d, kind=kind, n=len(xs), deficit=st.mean(x[0] for x in xs), cloud=st.mean(x[1] for x in xs), uwind=st.mean(x[2] for x in xs), swind=st.mean(x[3] for x in xs),
                         modeled=st.mean(x[4] for x in xs), temp=st.mean(x[5] for x in xs), mintemp=min(x[5] for x in xs)))
json.dump(rows, open(c.LOG / f"snow_effect_{ap}_nights.json", "w"))
print(f"\n{'night':<12}{'kind':<9}{'hrs':>4}{'deficit':>9}{'modeled':>9}{'miss':>7}{'cloud%':>8}{'URMA wind':>11}{'stn wind':>10}{'station T':>11}")
for r in rows:
    print(f"{r['night']:<12}{r['kind']:<9}{r['n']:>4}{r['deficit']:>8.1f}F{r['modeled']:>8.1f}F{r['deficit'] - r['modeled']:>+6.1f}{r['cloud']:>8.0f}{r['uwind']:>11.1f}{r['swind']:>10.1f}{r['temp']:>10.0f}F")


def boot(vals, n=4000):
    rnd = random.Random(1)
    return sorted(st.mean(rnd.choice(vals) for _ in vals) for _ in range(n)) if vals else []


print("\nclear (night mean cloud < 30%) and calm (URMA wind < 8 mph) nights only:")
for kind in ("control", "snow"):
    g = [r for r in rows if r["kind"] == kind and r["cloud"] < 30 and r["uwind"] < 8]
    if g:
        b = boot([r["deficit"] for r in g]); bm = boot([r["deficit"] - r["modeled"] for r in g])
        print(f"  {kind:<8} n={len(g):>2} nights  deficit {st.mean(r['deficit'] for r in g):4.1f}F (90% range {b[int(len(b)*.05)]:.1f} to {b[int(len(b)*.95)]:.1f}) | what the autumn equation misses {st.mean(r['deficit'] - r['modeled'] for r in g):+.1f}F ({bm[int(len(bm)*.05)]:+.1f} to {bm[int(len(bm)*.95)]:+.1f})")
print("(the autumn equation was fitted on no-snow nights, so its miss on a snow night is the effect plus whatever winter itself does)")
print("all nights, any sky:")
for kind in ("control", "snow"):
    g = [r for r in rows if r["kind"] == kind]
    if g:
        print(f"  {kind:<8} n={len(g):>2} nights  deficit {st.mean(r['deficit'] for r in g):4.1f}F | misses {st.mean(r['deficit'] - r['modeled'] for r in g):+.1f}F")
