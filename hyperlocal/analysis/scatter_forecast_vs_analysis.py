#!/usr/bin/env python3
"""Predicted night low against the station's actual low, one dot per night: the equation on a real forecast (NBS) next to the equation on analysis inputs. Needs log/nbs_nights.json (nbs_score.py).
   python scatter_forecast_vs_analysis.py [OUT.png]"""
import json, os, statistics as st, sys
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
rows = json.load(open(os.path.join(HERE, "log", "nbs_nights.json")))
SEAS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
COL = {"winter": "#6250d6", "spring": "#1baf7a", "summer": "#eda100", "autumn": "#d95926"}
fig, ax = plt.subplots(1, 2, figsize=(13, 6.2), dpi=130, sharex=True, sharey=True)
lo, hi = min(r["actual"] for r in rows) - 4, max(r["actual"] for r in rows) + 4
for a, key, title in ((ax[0], "fall", "Equation on the NBS forecast (dewpoint falling 0.3 F/h)"), (ax[1], "ana", "Equation on analysis inputs (URMA, actual dewpoint)")):
    e = [r[key] for r in rows]; m, sd, mae = st.mean(e), st.pstdev(e), st.mean(abs(x) for x in e)
    SD = 2.8 if key == "fall" else sd   # the notification's SD on the forecast panel; the measured SD on the analysis panel
    n1 = sum(abs(x) <= SD for x in e); n2 = sum(abs(x) <= 2 * SD for x in e)
    a.fill_between([lo, hi], [lo - SD, hi - SD], [lo + SD, hi + SD], color="gray", alpha=0.16, zorder=0, label=f"+-1 SD = {SD:.1f} F: {n1}/{len(e)} nights ({100*n1/len(e):.0f}%)")
    for sgn in (1, -1):
        a.plot([lo, hi], [lo + sgn * 2 * SD, hi + sgn * 2 * SD], color="gray", lw=1, ls="--", zorder=0, label=f"+-2 SD = {2*SD:.1f} F: {n2}/{len(e)} ({100*n2/len(e):.0f}%)" if sgn == 1 else None)
    a.plot([lo, hi], [lo, hi], color="black", lw=1, zorder=1)
    if key == "fall":
        a.scatter([r["actual"] for r in rows], [r["actual"] + r["nbs"] for r in rows], s=22, facecolors="none", edgecolors="#999", linewidths=0.8, zorder=2, label="the NBS low alone")
    for s, c in COL.items():
        rs = [r for r in rows if SEAS[r["mo"]] == s]
        a.scatter([r["actual"] for r in rs], [r["actual"] + r[key] for r in rs], s=26, color=c, alpha=0.85, edgecolor="white", linewidths=0.4, zorder=3, label=f"{s} ({len(rs)})")
    a.set_title(f"{title}\n{len(rows)} nights: typical miss {mae:.1f} F, mean error {m:+.1f} F", fontsize=10)
    a.set_xlabel("the station's actual night low (F)"); a.grid(alpha=0.25); a.set_xlim(lo, hi); a.set_ylim(lo, hi); a.set_aspect("equal")
    a.legend(loc="upper left", fontsize=8, frameon=False)
ax[0].set_ylabel("predicted night low (F)")
fig.suptitle("Predicted against actual night low, Feb 2025 - Nov 2026 (black line = a perfect prediction, grey = bias 0 with +-1 and +-2 SD)", fontsize=11)
fig.tight_layout(); out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "maps", "forecast_vs_analysis_scatter.png"); fig.savefig(out); print("saved", out)
