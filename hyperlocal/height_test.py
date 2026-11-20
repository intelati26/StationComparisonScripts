#!/usr/bin/env python3
"""Which wind height works, in mountains?  python height_test.py NAME LAT LON
Stations of one mountain place sit at very different heights, so a pressure level is a different height above each station's ground. For each station:
night temperature minus URMA at the place's centre = a + b x factor(wind), the factor as in EQUATION.md, with the wind taken as:
  - 'local h': the wind h metres above THIS station's ground (the HRRR's levels at the centre, interpolated in height)
  - 'grid h': the wind h metres above the model grid's ground (the same for every station)
  - a pressure level (925, 850 hPa) and the 10 m wind
Scored leave-one-night-out, summed over the stations."""
import json, math, os, statistics as st, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coldpool as c, terrain as T

name, clat, clon = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
c.load_env()
wu = json.load(open(c.LOG / f"wu_hourly_{name}.json"))
urma = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / f"urma_{name}.json")).items()}
LEV = (1000, 975, 950, 925, 900, 850, 800, 700)
zgrid, HR = c.hrrr_levels(clat, clon, "2026-10-01", "2026-11-19")
HI = {t: row for t, row in HR.items()}
rows, hdr = T.window(clat, clon, half=0.12, source="3dep")
nr, nc, cs = int(hdr["nrows"]), int(hdr["ncols"]), hdr["cellsize"]
def elev(la, lo):
    r = int((hdr["yllcorner"] + nr * cs - la) / cs); k = int((lo - hdr["xllcorner"]) / cs)
    return rows[max(0, min(nr - 1, r))][max(0, min(nc - 1, k))]
print(f"model grid ground at the centre {zgrid:.0f} m; stations:", {sid[-5:]: round(elev(s['lat'], s['lon'])) for sid, s in wu.items()}, "m")


def wind_at_msl(row, z_msl, floor):
    """The wind at a height above sea level, interpolated between the 10 m wind (at the model's ground) and the levels that are above `floor` (a station's ground)."""
    pts = [(zgrid + 10.0, row["w10"])] if row.get("w10") is not None else []
    for l in LEV:
        w, z = row.get(f"w{l}"), row.get(f"z{l}")
        if w is not None and z is not None:
            pts.append((z, w))
    pts = sorted(p for p in pts if p[0] >= floor + 10)
    if len(pts) < 2:
        return None
    for (a, wa), (b, wb) in zip(pts, pts[1:]):
        if a <= z_msl <= b:
            return wa + (wb - wa) * (z_msl - a) / (b - a)
    return pts[-1][1] if z_msl > pts[-1][0] else pts[0][1]


def factor(U, cloud, tu, td, w):
    return math.exp(-(w / 30.0) ** 3) * (1 - 0.8 * cloud / 100) * min(1.0, max(0.0, tu - td) / 12.0)


variants = ["local 200 m", "local 450 m", "local 700 m", "grid 450 m", "925 hPa level", "850 hPa level", "10 m"]
stations = []
for sid, s in wu.items():
    z = elev(s["lat"], s["lon"]); data = {v: [] for v in variants}; ys = []; nights = []
    for kiso, v in s["hours"].items():
        if v[0] is None or v[1] is None:
            continue
        t = datetime.fromisoformat(kiso.replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0)
        u, row = urma.get(t), HI.get(t)
        if u is None or row is None or c.night_factor(t, clat, clon) < 1.0:
            continue
        w = {"local 200 m": wind_at_msl(row, z + 200, z), "local 450 m": wind_at_msl(row, z + 450, z), "local 700 m": wind_at_msl(row, z + 700, z), "grid 450 m": wind_at_msl(row, zgrid + 450, zgrid),
             "925 hPa level": row["w925"], "850 hPa level": row["w850"], "10 m": row["w10"]}
        if any(x is None for x in w.values()):
            continue
        for var in variants:
            data[var].append(factor(0, u[1], u[0], v[1], w[var]))
        ys.append(u[0] - v[0]); nights.append((t - timedelta(hours=13)).date())
    if len(ys) >= 150:
        stations.append(dict(sid=sid, z=z, y=ys, nights=nights, x=data))
print(len(stations), "stations with data\n")


def fit(x, y):
    mx, my = st.mean(x), st.mean(y); sxx = sum((a - mx) ** 2 for a in x) or 1e-9
    b = sum((a - mx) * (c_ - my) for a, c_ in zip(x, y)) / sxx; return my - b * mx, b


print(f"{'wind':<16}{'in-sample rmse':>15}{'leave-one-night-out':>21}{'mean R2':>9}")
for var in variants:
    ins = cv = n = 0.0; r2 = []
    for s in stations:
        x, y, nt = s["x"][var], s["y"], s["nights"]; a, b = fit(x, y)
        sse = sum((yy - a - b * xx) ** 2 for xx, yy in zip(x, y)); ins += sse; n += len(y)
        r2.append(1 - sse / (sum((yy - st.mean(y)) ** 2 for yy in y) or 1))
        for night in set(nt):
            tr = [(xx, yy) for xx, yy, nn in zip(x, y, nt) if nn != night]; te = [(xx, yy) for xx, yy, nn in zip(x, y, nt) if nn == night]
            if len(tr) < 50:
                continue
            a2, b2 = fit([p[0] for p in tr], [p[1] for p in tr]); cv += sum((yy - a2 - b2 * xx) ** 2 for xx, yy in te)
    print(f"{var:<16}{math.sqrt(ins / n):>14.2f}F{math.sqrt(cv / n):>20.2f}F{st.mean(r2):>9.2f}")
print("\nper station (cross-validated, F):  " + "  ".join(f"{v}" for v in variants))
for s in sorted(stations, key=lambda s: s["z"]):
    line = []
    for var in variants:
        x, y, nt = s["x"][var], s["y"], s["nights"]; cv = 0.0; m = 0
        for night in set(nt):
            tr = [(xx, yy) for xx, yy, nn in zip(x, y, nt) if nn != night]; te = [(xx, yy) for xx, yy, nn in zip(x, y, nt) if nn == night]
            a2, b2 = fit([p[0] for p in tr], [p[1] for p in tr]); cv += sum((yy - a2 - b2 * xx) ** 2 for xx, yy in te); m += len(te)
        line.append(f"{math.sqrt(cv / m):.2f}")
    print(f"  {s['z']:>5.0f} m   " + "  ".join(f"{x:>11}" for x in line))
