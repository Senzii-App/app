"""Geocoding service using Nominatim (OpenStreetMap).
Direct port of src/services/geocode.rs.
"""
import urllib.parse
import httpx
from app.config import NOMINATIM_EMAIL
from typing import NamedTuple


class Coordinates(NamedTuple):
    latitude: float
    longitude: float


async def geocode_address(address: str) -> Coordinates | None:
    """Geocode an address string to (lat, lng) using Nominatim.
    Returns None if the address cannot be resolved.
    """
    if not address or not address.strip():
        return None

    email = NOMINATIM_EMAIL or "unset"
    encoded = urllib.parse.quote_plus(address)
    url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1"

    headers = {"User-Agent": f"SenziiApp/1.0 ({email})"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code != 200:
        print(f"[geocode] Nominatim returned status {resp.status_code}")
        return None

    results = resp.json()
    if not results:
        return None

    try:
        lat = float(results[0]["lat"])
        lon = float(results[0]["lon"])
    except (KeyError, ValueError):
        return None

    return Coordinates(latitude=lat, longitude=lon)