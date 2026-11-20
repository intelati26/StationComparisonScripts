#!/usr/bin/env python3
"""A station's amplitude (the 'A' of EQUATION.md: how much colder than the model it gets on a clear, calm, dry night), from a few weeks of its own data.

  python amp.py [DAYS]                 the station in .env (Ambient API), the last DAYS (default 28) ending a day ago; the model is the HRRR's own 2 m temperature, cloud and wind from Open-Meteo
  python amp.py DAYS --db FILE         the hourly station data from a StationComparisonScripts database (station_obs, HYPERLOCAL_STATION) instead

For each night hour: model temperature - station temperature = a + A x factor, factor = exp(-(U/30)^3) x (1 - 0.8 cloud) x min(1, (model T - station dewpoint)/12), U the wind about 450 m above the ground.
Prints A with a range from resampling nights, the offset a, and the quick check (the mean difference on the clearest, calmest, driest hours).  Put the result in .env as HYPERLOCAL_AMP."""
import argparse, math, os, random, statistics as st, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

p = argparse.ArgumentParser()
p.add_argument("days", nargs="?", type=int, default=28)
p.add_argument("--db")
p.add_argument("--end", help="last day, YYYY-MM-DD (default: yesterday)")
a = p.parse_args()
c.load_env()
cfg = c.load_config()
end = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc) if a.end else (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
start = end - timedelta(days=a.days)
if a.db:
    import sqlite3
    db = sqlite3.connect(f"file:{os.path.abspath(a.db)}?mode=ro", uri=True)
    acc = {}
    for ts, t, dp in db.execute("SELECT timestamp, temperature, dewpoint FROM station_obs WHERE station=? AND temperature IS NOT NULL", (os.environ.get("HYPERLOCAL_STATION") or sys.exit("set HYPERLOCAL_STATION"),)):
        k = c.hour_of(ts)
        if start <= k <= end:
            x = acc.setdefault(k, [0.0, 0.0, 0, 0.0, 0]); x[0] += t; x[2] += 1
            if dp is not None:
                x[3] += dp; x[4] += 1
    obs = {k: (v[0] / v[2], 0.0, v[3] / v[4] if v[4] else None) for k, v in acc.items()}
else:
    obs = c.ambient_history(start, end)
ground, HR = c.hrrr_levels(cfg["lat"], cfg["lon"], start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), extra=("temperature_2m", "cloud_cover"))
rows = []
for t, o in sorted(obs.items()):
    m = HR.get(t)
    if m is None or o[2] is None or m["temperature_2m"] is None or m["cloud_cover"] is None or c.night_factor(t, cfg["lat"], cfg["lon"]) < 1.0:
        continue
    U = c.wind_agl(m, ground, 450.0)
    if U is None:
        continue
    f = math.exp(-(U / 30.0) ** 3) * (1 - 0.8 * m["cloud_cover"] / 100) * min(1.0, max(0.0, (m["temperature_2m"] - o[2]) / 12.0))
    rows.append(((t - timedelta(hours=13)).date(), f, m["temperature_2m"] - o[0]))
nights = sorted({r[0] for r in rows})
print(f"{len(rows)} night hours on {len(nights)} nights ({start:%d %b} to {end:%d %b})")
if len(rows) < 60 or len(nights) < 5:
    sys.exit("too little data: use more days (the clear, calm nights are what carry the amplitude)")


def fit(rs):
    xs = [r[1] for r in rs]; ys = [r[2] for r in rs]; mx, my = st.mean(xs), st.mean(ys); sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-6:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return b, my - b * mx


b, off = fit(rows)
rnd = random.Random(1)
bs = []
for _ in range(400):
    pick = [rnd.choice(nights) for _ in nights]
    rs = [r for d in pick for r in by[d]] if (by := {d: [r for r in rows if r[0] == d] for d in nights}) else []
    f_ = fit(rs)
    if f_:
        bs.append(f_[0])
bs.sort()
good = [r[2] for r in rows if r[1] >= 0.6]
print(f"amplitude A = {b:.1f} F   (90% range {bs[int(len(bs) * .05)]:.1f} to {bs[int(len(bs) * .95)]:.1f}, from resampling the nights);  offset a = {off:+.1f} F")
print(f"quick check: on the {len(good)} hours with a weather factor of 0.6 or more the model is {st.mean(good):.1f} F warmer than the station on average" if good else "quick check: no clear, calm, dry hours in this window")
print("for reference: 29 stations in four lowland places gave a median of 3.9 F (mean 4.5, range -1 to 13); this station's earlier fit against URMA: 10")
print("HYPERLOCAL_AMP=%.0f" % max(0.0, round(b)))
