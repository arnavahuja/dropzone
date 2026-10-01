"""Solar geometry, computed locally with `astral`.

`read_sun` is the game's sharpest tool: sun elevation plus a UTC watch time is
enough to derive latitude and longitude by hand. So this module deliberately
exposes angles and UTC instants and never a local time or a timezone name.
"""

from __future__ import annotations

import datetime as dt

from astral import Observer
from astral.sun import azimuth, elevation, sunrise, sunset

UTC = dt.timezone.utc


def sun_position(lat: float, lon: float, when: dt.datetime) -> tuple[float, float]:
    """`(elevation_deg, azimuth_deg)` of the sun at a point and instant."""
    observer = Observer(latitude=lat, longitude=lon)
    return elevation(observer, when), azimuth(observer, when)


def _event(func, lat: float, lon: float, date: dt.date) -> dt.datetime | None:
    """A sunrise/sunset instant, or None where the sun does not cross (polar)."""
    try:
        return func(Observer(latitude=lat, longitude=lon), date, tzinfo=UTC)
    except Exception:  # noqa: BLE001 - astral raises for polar day/night
        return None


def next_sunset(lat: float, lon: float, when: dt.datetime) -> dt.datetime | None:
    """The next sunset at or after `when`, searching a few days for polar cases."""
    for offset in range(0, 4):
        candidate = _event(sunset, lat, lon, (when + dt.timedelta(days=offset)).date())
        if candidate and candidate > when:
            return candidate
    return None


def next_sunrise(lat: float, lon: float, when: dt.datetime) -> dt.datetime | None:
    """The next sunrise at or after `when`."""
    for offset in range(0, 4):
        candidate = _event(sunrise, lat, lon, (when + dt.timedelta(days=offset)).date())
        if candidate and candidate > when:
            return candidate
    return None


def is_daylight(lat: float, lon: float, when: dt.datetime) -> bool:
    """True when the sun is above the horizon."""
    return sun_position(lat, lon, when)[0] > -0.833


def morning_start(lat: float, lon: float, when: dt.datetime) -> dt.datetime:
    """Shift a night-time drop forward to about an hour after local sunrise.

    Sections 3 of the spec: nobody wants to be dropped into the dark with a
    six-hour clock, so the game's start instant moves, not the player.
    """
    if is_daylight(lat, lon, when):
        return when
    rise = next_sunrise(lat, lon, when)
    if rise is None:
        return when  # polar night: nothing to shift to
    return rise + dt.timedelta(hours=1)


def describe_elevation(degrees: float) -> str:
    """A phrase for how high the sun is sitting."""
    if degrees < -6:
        return "well below the horizon"
    if degrees < -0.833:
        return "below the horizon, sky still lit"
    if degrees < 10:
        return "low, close to the horizon"
    if degrees < 30:
        return "low in the sky"
    if degrees < 55:
        return "well up"
    if degrees < 80:
        return "high overhead"
    return "almost directly overhead"
