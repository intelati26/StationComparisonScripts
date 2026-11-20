#!/usr/bin/env python3
"""Column-water term from the forecast PWAT (BUFKIT HRRR 22Z run at the home airport) on all nights: does multiplying the drop by (PW_expected_from_surface_dewpoint / PW)^n cut the equation's miss?  Needs log/bufkit_pw.json (bufkit_pull.py), log/nbs_nights.json.   python pw_fit.py"""
import glob, json, math, os, sqlite3, statistics as st, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta
import coldpool as c
cfg = c.load_config()
URMA = {datetime.fromisoformat(k): v for k, v in json.load(open(c.LOG / "downloaded.json"))["mod"].items()}
W = {}
for f in glob.glob(str(c.LOG / "w450_home_*.json")): W.update({datetime.fromisoformat(k): v for k, v in json.load(open(f)).items()})
acc = {}
for t, tf, dp in sqlite3.connect(c.LOG / "ambient_all.db").execute("SELECT utc, tempf, dewpoint FROM ambient"):
    h = datetime.fromisoformat(t.replace("Z", "+00:00")).replace(minute=0, second=0)
    a = acc.setdefault(h, [0, 0.0, 0, 0.0]); a[0] += 1; a[1] += tf
    if dp is not None: a[2] += 1; a[3] += dp
OBS = {h: (a[1] / a[0], a[3] / a[2]) for h, a in acc.items() if a[0] >= 8 and a[2] >= 8}
PWF = json.load(open(c.LOG / "bufkit_pw.json")); good = {r["E"] for r in json.load(open(c.LOG / "nbs_nights.json"))}
nights = {}
for t in sorted(URMA):
    if t in OBS and t in W and c.night_factor(t, cfg["lat"], cfg["lon"]) >= 1.0:
        E = (t - timedelta(hours=13)).date().isoformat()
        if E in good and PWF.get(E): nights.setdefault(E, []).append(t)
sat = lambda tf: 6.112 * math.exp(17.62 * ((tf - 32) / 1.8) / (243.12 + (tf - 32) / 1.8))
D = {}
for E, ks in list(nights.items()):
    pw = [PWF[E]["pw"].get(k.strftime("%Y-%m-%dT%H")) for k in ks]; pw = [p for p in pw if p is not None]
    if len(ks) < 9 or len(pw) < 6: del nights[E]; continue
    D[E] = (st.mean(pw), sat(st.mean(OBS[k][1] for k in ks)))
Es = sorted(nights); n = len(Es); mx = st.mean(D[E][1] for E in Es); my = st.mean(D[E][0] for E in Es)
b = sum((D[E][1] - mx) * (D[E][0] - my) for E in Es) / sum((D[E][1] - mx) ** 2 for E in Es); a0 = my - b * mx
def corr(x, y):
    mx_, my_ = st.mean(x), st.mean(y); return sum((p - mx_) * (q - my_) for p, q in zip(x, y)) / (len(x) * st.pstdev(x) * st.pstdev(y))
print(f"{n} nights; forecast column water vs the surface dewpoint's vapour pressure: corr {corr([D[E][1] for E in Es],[D[E][0] for E in Es]):+.2f}  (PW ~ {a0:.1f} + {b:.2f} e)")
ratio = {E: D[E][0] / max(3.0, a0 + b * D[E][1]) for E in Es}
def run(A, nexp, sub=None):
    e = []
    for E in (sub or Es):
        ks = nights[E]; g = ratio[E] ** -nexp
        low = min(URMA[t][0] - A * g * math.exp(-(W[t] / 30) ** 3) * (1 - 0.8 * URMA[t][1] / 100) * min(1.0, max(0.0, URMA[t][0] - OBS[t][1]) / 12) for t in ks)
        e.append(low - min(OBS[t][0] for t in ks))
    return e
worst = sorted(Es, key=lambda E: -abs(run(10, 0, [E])[0]))[:20]
print("exponent n: drop x (expected PW / forecast PW)^n -> best A, MAE, SD (all nights | the 20 worst)")
for nexp in (0, 0.25, 0.5, 0.75, 1.0, 1.5):
    best = min(((st.mean(abs(x) for x in run(A, nexp)), A) for A in (8, 9, 10, 11, 12, 13)))
    e = run(best[1], nexp); ew = run(best[1], nexp, worst)
    print(f"  n={nexp:<4} A={best[1]:<3} MAE {best[0]:.2f} SD {st.pstdev(e):.2f} mean {st.mean(e):+.2f} | worst-20 MAE {st.mean(abs(x) for x in ew):.2f}")
