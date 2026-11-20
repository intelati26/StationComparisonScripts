# hyperlocal

Overnight low at one station, and why it runs colder (or not) than the grid: a small equation and the tools used to fit and test it.

**Start here:** `EQUATION.md` (the equation, each term and what it earned), `RESEARCH.md` (what the literature says and what the data did and did not show).

## Use it
- `termux/low.py` tonight's low from Open-Meteo alone (stdlib only; runs in Termux). `python low.py` uses the place and site amplitude in `.env` (WEATHER_LAT, WEATHER_LON, HYPERLOCAL_AMP); `python low.py LAT LON [--amp 5|10] [--table]` overrides them; `termux/low-temp.sh` is a Termux:Widget shortcut.
- `maps/tonight_live.html` a page (open it in a browser) with live MAG and SPC 925 mb pictures and the low for a place you enter; nothing about the place is stored in the file.
- `coldpool.py forecast` the same equation with the station's live Ambient reading and the NWS hourly forecast; `coldpool.py fit [N]` fits it to a StationComparisonScripts database (and to the last N days downloaded); `coldpool.py track [H]`, `backfill`, `gribfill`, `dptfill` fill and compare the history.
- `spc925.py` keeps SPC's hourly 925 mb analysis crop so the night's pictures can be scrolled through.

## Tests and studies (each prints its own table)
`check_equation.py` residuals and leave-one-night-out; `wind_check.py` the wind term; `end_to_end.py` the equation on archived NBM and HRRR forecasts (`nbm_leads.py`, `hrrr_leads.py`); `td_summer.py` the dewpoint bias by season;
`snow_nights.py`, `snow_effect.py`, `snowpack.py` snow nights; `wu_pull.py`, `urma_site.py`, `site_analysis.py`, `all_stations.py` the neighbouring Wunderground stations, terrain (`terrain.py`, `terrain_local.py`) and the site amplitude.

## Setup
Copy `.env.example` to `.env` (git-ignored) and fill in the same variables as the rest of this repo (AMBIENT_*, WEATHER_LAT, WEATHER_LON, WEATHER_DB_PATH), plus HYPERLOCAL_STATION (the station's name in `station_obs`) and, for the optional data pulls, GRIBSTREAM_TOKEN, WUNDERGROUND_API_KEY and HOME_AIRPORT.
Nothing in this folder holds a place: coordinates, station ids and keys are read from `.env` and `log/` (both git-ignored). Set the contact string in `coldpool.py`'s `UA` before using the NWS API.
