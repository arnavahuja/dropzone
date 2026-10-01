"""Geodesy helpers: distances, bearings, and the offset vocabulary the model sees.

Nothing in here is allowed to produce a latitude or longitude in human-readable
form. The only outward-facing descriptions are relative ones.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6371008.8

COMPASS_16 = [
    "north", "north-north-east", "north-east", "east-north-east",
    "east", "east-south-east", "south-east", "south-south-east",
    "south", "south-south-west", "south-west", "west-south-west",
    "west", "west-north-west", "north-west", "north-north-west",
]

COMPASS_8 = [
    "north", "north-east", "east", "south-east",
    "south", "south-west", "west", "north-west",
]

# Accepted spellings for `move(direction=...)`.
DIRECTION_ALIASES = {
    "n": 0.0, "north": 0.0,
    "nne": 22.5, "north-north-east": 22.5, "north north east": 22.5,
    "ne": 45.0, "north-east": 45.0, "northeast": 45.0, "north east": 45.0,
    "ene": 67.5, "east-north-east": 67.5, "east north east": 67.5,
    "e": 90.0, "east": 90.0,
    "ese": 112.5, "east-south-east": 112.5, "east south east": 112.5,
    "se": 135.0, "south-east": 135.0, "southeast": 135.0, "south east": 135.0,
    "sse": 157.5, "south-south-east": 157.5, "south south east": 157.5,
    "s": 180.0, "south": 180.0,
    "ssw": 202.5, "south-south-west": 202.5, "south south west": 202.5,
    "sw": 225.0, "south-west": 225.0, "southwest": 225.0, "south west": 225.0,
    "wsw": 247.5, "west-south-west": 247.5, "west south west": 247.5,
    "w": 270.0, "west": 270.0,
    "wnw": 292.5, "west-north-west": 292.5, "west north west": 292.5,
    "nw": 315.0, "north-west": 315.0, "northwest": 315.0, "north west": 315.0,
    "nnw": 337.5, "north-north-west": 337.5, "north north west": 337.5,
}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2, in degrees from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination(lat: float, lon: float, bearing: float, distance_m: float) -> tuple[float, float]:
    """The point reached by travelling `distance_m` along `bearing` from a point."""
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    b = math.radians(bearing)
    d = distance_m / EARTH_RADIUS_M
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def compass_16(bearing: float) -> str:
    """A 16-point compass word for a bearing, e.g. 'east-south-east'."""
    return COMPASS_16[int((bearing % 360.0) / 22.5 + 0.5) % 16]


def compass_8(bearing: float) -> str:
    """An 8-point compass word for a bearing, e.g. 'south-west'."""
    return COMPASS_8[int((bearing % 360.0) / 45.0 + 0.5) % 8]


def describe_offset(drop_lat: float, drop_lon: float, lat: float, lon: float) -> str:
    """Position as a phrase relative to the drop point, never as coordinates."""
    d = haversine_m(drop_lat, drop_lon, lat, lon)
    if d < 25:
        return "at the drop point"
    word = compass_16(bearing_deg(drop_lat, drop_lon, lat, lon))
    if d < 1000:
        return f"{round(d / 10) * 10:.0f} m {word} of the drop point"
    return f"{d / 1000:.1f} km {word} of the drop point"


def round_sig(value: float, digits: int = 2) -> float:
    """Round to a number of significant figures (used to blur guess distances)."""
    if value == 0:
        return 0.0
    mag = math.floor(math.log10(abs(value)))
    factor = 10 ** (digits - 1 - mag)
    rounded = round(value * factor) / factor
    return float(round(rounded)) if mag >= digits - 1 else float(rounded)


def snap_distance(distance_m: float) -> str:
    """A coarse, human distance band so exact metres never pin a feature down."""
    if distance_m < 50:
        return "under 50 m"
    if distance_m < 1000:
        return f"{round(distance_m / 25) * 25:.0f} m"
    return f"{distance_m / 1000:.1f} km"
