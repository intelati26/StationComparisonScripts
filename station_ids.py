"""
station_ids.py -- the id the personal (Ambient Weather) station goes by in the
database's station_obs / clearsky_calibration tables.

Set AMBIENT_STATION_ID in the environment or a local .env file; with nothing
set it is "AMBIENT". A database whose rows already carry another id keeps
working when that id is set here (no rows need rewriting).

This module needs no API keys, so the plotting and analysis scripts can import
it without weather_common's required environment variables.
"""
import os

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True) or find_dotenv())
except ImportError:
    pass  # dotenv is optional

AMBIENT_ID = os.environ.get("AMBIENT_STATION_ID") or "AMBIENT"
