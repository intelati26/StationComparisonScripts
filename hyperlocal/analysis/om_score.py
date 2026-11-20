#!/usr/bin/env python3
"""The tool in hindsight: termux/low.py's equation on Open-Meteo's analysis-like HRRR history (T2m, dewpoint less the seasonal adjustment, cloud, wind at 450 m), scored on the station's night low, whole record, by season and month.
Reference lines: the HRRR low alone, and the equation on URMA inputs for the same nights.   Needs log/om_home_*.json (om_history.py).   python om_score.py"""
import glob, json, math, os, sqlite3, statistics as st, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone
import coldpool as c

cfg = c.load_config()
OM = {}
for f in glob.glob(str(c.LOG / "om_home_*.json")):
    OM.update({datetime.fromisoformat(k): v for k, v in json.load(open(f)).items()})
URMA = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / "downloaded.json"))["mod"].items()}
acc = {}
for t, tf, dp in sqlite3.connect(c.LOG / "ambient_all.db").execute("SELECT utc, tempf, dewpoint FROM ambient"):
    h = datetime.fromisoformat(t.replace("Z", "+00:00")).replace(minute=0, second=0)
    a = acc.setdefault(h, [0, 0.0, 0, 0.0]); a[0] += 1; a[1] += tf
    if dp is not None:
        a[2] += 1; a[3] += dp
OBS = {h: (a[1] / a[0], a[3] / a[2]) for h, a in acc.items() if a[0] >= 8 and a[2] >= 8}
adj = lambda m: 1.0 if 6 <= m <= 8 else 3.0 if 9 <= m <= 11 else 2.0          # low.py's seasonal dewpoint correction
nights = {}
for t in sorted(OM):
    if t in OBS and c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0 and datetime(2025, 2, 1, tzinfo=t.tzinfo) <= t < datetime(2026, 11, 20, tzinfo=t.tzinfo):
        nights.setdefault((t - timedelta(hours=13)).date(), []).append(t)
out = []
for E, ks in sorted(nights.items()):
    if len(ks) < 9:
        continue
    actual = min(OBS[k][0] for k in ks)
    def low(td_adj):
        return min(OM[k][0] - 10 * math.exp(-(OM[k][3] / 30) ** 3) * (1 - 0.8 * OM[k][2] / 100) * min(1.0, max(0.0, OM[k][0] - (OM[k][1] - td_adj)) / 12) for k in ks)
    r = dict(E=E, mo=E.month, actual=actual, om=min(OM[k][0] for k in ks) - actual, tool=low(adj(E.month)) - actual, raw=low(0.0) - actual)
    if all(k in URMA for k in ks):
        r["ana"] = min(URMA[k][0] - 10 * math.exp(-(OM[k][3] / 30) ** 3) * (1 - 0.8 * URMA[k][1] / 100) * min(1.0, max(0.0, URMA[k][0] - OBS[k][1]) / 12) for k in ks) - actual
    out.append(r)
json.dump([{**r, "E": r["E"].isoformat()} for r in out], open(c.LOG / "om_nights.json", "w"))
print(len(out), "nights;", sum(1 for r in out if "ana" in r), "of them with URMA inputs too")
SEAS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
q = lambda e, p: sorted(e)[min(len(e) - 1, int(p * len(e)))]
print(f"{'season':<8}{'nights':>7} | {'prediction':<40}{'mean':>7}{'SD':>6}{'MAE':>6}   middle 80%")
for s in ("winter", "spring", "summer", "autumn", "all"):
    rs = [r for r in out if s == "all" or SEAS[r["mo"]] == s]
    for k, lab in (("om", "HRRR (Open-Meteo) low alone"), ("tool", "the tool in hindsight (seasonal Td adjustment)"), ("raw", "same, no dewpoint adjustment"), ("ana", "equation on URMA inputs (same nights)")):
        e = [r[k] for r in rs if k in r]
        if e:
            print(f"{s if k == 'om' else '':<8}{len(rs) if k == 'om' else '':>7} | {lab:<40}{st.mean(e):>+7.2f}{st.pstdev(e):>6.2f}{st.mean(abs(x) for x in e):>6.2f}   {q(e, .1):+.1f} to {q(e, .9):+.1f}")
print("\nby month, the tool in hindsight: nights, mean error, SD  |  the equation on URMA inputs: mean, SD")
for y, m in [(2025, k) for k in range(2, 13)] + [(2026, k) for k in range(1, 12)]:
    rs = [r for r in out if r["E"].year == y and r["mo"] == m]
    if len(rs) >= 8:
        an = [r["ana"] for r in rs if "ana" in r]
        print(f"  {y}-{m:02d}  {len(rs):>3}  {st.mean(r['tool'] for r in rs):+5.2f}  {st.pstdev([r['tool'] for r in rs]):4.2f}   | " + (f"{st.mean(an):+5.2f}  {st.pstdev(an):4.2f}" if len(an) >= 8 else "-"))
