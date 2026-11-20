#!/usr/bin/env python3
"""The equation on a real archived forecast (the 19Z NBM text bulletin, NBS, from log/nbs_19z.json, interpolated to hourly), scored on the night's low at the station, by season.
The wind aloft is the HRRR analysis wind (the NBS has only a 10 m wind), so its small forecast error is not in these numbers.   python nbs_score.py"""
import glob, json, math, os, sqlite3, statistics as st, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
import coldpool as c

cfg = c.load_config()
NBS = json.load(open(c.LOG / "nbs_19z.json"))
URMA = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / "downloaded.json"))["mod"].items()}
W = {}
for f in glob.glob(str(c.LOG / "w450_home_*.json")):
    W.update({datetime.fromisoformat(k): v for k, v in json.load(open(f)).items()})
acc = {}
for t, tf, dp in sqlite3.connect(c.LOG / "ambient_all.db").execute("SELECT utc, tempf, dewpoint FROM ambient"):
    h = datetime.fromisoformat(t.replace("Z", "+00:00")).replace(minute=0, second=0)
    a = acc.setdefault(h, [0, 0.0, 0, 0.0]); a[0] += 1; a[1] += tf
    if dp is not None:
        a[2] += 1; a[3] += dp
OBS = {h: (a[1] / a[0], a[3] / a[2]) for h, a in acc.items() if a[0] >= 8 and a[2] >= 8}
nights = {}
for t, u in sorted(URMA.items()):
    if t in OBS and t in W and c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0 and datetime(2025, 2, 1, tzinfo=t.tzinfo) <= t < datetime(2026, 11, 19, tzinfo=t.tzinfo):
        nights.setdefault((t - timedelta(hours=13)).date(), []).append(t)


def series(rows, idx):
    pts = [(datetime.fromisoformat(r[0]).replace(tzinfo=timezone.utc), r[idx]) for r in rows if r[idx] is not None]
    pts.sort()
    return pts


def at(pts, t):
    for (a, va), (b, vb) in zip(pts, pts[1:]):
        if a <= t <= b:
            return va + (vb - va) * (t - a).total_seconds() / (b - a).total_seconds()
    return None


out = []
for E, ks in sorted(nights.items()):
    rows = NBS.get(E.isoformat())
    if not rows or len(ks) < 9:
        continue
    Ts, Ds, Ss = series(rows, 1), series(rows, 2), series(rows, 3)
    T = {k: at(Ts, k) for k in ks}; D = {k: at(Ds, k) for k in ks}; S = {k: at(Ss, k) for k in ks}
    if any(v is None for v in list(T.values()) + list(D.values()) + list(S.values())):
        continue
    td0 = OBS[ks[0]][1]
    actual = min(OBS[k][0] for k in ks)

    def low(tdf):
        return min(T[k] - 10 * math.exp(-(W[k] / 30) ** 3) * (1 - 0.8 * S[k] / 100) * min(1.0, max(0.0, T[k] - tdf(i, k)) / 12) for i, k in enumerate(ks))
    ana = min(URMA[k][0] - 10 * math.exp(-(W[k] / 30) ** 3) * (1 - 0.8 * URMA[k][1] / 100) * min(1.0, max(0.0, URMA[k][0] - OBS[k][1]) / 12) for k in ks)
    out.append(dict(E=E, mo=E.month, actual=actual, nbs=min(T.values()) - actual, held=low(lambda i, k: td0) - actual, fall=low(lambda i, k: td0 - 0.3 * i) - actual, nbsd=low(lambda i, k: D[k]) - actual,
                    ana=ana - actual, dT=st.mean(T[k] - URMA[k][0] for k in ks), dD=st.mean(D[k] - OBS[k][1] for k in ks), dS=st.mean(S[k] - URMA[k][1] for k in ks)))
json.dump([{**r, "E": r["E"].isoformat()} for r in out], open(c.LOG / "nbs_nights.json", "w"))          # one row per night for the plots (errors are prediction minus the actual low)
print(len(out), "nights with a 19Z NBS run and every input")
SEAS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
q = lambda e, p: sorted(e)[min(len(e) - 1, int(p * len(e)))]
print(f"{'season':<8}{'nights':>7} | {'prediction':<34}{'mean':>7}{'SD':>6}{'MAE':>6}   middle 80%")
for s in ("winter", "spring", "summer", "autumn", "all"):
    rs = [r for r in out if s == "all" or SEAS[r["mo"]] == s]
    if not rs:
        continue
    for k, lab in (("nbs", "NBS low alone (the forecast)"), ("held", "equation, evening dewpoint held"), ("fall", "equation, dewpoint falling 0.3 F/h"), ("nbsd", "equation, NBS dewpoint"), ("ana", "equation, analysis inputs (reference)")):
        e = [r[k] for r in rs]
        print(f"{s if k == 'nbs' else '':<8}{len(rs) if k == 'nbs' else '':>7} | {lab:<34}{st.mean(e):>+7.2f}{st.pstdev(e):>6.2f}{st.mean(abs(x) for x in e):>6.2f}   {q(e, .1):+.1f} to {q(e, .9):+.1f}")
    print(f"{'':<8}{'':>7} | inputs: NBS temperature minus URMA {st.mean(r['dT'] for r in rs):+.1f} F, NBS dewpoint minus the station's {st.mean(r['dD'] for r in rs):+.1f} F, NBS cloud minus URMA {st.mean(r['dS'] for r in rs):+.0f} points")
print("\nSD of the error by month (equation with the dewpoint falling 0.3 F/h on the NBS forecast | equation on analysis inputs), nights:")
for y, m in [(2025, k) for k in range(2, 13)] + [(2026, k) for k in range(1, 12)]:
    rs = [r for r in out if r["E"].year == y and r["mo"] == m]
    if len(rs) >= 8:
        print(f"  {y}-{m:02d}  {len(rs):>3}   forecast inputs: mean {st.mean(r['fall'] for r in rs):+5.2f}  SD {st.pstdev([r['fall'] for r in rs]):4.2f}   | analysis inputs: mean {st.mean(r['ana'] for r in rs):+5.2f}  SD {st.pstdev([r['ana'] for r in rs]):4.2f}")
