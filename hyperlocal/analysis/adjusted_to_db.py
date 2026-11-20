#!/usr/bin/env python3
"""URMA, and URMA adjusted by the cold-pool equation (what the station should read: URMA - A x factor), as model_analysis rows in the plotter's format: model "urma" and model "coldpool".
Night hours with all the inputs: URMA (log/downloaded.json), the station's hourly dewpoint (log/ambient_all.db), the HRRR wind 450 m up (log/w450_home_*.json). A = HYPERLOCAL_AMP (default 10).
   python adjusted_to_db.py [OUT.db] [--fill-day]      default log/model_adjusted.db; a new file
--fill-day: hours with no URMA row (the daytime, which was never downloaded) get the station's own hourly mean in both models, so the plotter draws no straight lines and no 90-degree biases across the day. For display only: those hours are zero bias by construction."""
import glob, json, math, os, sqlite3, statistics as st, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coldpool as c

c.load_env(); cfg = c.load_config()
A = float(os.environ.get("HYPERLOCAL_AMP", "10"))
FILL = "--fill-day" in sys.argv; args = [x for x in sys.argv[1:] if not x.startswith("--")]
out = args[0] if args else str(c.LOG / "model_adjusted.db")
if os.path.exists(out):
    os.unlink(out)
cache = json.load(open(c.LOG / "downloaded.json"))
URMA = {datetime.fromisoformat(k): v for k, v in cache["mod"].items()}
W = {}
for f in glob.glob(str(c.LOG / "w450_home_*.json")):
    W.update({datetime.fromisoformat(k): v for k, v in json.load(open(f)).items()})
acc = {}
for t, tf, dp in sqlite3.connect(c.LOG / "ambient_all.db").execute("SELECT utc, tempf, dewpoint FROM ambient"):
    h = datetime.fromisoformat(t.replace("Z", "+00:00")).replace(minute=0, second=0)
    a = acc.setdefault(h, [0, 0.0, 0, 0.0])
    a[0] += 1; a[1] += tf
    if dp is not None:
        a[2] += 1; a[3] += dp
OBS = {h: (a[1] / a[0], a[3] / a[2]) for h, a in acc.items() if a[0] >= 8 and a[2] >= 8}
db = sqlite3.connect(out)
ref = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "weather_archive.db")
ddl = sqlite3.connect(ref).execute("SELECT sql FROM sqlite_master WHERE name='model_analysis'").fetchone()[0] if os.path.exists(ref) else \
    "CREATE TABLE model_analysis (model TEXT NOT NULL, valid_time TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, temp_f REAL, temp_sd REAL, cloud_cover REAL, wind_speed REAL, precip REAL, PRIMARY KEY (model, valid_time, latitude, longitude))"
db.execute(ddl)
ins = "INSERT OR REPLACE INTO model_analysis (model, valid_time, latitude, longitude, temp_f, cloud_cover, wind_speed) VALUES (?,?,?,?,?,?,?)"
n_u = n_a = 0; eu, ea, bu, ba = [], [], [], []
for t, u in sorted(URMA.items()):
    db.execute(ins, ("urma", t.isoformat(), cfg["lat"], cfg["lon"], u[0], u[1], u[2])); n_u += 1
    o, w = OBS.get(t), W.get(t)
    if o is None or w is None:
        continue
    f = math.exp(-(w / 30.0) ** 3) * (1 - 0.8 * u[1] / 100) * min(1.0, max(0.0, u[0] - o[1]) / 12.0) * c.night_factor(t, cfg["lat"], cfg["lon"])
    adj = u[0] - A * f
    db.execute(ins, ("coldpool", t.isoformat(), cfg["lat"], cfg["lon"], adj, u[1], u[2])); n_a += 1
    eu.append(abs(u[0] - o[0])); ea.append(abs(adj - o[0])); bu.append(u[0] - o[0]); ba.append(adj - o[0])
if FILL:
    lo, hi = min(URMA), max(URMA); n_f = 0
    for h, o in OBS.items():
        if lo <= h <= hi and h not in URMA:
            for m in ("urma", "coldpool"):
                db.execute(ins, (m, h.isoformat(), cfg["lat"], cfg["lon"], o[0], None, None))
            n_f += 1
    print(f"--fill-day: {n_f} hours filled from the station")
db.commit()
print(f"urma rows {n_u}; coldpool (adjusted) rows {n_a} -> {out}")
print(f"over the {n_a} hours with a station reading: URMA minus station: mean {st.mean(bu):+.2f} F, mean abs {st.mean(eu):.2f}, rms {math.sqrt(st.mean(x*x for x in bu)):.2f};  adjusted minus station: mean {st.mean(ba):+.2f} F, mean abs {st.mean(ea):.2f}, rms {math.sqrt(st.mean(x*x for x in ba)):.2f}")
