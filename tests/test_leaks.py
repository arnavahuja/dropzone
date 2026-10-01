"""The test that matters: nothing a tool returns may give the location away.

For several seed locations, every tool is called and its serialised result is
searched for coordinates near the truth, for feature names present in the map
data, and for country, city and timezone strings. The system prompt is checked
too, since it is the other thing the model sees.
"""

from __future__ import annotations

import json
import re

import pytest

from dropzone import geo, overpass, prompts, tools
from tests.conftest import make_session, overpass_fixture, run

# Drop points on four continents, with the strings that must never appear.
CASES = [
    {
        "lat": 38.7121, "lon": -9.1300, "country": "PT",
        "forbidden": ["Portugal", "Lisbon", "Lisboa", "Alfama", "Europe/Lisbon"],
        "forbidden_exact": ["WEST", "WET"],
    },
    {
        "lat": 35.7270, "lon": 139.7710, "country": "JP",
        "forbidden": ["Japan", "Tokyo", "Yanaka", "Asia/Tokyo"],
        "forbidden_exact": ["JST"],
    },
    {
        "lat": -33.8970, "lon": 151.1790, "country": "AU",
        "forbidden": ["Australia", "Sydney", "Newtown", "Australia/Sydney"],
        "forbidden_exact": ["AEDT", "AEST"],
    },
    {
        "lat": 4.5960, "lon": -74.0730, "country": "CO",
        "forbidden": ["Colombia", "Bogota", "Bogotá", "America/Bogota"],
        "forbidden_exact": ["COT"],
    },
]

# Names and references present in the Overpass fixture. None may be echoed.
FIXTURE_NAMES = [
    "Rua dos Remédios", "Remédios", "Avenida Infante Dom Henrique", "Infante",
    "Café Alfama", "Igreja de São Miguel", "São Miguel", "Linha 28", "Tejo",
    "Almada", "Padaria São Roque", "Monte Agudo", "Ribeira de Alcântara",
]


def every_tool_result(session) -> list[tuple[str, dict]]:
    """Call every tool once in a sensible order and collect the results."""
    results: list[tuple[str, dict]] = []

    def record(name: str, **args) -> dict:
        result = run(tools.call_tool(session, name, args))
        results.append((name, result))
        return result

    look = record("look_around", radius_m=1000)
    ids = [
        feature["id"]
        for group in look.get("features_by_group", {}).values()
        for feature in group
    ]
    record("check_weather")
    record("read_sun")
    record("survey_terrain")
    record("spot_wildlife")
    record("listen")
    record("greet_local")
    record("scan_horizon")
    record("check_status")
    record("radio_check")
    for fid in ids:
        record("read_sign", feature_id=fid)
        record("inspect_road", feature_id=fid)
    record("move", direction="north-east", distance_m=600)
    record("submit_guess", place_name="Porto")
    return results


def coordinate_strings(lat: float, lon: float) -> list[str]:
    """Textual forms of a coordinate pair a leak would plausibly take."""
    out: list[str] = []
    for value in (lat, lon):
        for digits in (2, 3, 4, 5):
            out.append(f"{value:.{digits}f}")
            out.append(f"{abs(value):.{digits}f}")
    return out


