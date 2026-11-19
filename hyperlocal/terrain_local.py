#!/usr/bin/env python3
"""Small-scale terrain position of a point (needs numpy): how much it sits in a hollow, near a channel, with ground draining toward it.

  python terrain_local.py LAT LON        prints the indices (3DEP 1/3 arc-second, whole 1-degree tiles only are requested)

  tpi_R     the point's height minus the mean height within R metres (negative: in a hollow)
  above_min_R   height above the lowest ground within R metres
  rel_R     (height - lowest) / (highest - lowest) within R metres, 0 = at the bottom, 1 = on top
  upslope_ha    area draining to the point (D8 flow on the 24 m grid, hectares); chan_m the distance to ground draining 5 ha or more
"""
import math, sys
import numpy as np
import terrain as T

FT = T.FT


def grid(lat, lon, half=0.05):
    rows, hdr = T.window(lat, lon, half=half, source="3dep")
    z = np.array(rows, dtype=float)
    z[z < -1000] = np.nan
    cs = hdr["cellsize"]
    nr, nc = z.shape
    ky, kx = 110574.0, 111320.0 * math.cos(math.radians(lat))          # metres per degree
    # row 0 is the north edge
    ys = (hdr["yllcorner"] + (nr - np.arange(nr) - 0.5) * cs - lat) * ky
    xs = (hdr["xllcorner"] + (np.arange(nc) + 0.5) * cs - lon) * kx
    return z, xs, ys, cs * kx, cs * ky


def indices(lat, lon):
    z, xs, ys, dx, dy = grid(lat, lon)
    X, Y = np.meshgrid(xs, ys)
    D = np.hypot(X, Y)
    r0, c0 = np.unravel_index(np.nanargmin(D), D.shape)
    z0 = z[r0, c0]
    out = {"elev_ft": round(z0 * FT)}
    for R in (100, 250, 500, 1000):
        sel = (D <= R) & ~np.isnan(z)
        zz = z[sel]
        out[f"tpi_{R}_ft"] = round((z0 - zz.mean()) * FT, 1)
        out[f"above_min_{R}_ft"] = round((z0 - zz.min()) * FT, 1)
        out[f"rel_{R}"] = round(float((z0 - zz.min()) / max(zz.max() - zz.min(), 0.1)), 2)
    # D8 flow accumulation on a 24 m grid (every 3rd cell, block mean)
    k = 3
    nr, nc = (z.shape[0] // k) * k, (z.shape[1] // k) * k
    zb = np.nanmean(z[:nr, :nc].reshape(nr // k, k, nc // k, k), axis=(1, 3))
    Xb = X[:nr, :nc].reshape(nr // k, k, nc // k, k).mean(axis=(1, 3))
    Yb = Y[:nr, :nc].reshape(nr // k, k, nc // k, k).mean(axis=(1, 3))
    h, w = zb.shape
    acc = np.ones((h, w))
    order = np.argsort(-np.nan_to_num(zb, nan=-1e9), axis=None)
    cell_ha = (dx * k) * (dy * k) / 1e4
    for idx in order:
        r, c = divmod(int(idx), w)
        if np.isnan(zb[r, c]):
            continue
        best, bz = None, zb[r, c]
        for rr in (r - 1, r, r + 1):
            for cc in (c - 1, c, c + 1):
                if (rr, cc) != (r, c) and 0 <= rr < h and 0 <= cc < w and zb[rr, cc] < bz:
                    d_ = 1.414 if rr != r and cc != c else 1.0
                    drop = (bz - zb[rr, cc]) / d_
                    if best is None or drop > best[0]:
                        best = (drop, rr, cc)
        if best:
            acc[best[1], best[2]] += acc[r, c]
    Db = np.hypot(Xb, Yb)
    rb, cb = np.unravel_index(np.nanargmin(Db), Db.shape)
    near = acc[max(rb - 1, 0):rb + 2, max(cb - 1, 0):cb + 2].max()
    out["upslope_ha"] = round(float(near * cell_ha), 1)
    ch = (acc * cell_ha >= 5) & ~np.isnan(zb)
    out["chan_m"] = round(float(Db[ch].min()), 0) if ch.any() else None
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(indices(float(sys.argv[1]), float(sys.argv[2])), indent=1))
