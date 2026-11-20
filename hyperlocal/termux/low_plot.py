#!/usr/bin/env python3
"""A picture of tonight's low: the forecast temperature, the equation's adjusted temperature, the +- band around the predicted low, the wind aloft and the hourly drop. Needs matplotlib (Termux: pkg install matplotlib), nothing else; it runs low.py --json beside it, so the place, amplitude, bias and SD come from the same .env and options.
  python low_plot.py [OUT.png] [any low.py options]        default ~/low.png (or ./low.png off Termux)"""
import json, os, subprocess, sys
from datetime import datetime, timedelta, timezone
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, matplotlib.dates as md
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]; out = args.pop(0) if args and args[0].lower().endswith(".png") else os.path.join(os.path.expanduser("~") if os.path.isdir("/data/data/com.termux") else ".", "low.png")
r = subprocess.run([sys.executable, os.path.join(here, "low.py"), "--json"] + args, capture_output=True, text=True)
if r.returncode:
    sys.exit(r.stderr.strip() or "low.py failed")
d = json.loads(r.stdout); tz = timezone(timedelta(seconds=d["utc_offset_seconds"]))
H = d["hours"]; t = [datetime.fromisoformat(h["utc"].replace("Z", "+00:00")).astimezone(tz) for h in H]
fc = [h["forecast"] for h in H]; fin = [h["final"] + d["bias"] for h in H]
low_t = datetime.fromisoformat(d["low_utc"].replace("Z", "+00:00")).astimezone(tz)
dark = "#14111f"; fg = "#e8e6f0"
fig, ax = plt.subplots(3, 1, figsize=(7.2, 6.4), dpi=130, sharex=True, gridspec_kw={"height_ratios": [3, 1.1, 1.1]}, facecolor=dark)
for a in ax:
    a.set_facecolor(dark); a.tick_params(colors=fg, labelsize=8); [s.set_color("#59546e") for s in a.spines.values()]; a.grid(alpha=0.2)
a = ax[0]
a.plot(t, fc, color="#8f8aa6", lw=1.6, ls="--", label=f"{d['source']} forecast (low {d['forecast_low']:.0f} F)")
a.plot(t, fin, color="#3ddc97", lw=2.2, label=f"adjusted by the equation (low {d['low']:.0f} F)")
if d["sd"]:
    a.fill_between(t, [v - d["sd"] for v in fin], [v + d["sd"] for v in fin], color="#3ddc97", alpha=0.15, label=f"+-{d['sd']:g} F")
    a.fill_between(t, [v - 2 * d["sd"] for v in fin], [v + 2 * d["sd"] for v in fin], color="#3ddc97", alpha=0.07, label=f"+-{2*d['sd']:g} F")
a.scatter([low_t], [d["low"]], marker="*", s=220, color="#ffd23f", edgecolor="black", zorder=5)
a.annotate(f"{d['low']:.0f} F", (low_t, d["low"]), textcoords="offset points", xytext=(8, -14), color="#ffd23f", fontsize=10, weight="bold")
if 32 >= min(fin) - 2 * d["sd"]: a.axhline(32, color="#6ec6ff", lw=0.8, ls=":")
a.set_ylabel("temperature (F)", color=fg, fontsize=9); a.legend(fontsize=7.5, frameon=False, labelcolor=fg, loc="upper right")
a.set_title(f"Tonight's low {d['low']:.0f} F +-{d['sd']:g}  (forecast {d['forecast_low']:.0f} F)", color=fg, fontsize=11)
ax[1].plot(t, [h["wind_aloft"] for h in H], color="#ff9f6e", lw=1.8); ax[1].axhline(30, color="#ff9f6e", lw=0.8, ls=":")
ax[1].set_ylabel("wind 450 m\n(mph)", color=fg, fontsize=8)
ax[2].bar(t, [h["drop"] for h in H], width=0.03, color="#3ddc97"); ax[2].set_ylabel("drop (F)", color=fg, fontsize=8)
ax[2].xaxis.set_major_formatter(md.DateFormatter("%-I%p", tz=tz)); ax[2].xaxis.set_major_locator(md.HourLocator(interval=2, tz=tz))
fig.tight_layout(); fig.savefig(out, facecolor=dark); print(out)
