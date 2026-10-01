"""Open-Meteo forecast client (no API key).

Whitelists the handful of numbers the game needs. The raw response carries
`timezone`, `timezone_abbreviation` and the echoed coordinates, none of which
may reach a tool result.
"""

from __future__ import annotations

from typing import Any

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = httpx.Timeout(15.0)

CURRENT_FIELDS = [
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "precipitation",
    "rain",
    "snowfall",
    "weather_code",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "visibility",
]

# Condensed WMO weather code descriptions.
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light rain showers", 81: "rain showers",
    82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


async def fetch_current(lat: float, lon: float) -> dict[str, Any]:
    """Current conditions as a whitelisted dict of plain numbers."""
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": ",".join(CURRENT_FIELDS),
        "wind_speed_unit": "kmh",
        "timezone": "UTC",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(FORECAST_URL, params=params)
        response.raise_for_status()
        current = response.json().get("current", {}) or {}
    return {field: current.get(field) for field in CURRENT_FIELDS}


def describe_code(code: Any) -> str:
    """A short phrase for a WMO weather code."""
    try:
        return WMO.get(int(code), "unsettled")
    except (TypeError, ValueError):
        return "unclear"


def describe_temperature(celsius: Any) -> str:
    """A felt-temperature phrase, so the narration has something to chew on."""
    try:
        t = float(celsius)
    except (TypeError, ValueError):
        return "unknown"
    if t <= -15:
        return "bitterly cold"
    if t <= -2:
        return "freezing"
    if t <= 7:
        return "cold"
    if t <= 14:
        return "cool"
    if t <= 22:
        return "mild"
    if t <= 29:
        return "warm"
    if t <= 36:
        return "hot"
    return "searing"


def describe_wind(kmh: Any) -> str:
    """A Beaufort-ish phrase for a wind speed in km/h."""
    try:
        v = float(kmh)
    except (TypeError, ValueError):
        return "unknown"
    if v < 2:
        return "still"
    if v < 12:
        return "light air"
    if v < 29:
        return "a steady breeze"
    if v < 50:
        return "a strong wind"
    if v < 75:
        return "a gale"
    return "a storm"


def describe_cloud(percent: Any) -> str:
    """A sky-cover phrase for a cloud-cover percentage."""
    try:
        c = float(percent)
    except (TypeError, ValueError):
        return "unknown"
    if c < 10:
        return "cloudless"
    if c < 35:
        return "mostly clear"
    if c < 70:
        return "broken cloud"
    if c < 95:
        return "heavily clouded"
    return "solid grey"
