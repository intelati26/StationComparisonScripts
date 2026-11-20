#!/usr/bin/env python3
"""Sky view and obstruction horizon at a point from USGS 3DEP lidar (public EPT on AWS), by laspy: no keys, no coordinates in the file.

  python skyview_lidar.py [LAT LON] [--height 1.5] [--radius 100] [--json OUT]       (no LAT LON: the place in .env, through coldpool.py)

Finds the newest lidar project covering the point (index: hobuinc/usgs-lidar boundaries, cached in log/), reads the tiles that touch a box around it, makes a 1 m surface model (highest return) and ground model (lowest ground return) and looks out from a sensor `height` metres above the ground in 32 directions.
Prints: sky view (cosine-weighted share of the sky above the horizon), the same by the 1 - mean sin(angle) form used in svf.py, per-direction horizon angle / limiting distance / obstruction height above the sensor, the point density, the project and its year.
Needs numpy, scipy, laspy[lazrs], pyproj.  The lidar's leaf-on/leaf-off state is whatever the flight was (the project name carries its year)."""
import argparse, io, json, math, os, re, sys, time, urllib.request
import numpy as np
import laspy, pyproj
from scipy import ndimage
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); LOG = os.path.join(HERE, "log")
INDEX = "https://raw.githubusercontent.com/hobuinc/usgs-lidar/master/boundaries/resources.geojson"
UA = {"User-Agent": "skyview-lidar-research"}
def get(url, tries=3):
    for i in range(tries):
        try: return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90).read()
        except Exception as e:
            if getattr(e, "code", 0) in (403, 404): return None
            time.sleep(3 * (i + 1))
def inside(lon, lat, ring):
    x = np.array([p[0] for p in ring]); y = np.array([p[1] for p in ring]); n = len(ring); c = False
    j = n - 1
    for i in range(n):
        if (y[i] > lat) != (y[j] > lat) and lon < (x[j] - x[i]) * (lat - y[i]) / (y[j] - y[i]) + x[i]: c = not c
        j = i
    return c
def projects(lat, lon):
    f = os.path.join(LOG, "usgs_lidar_resources.geojson")
    if not os.path.exists(f): os.makedirs(LOG, exist_ok=True); open(f, "wb").write(get(INDEX))
    out = []
    for ft in json.load(open(f))["features"]:
        g = ft["geometry"]; polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        if any(inside(lon, lat, p[0]) and not any(inside(lon, lat, h) for h in p[1:]) for p in polys): out.append(ft["properties"])
    yr = lambda p: max([int(m) for m in re.findall(r"(?:19|20)\d\d", p["name"])] or [0])
    return sorted(out, key=yr, reverse=True)
def tiles(base, bounds, box):
    """EPT nodes (key) whose cube touches box = (xmin, ymin, xmax, ymax), native coordinates."""
    H = json.loads(get(base + "ept-hierarchy/0-0-0-0.json")); stack = ["0-0-0-0"]; out = []
    minx, miny, minz, maxx, maxy, maxz = bounds
    while stack:
        k = stack.pop(); n = H.get(k)
        if n is None: continue
        if n == -1:
            H.update(json.loads(get(base + f"ept-hierarchy/{k}.json"))); n = H[k]
        d, x, y, z = map(int, k.split("-")); s = (maxx - minx) / 2 ** d
        x0, y0 = minx + x * s, miny + y * s
        if x0 > box[2] or x0 + s < box[0] or y0 > box[3] or y0 + s < box[1]: continue
        if n > 0: out.append(k)
        stack += [f"{d+1}-{2*x+i}-{2*y+j}-{2*z+l}" for i in (0, 1) for j in (0, 1) for l in (0, 1)]
    return out
def fill(a):
    bad = np.isnan(a)
    if not bad.any(): return a
    if bad.all(): return a
    idx = ndimage.distance_transform_edt(bad, return_distances=False, return_indices=True)
    return a[tuple(idx)]
