#!/usr/bin/env python3
"""Cut the newest N days out of a weather_archive.db into a small database with the same tables and columns.

  python sample_db.py --db weather_archive.db --days 7 --out sample.db

Reads the original only (opened read-only). Copies station_obs and model_analysis rows for the last N days
(counted back from the newest row, not from today) and prints what the data looks like (stations, models, time format, units)
so the numbers can be checked. No keys or settings are copied; the rows do carry the station's coordinates
(model_analysis.latitude/longitude), so share the file only with whom you mean to.
"""
import argparse, sqlite3, sys
from datetime import datetime, timedelta
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--db", default="weather_archive.db")
p.add_argument("--days", type=int, default=7)
p.add_argument("--out", default="sample.db")
a = p.parse_args()

if not Path(a.db).exists():
    sys.exit(f"not found: {a.db}")
if Path(a.out).exists():
    sys.exit(f"{a.out} exists: remove it or pick another --out")

src = sqlite3.connect(f"file:{Path(a.db).resolve().as_posix()}?mode=ro", uri=True)
out = sqlite3.connect(a.out)


def cutoff(table, col):
    newest = src.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
    if not newest:
        return None, None
    # shift the date part and keep the rest of the text (time, separator, zone) as stored, so text comparison orders correctly
    day = datetime.strptime(newest[:10], "%Y-%m-%d") - timedelta(days=a.days)
    return newest, day.strftime("%Y-%m-%d") + newest[10:]


for table, col in (("station_obs", "timestamp"), ("model_analysis", "valid_time")):
    ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not ddl:
        print(f"{table}: not in the database, skipped")
        continue
    out.execute(ddl[0])
    newest, cut = cutoff(table, col)
    if cut is None:
        print(f"{table}: empty")
        continue
    cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
    marks = ",".join("?" * len(cols))
    rows = src.execute(f"SELECT * FROM {table} WHERE {col} >= ?", (cut,)).fetchall()
    out.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    print(f"\n{table}: {len(rows)} rows from {cut} to {newest}")
    print("  example", rows[0] if rows else None)
out.commit()

print("\nstations:", out.execute("SELECT station, COUNT(*), MIN(timestamp), MAX(timestamp) FROM station_obs GROUP BY station").fetchall())
print("models:  ", out.execute("SELECT model, COUNT(*), MIN(valid_time), MAX(valid_time) FROM model_analysis GROUP BY model").fetchall())
print("wind_speed ranges (unit check):",
      out.execute("SELECT model, MIN(wind_speed), MAX(wind_speed) FROM model_analysis GROUP BY model").fetchall(),
      out.execute("SELECT station, MIN(wind_speed), MAX(wind_speed) FROM station_obs GROUP BY station").fetchall())
out.close()
print(f"\nwritten: {a.out} ({Path(a.out).stat().st_size / 1e6:.1f} MB)")
