#!/usr/bin/env python3
"""Which quantity carries the 'coupling' (decoupling) effect best? Variants of exp(-(x/x0)^3), A refitted, nightly-low MAE per window, pooled over the home station's windows:
  U450 (current) | HRRR 10 m gust | shear U450 - U10 | Richardson-style shear/sqrt(inversion) | the station's own hourly gust (diagnostic, not forecastable)
   python shear_test.py"""
import glob, json, math, os, sqlite3, statistics as st, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
import coldpool as c
import winter_tests as w

cfg = c.load_config()
WIN = {"autumn": ("2026-10-01", "2026-11-18"), "jan": ("2026-01-08", "2026-01-21"), "feb": ("2026-02-01", "2026-02-28"), "mar": ("2026-03-08", "2026-03-21"), "apr": ("2026-04-01", "2026-04-30"),
       "may": ("2026-05-08", "2026-05-21"), "jun": ("2026-06-17", "2026-06-30"), "jul": ("2026-07-01", "2026-07-31"), "aug": ("2026-08-08", "2026-08-21"), "sep": ("2026-09-08", "2026-09-21")}
EXTRA = ("wind_gusts_10m", "temperature_2m", "temperature_925hPa")
X = {}                                           # hour -> dict(gust, t2, t925, w10, w450)
for wn, (d0, d1) in WIN.items():
    f = c.LOG / f"shear_home_{wn}.json"
    if f.exists():
        X.update({datetime.fromisoformat(k): v for k, v in json.load(open(f)).items()}); continue
    for _ in range(8):
        try:
            ground, HR = c.hrrr_levels(cfg["lat"], cfg["lon"], d0, (datetime.fromisoformat(d1) + timedelta(days=1)).date().isoformat(), extra=EXTRA); break
        except SystemExit:
            print("Open-Meteo busy, waiting 10 min", flush=True); time.sleep(600)
    d = {t: dict(gust=r["wind_gusts_10m"], t2=r["temperature_2m"], t925=r["temperature_925hPa"], w10=r["w10"], w450=c.wind_agl(r, ground, 450.0)) for t, r in HR.items()}
    json.dump({k.isoformat(): v for k, v in d.items()}, open(f, "w")); X.update(d); print("fetched", wn, flush=True)
SG = {}                                          # station hourly max gust
for t, g in sqlite3.connect(c.LOG / "ambient_all.db").execute("SELECT utc, windgustmph FROM ambient WHERE windgustmph IS NOT NULL"):
    h = datetime.fromisoformat(t.replace("Z", "+00:00")).replace(minute=0, second=0); SG[h] = max(SG.get(h, 0), g)
GROUP = {"2026-10": "autumn", "2026-11": "autumn", "2026-01": "jan", "2026-02": "feb", "2026-03": "mar", "2026-04": "apr", "2026-05": "may", "2026-06": "jun", "2026-07": "jul", "2026-08": "aug", "2026-09": "sep"}
nights = {}
for t, u in sorted(w.URMA.items()):
    g = GROUP.get(f"{t.year}-{t.month:02d}"); o = w.OBS.get(t); x = X.get(t)
    if not g or not o or not x or x["w450"] is None or x["gust"] is None or t not in SG or c.night_factor(t, cfg["lat"], cfg["lon"]) < 1.0:
        continue
    if g == "jun" and t.day < 17 or g == "aug" and not 8 <= t.day <= 21 or g == "sep" and not 8 <= t.day <= 21 or g == "may" and not 8 <= t.day <= 21 or g == "mar" and not 8 <= t.day <= 21:
        continue
    nights.setdefault((g, (t - timedelta(hours=13)).date()), []).append((t, u, o, x))
nights = {k: v for k, v in nights.items() if len(v) >= 6}
print(len(nights), "nights over", len({k[0] for k in nights}), "windows", flush=True)


def coupling(kind, x0, u, o, x, t):
    if kind == "u450": q = x["w450"]
    elif kind == "gust": q = x["gust"]
    elif kind == "shear": q = max(0.0, x["w450"] - x["w10"])
    elif kind == "sgust": q = SG[t]
    elif kind == "ri":
        dth = (x["t925"] + 459.67) / 1.8 * (1000 / 925) ** 0.286 - (x["t2"] + 459.67) / 1.8 * (1000 / 975) ** 0.286      # K, 925 hPa potential temperature minus the 2 m one (surface about 975 hPa)
        dU = max(0.0, x["w450"] - x["w10"]) * 0.44704
        q = dU / math.sqrt(max(dth, 0.5) + 1.0)                                                                    # m/s per sqrt(K); large when shear is strong against a weak inversion
        q *= 2.237
    return math.exp(-(q / x0) ** 3)


def score(kind, x0, A):
    out = {}
    for (g, d), v in nights.items():
        e = []
        for t, u, o, x in v:
            f = coupling(kind, x0, u, o, x, t) * (1 - 0.8 * u[1] / 100) * min(1.0, max(0.0, u[0] - o[1]) / 12.0)
            e.append((u[0] - A * f, o[0]))
        out.setdefault(g, []).append(min(a for a, b in e) - min(b for a, b in e))
    return out


GRID = {"u450": range(24, 40, 2), "gust": range(14, 44, 3), "shear": range(14, 40, 3), "sgust": range(2, 20, 2), "ri": range(8, 40, 4)}
print(f"{'variant':<30}{'x0':>4}{'A':>6} | pooled | " + " ".join(f"{g:>5}" for g in WIN))
for kind in ("u450", "gust", "shear", "ri", "sgust"):
    best = None
    for x0 in GRID[kind]:
        for A in [a / 2 for a in range(14, 33)]:
            r = score(kind, x0, A); m = st.mean(abs(v) for vs in r.values() for v in vs)
            if best is None or m < best[0]:
                best = (m, x0, A, r)
    m, x0, A, r = best
    label = {"u450": "wind at 450 m (current)", "gust": "HRRR 10 m gust", "shear": "shear U450 - U10", "ri": "shear / sqrt(inversion)", "sgust": "station's own gust (diagnostic)"}[kind]
    print(f"{label:<30}{x0:>4}{A:>6.1f} | {m:.3f}  | " + " ".join(f"{st.mean(abs(v) for v in r[g]):>5.2f}" if g in r else "    -" for g in WIN), flush=True)
