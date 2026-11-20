"""The places the analysis scripts work on: "home" (the place in .env) plus the random test places listed in log/places.json (git-ignored, so no place names or coordinates live in the code):
    {"p1": {"lat": 0.0, "lon": 0.0, "tag": "<suffix of that place's files>"}, "p2": {...}}
A place's files in log/ are wu_hourly_<tag>.json, urma_<tag>.json (and wu_nearby_<tag>.json, site_indices_<tag>.json); home's are wu_hourly.json and wu_nearby.json."""
import json
import coldpool as c


def load(cfg):
    out = {"home": {"lat": cfg["lat"], "lon": cfg["lon"], "tag": None}}
    f = c.LOG / "places.json"
    if f.exists():
        out.update(json.load(open(f)))
    return out


def airport(env=None):
    """The 3-letter id (lower case) of the nearest airport, HYPERLOCAL_AIRPORT in .env (the airport scripts need it)."""
    import os
    c.load_env()
    a = (os.environ.get("HYPERLOCAL_AIRPORT") or "").strip().lower()
    if not a:
        raise SystemExit("set HYPERLOCAL_AIRPORT (the 3-letter id of the nearest ASOS airport) in .env")
    return a


def other_airports():
    """Other open-site airports for comparison, HYPERLOCAL_AIRPORTS_OTHER in .env (comma separated 3-letter ids)."""
    import os
    c.load_env()
    return [x.strip().lower() for x in (os.environ.get("HYPERLOCAL_AIRPORTS_OTHER") or "").split(",") if x.strip()]
