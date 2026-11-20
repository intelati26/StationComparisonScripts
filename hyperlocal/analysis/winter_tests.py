#!/usr/bin/env python3
"""Two tests of why midwinter nights run too cold with the equation: (1) a fade with the hours after sunset (long nights), (2) the dryness term in vapour pressure / relative humidity in place of T - Td in F.
A is refitted for each variant (grid) so the comparison is fair. Nightly-low MAE per window group.   python winter_tests.py"""
import glob, json, math, os, sqlite3, statistics as st, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta
import coldpool as c

c.load_env(); cfg = c.load_config()
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
GROUP = {"2026-10": "autumn", "2026-11": "autumn", "2026-02": "feb", "2026-04": "apr", "2026-07": "jul", "2026-01": "jan", "2026-03": "mar", "2026-05": "may"}
nights = {}
for t, u in sorted(URMA.items()):
    g = GROUP.get(f"{t.year}-{t.month:02d}"); o = OBS.get(t); w = W.get(t)
    if not g or not o or w is None or c.night_factor(t, cfg["lat"], cfg["lon"]) < 1.0:
        continue
    ha = c.hours_after_sunset(t, cfg["lat"], cfg["lon"])
    d0 = (t - timedelta(hours=13)).date()
    night_len = (c.sun_times(cfg["lat"], cfg["lon"], d0 + timedelta(days=1))[0] - c.sun_times(cfg["lat"], cfg["lon"], d0)[1]).total_seconds() / 3600
    nights.setdefault((g, d0), []).append((ha, u[0], u[1], w, o[0], o[1], ha / night_len))
nights = {k: v for k, v in nights.items() if len(v) >= 6}
print(len(nights), "nights")


def esat(tf):                                        # Magnus, hPa, from F
    tc = (tf - 32) / 1.8
    return 6.112 * math.exp(17.62 * tc / (243.12 + tc))


def dry_T(T, Td, ref):  return min(1.0, max(0.0, T - Td) / ref)
def dry_e(T, Td, ref):  return min(1.0, max(0.0, esat(T) - esat(Td)) / ref)               # vapour pressure deficit, hPa
def dry_rh(T, Td, ref): return min(1.0, max(0.0, 1 - esat(Td) / esat(T)) / ref)           # 1 - RH


def score(A, fade, dry, ref, frac=None):
    out = {}
    for (g, d), v in nights.items():
        e = []
        for h, T, cl, w, s, td, fr in v:
            fac = math.exp(-(w / 30.0) ** 3) * (1 - 0.8 * cl / 100) * dry(T, td, ref)
            if fade:
                fac *= max(0.0, 1 - fade * max(0.0, h - 4) / 10)
            if frac:
                fac *= max(0.0, 1 - frac[0] * max(0.0, fr - frac[1]) / (1 - frac[1]))      # fades by k over the part of the night after fraction f0, in proportion to its length
            e.append((T - A * fac, s))
        out.setdefault(g, []).append(min(a for a, b in e) - min(b for a, b in e))
    return out


def report(label, **kw):
    kw.setdefault('dry', dry_T); kw.setdefault('ref', 12); kw.setdefault('fade', 0)
    best = None
    for A in [x / 2 for x in range(12, 33)]:             # 6 .. 16
        r = score(A, **kw)
        m = st.mean(abs(x) for v in r.values() for x in v)
        if best is None or m < best[0]:
            best = (m, A, r)
    m, A, r = best
    cols = "".join(f"{st.mean(abs(x) for x in r[g]):>6.2f}({st.mean(r[g]):+.1f})" for g in ("autumn", "jan", "feb", "mar", "apr", "may", "jul"))
    win = [x for g in ("jan", "feb") for x in r[g]]
    print(f"{label:<34}A={A:>4.1f} | pooled {m:.3f} | winter(jan+feb) {st.mean(abs(x) for x in win):.2f} ({st.mean(win):+.2f}) |{cols}")


if __name__ == "__main__":
    print(f"{'variant':<34}{'':<6}| pooled mean abs error | by window: autumn  jan  feb  mar  apr  may  jul  (bias)")
    report("current (T-Td)/12 F", fade=0, dry=dry_T, ref=12)
    for k in (0.2, 0.4, 0.6, 0.8):
        report(f"fade after hour 4, k={k}", fade=k, dry=dry_T, ref=12)
    for ref in (10, 14, 18):
        report(f"dryness (T-Td)/{ref} F", fade=0, dry=dry_T, ref=ref)
    for ref in (1.0, 1.5, 2.0, 3.0, 4.0):
        report(f"dryness vapour deficit/{ref} hPa", fade=0, dry=dry_e, ref=ref)
    for ref in (0.15, 0.25, 0.35, 0.5):
        report(f"dryness (1-RH)/{ref}", fade=0, dry=dry_rh, ref=ref)