@pytest.mark.parametrize("case", CASES, ids=[c["country"] for c in CASES])
def test_no_tool_result_leaks_the_location(case, monkeypatch) -> None:
    lat, lon = case["lat"], case["lon"]

    async def fake_overpass(query: str, timeout_s: float = 15.0):
        # Map data around *this* drop, so the coordinates in it are the real ones.
        return overpass_fixture(lat, lon)

    async def fake_weather(lat_, lon_):
        from dropzone import weather

        return {
            "temperature_2m": 19.0, "relative_humidity_2m": 55,
            "apparent_temperature": 18.0, "precipitation": 0.0, "rain": 0.0,
            "snowfall": 0.0, "weather_code": 3, "cloud_cover": 80,
            "wind_speed_10m": 14.0, "wind_direction_10m": 200,
            "wind_gusts_10m": 22.0, "visibility": 20000.0,
        }

    async def fake_elevations(points):
        return [40.0 + i for i, _ in enumerate(points)]

    async def fake_species(lat_, lon_, radius_km, limit):
        return [{"common_name": "Common Blackbird", "group": "bird"}]

    async def fake_geocode(place_name: str):
        return {"lat": 41.1579, "lon": -8.6291, "display_name": "Porto"}

    from dropzone import elevation, geocode, weather, wildlife

    monkeypatch.setattr(overpass, "run_query", fake_overpass)
    monkeypatch.setattr(weather, "fetch_current", fake_weather)
    monkeypatch.setattr(elevation, "fetch_elevations", fake_elevations)
    monkeypatch.setattr(wildlife, "fetch_species", fake_species)
    monkeypatch.setattr(geocode, "geocode_place", fake_geocode)

    session = make_session(mode="expedition", lat=lat, lon=lon)
    session.country_code = case["country"]
    session.reveal_label = ", ".join(case["forbidden"][:2])

    results = every_tool_result(session)
    assert len(results) > 20, "the sweep should exercise every tool"

    for name, result in results:
        blob = json.dumps(result, ensure_ascii=False)

        for needle in coordinate_strings(lat, lon):
            assert needle not in blob, f"{name} leaked a coordinate ({needle})"

        # Whole words only: a script name like "Georgian" or "Mongolian" is a
        # legitimate result, a country name is not.
        for needle in case["forbidden"]:
            pattern = rf"\b{re.escape(needle)}\b"
            assert not re.search(pattern, blob, re.IGNORECASE), f"{name} leaked {needle!r}"

        # Timezone abbreviations are checked case-sensitively, since "WEST"
        # collides with the compass word.
        for needle in case["forbidden_exact"]:
            assert not re.search(rf"\b{needle}\b", blob), f"{name} leaked {needle!r}"

        for needle in FIXTURE_NAMES:
            assert needle not in blob, f"{name} leaked the map name {needle!r}"

        # The country code itself, as a standalone token.
        assert not re.search(rf"\b{case['country']}\b", blob), f"{name} leaked a country code"

        # No bare decimal that happens to be a coordinate of the truth.
        for match in re.findall(r"-?\d+\.\d{3,}", blob):
            value = float(match)
            assert abs(value - lat) > 0.01, f"{name} leaked a latitude-like number {match}"
            assert abs(value - lon) > 0.01, f"{name} leaked a longitude-like number {match}"


def test_system_prompt_hides_the_location() -> None:
    session = make_session(mode="expedition")
    session.country_code = "PT"
    session.reveal_label = "Alfama, Lisboa, Portugal"
    prompt = prompts.system_prompt(session)

    for needle in ("38.7", "-9.1", "9.13", "Portugal", "Lisboa", "Alfama", "PT"):
        assert needle not in prompt, f"the system prompt leaked {needle!r}"
    # And it tells the narrator the rules.
    assert "do not know where the player is" in prompt.lower()
    assert "never" in prompt.lower()


def test_tool_schemas_take_no_coordinates() -> None:
    """No tool may accept a latitude or longitude: position comes from state."""
    for spec in tools.REGISTRY.values():
        for arg in spec.parameters.get("properties", {}):
            assert arg not in ("lat", "lon", "latitude", "longitude", "coordinates"), spec.name
        blob = json.dumps(spec.parameters).lower()
        assert "latitude" not in blob and "longitude" not in blob, spec.name


def test_tool_descriptions_do_not_promise_names() -> None:
    """The model should be told names are unavailable, not left to discover it."""
    for name in ("look_around", "read_sign", "inspect_road", "scan_horizon"):
        description = tools.REGISTRY[name].description.lower()
        assert "name" in description, name


def test_offset_wording_is_the_only_position_disclosure(monkeypatch) -> None:
    """A long walk still describes position relatively, never absolutely."""

    async def fake_overpass(query: str, timeout_s: float = 15.0):
        return []

    async def fake_elevations(points):
        return [10.0 for _ in points]

    from dropzone import elevation

    monkeypatch.setattr(overpass, "run_query", fake_overpass)
    monkeypatch.setattr(elevation, "fetch_elevations", fake_elevations)

    session = make_session()
    for _ in range(3):
        result = run(tools.call_tool(session, "move", {"direction": "east", "distance_m": 3000}))

    assert "km" in result["position"]
    assert "east" in result["position"]
    distance = geo.haversine_m(session.drop_lat, session.drop_lon, session.true_lat, session.true_lon)
    assert distance > 8000
    assert "-9." not in json.dumps(result) and "38." not in json.dumps(result)
