"""
Location-name -> coordinates.

Uses Google Geocoding when a key is present (better Indonesian coverage,
returns admin components), falls back to OSM Nominatim so the app is usable
with no key at all.
"""
import time

import requests

import config

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_GOOGLE_GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"

# Nominatim's usage policy is max 1 request/second. Enforce it here rather
# than trusting callers.
_last_nominatim_call = 0.0


def geocode(place_name, limit=5):
    """Return [{display, lat, lon, source}] for a free-text location name."""
    place_name = (place_name or "").strip()
    if not place_name:
        return []
    if config.GOOGLE_API_KEY:
        try:
            return _geocode_google(place_name, limit)
        except Exception:
            pass  # fall through to Nominatim
    return _geocode_nominatim(place_name, limit)


def _geocode_google(place_name, limit):
    r = requests.get(
        _GOOGLE_GEOCODE,
        params={
            "address": place_name,
            "key": config.GOOGLE_API_KEY,
            "region": "id",
            "language": "id",
        },
        timeout=15,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") not in ("OK", "ZERO_RESULTS"):
        raise RuntimeError(payload.get("error_message") or payload.get("status"))
    out = []
    for res in payload.get("results", [])[:limit]:
        loc = res["geometry"]["location"]
        out.append(
            {
                "display": res.get("formatted_address", place_name),
                "lat": loc["lat"],
                "lon": loc["lng"],
                "source": "google",
            }
        )
    return out


def _geocode_nominatim(place_name, limit):
    global _last_nominatim_call
    gap = time.time() - _last_nominatim_call
    if gap < 1.0:
        time.sleep(1.0 - gap)
    r = requests.get(
        _NOMINATIM,
        params={
            "q": place_name,
            "format": "json",
            "limit": limit,
            "countrycodes": "id",
            "addressdetails": 0,
        },
        headers={"User-Agent": config.NOMINATIM_UA},
        timeout=20,
    )
    _last_nominatim_call = time.time()
    r.raise_for_status()
    return [
        {
            "display": item.get("display_name", place_name),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "source": "nominatim",
        }
        for item in r.json()
    ]
