# hyperlocal

Overnight low at one station, and why it runs colder (or not) than the grid: a small equation and the tools used to fit and test it.

**Start here:** `EQUATION.md` (the equation, each term and what it earned), `RESEARCH.md` (what the literature says and what the data did and did not show).

## Layout
- `coldpool.py` the shared module (the equation, the data readers) and its command line; `plot_low.py` tonight's low as a picture (station, forecast, adjustment, band); `forecast_to_db.py` the adjusted forecast (and a backfill) into the plotter's database; `termux/` the phone tools; the `.md` files the notes.
- `analysis/` every study and data-pull script (the fits, the tests, the plots). Run them from this folder: `python analysis/<name>.py`. They keep nothing about a place in the code: the home place and keys come from `.env`; the extra test places (`analysis/places.py`) from `log/places.json` (`{"p1": {"lat": 0.0, "lon": 0.0, "tag": "<file suffix>"}}`); the airport scripts from `HYPERLOCAL_AIRPORT` / `HYPERLOCAL_AIRPORTS_OTHER`. `log/` and `.env` are git-ignored.

## Use it
- `termux/low.py` tonight's low from Open-Meteo alone (stdlib only; runs in Termux). `python low.py` uses the place and site amplitude in `.env` (WEATHER_LAT, WEATHER_LON, HYPERLOCAL_AMP); `python low.py LAT LON [--amp 5|10] [--table]` overrides them; the low it prints has a +- 2.8 F standard deviation (`--sd`) and an optional bias correction (`--bias`, default 0); `--log` keeps each night's prediction, `analysis/score_low_log.py` scores the kept nights against the station; `termux/low-temp.sh` is a Termux:Widget shortcut.
- `maps/tonight_live.html` a page (open it in a browser) with live MAG and SPC 925 mb pictures and the low for a place you enter; nothing about the place is stored in the file.
- `coldpool.py forecast` the same equation with the station's live Ambient reading and the NWS hourly forecast; `coldpool.py fit [N]` fits it to a StationComparisonScripts database (and to the last N days downloaded); `coldpool.py track [H]`, `backfill`, `gribfill`, `dptfill` fill and compare the history.
- `analysis/spc925.py` keeps SPC's hourly 925 mb analysis crop so the night's pictures can be scrolled through.

## Plotting the low (the equation in `plotter.py`)
What the plotter draws once the cold-pool rows are in its database (all green; model names are the `model_analysis` rows):
- **Solid, "URMA adjusted"** (`coldpool`): URMA minus the equation's drop, night hours. Made by `analysis/adjusted_to_db.py` (a separate database, or copy its rows in).
- **Dashed, with the +- SD band and a star at the lowest hour** (`coldpool_fc`): the forecast temperature minus the equation's drop, as a run issued it. This is the real test: earlier nights stay on the plot as they were issued, next to what the station did.
- **Dotted** (`coldpool_bf`): a hindsight backfill from Open-Meteo's stitched HRRR hours, so a first `--days 7` plot is not empty. It is not a forecast; it carries the HRRR stream's bias (about 1.9 F cold, see `EQUATION.md`).
- The bias panel carries the same lines as errors (model minus station), and the console prints, per night, `predicted low / station low / difference`, plus a status line saying how many cold-pool rows were loaded and from which database (if the plot shows nothing, that line says why).

