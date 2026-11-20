#!/usr/bin/env python3
"""Terrain around a point from the Copernicus 30 m surface model (public AWS bucket), read with GDAL.

  python terrain.py                 the station (WEATHER_LAT / WEATHER_LON): writes log/terrain.json
  python terrain.py LAT LON [lidar|3dep|copernicus]   one other point, printed

Only whole 1-degree tiles are named in the requests and only the window needed is read (ranged reads): the point's own coordinates are not sent.
The surface model includes trees and buildings (a few metres), cells are 30 m.
Needs the gdal command line tools (gdalbuildvrt, gdal_translate).
"""
import json, math, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).parent.parent
BUCKET = "/vsicurl/https://copernicus-dem-30m.s3.amazonaws.com"
FT = 3.28084
DIRS = (("N", 0), ("NE", 45), ("E", 90), ("SE", 135), ("S", 180), ("SW", 225), ("W", 270), ("NW", 315))


SOURCES = ("lidar", "3dep", "copernicus")      # 3DEP: USGS 1/3 arc-second (about 10 m) bare earth, US only; Copernicus: 30 m surface (trees and buildings included), the world


def tile_3dep(lat_deg, lon_deg):
    """3DEP 1/3 arc-second tiles are named by their north-west corner: n37w095 covers 36-37 N, 95-94 W."""
    return f"/vsicurl/https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/n{lat_deg + 1:02d}w{abs(lon_deg):03d}/USGS_13_n{lat_deg + 1:02d}w{abs(lon_deg):03d}.tif"


def tile(lat_deg, lon_deg):
    ns = "N" if lat_deg >= 0 else "S"
    ew = "E" if lon_deg >= 0 else "W"
    name = f"Copernicus_DSM_COG_10_{ns}{abs(lat_deg):02d}_00_{ew}{abs(lon_deg):03d}_00_DEM"
    return f"{BUCKET}/{name}/{name}.tif"


def window_lidar(lat, lon, hlat=0.03, hlon=0.036, step=0.00004):
    """1 m lidar DEM tiles listed in log/lidar_items.json (from the National Map product search, run with one-decimal coordinates only),
    newest project per tile, warped to a lat/lon grid of about 4 m cells over the window; the window is cut here, only tile bytes are requested."""
    items = json.loads((HERE / "log" / "lidar_items.json").read_text())
    best = {}
    for it in items:
        bb = it["boundingBox"]
        if bb["maxX"] < lon - hlon or bb["minX"] > lon + hlon or bb["maxY"] < lat - hlat or bb["minY"] > lat + hlat:
            continue
        key = it["title"].split()[3] + it["title"].split()[4] if False else " ".join(it["title"].split()[3:5])      # the tile, e.g. "x36y410"
        key = [w for w in it["title"].split() if w.startswith("x") and "y" in w][0]
        if key not in best or it["publicationDate"] > best[key]["publicationDate"]:
            best[key] = it
    urls = ["/vsicurl/" + it["downloadURL"] for it in best.values()]
    with tempfile.TemporaryDirectory() as d:
        vrt, tif, asc = (os.path.join(d, n) for n in ("d.vrt", "w.tif", "w.asc"))
        subprocess.run(["gdalbuildvrt", "-q", vrt] + urls, check=True, capture_output=True)
        subprocess.run(["gdalwarp", "-q", "-t_srs", "EPSG:4326", "-te", str(lon - hlon), str(lat - hlat), str(lon + hlon), str(lat + hlat),
                        "-tr", str(step), str(step), "-r", "average", "-dstnodata", "-9999", vrt, tif], check=True, capture_output=True)
        subprocess.run(["gdal_translate", "-q", "-of", "AAIGrid", tif, asc], check=True, capture_output=True)
        lines = Path(asc).read_text().splitlines()
    hdr, i = {}, 0
    while not lines[i].split()[0].replace(".", "", 1).replace("-", "", 1).isdigit():
        k, v = lines[i].split()
        hdr[k.lower()] = float(v)
        i += 1
    print(f"lidar: {len(best)} tiles ({', '.join(sorted(best))}), projects {sorted({'_'.join(it['title'].split()[5:]) for it in best.values()})}", file=sys.stderr)
    return [[float(x) for x in l.split()] for l in lines[i:] if l.strip()], hdr


