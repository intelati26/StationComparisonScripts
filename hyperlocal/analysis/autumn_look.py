#!/usr/bin/env python3
"""The autumn nights (Sep-Nov) of the equation on analysis inputs: monthly picture and the worst nights with their regime.   python autumn_look.py"""
import math, statistics as st, sys, runpy, io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    g = runpy.run_path(__file__.replace("autumn_look", "pw_fit"))
Es, nights, URMA, W, OBS, ratio, run = (g[k] for k in ("Es", "nights", "URMA", "W", "OBS", "ratio", "run"))
e = dict(zip(Es, run(10, 0)))
aut = [E for E in Es if E[5:7] in ("09", "10", "11")]
print(f"autumn: {len(aut)} nights, mean {st.mean(e[E] for E in aut):+.2f}, SD {st.pstdev([e[E] for E in aut]):.2f}, MAE {st.mean(abs(e[E]) for E in aut):.2f}")
for ym in sorted({E[:7] for E in aut}):
    g_ = [e[E] for E in aut if E[:7] == ym]; print(f"  {ym} n={len(g_):>2} mean {st.mean(g_):+5.2f} SD {st.pstdev(g_):4.2f} MAE {st.mean(abs(v) for v in g_):4.2f}")
def feat(E):
    ks = nights[E]; T = [URMA[k][0] for k in ks]
    return dict(low=min(OBS[k][0] for k in ks), cloud=st.mean(URMA[k][1] for k in ks), wind=max(W[k] for k in ks), wmean=st.mean(W[k] for k in ks), spread=st.mean(URMA[k][0] - OBS[k][1] for k in ks), pw=ratio[E], t0=T[0], tmin=min(T))
print("\nworst autumn nights (error = predicted - actual):")
print(f"  {'night':<11}{'err':>5} {'low':>4} {'cloud%':>6} {'wind max/mean':>14} {'T-Td':>5} {'PWratio':>8}")
for E in sorted(aut, key=lambda E: -abs(e[E]))[:14]:
    f = feat(E); print(f"  {E:<11}{e[E]:>+5.1f} {f['low']:>4.0f} {f['cloud']:>6.0f} {f['wind']:>6.0f}/{f['wmean']:<6.0f} {f['spread']:>5.1f} {f['pw']:>8.2f}")
F = {E: feat(E) for E in aut}
print("\nautumn error by regime (n, mean, SD):")
def grp(name, key, cuts):
    for lo, hi in cuts:
        v = [e[E] for E in aut if lo <= F[E][key] < hi]
        if len(v) >= 6: print(f"  {name} {lo}-{hi}: n={len(v):>3} mean {st.mean(v):+5.2f} SD {st.pstdev(v):4.2f}")
grp("cloud%", "cloud", [(0, 15), (15, 50), (50, 101)]); grp("wind max mph", "wind", [(0, 20), (20, 30), (30, 40), (40, 99)]); grp("T-Td F", "spread", [(0, 5), (5, 10), (10, 99)]); grp("actual low F", "low", [(0, 40), (40, 55), (55, 66), (66, 99)])
