# StationComparisonScripts
Set of scripts to compare a personal weather station to observations/other stations

## Contents

### Core Comparison Scripts
- `BarMatrix.py` — Temperature×Humidity matrix with marginal bars and density contours
- `bymonth.py` — Unified monthly weather calendar with calendar and visual formats
- `bymonth_viz.py` — Compact monthly calendar with integrated mini-charts, sparklines, and day-level summaries (standalone visual format)
- `consecutive_days.py` — Detect consecutive days of high/low temperature/rain
- `diurnal.py` — High-resolution daily profile extraction from ~1min-level data
- `plotter.py` — Interactive visualization of station comparisons and model forecasts

### Data Collection Scripts
- `collect_models.py` — Fetch fast-turnaround model tiers (HRRR/RRFS) only
- `collect_stations.py` — Fetch NWS station obs (KSGF, KBBG) and Ambient Weather obs
- `collect_urma.py` — Gold-standard URMA/RTMA sweep with deep backfill and anomaly repair

### Shared Library
- `weather_common.py` — Common utilities for database operations, data fetching, and parsing

## Use

### Quick Start
```bash
# View this month (current month) - detailed calendar table
python bymonth.py --calendar

# View specific month - compact visual calendar  
python bymonth.py --viz 2026-08

# Quick comparison matrix
python BarMatrix.py --station G6964

# Both formats side-by-side (HTML only)
python bymonth.py --both

# Inspect database structure
python bymonth.py --inspect

# View interactive plots
python plotter.py
```


### Data Collection
```bash
# Fetch latest station observations (last 48h)
python collect_stations.py --station KSGF,KBBG

# Fetch model forecasts (last 48h)
python collect_models.py

# Gold-standard URMA/RTMA data (last 48h)
python collect_urma.py

# Re-download any data
python collect_stations.py --station KSGF --force
python collect_models.py --force
python collect_urma.py --force
```

### Advanced Usage
```bash
# Use a different database file
python bymonth.py --db "/path/to/weather_archive.db"

# Inspect database structure
python bymonth.py --inspect

# Export monthly summary in text format
python bymonth.py 2026-08 --format text --out aug.txt

# View interactive plots
python plotter.py
```

## bymonth.py Usage

### Basic Calendar Views
- `python bymonth.py --calendar` - Current month calendar format (default)
- `python bymonth.py --viz` - Current month visual calendar format
- `python bymonth.py --both` - Both formats side-by-side (HTML only)
- `python bymonth.py --calendar 2026-08` - Specific month calendar format
- `python bymonth.py --viz 2026-08` - Specific month visual calendar format
- `python bymonth.py --both 2026-08` - Both formats for specific month (HTML only)

### Station Selection
- `--station G6964,KBBG` - Restrict to specific stations (comma-separated)
- `--dewpoint-c G6964` - Convert dewpoint from °C to °F for specific station

### Output Options
- `--format html` - HTML output (default)
- `--format text` - Text output
- `--out filename` - Custom output filename
- `--open` - Open HTML in browser

### Database & Advanced
- `--db path/to/db.sqlite` - Specify custom database file
- `--inspect` - Dump database structure and sample data
- `--tz America/Chicago` - Set timezone for day boundaries
- `--first-weekday {sun|mon}` - Start week on Sunday or Monday
- `--title "My Weather Summary"` - Custom title for output

## Options

The scripts support several common command-line options:

### Time Range Options
- `--days N` — Lookback window in days (default: varies by script)
- `--hours N` — Lookback window in hours (overrides --days)
- `--start-date YYYY-MM-DD` — Start date (inclusive)
- `--end-date YYYY-MM-DD` — End date (inclusive)

### Station Options
- `--station ID` — Station identifier (e.g., "G6964", "KSGF", "KBBG")
- Can accept comma-separated list for multiple stations

### Output Options
- `--format {html,text}` — Output format (bymonth.py, bymonth_viz.py)
- `--output FILE` or `-o FILE` — Output file path
- `--open` — Open HTML output in browser (where supported)

### Database Options
- `--db PATH` — Path to SQLite database (default: weather_archive.db)

### Model-Specific Options
- `--enable-rrfs` — Include RRFS in model fetch (collect_models.py)
- `--hrrr-max-lead N` — Maximum HRRR forecast lead hours (collect_models.py)

### Maintenance Options
- `--vacuum` — Reclaim disk space from overwritten rows (collect_urma.py)
- `--backup` — Snapshot and gzip the database (collect_urma.py)
- `--inspect` — Dump table/device/sample row and exit
