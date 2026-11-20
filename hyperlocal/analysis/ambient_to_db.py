#!/usr/bin/env python3
"""log/ambient_all.db (raw Ambient records) -> a database in StationComparisonScripts' own format (the station_obs table, the AMBIENT_FIELD_MAP columns), for the plotter and the other scripts. Writes a new file; the original is never touched.
  python ambient_to_db.py [OUT.db]        station name from HYPERLOCAL_STATION; the schema from weather_archive.db (this folder) or from ../weather_common.py's table"""
import ast, json, os, re, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))
import coldpool as c

c.load_env()
station = os.environ.get("HYPERLOCAL_STATION") or sys.exit("set HYPERLOCAL_STATION")
out = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "log" / "ambient_station_obs.db")
src = None
for cand in (HERE.parent / "weather_common.py", Path(os.environ.get("SCS_DIR", "")) / "weather_common.py"):
    if cand.is_file():
        src = cand.read_text(); break
src = src or sys.exit("weather_common.py not found: put this folder in the StationComparisonScripts clone, or set SCS_DIR to it")
m = re.search(r"AMBIENT_FIELD_MAP\s*=\s*(\{.*?\n\})", src, re.S)
fmap = ast.literal_eval(m.group(1))                                   # {api field: db column}
schema = re.search(r"(CREATE TABLE IF NOT EXISTS station_obs \(.*?\n\)\s*;?)", src, re.S)
if out.exists():
    out.unlink()
db = sqlite3.connect(out)
ref = HERE / "weather_archive.db"                                      # the user's own database (or its sample) has the full, migrated schema: copy that when it is here
if ref.exists():
    for (ddl,) in sqlite3.connect(ref).execute("SELECT sql FROM sqlite_master WHERE tbl_name='station_obs' AND sql IS NOT NULL"):
        db.execute(ddl)
else:
    db.executescript(schema.group(1))
cols = ["station", "timestamp"] + list(fmap.values())
sql = f"INSERT OR REPLACE INTO station_obs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
n = 0
for (raw,) in sqlite3.connect(c.LOG / "ambient_all.db").execute("SELECT raw FROM ambient ORDER BY utc"):
    r = json.loads(raw)
    if not r.get("date"):
        continue
    ts = datetime.fromisoformat(r["date"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    vals = []
    for f in fmap:
        v = r.get(f)
        try:
            vals.append(None if v is None else (int(v) if f in ("lightning_day", "lightning_hour") else float(v)))
        except (TypeError, ValueError):
            vals.append(None)
    db.execute(sql, (station, ts, *vals)); n += 1
db.commit()
print(f"{n} rows -> {out}; {len(fmap)} fields; example timestamp {ts}")
