# hyperlocal

Valley cold-air-pool prediction for one station.

- `coldpool.py forecast` hourly table for the next 24 h (NWS gridpoint temperature, dewpoint, cloud, wind, plus the station's live reading); logs tonight's predicted low
- `coldpool.py fit` fits the model's coefficients to this project's `weather_archive.db` (URMA/RTMA minus the station on clear nights); writes `log/fit.json`
- Config: the same `.env` variables as the rest of this repo (AMBIENT_*, WEATHER_LAT/LON, WEATHER_DB_PATH) plus HYPERLOCAL_VALLEY_FT and HYPERLOCAL_RIM_FT (valley floor and surrounding rim elevations). Set the contact string in `UA` before using the NWS API.
- It stops with an error when a source fails; it never substitutes made-up data. Stdlib only.
