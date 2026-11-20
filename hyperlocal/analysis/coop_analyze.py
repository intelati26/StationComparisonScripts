#!/usr/bin/env python3
"""COOP stations (IEM) near the home place: the night's low (observer's minimum for the morning) against URMA's lowest night hour at the same point, by window, on calm-and-clear nights (home factor >= 0.5) and on the rest.
Needs log/coop_stations.json and log/coop_urma.json (coop_urma.py).   python coop_analyze.py"""
import csv, glob, io, json, math, os, sqlite3, statistics as st, sys, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date
import coldpool as c
import winter_tests as w

ST = json.load(open(c.LOG / "coop_stations.json"))
UR = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / "coop_urma.json")).items()}
import time
cache_f = c.LOG / "coop_daily.json"
raw = json.load(open(cache_f)) if cache_f.exists() else {}
for net in sorted({s[0] for s in ST}):
    if net in raw:
        continue
    ids = [s[1] for s in ST if s[0] == net]
    q = f"https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py?network={net}&" + "&".join(f"stations={i}" for i in ids) + "&sts=2026-01-01&ets=2026-11-19&format=csv"
    for wait in (0, 30, 60, 120, 240):
        time.sleep(wait)
        try:
            body = urllib.request.urlopen(q, timeout=120).read().decode(); break
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            print("IEM busy (429), waiting", flush=True)
    else:
        sys.exit("IEM kept answering 429; try again later")
    raw[net] = {f"{r['station']}|{r['day']}": float(r["min_temp_f"]) for r in csv.DictReader(io.StringIO(body)) if r["min_temp_f"] not in ("", "None")}
    json.dump(raw, open(cache_f, "w"))
    time.sleep(5)
daily = {tuple(k.split("|")): v for net in raw.values() for k, v in net.items()}
print(len(daily), "station-days of COOP minimum")
WIN = {"autumn": ("2026-10-15", "2026-11-15"), "jan": ("2026-01-08", "2026-01-21"), "feb": ("2026-02-01", "2026-02-28")}
# home factor per night: mean over the night hours (URMA cloud at home, station dewpoint, wind 450 m up)
fac = defaultdict(list)
for t, u in w.URMA.items():
    o, wd = w.OBS.get(t), w.W.get(t)
    if o and wd is not None and c.night_factor(t, w.cfg["lat"], w.cfg["lon"]) >= 1.0:
        fac[(t - timedelta(hours=13)).date()].append(math.exp(-(wd / 30) ** 3) * (1 - 0.8 * u[1] / 100) * min(1, max(0, u[0] - o[1]) / 12))
nightf = {d: st.mean(v) for d, v in fac.items() if len(v) >= 6}
night_hours = defaultdict(list)
for t in UR:
    night_hours[(t - timedelta(hours=13)).date()].append(t)
res = {g: {k: defaultdict(list) for k in ("calm", "other")} for g in WIN}
for g, (d0, d1) in WIN.items():
    e = date.fromisoformat(d0)
    while e <= date.fromisoformat(d1):
        hrs = night_hours.get(e, [])
        f = nightf.get(e)
        if len(hrs) >= 8 and f is not None:
            D = (e + timedelta(days=1)).isoformat()
            for i, s in enumerate(ST):
                cm = daily.get((s[1], D))
                if cm is None:
                    continue
                um = min(UR[h][i] for h in hrs)
                res[g]["calm" if f >= 0.5 else "other"][s[1]].append(um - cm)
        e += timedelta(days=1)
print(f"{'window':<8}{'nights calm/other':>20}{'stations':>10} | mean URMA-COOP low, calm & clear | other nights | share of stations with a positive deficit (calm)")
summ = {}
for g in WIN:
    cal = {k: st.mean(v) for k, v in res[g]["calm"].items() if len(v) >= 3}; oth = {k: st.mean(v) for k, v in res[g]["other"].items() if len(v) >= 3}
    nc = len({d for d in nightf if WIN[g][0] <= d.isoformat() <= WIN[g][1] and nightf[d] >= 0.5}); no = len({d for d in nightf if WIN[g][0] <= d.isoformat() <= WIN[g][1] and nightf[d] < 0.5})
    summ[g] = cal
    if cal:
        print(f"{g:<8}{nc:>10}/{no:<9}{len(cal):>10} | {st.mean(cal.values()):>+8.1f} (median {st.median(cal.values()):+.1f}, sd {st.pstdev(cal.values()):.1f})      | {(st.mean(oth.values()) if oth else float('nan')):>+6.1f}      | {sum(v > 0.5 for v in cal.values()) / len(cal):.0%}")
both = [k for k in summ["autumn"] if k in summ["jan"] or k in summ["feb"]]
for g in ("jan", "feb"):
    ks = [k for k in summ["autumn"] if k in summ[g]]
    if ks:
        d = [summ[g][k] - summ["autumn"][k] for k in ks]
        print(f"{g} minus autumn, per station on calm & clear nights: mean {st.mean(d):+.2f} F over {len(ks)} stations; stations with a smaller deficit in {g}: {sum(x < -0.3 for x in d)} of {len(ks)}")