def load(lat, lon, half=150):
    pj = projects(lat, lon)
    if not pj: return {"error": "no USGS lidar project covers this point"}
    for p in pj:
        base = p["url"].rsplit("/", 1)[0] + "/"; ept = json.loads(get(base + "ept.json"))
        crs = pyproj.CRS.from_user_input(ept["srs"].get("wkt") or f'{ept["srs"]["authority"]}:{ept["srs"]["horizontal"]}')
        u = crs.axis_info[0].unit_conversion_factor or 1.0
        X, Y = pyproj.Transformer.from_crs(4326, crs, always_xy=True).transform(lon, lat)
        hm = half / u; box = (X - hm, Y - hm, X + hm, Y + hm); keys = tiles(base, ept["bounds"], box)
        if keys: break
    else: return {"error": "the lidar projects found have no points here", "projects": [p["name"] for p in pj]}
    xs, ys, zs, cl = [], [], [], []
    for k in keys:
        raw = get(base + f"ept-data/{k}.laz")
        if not raw: continue
        las = laspy.read(io.BytesIO(raw)); m = (las.x >= box[0]) & (las.x <= box[2]) & (las.y >= box[1]) & (las.y <= box[3])
        xs.append(np.asarray(las.x[m])); ys.append(np.asarray(las.y[m])); zs.append(np.asarray(las.z[m])); cl.append(np.asarray(las.classification[m]))
    x = (np.concatenate(xs) - X) * u; y = (np.concatenate(ys) - Y) * u; z = np.concatenate(zs) * u; c = np.concatenate(cl)
    keep = ~np.isin(c, (7, 18)); x, y, z, c = x[keep], y[keep], z[keep], c[keep]
    n = int(2 * half); ix = np.clip(((x + half)).astype(int), 0, n - 1); iy = np.clip(((half - y)).astype(int), 0, n - 1)
    dsm = np.full((n, n), np.nan); dtm = np.full((n, n), np.nan)
    flat = iy * n + ix; order = np.argsort(z)
    D = np.full(n * n, -np.inf); np.maximum.at(D, flat, z); dsm = np.where(np.isfinite(D.reshape(n, n)), D.reshape(n, n), np.nan)
    g = c == 2; G = np.full(n * n, np.inf); np.minimum.at(G, flat[g], z[g]); dtm = np.where(np.isfinite(G.reshape(n, n)), G.reshape(n, n), np.nan)
    dens = len(x) / ((2 * half) ** 2); dtm = fill(dtm); dsm = fill(dsm); dsm = np.maximum(dsm, dtm)
    meta = {"project": p["name"], "tiles": len(keys), "points": int(len(x)), "points_per_m2": round(dens, 1), "ground_returns_share": round(float(g.mean()), 2)}
    return dsm, dtm, meta
