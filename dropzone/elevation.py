"""Open-Meteo elevation client (no API key).

One request carries every sample point, so a terrain survey is a single call.
"""

from __future__ import annotations

import httpx

ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
TIMEOUT = httpx.Timeout(15.0)


async def fetch_elevations(points: list[tuple[float, float]]) -> list[float | None]:
    """Metres above sea level for each (lat, lon), in the order given."""
    params = {
        "latitude": ",".join(f"{lat:.5f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.5f}" for _, lon in points),
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(ELEVATION_URL, params=params)
        response.raise_for_status()
        values = response.json().get("elevation") or []
    out: list[float | None] = []
    for index in range(len(points)):
        try:
            out.append(float(values[index]))
        except (IndexError, TypeError, ValueError):
            out.append(None)
    return out


def describe_slope(here: float, there: float, distance_m: float) -> str:
    """A slope phrase for the drop between two sample points."""
    if distance_m <= 0:
        return "level"
    grade = (there - here) / distance_m * 100.0
    if grade >= 25:
        return "climbs very steeply"
    if grade >= 10:
        return "climbs steeply"
    if grade >= 3:
        return "rises"
    if grade > -3:
        return "stays level"
    if grade > -10:
        return "falls away"
    if grade > -25:
        return "drops steeply"
    return "drops very steeply"
