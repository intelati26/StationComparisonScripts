#!/usr/bin/env python3
"""Sky view from the terrain (needs numpy): 1 - mean sin(horizon angle) over 16 directions, to a radius, from the 3DEP 8 m terrain.  python svf.py LAT LON
Only terrain is used: trees and buildings are not in it."""
import math, sys
import numpy as np
import terrain as T


def svf(lat, lon, radii=(300, 1000, 2000)):
    rows, hdr = T.window(lat, lon, half=0.04, source="3dep")
    z = np.array(rows, dtype=float); z[z < -1000] = np.nan
    nr, nc, cs = z.shape[0], z.shape[1], hdr["cellsize"]
    ky, kx = 110574.0, 111320.0 * math.cos(math.radians(lat))
    r0 = int(round((hdr["yllcorner"] + nr * cs - lat) / cs - 0.5)); c0 = int(round((lon - hdr["xllcorner"]) / cs - 0.5))
    z0 = z[r0, c0]
    out = {}
    for R in radii:
        angs = []
        for az in np.arange(0, 360, 22.5):
            dx, dy = math.sin(math.radians(az)), math.cos(math.radians(az))
            best = -90.0
            for d in np.arange(12.0, R + 1, 12.0):
                r = int(round(r0 - (d * dy) / (cs * ky))); cc = int(round(c0 + (d * dx) / (cs * kx)))
                if 0 <= r < nr and 0 <= cc < nc and not np.isnan(z[r, cc]):
                    best = max(best, math.degrees(math.atan2(z[r, cc] - z0, d)))
            angs.append(max(best, 0.0))
        out[R] = 1 - float(np.mean([math.sin(math.radians(a)) for a in angs])), float(np.mean(angs))
    return out


if __name__ == "__main__":
    for R, (s, m) in svf(float(sys.argv[1]), float(sys.argv[2])).items():
        print(f"radius {R} m: sky view {s:.3f}, mean horizon angle {m:.1f} deg")
