#!/usr/bin/env python3
"""A plotter-style picture of tonight's low: the station's recent readings, the HRRR forecast, the equation's temperature with the bias and SD band, the predicted low, and underneath the wind at 450 m and the equation's drop.
It runs termux/low.py --json (so it uses the same place, amplitude and options from .env) and reads the station's last hours from the Ambient API through coldpool.py.
   python plot_low.py [--back 18] [--png] [any low.py option, e.g. --amp 10 --nws]       -> log/low_plot_<time>.svg (and .png)"""
import argparse, json, os, subprocess, sys
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import matplotlib; matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import coldpool as c

ap = argparse.ArgumentParser(add_help=True)
ap.add_argument("--back", type=float, default=18.0, help="hours of the station's readings before now (default 18)")
ap.add_argument("--png", action="store_true", help="also write a PNG")
ap.add_argument("--out", default=None, help="the SVG path (default log/low_plot_<time>.svg)")
a, rest = ap.parse_known_args()
j = json.loads(subprocess.run([sys.executable, os.path.join(HERE, "termux", "low.py"), "--json"] + rest, capture_output=True, text=True, check=True).stdout)
off = timedelta(seconds=j["utc_offset_seconds"])
loc = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ") + off                 # local, naive
H = j["hours"]
T = [loc(h["utc"]) for h in H]
now = datetime.now(timezone.utc)
c.load_env()
try:
    st = c.ambient_history(now - timedelta(hours=a.back), now)
    S = sorted((t.replace(tzinfo=None) + off, v[0]) for t, v in st.items())
except SystemExit as e:
    S = []; print("station readings unavailable:", e)
fig, ax = plt.subplots(3, 1, figsize=(12, 9), dpi=130, sharex=True, gridspec_kw={"height_ratios": [3, 1, 1]})
t0, t1 = (S[0][0] if S else T[0]) - timedelta(hours=1), T[-1] + timedelta(hours=1)
for x in ax:
    x.axvspan(T[0], T[-1], color="#6250d6", alpha=0.06, zorder=0); x.grid(alpha=0.25)
    x.axvline(now.replace(tzinfo=None) + off, color="red", lw=1.2, alpha=0.6, zorder=2)
top = ax[0]
if S:
    top.plot([t for t, _ in S], [v for _, v in S], color="crimson", lw=1.6, label="station (hourly mean)")
top.plot(T, [h["forecast"] for h in H], color="purple", ls="--", lw=1.4, marker="o", ms=3, alpha=0.85, label=f"{j['source']} forecast")
fin = [h["final"] for h in H]
top.fill_between(T, [f + j["bias"] - j["sd"] for f in fin], [f + j["bias"] + j["sd"] for f in fin], color="seagreen", alpha=0.16, zorder=1, label=f"equation {j['bias']:+g} F, +-{j['sd']:g} F")
top.plot(T, fin, color="seagreen", lw=1.4, ls=":", alpha=0.9, label="equation (raw)")
top.plot(T, [f + j["bias"] for f in fin], color="seagreen", lw=2.2, marker="o", ms=3, label="equation, bias applied")
lt = loc(j["low_utc"])
top.errorbar([lt], [j["low"]], yerr=[j["sd"]], fmt="*", color="darkgreen", ms=15, capsize=5, zorder=5, label=f"predicted low {j['low']:.0f} F +-{j['sd']:g}")
top.axhline(j["forecast_low"], color="purple", lw=0.8, ls=":", alpha=0.6)
top.annotate(f"{j['low']:.0f} F", (lt, j["low"] - j["sd"]), textcoords="offset points", xytext=(0, -16), ha="center", fontsize=10, color="darkgreen", weight="bold")
top.set_ylabel("temperature (F)"); top.legend(loc="upper right", fontsize=8, ncol=2, frameon=True)
top.set_title(f"Tonight's low {j['low']:.0f} F +-{j['sd']:g}  (equation {j['equation_low']:.0f} F, bias {j['bias']:+g}; forecast low {j['forecast_low']:.0f} F, rule {j['rule']:.0f} F)   cloud {j['cloud_mean']}%, wind aloft up to {j['wind_aloft_max']} mph", fontsize=10)
ax[1].plot(T, [h["wind_aloft"] for h in H], color="#2a78d6", lw=1.8, marker="o", ms=3)
ax[1].axhline(30, color="gray", ls="--", lw=1); ax[1].text(t0, 30.5, " 30 mph: the drop is nearly gone", fontsize=8, color="gray", va="bottom")
ax[1].set_ylabel("wind 450 m\nup (mph)")
ax[2].bar(T, [h["drop"] for h in H], width=0.03, color="#d95926", alpha=0.85); ax[2].set_ylabel("equation's\ndrop (F)")
ax[2].set_xlim(t0, t1); ax[2].xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M")); ax[2].xaxis.set_major_locator(mdates.HourLocator(interval=3))
ax[2].set_xlabel("local time (shaded: the night hours the equation covers; red line: now)")
fig.autofmt_xdate(); fig.tight_layout()
out = a.out or os.path.join(HERE, "log", f"low_plot_{datetime.now():%Y%m%d_%H%M}.svg")
os.makedirs(os.path.dirname(out), exist_ok=True)
fig.savefig(out, bbox_inches="tight")
if a.png:
    fig.savefig(out[:-4] + ".png", bbox_inches="tight")
print("saved", out)
