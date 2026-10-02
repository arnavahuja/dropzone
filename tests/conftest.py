"""Shared fixtures. Every test runs offline: no test touches the network.

The fake Overpass / Open-Meteo / iNaturalist / Nominatim responses deliberately
contain exactly the things that must not leak: coordinates, feature names,
country codes and timezone strings. A tool that forwards a response instead of
whitelisting it will fail the leak test.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from dropzone import elevation, geocode, overpass, weather, wildlife
from dropzone.session import GameSession

UTC = dt.timezone.utc

# A drop in Lisbon, used by most tests.
TRUE_LAT, TRUE_LON = 38.7121, -9.1300


def overpass_fixture(lat: float = TRUE_LAT, lon: float = TRUE_LON) -> list[dict[str, Any]]:
    """Overpass elements around a point, with names and coordinates in place."""
    return [
        {
            "type": "way", "id": 1001,
            "center": {"lat": lat + 0.0004, "lon": lon + 0.0002},
            "tags": {
                "highway": "residential", "name": "Rua dos Remédios",
                "surface": "sett", "lanes": "1", "maxspeed": "30",
                "sidewalk": "both", "lit": "yes",
            },
        },
        {
            "type": "way", "id": 1002,
            "center": {"lat": lat + 0.0090, "lon": lon + 0.0070},
            "tags": {"highway": "primary", "name": "Avenida Infante Dom Henrique", "lanes": "4"},
        },
        {
            "type": "node", "id": 1003,
            "lat": lat + 0.0003, "lon": lon - 0.0003,
            "tags": {"amenity": "cafe", "name": "Café Alfama", "cuisine": "portuguese"},
        },
        {
            "type": "node", "id": 1004,
            "lat": lat - 0.0006, "lon": lon + 0.0009,
            "tags": {"amenity": "place_of_worship", "religion": "christian",
                     "name": "Igreja de São Miguel"},
        },
        {
            "type": "way", "id": 1005,
            "center": {"lat": lat + 0.0020, "lon": lon - 0.0010},
            "tags": {"railway": "tram", "name": "Linha 28"},
        },
        {
            "type": "way", "id": 1006,
            "center": {"lat": lat + 0.0025, "lon": lon + 0.0030},
            "tags": {"natural": "coastline", "name": "Tejo"},
        },
        {
            "type": "node", "id": 1007,
            "lat": lat + 0.0100, "lon": lon + 0.0100,
            "tags": {"place": "town", "name": "Almada", "population": "174030"},
        },
        {
            "type": "node", "id": 1008,
            "lat": lat + 0.0001, "lon": lon + 0.0001,
            "tags": {"shop": "bakery", "name": "Padaria São Roque"},
        },
        {
            "type": "node", "id": 1009,
            "lat": lat + 0.0002, "lon": lon - 0.0001,
            "tags": {"natural": "peak", "ele": "228", "name": "Monte Agudo"},
        },
        {
            "type": "way", "id": 1010,
            "center": {"lat": lat - 0.0015, "lon": lon - 0.0015},
            "tags": {"waterway": "stream", "name": "Ribeira de Alcântara"},
        },
        # No recognised tag: must be dropped, not passed through.
        {"type": "node", "id": 1011, "lat": lat, "lon": lon, "tags": {"note": "survey point"}},
    ]


WEATHER_FIXTURE = {
    "latitude": TRUE_LAT,
    "longitude": TRUE_LON,
    "timezone": "Europe/Lisbon",
    "timezone_abbreviation": "WEST",
    "elevation": 32.0,
    "current": {
        "time": "2026-10-01T12:00",
        "temperature_2m": 21.4,
        "relative_humidity_2m": 62,
        "apparent_temperature": 21.0,
        "precipitation": 0.0,
        "rain": 0.0,
        "snowfall": 0.0,
        "weather_code": 2,
        "cloud_cover": 40,
        "wind_speed_10m": 18.5,
        "wind_direction_10m": 315,
        "wind_gusts_10m": 27.0,
        "visibility": 24140.0,
    },
}

WILDLIFE_FIXTURE = {
    "total_results": 2,
    "results": [
        {
            "count": 412,
            "taxon": {
                "id": 1, "rank": "species", "name": "Erithacus rubecula",
                "preferred_common_name": "European Robin",
                "iconic_taxon_name": "Aves",
                "wikipedia_url": "https://en.wikipedia.org/wiki/European_robin",
            },
        },
        {
            "count": 310,
            "taxon": {
                "id": 2, "rank": "species", "name": "Olea europaea",
                "preferred_common_name": "Olive",
                "iconic_taxon_name": "Plantae",
            },
        },
        {   # genus rank: must be filtered out
            "count": 90,
            "taxon": {"id": 3, "rank": "genus", "name": "Quercus",
                      "preferred_common_name": "Oaks", "iconic_taxon_name": "Plantae"},
        },
    ],
}


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every outbound call with a fixture."""

    async def fake_overpass(query: str, timeout_s: float = 15.0) -> list[dict[str, Any]]:
        return overpass_fixture()

    async def fake_weather(lat: float, lon: float) -> dict[str, Any]:
        current = WEATHER_FIXTURE["current"]
        return {field: current.get(field) for field in weather.CURRENT_FIELDS}

    async def fake_elevations(points: list[tuple[float, float]]) -> list[float | None]:
        # A gentle slope rising to the north, so terrain answers are meaningful.
        base = 30.0
        return [base + (lat - TRUE_LAT) * 20000.0 for lat, _ in points]

    async def fake_species(lat: float, lon: float, radius_km: float, limit: int):
        species = []
        for row in WILDLIFE_FIXTURE["results"]:
            taxon = row["taxon"]
            if taxon["rank"] != "species":
                continue
            species.append({
                "common_name": taxon["preferred_common_name"],
                "group": wildlife.GROUPS.get(taxon["iconic_taxon_name"], "other"),
            })
        return species[:limit]

    async def fake_geocode(place_name: str):
        places = {
            "lisbon": (38.7223, -9.1393),
            "porto": (41.1579, -8.6291),
            "tokyo": (35.6762, 139.6503),
        }
        hit = places.get(place_name.strip().lower())
        if hit is None:
            return None
        return {"lat": hit[0], "lon": hit[1], "display_name": place_name.title()}

    async def fake_reverse(lat: float, lon: float):
        return "PT"

    monkeypatch.setattr(overpass, "run_query", fake_overpass)
    monkeypatch.setattr(weather, "fetch_current", fake_weather)
    monkeypatch.setattr(elevation, "fetch_elevations", fake_elevations)
    monkeypatch.setattr(wildlife, "fetch_species", fake_species)
    monkeypatch.setattr(geocode, "geocode_place", fake_geocode)
    monkeypatch.setattr(geocode, "reverse_country_code", fake_reverse)


def make_session(
    mode: str = "expedition",
    lat: float = TRUE_LAT,
    lon: float = TRUE_LON,
    minutes: int = 360,
) -> GameSession:
    """A session with a fixed clock, so time assertions are deterministic."""
    start = dt.datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    session = GameSession(
        mode=mode,  # type: ignore[arg-type]
        true_lat=lat,
        true_lon=lon,
        drop_lat=lat,
        drop_lon=lon,
        start_utc=start,
        deadline_utc=start + dt.timedelta(minutes=minutes),
        country_code="PT",
        reveal_label="Alfama, Lisboa, Portugal",
    )
    if mode in ("escape", "expedition"):
        session.extraction_lat = lat + 0.027
        session.extraction_lon = lon
    return session


def run(coro):
    """Drive a coroutine to completion.

    Used instead of pytest-asyncio to keep the dev dependencies to pytest and
    respx; every test is a plain synchronous function.
    """
    import asyncio

    return asyncio.run(coro)