e0 = run(10, 0); print(f"corr(error, log PW ratio) = {corr(e0,[math.log(ratio[E]) for E in Es]):+.2f}")
if "--plot" in sys.argv:
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    SEAS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
    COL = {"winter": "#6250d6", "spring": "#1baf7a", "summer": "#eda100", "autumn": "#d95926"}
    act = {E: min(OBS[t][0] for t in nights[E]) for E in Es}
    e0 = run(10, 0); e5 = run(10, 0.5); wset = set(worst)
    fig, ax = plt.subplots(1, 2, figsize=(13, 6), dpi=130)
    x = [math.log2(ratio[E]) for E in Es]
    for s, col in COL.items():
        ix = [i for i, E in enumerate(Es) if SEAS[int(E[5:7])] == s]
        ax[0].scatter([x[i] for i in ix], [e0[i] for i in ix], s=24, color=col, alpha=.85, edgecolor="white", linewidths=.4, label=f"{s} ({len(ix)})")
    iw = [i for i, E in enumerate(Es) if E in wset]
    ax[0].scatter([x[i] for i in iw], [e0[i] for i in iw], s=70, facecolors="none", edgecolors="black", linewidths=.9, label="20 worst nights")
    mx_, my_ = st.mean(x), st.mean(e0); sl = sum((p - mx_) * (q - my_) for p, q in zip(x, e0)) / sum((p - mx_) ** 2 for p in x)
    xs = [min(x), max(x)]; ax[0].plot(xs, [my_ + sl * (v - mx_) for v in xs], color="black", lw=1.2, label=f"trend {sl:+.1f} F per doubling, corr {corr(x, e0):+.2f}")
    ax[0].axhline(0, color="gray", lw=.8); ax[0].axvline(0, color="gray", lw=.8, ls=":")
    ax[0].set_xlabel("forecast column water vs what the surface dewpoint predicts\n(left = drier column than the surface suggests; each step = a factor of 2)")
    ax[0].set_ylabel("the equation's miss, predicted - actual low (F)"); ax[0].set_title("Does the dry column explain the misses? (analysis inputs, 287 nights)", fontsize=10); ax[0].grid(alpha=.25); ax[0].legend(fontsize=8, frameon=False, loc="upper right")
    lo, hi = min(act.values()) - 4, max(act.values()) + 4
    for s, col in COL.items():
        ix = [i for i, E in enumerate(Es) if SEAS[int(E[5:7])] == s]
        ax[1].scatter([act[Es[i]] for i in ix], [act[Es[i]] + e5[i] for i in ix], s=24, color=col, alpha=.85, edgecolor="white", linewidths=.4)
    ax[1].scatter([act[Es[i]] for i in iw], [act[Es[i]] + e5[i] for i in iw], s=70, facecolors="none", edgecolors="black", linewidths=.9)
    sd5 = st.pstdev(e5)
    ax[1].fill_between([lo, hi], [lo - sd5, hi - sd5], [lo + sd5, hi + sd5], color="gray", alpha=.16, label=f"+-1 SD = {sd5:.1f} F")
    for sg in (1, -1): ax[1].plot([lo, hi], [lo + sg * 2 * sd5, hi + sg * 2 * sd5], color="gray", lw=1, ls="--", label=f"+-2 SD = {2*sd5:.1f} F" if sg == 1 else None)
    ax[1].plot([lo, hi], [lo, hi], color="black", lw=1); ax[1].set_xlim(lo, hi); ax[1].set_ylim(lo, hi); ax[1].set_aspect("equal"); ax[1].grid(alpha=.25)
    ax[1].set_xlabel("the station's actual night low (F)"); ax[1].set_ylabel("predicted (F)")
    ax[1].set_title(f"With the column-water term (n = 0.5): MAE {st.mean(abs(v) for v in e5):.2f} F against {st.mean(abs(v) for v in e0):.2f} F without", fontsize=10); ax[1].legend(fontsize=8, frameon=False, loc="upper left")
    fig.tight_layout(); out = c.HERE / "maps" / "pw_scatter.png" if hasattr(c, "HERE") else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps", "pw_scatter.png"); fig.savefig(out); print("saved", out)
