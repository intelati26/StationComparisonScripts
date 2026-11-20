#!/usr/bin/env python3
"""Isometric 3D picture of the lidar surface around a point (trees and roofs at their heights, coloured by height above the ground), with the point and optional shifted points marked. Coordinates only: LAT LON, or the place in .env.
  python skyview_3d.py [LAT LON] [--half 60] [--mark N_M E_M ...] [--out FILE.png] [--azim 225] [--elev 38]
The lidar grid is cached in log/skyview_grid.npz (git-ignored)."""
import argparse, os, sys
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LightSource
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
import skyview_lidar as S
ap = argparse.ArgumentParser(); ap.add_argument("lat", nargs="?", type=float); ap.add_argument("lon", nargs="?", type=float)
ap.add_argument("--half", type=float, default=60); ap.add_argument("--mark", type=float, nargs="*", default=[], help="pairs: north_m east_m of extra points to mark")
ap.add_argument("--out", default=os.path.join(HERE, "maps", "skyview_3d.png")); ap.add_argument("--azim", type=float, default=225); ap.add_argument("--elev", type=float, default=35.264)
a = ap.parse_args()
if a.lat is None:
    import coldpool as c; c.load_env(); cfg = c.load_config(); a.lat, a.lon = cfg["lat"], cfg["lon"]
cache = os.path.join(HERE, "log", "skyview_grid.npz")
if os.path.exists(cache): z = np.load(cache); dsm, dtm = z["dsm"], z["dtm"]
else:
    out = S.load(a.lat, a.lon, 150)
    if isinstance(out, dict): sys.exit(out["error"])
    dsm, dtm, meta = out; np.savez_compressed(cache, dsm=dsm, dtm=dtm); print("lidar:", meta["project"], meta["points_per_m2"], "points/m2")
n = dsm.shape[0]; c0 = n // 2; h = int(a.half)
sl = (slice(c0 - h, c0 + h), slice(c0 - h, c0 + h)); D = dsm[sl]; T = dtm[sl]; H = D - T
east, north = np.meshgrid(np.arange(-h, h) + 0.5, h - np.arange(0, 2 * h) - 0.5)       # row 0 is the north edge
from matplotlib.colors import LinearSegmentedColormap
CM = LinearSegmentedColormap.from_list('hag', ['#d9c9a3', '#c8d98a', '#6fbf5a', '#2e8b3d', '#0b4f2a'])
step = 1; fig = plt.figure(figsize=(12, 8.5), dpi=130); ax = fig.add_subplot(111, projection="3d")
ls = LightSource(azdeg=a.azim + 90, altdeg=45); rgb = ls.shade(np.clip(H, 0, 25), cmap=CM, vmin=0, vmax=22, blend_mode="soft")
ax.plot_surface(east[::step, ::step], north[::step, ::step], D[::step, ::step] - np.nanmin(T), rstride=1, cstride=1, facecolors=rgb[::step, ::step], linewidth=0, antialiased=False, shade=False, zorder=1)
zmax = float(np.nanmax(D) - np.nanmin(T))
def mark(dn, de, label, col, size=60):
    r, cc = int(round(h - dn)), int(round(h + de)); r = min(max(r, 0), 2 * h - 1); cc = min(max(cc, 0), 2 * h - 1)
    z0 = T[r, cc] - np.nanmin(T) + 1.5
    ax.plot([de, de], [dn, dn], [z0, z0 + 22], color=col, lw=3, zorder=20); ax.scatter([de], [dn], [z0 + 22], color=col, s=size, edgecolor="black", zorder=21, depthshade=False)
    ax.text(de, dn, z0 + 25, label, color=col, fontsize=9, ha="center", weight="bold", zorder=22)
mark(0, 0, "configured point", "#d62728")
for i in range(0, len(a.mark) - 1, 2): mark(a.mark[i], a.mark[i + 1], f"N{a.mark[i]:+.0f} E{a.mark[i+1]:+.0f}", "#1f4fd8")
ax.set_xlabel("east (m)"); ax.set_ylabel("north (m)"); ax.set_zlabel("height (m)"); ax.set_proj_type('ortho'); ax.view_init(elev=a.elev, azim=a.azim); ax.set_box_aspect((1, 1, 0.28))
ax.set_zlim(0, max(34, zmax + 8))
for lab, (x, y) in {"N": (0, h * 1.05), "E": (h * 1.05, 0), "S": (0, -h * 1.05), "W": (-h * 1.05, 0)}.items(): ax.text(x, y, 0, lab, fontsize=11, weight="bold", ha="center")
sm = plt.cm.ScalarMappable(cmap=CM, norm=plt.Normalize(0, 22)); fig.colorbar(sm, ax=ax, shrink=0.5, pad=0.02, label="height above the ground (m)")
ax.set_title(f"First-return surface from the USGS lidar, {2*h} m square, isometric view from the south-west (north is to the upper left, east to the upper right; red = the point in .env)", fontsize=10)
fig.tight_layout(); os.makedirs(os.path.dirname(a.out), exist_ok=True); fig.savefig(a.out); print("saved", a.out)