Daily routine, from the folder the collectors run in (the settings are `WEATHER_LAT`, `WEATHER_LON`, optional `HYPERLOCAL_AMP`, `HYPERLOCAL_BIAS`, `HYPERLOCAL_SD`):
```
python hyperlocal/forecast_to_db.py --backfill 7     # once: the last 7 days in hindsight
python hyperlocal/forecast_to_db.py                  # each evening: tonight's forecast (a cron job or timer)
python plotter.py --days 7
```
- `forecast_to_db.py` writes into the collectors' own database: it finds `weather_common.py` (in the folder above, in `collect/`, or where you run it) and takes `WEATHER_DB_PATH` relative to that file's folder, as the collectors do, and prints `database: ...` first. It touches only `coldpool_fc` / `coldpool_bf` rows, creates `model_analysis` if the database has none, and a new run replaces only its own hours onward. `--from-log FILE` loads the runs `termux/low.py --log` kept; `--db FILE` names the database.
- `plot_low.py --png` is the quick, database-free picture of tonight (the station's last hours from the Ambient API, the forecast, the adjustment, the band, the wind aloft and the hourly drop). `termux/low_plot.py` is its phone version for the notification.
- The plotter's layout: everything below the bias panel (the axis, its legend, the daily summary box, the cloudiness panel of solar radiation against the clear-sky curve, and the `--windrose` rose) is one stack positioned from measured extents, so no gap is fixed and a new block just joins the stack. A missing `clearsky_calibration` table falls back to the raw Haurwitz curve.

## Tests and studies (each prints its own table)

- Ideas still to try, and what was tried and dropped: `IDEAS.md`.
- Adjusted forecast in the plotter: `forecast_to_db.py` writes tonight's forecast (the equation's hourly adjusted temperature, +- SD) into the plotter's database as model `coldpool_fc`; `plotter.py` then draws it dashed with the SD band and a star at the predicted low, and in the bias panel the error of the forecast *as it was issued* against the station, with a per-night "predicted low / station low" printout. Run it each evening (cron or a timer): a new run replaces its own hours onward and leaves earlier hours as issued, so a multi-day window (`--days 7`) holds the last nights' forecasts against what happened. `--from-log FILE` loads every run that `termux/low.py --log` kept (copy the phone's log over); `--backfill N` reconstructs the last N days in hindsight from Open-Meteo's stitched HRRR hours (model `coldpool_bf`, drawn dotted and labelled as hindsight: for a first `--days 7` plot before any forecasts have been kept; hours that already hold a real run are skipped). `analysis/ambient_all.py` with no option now pulls the newest station records; `--older` continues into the history.
- Whole-year data (the home station): `analysis/ambient_all.py` pulls every Ambient record the API still holds (all fields, valid ones) into `log/ambient_all.db`, stepping over outages; `analysis/ambient_to_db.py` writes them as a `station_obs` database in StationComparisonScripts' own format; `season_pull.py START END` adds URMA (AWS files, free, four hours at a time) for the night hours; `home_seasons.py [months]` fits the equation per season or month (amplitude, offset, errors, the wind threshold); `analysis/adjusted_to_db.py` writes URMA and URMA adjusted by the equation as `model_analysis` rows (model `urma` and `coldpool`), which `plotter.py` draws as a green line.
- Other checks: `analysis/winter_tests.py` (a fade through the night, the dryness term in vapour terms), `analysis/shear_test.py` and `analysis/gate_test.py` (what carries the decoupling: the wind at 450 m, gusts, shear, a Richardson-style term, a second 10 m gate), `analysis/wind_points.py` (the wind at each station's own point), `analysis/airport_phase.py` (URMA and the station against the nearest airport through the night), `analysis/coop_urma.py` and `analysis/coop_analyze.py` (observer stations from the Iowa Environmental Mesonet against URMA at their points; the field is read once per hour for all stations).
`analysis/check_equation.py` residuals and leave-one-night-out; `analysis/wind_check.py` the wind term; `analysis/end_to_end.py` the equation on archived NBM and HRRR forecasts (`analysis/nbm_leads.py`, `analysis/hrrr_leads.py`); `analysis/td_summer.py` the dewpoint bias by season;
`analysis/snow_nights.py`, `analysis/snow_effect.py`, `analysis/snowpack.py` snow nights; `analysis/wu_pull.py`, `analysis/urma_site.py`, `analysis/site_analysis.py`, `analysis/all_stations.py` the neighbouring Wunderground stations, terrain (`analysis/terrain.py`, `analysis/terrain_local.py`) and the site amplitude.

## Setup
Copy `.env.example` to `.env` (git-ignored) and fill in the same variables as the rest of this repo (AMBIENT_*, WEATHER_LAT, WEATHER_LON, WEATHER_DB_PATH), plus HYPERLOCAL_STATION (the station's name in `station_obs`) and, for the optional data pulls, GRIBSTREAM_TOKEN, WUNDERGROUND_API_KEY and HOME_AIRPORT.
Nothing in this folder holds a place: coordinates, station ids and keys are read from `.env` and `log/` (both git-ignored). Set the contact string in `coldpool.py`'s `UA` before using the NWS API.