def window(lat, lon, half=0.07, source="3dep"):
    if source == "lidar":
        return window_lidar(lat, lon)
    """(rows, header) of the surface heights (m) around a point, north row first."""
    south, north, west, east = math.floor(lat - half), math.floor(lat + half), math.floor(lon - half), math.floor(lon + half)
    tiles = sorted({(tile_3dep if source == "3dep" else tile)(la, lo) for la in range(south, north + 1) for lo in range(west, east + 1)})
    with tempfile.TemporaryDirectory() as d:
        vrt, asc = os.path.join(d, "d.vrt"), os.path.join(d, "d.asc")
        subprocess.run(["gdalbuildvrt", "-q", vrt] + tiles, check=True, capture_output=True)
        subprocess.run(["gdal_translate", "-q", "-projwin", str(lon - half), str(lat + half * 0.85), str(lon + half), str(lat - half * 0.85), "-of", "AAIGrid", vrt, asc],
                       check=True, capture_output=True)
        lines = Path(asc).read_text().splitlines()
    hdr, i = {}, 0
    while not lines[i].split()[0].replace(".", "", 1).replace("-", "", 1).isdigit():
        k, v = lines[i].split()
        hdr[k.lower()] = float(v)
        i += 1
    return [[float(x) for x in l.split()] for l in lines[i:] if l.strip()], hdr


def profile(lat, lon, source="3dep"):
    rows, hdr = window(lat, lon, source=source)
    nr, nc, cs = int(hdr["nrows"]), int(hdr["ncols"]), hdr["cellsize"]
    kx, ky = 111.320 * math.cos(math.radians(lat)), 110.574
    pts = []
    for r in range(nr):
        for c in range(nc):
            dx = (hdr["xllcorner"] + (c + 0.5) * cs - lon) * kx
            dy = (hdr["yllcorner"] + (nr - r - 0.5) * cs - lat) * ky
            if rows[r][c] > -1000:
                pts.append((math.hypot(dx, dy), (math.degrees(math.atan2(dx, dy)) + 360) % 360, rows[r][c] * FT))
    z0 = min(pts, key=lambda p: p[0])[2]
    out = {"source": source, "cell_m": round(cs * 111320 * math.cos(math.radians(lat)), 1), "elevation_ft": round(z0)}
    for R in (0.5, 1, 2, 3, 5):
        sel = [p for p in pts if p[0] <= R]
        hi, lo = max(sel, key=lambda p: p[2]), min(sel, key=lambda p: p[2])
        out[f"within_{R}km"] = {"highest_ft": round(hi[2]), "highest_km": round(hi[0], 2), "highest_bearing": round(hi[1]),
                                "lowest_ft": round(lo[2]), "lowest_km": round(lo[0], 2), "lowest_bearing": round(lo[1])}
    out["rim_by_direction_3km_ft_above"] = {n: round(max((p for p in pts if p[0] <= 3 and abs(((p[1] - b + 180) % 360) - 180) <= 22.5), key=lambda p: p[2])[2] - z0) for n, b in DIRS}
    # the numbers the model uses: the floor is the lowest ground within 2 km, the rim the highest within 3 km
    out["floor_ft"] = out["within_2km"]["lowest_ft"]
    out["rim_ft"] = out["within_3km"]["highest_ft"]
    out["height_above_floor_ft"] = round(z0 - out["floor_ft"])
    out["height_below_rim_ft"] = round(out["rim_ft"] - z0)
    return out


def load_env():
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        print(json.dumps(profile(float(sys.argv[1]), float(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "3dep"), indent=1))
    else:
        load_env()
        prof = profile(float(os.environ["WEATHER_LAT"]), float(os.environ["WEATHER_LON"]))
        (HERE / "log").mkdir(exist_ok=True)
        (HERE / "log" / "terrain.json").write_text(json.dumps(prof, indent=1))
        print(f"station {prof['elevation_ft']} ft; floor {prof['floor_ft']} ft (lowest within 2 km), rim {prof['rim_ft']} ft (highest within 3 km)")
        print("written to log/terrain.json")
