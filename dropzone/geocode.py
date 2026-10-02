"""Nominatim geocoding, used in exactly two places.

- Reverse, once at game start, to learn the country code. The code is stored in
  session state and is consumed only by `tables.py`.
- Forward, for `submit_guess`, to turn the player's place name into a point we
  can measure a distance from.

Nominatim's usage policy caps us at one request per second, so every call goes
through a process-wide lock.
"""

from __future__ import annotations

import asyncio
import time

import httpx

SEARCH_URL = "https://nominatim.openstreetmap.org/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "Dropzone/0.1 (course project; tool-calling geography game; contact aa5790)"
TIMEOUT = httpx.Timeout(15.0)
MIN_INTERVAL_S = 1.0

# Ask for English names. Without this Nominatim answers in the local language,
# so an end-of-run reveal could come back in Arabic, Thai or Cyrillic - which
# both looks like a bug and is no use to a player reading the result.
LANGUAGE = "en"

_lock = asyncio.Lock()
_last_request = 0.0


async def _get(url: str, params: dict[str, str]) -> dict | list | None:
    """Rate-limited GET against Nominatim."""
    global _last_request
    async with _lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": LANGUAGE,
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
                response = await client.get(url, params={**params, "accept-language": LANGUAGE})
                response.raise_for_status()
                return response.json()
        finally:
            _last_request = time.monotonic()


async def geocode_place(place_name: str) -> dict[str, float | str] | None:
    """Best match for a place name: `{lat, lon, display_name}` or None.

    The display name is for the end-of-game reveal only, never a tool result
    before the final guess.
    """
    data = await _get(SEARCH_URL, {"q": place_name, "format": "jsonv2", "limit": "1"})
    if not isinstance(data, list) or not data:
        return None
    hit = data[0]
    try:
        return {
            "lat": float(hit["lat"]),
            "lon": float(hit["lon"]),
            "display_name": str(hit.get("display_name", place_name)),
        }
    except (KeyError, TypeError, ValueError):
        return None


async def reverse_country_code(lat: float, lon: float) -> str | None:
    """The ISO country code for a point, or None.

    This is all the game wants from a reverse geocode. Place names deliberately
    come from the seed file instead: Nominatim's `city` field is inconsistent at
    street granularity - it answers "Bella Vista" for central Panama City and
    "Taito" for Yanaka in Tokyo - and it names places in the local language, so
    a reveal built from it could come back in a script the player cannot read.
    """
    data = await _get(
        REVERSE_URL,
        {"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "format": "jsonv2", "zoom": "14"},
    )
    if not isinstance(data, dict):
        return None
    code = (data.get("address") or {}).get("country_code")
    return str(code).upper() if code else None
