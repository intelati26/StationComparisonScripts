#!/usr/bin/env python3
"""Scores the predictions kept by termux/low.py --log against the station: per run, the equation's low (without the bias) and the hour by hour temperatures against what the station read, then the mean and SD over the nights.
The station comes from the Ambient API (coldpool.py, the same .env).   python score_low_log.py [LOGFILE]     default HYPERLOCAL_LOG from .env, else log/low_log.jsonl"""
import json, os, statistics as st, sys
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from datetime import datetime, timedelta, timezone
import coldpool as c

c.load_env()
path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HYPERLOCAL_LOG") or os.path.join(HERE, "log", "low_log.jsonl")
runs = [json.loads(l) for l in open(path) if l.strip()]
z = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
now = datetime.now(timezone.utc)
done = [r for r in runs if z(r["hours"][-1]["utc"]) < now - timedelta(minutes=10)]
print(f"{len(runs)} runs logged, {len(done)} whose night is over")
if not done:
    sys.exit(0)
lo, hi = min(z(r["hours"][0]["utc"]) for r in done), max(z(r["hours"][-1]["utc"]) for r in done)
S = c.ambient_history(lo - timedelta(hours=1), hi + timedelta(hours=1))
S = {k.replace(tzinfo=timezone.utc) if k.tzinfo is None else k: v[0] for k, v in S.items()}
rows = []
print(f"{'run (UTC)':<17}{'lead':>5}{'night of':>10}{'source':>7} | {'equation low':>12}{'forecast low':>13}{'actual':>8}{'eq - actual':>12}{'HRRR - actual':>14} | hourly: mean  SD (hours)")
for r in done:
    hrs = r["hours"]; ks = [z(h["utc"]) for h in hrs]
    have = [(h, S[k]) for h, k in zip(hrs, ks) if k in S]
    if len(have) < 6:
        continue
    actual = min(v for _, v in have)
    eq = r["equation_low"]; fc = r["forecast_low"]
    hm = [h["final"] - v for h, v in have]
    lead = (ks[0] - z(r["run_utc"])).total_seconds() / 3600
    night = (ks[0] - timedelta(hours=6)).strftime("%d %b")
    rows.append(dict(eq=eq - actual, fc=fc - actual, hm=hm))
    print(f"{r['run_utc'][5:16].replace('T', ' '):<17}{lead:>5.0f}{night:>10}{r['source']:>7} | {eq:>12.1f}{fc:>13.1f}{actual:>8.1f}{eq - actual:>+12.1f}{fc - actual:>+14.1f} | {st.mean(hm):+6.1f} {st.pstdev(hm):4.1f} ({len(hm)})")
if len(rows) >= 3:
    for k, lab in (("eq", "the equation's low"), ("fc", "the HRRR low alone")):
        e = [x[k] for x in rows]
        print(f"{lab:<22} over {len(e)} runs: mean {st.mean(e):+.2f} F, SD {st.pstdev(e):.2f} F, typical miss {st.mean(abs(x) for x in e):.2f} F")
else:
    print("(a mean and SD need at least 3 runs)")
