#!/usr/bin/env python3
"""Second gate: the drop is also cut when the model's 10 m wind is not calm.  coupling = exp(-(U450/x0)^3) x exp(-(U10/x1)^3).  A refitted. Uses the caches of shear_test.py.   python gate_test.py"""
import glob, json, math, os, statistics as st, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta
import coldpool as c
import winter_tests as w

cfg = c.load_config()
X = {}
for f in glob.glob(str(c.LOG / "shear_home_*.json")):
    X.update({datetime.fromisoformat(k): v for k, v in json.load(open(f)).items()})
GROUP = {"2026-10": "autumn", "2026-11": "autumn", "2026-01": "jan", "2026-02": "feb", "2026-03": "mar", "2026-04": "apr", "2026-05": "may", "2026-06": "jun", "2026-07": "jul", "2026-08": "aug", "2026-09": "sep"}
nights = {}
for t, u in sorted(w.URMA.items()):
    g = GROUP.get(f"{t.year}-{t.month:02d}"); o = w.OBS.get(t); x = X.get(t)
    if not g or not o or not x or x["w450"] is None or c.night_factor(t, cfg["lat"], cfg["lon"]) < 1.0:
        continue
    if g in ("jun", "may", "mar", "aug", "sep") and not (8 <= t.day <= 21 if g != "jun" else t.day >= 17):
        continue
    nights.setdefault((g, (t - timedelta(hours=13)).date()), []).append((t, u, o, x))
nights = {k: v for k, v in nights.items() if len(v) >= 6}
print(len(nights), "nights")


def score(x0, x1, A):
    out = {}
    for (g, d), v in nights.items():
        e = []
        for t, u, o, x in v:
            q = math.exp(-(x["w450"] / x0) ** 3) * (math.exp(-(x["w10"] / x1) ** 3) if x1 else 1.0)
            f = q * (1 - 0.8 * u[1] / 100) * min(1.0, max(0.0, u[0] - o[1]) / 12.0)
            e.append((u[0] - A * f, o[0]))
        out.setdefault(g, []).append(min(a for a, b in e) - min(b for a, b in e))
    return out


order = ["autumn", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep"]
print(f"{'variant':<34}{'x0':>4}{'x1':>4}{'A':>6} | pooled | " + " ".join(f"{g:>5}" for g in order) + " | winter(jan+feb) bias")
for label, x1s in (("450 m wind only (current)", [0]), ("x 10 m wind gate", [4, 6, 8, 10, 12, 16, 20])):
    best = None
    for x0 in range(24, 42, 2):
        for x1 in x1s:
            for A in [a / 2 for a in range(16, 34)]:
                r = score(x0, x1, A); m = st.mean(abs(v) for vs in r.values() for v in vs)
                if best is None or m < best[0]:
                    best = (m, x0, x1, A, r)
    m, x0, x1, A, r = best
    wb = st.mean(v for g in ("jan", "feb") for v in r[g])
    print(f"{label:<34}{x0:>4}{x1:>4}{A:>6.1f} | {m:.3f}  | " + " ".join(f"{st.mean(abs(v) for v in r[g]):>5.2f}" for g in order) + f" | {wb:+.2f}")
