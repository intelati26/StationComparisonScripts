# hyperlocal

Overnight low at one station, and why it runs colder (or not) than the grid: a small equation and the tools used to fit and test it.

**Start here:** `EQUATION.md` (the equation, each term and what it earned), `RESEARCH.md` (what the literature says and what the data did and did not show).

## Layout
- `coldpool.py` the shared module (the equation, the data readers) and its command line; `termux/` the phone tools; the `.md` files the notes.
- `analysis/` every study and data-pull script (the fits, the tests, the plots). Run them from this folder: `python analysis/<name>.py`. They keep nothing about a place in the code: the home place and keys come from `.env`; the extra test places (`analysis/places.py`) from `log/places.json` (`{"p1": {"lat": 0.0, "lon": 0.0, "tag": "<file suffix>"}}`); the airport scripts from `HYPERLOCAL_AIRPORT` / `HYPERLOCAL_AIRPORTS_OTHER`. `log/` and `.env` are git-ignored.

## Use it
- `termux/low.py` tonight's low from Open-Meteo alone (stdlib only; runs in Termux). `python low.py` uses the place and site amplitude in `.env` (WEATHER_LAT, WEATHER_LON, HYPERLOCAL_AMP); `python low.py LAT LON [--amp 5|10] [--table]` overrides them; the low it prints has a +- 2.8 F standard deviation (`--sd`) and an optional bias correction (`--bias`, default 0); `--log` keeps each night's prediction, `analysis/score_low_log.py` scores the kept nights against the station; `termux/low-temp.sh` is a Termux:Widget shortcut.
- `maps/tonight_live.html` a page (open it in a browser) with live MAG and SPC 925 mb pictures and the low for a place you enter; nothing about the place is stored in the file.
- `coldpool.py forecast` the same equation with the station's live Ambient reading and the NWS hourly forecast; `coldpool.py fit [N]` fits it to a StationComparisonScripts database (and to the last N days downloaded); `coldpool.py track [H]`, `backfill`, `gribfill`, `dptfill` fill and compare the history.
- `analysis/spc925.py` keeps SPC's hourly 925 mb analysis crop so the night's pictures can be scrolled through.

## Tests and studies (each prints its own table)

- Ideas still to try, and what was tried and dropped: `IDEAS.md`.
- Whole-year data (the home station): `analysis/ambient_all.py` pulls every Ambient record the API still holds (all fields, valid ones) into `log/ambient_all.db`, stepping over outages; `analysis/ambient_to_db.py` writes them as a `station_obs` database in StationComparisonScripts' own format; `season_pull.py START END` adds URMA (AWS files, free, four hours at a time) for the night hours; `home_seasons.py [months]` fits the equation per season or month (amplitude, offset, errors, the wind threshold); `analysis/adjusted_to_db.py` writes URMA and URMA adjusted by the equation as `model_analysis` rows (model `urma` and `coldpool`), which `plotter.py` draws as a green line.
- Other checks: `analysis/winter_tests.py` (a fade through the night, the dryness term in vapour terms), `analysis/shear_test.py` and `analysis/gate_test.py` (what carries the decoupling: the wind at 450 m, gusts, shear, a Richardson-style term, a second 10 m gate), `analysis/wind_points.py` (the wind at each station's own point), `analysis/airport_phase.py` (URMA and the station against the nearest airport through the night), `analysis/coop_urma.py` and `analysis/coop_analyze.py` (observer stations from the Iowa Environmental Mesonet against URMA at their points; the field is read once per hour for all stations).
`analysis/check_equation.py` residuals and leave-one-night-out; `analysis/wind_check.py` the wind term; `analysis/end_to_end.py` the equation on archived NBM and HRRR forecasts (`analysis/nbm_leads.py`, `analysis/hrrr_leads.py`); `analysis/td_summer.py` the dewpoint bias by season;
`analysis/snow_nights.py`, `analysis/snow_effect.py`, `analysis/snowpack.py` snow nights; `analysis/wu_pull.py`, `analysis/urma_site.py`, `analysis/site_analysis.py`, `analysis/all_stations.py` the neighbouring Wunderground stations, terrain (`analysis/terrain.py`, `analysis/terrain_local.py`) and the site amplitude.

## Setup
Copy `.env.example` to `.env` (git-ignored) and fill in the same variables as the rest of this repo (AMBIENT_*, WEATHER_LAT, WEATHER_LON, WEATHER_DB_PATH), plus HYPERLOCAL_STATION (the station's name in `station_obs`) and, for the optional data pulls, GRIBSTREAM_TOKEN, WUNDERGROUND_API_KEY and HOME_AIRPORT.
Nothing in this folder holds a place: coordinates, station ids and keys are read from `.env` and `log/` (both git-ignored). Set the contact string in `coldpool.py`'s `UA` before using the NWS API.