def look(dsm, dtm, height=1.5, radius=100, dn=0.0, de=0.0):
    """Horizon scan from the point moved dn metres north and de metres east of the given one."""
    n = dsm.shape[0]; cy = int(round(n // 2 - dn)); cx = int(round(n // 2 + de)); base_z = dtm[cy, cx] + height
    az = np.arange(0, 360, 11.25); rows = []
    for a in az:
        r = np.arange(1, radius + 1); px = np.clip((cx + r * math.sin(math.radians(a))).round().astype(int), 0, n - 1); py = np.clip((cy - r * math.cos(math.radians(a))).round().astype(int), 0, n - 1)
        ang = np.degrees(np.arctan2(dsm[py, px] - base_z, r)); j = int(np.argmax(ang)); gam = max(0.0, float(ang[j]))
        rows.append((float(a), gam, float(r[j]) if gam > 0 else None, float(dsm[py[j], px[j]] - base_z) if gam > 0 else 0.0))
    gam = np.radians([r[1] for r in rows])
    sec = {nm: float(np.mean([r[1] for r in rows if abs(((r[0] - c0 + 180) % 360) - 180) <= 45])) for nm, c0 in (("N", 0), ("E", 90), ("S", 180), ("W", 270))}
    return {"sensor_height_m": height, "radius_m": radius, "svf_cos2": round(float(np.mean(np.cos(gam) ** 2)), 3), "svf_sin": round(float(1 - np.mean(np.sin(gam))), 3),
            "mean_horizon_deg": round(float(np.degrees(gam).mean()), 1), "sector_horizon_deg": {k: round(v, 1) for k, v in sec.items()},
            "obstruction_height_mean_m": round(float(np.mean([r[3] for r in rows])), 1), "directions": [{"az": r[0], "horizon_deg": round(r[1], 1), "dist_m": r[2], "height_above_sensor_m": round(r[3], 1)} for r in rows]}
def analyse(lat, lon, height=1.5, radius=100, half=150, dn=0.0, de=0.0):
    out = load(lat, lon, half)
    if isinstance(out, dict): return out
    dsm, dtm, meta = out; return {**meta, **look(dsm, dtm, height, radius, dn, de)}
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("lat", nargs="?", type=float); ap.add_argument("lon", nargs="?", type=float)
    ap.add_argument("--height", type=float, default=1.5); ap.add_argument("--radius", type=float, default=100); ap.add_argument("--json")
    ap.add_argument("--shift", type=float, nargs=2, metavar=("NORTH_M", "EAST_M"), default=(0.0, 0.0), help="move the point this many metres north and east")
    ap.add_argument("--scan", type=float, metavar="M", help="print the horizon of the four quarters for shifts of up to M metres (5 m steps) around the point")
    a = ap.parse_args()
    if a.lat is None:
        sys.path.insert(0, HERE); import coldpool as c; c.load_env(); cfg = c.load_config(); a.lat, a.lon = cfg["lat"], cfg["lon"]
    if a.scan:
        out = load(a.lat, a.lon, 150 if a.scan <= 40 else a.scan + 110)
        if isinstance(out, dict): sys.exit(out["error"])
        dsm, dtm, meta = out; print(f"{meta['project']}: horizon (deg) N/E/S/W by shift of the point (metres north, east); south obstruction distance and height")
        for dn in np.arange(-a.scan, a.scan + 1, 5):
            for de in np.arange(-a.scan, a.scan + 1, 5):
                r = look(dsm, dtm, a.height, a.radius, dn, de); sd = [d for d in r["directions"] if 168 <= d["az"] <= 192 and d["dist_m"]]
                sdist = np.mean([d["dist_m"] for d in sd]) if sd else float("nan"); sh = np.mean([d["height_above_sensor_m"] for d in sd]) if sd else float("nan")
                q = r["sector_horizon_deg"]; print(f"  N{dn:+4.0f} E{de:+4.0f}: sky view {r['svf_cos2']:.2f}  N {q['N']:>4.0f} E {q['E']:>4.0f} S {q['S']:>4.0f} W {q['W']:>4.0f}   S obstruction {sdist:>5.0f} m away, {sh:>4.1f} m high")
        sys.exit()
    t0 = time.time(); r = analyse(a.lat, a.lon, a.height, a.radius, dn=a.shift[0], de=a.shift[1])
    if "error" in r: sys.exit(r["error"] + (": " + ", ".join(r.get("projects", [])) if r.get("projects") else ""))
    print(f"{r['project']}: {r['points']:,} points in {r['tiles']} tiles ({r['points_per_m2']}/m2, {r['ground_returns_share']:.0%} ground), {time.time()-t0:.0f} s")
    print(f"sky view {r['svf_cos2']} (cosine-weighted), {r['svf_sin']} (1 - mean sin); mean horizon {r['mean_horizon_deg']} deg; mean obstruction {r['obstruction_height_mean_m']} m above the sensor at {r['sensor_height_m']} m")
    print("horizon by quarter (deg): " + "  ".join(f"{k} {v}" for k, v in r["sector_horizon_deg"].items()))
    if a.json: json.dump(r, open(a.json, "w"), indent=1)
