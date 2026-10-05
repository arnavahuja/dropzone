"""One test per tool: the happy path, and the failure path returning a suggestion."""

from __future__ import annotations

import pytest

from dropzone import overpass, tools
from tests.conftest import make_session, run

pytestmark = pytest.mark.usefixtures("offline")


def call(session, name, **args):
    """Invoke a tool through the dispatcher the agent loop uses."""
    return run(tools.call_tool(session, name, args))


# --- look_around ----------------------------------------------------------


def test_look_around_returns_categories_and_ids() -> None:
    session = make_session()
    result = call(session, "look_around", radius_m=500)

    assert result["features_found"] >= 5
    groups = result["features_by_group"]
    assert "roads" in groups
    road = groups["roads"][0]
    assert set(road) == {"id", "category", "distance", "bearing"}
    assert road["id"].startswith("F")
    assert session.minutes_elapsed == 5


def test_look_around_clamps_the_radius() -> None:
    session = make_session()
    assert call(session, "look_around", radius_m=99_999)["radius_m"] == 2000
    assert call(session, "look_around", radius_m=1)["radius_m"] == 100


def test_look_around_reuses_a_wider_cached_sweep() -> None:
    session = make_session()
    call(session, "look_around", radius_m=2000)
    calls: list[str] = []

    async def counting_query(query: str, timeout_s: float = 15.0):
        calls.append(query)
        return []

    original = overpass.run_query
    overpass.run_query = counting_query  # type: ignore[assignment]
    try:
        call(session, "look_around", radius_m=300)
    finally:
        overpass.run_query = original  # type: ignore[assignment]
    assert calls == [], "a narrower look should be served from the cache"


def test_look_around_reports_a_map_failure_with_a_suggestion(monkeypatch) -> None:
    session = make_session()

    async def boom(query: str, timeout_s: float = 15.0):
        raise overpass.OverpassError("gateway timeout")

    monkeypatch.setattr(overpass, "run_query", boom)
    result = call(session, "look_around", radius_m=1000)
    assert "map service unavailable" in result["error"]
    assert "smaller radius_m" in result["suggestion"]
    assert session.minutes_elapsed == 0, "a failed call must not charge game time"


# --- read_sign ------------------------------------------------------------


def test_read_sign_describes_the_script_but_not_the_name() -> None:
    session = make_session()
    look = call(session, "look_around", radius_m=500)
    cafe = next(
        f for f in look["features_by_group"]["businesses"] if f["category"] == "cafe"
    )

    result = call(session, "read_sign", feature_id=cafe["id"])
    assert result["writing_system"] == "Latin"
    assert result["has_diacritics"] is True  # "Café Alfama"
    assert result["character_count"] == len("Café Alfama")
    assert result["first_two_characters"] == "Ca"
    assert "Alfama" not in str(result)


def test_read_sign_rejects_a_distant_feature() -> None:
    session = make_session()
    look = call(session, "look_around", radius_m=2000)
    far = next(f for f in look["features_by_group"]["settlements"])

    result = call(session, "read_sign", feature_id=far["id"])
    assert "too far" in result["error"]
    assert "Move" in result["suggestion"]


def test_read_sign_rejects_an_unknown_id() -> None:
    result = call(make_session(), "read_sign", feature_id="F999")
    assert "unknown feature id" in result["error"]
    assert "look_around" in result["suggestion"]


def test_read_sign_handles_an_unnamed_feature() -> None:
    session = make_session()
    call(session, "look_around", radius_m=500)
    # Strip the name from a cached feature and read it again.
    fid, record = next(
        (k, v) for k, v in session.features.items() if v["category"] == "cafe"
    )
    record["_tags"] = {"amenity": "cafe"}
    result = call(session, "read_sign", feature_id=fid)
    assert result["signed"] is False


# --- check_weather --------------------------------------------------------


def test_check_weather_describes_and_quantifies() -> None:
    session = make_session()
    result = call(session, "check_weather")

    assert result["temperature_c"] == 21.4
    assert result["feels"] == "mild"
    assert result["conditions"] == "partly cloudy"
    assert result["sky"] == "broken cloud"
    assert result["wind_blowing_from"] == "north-west"
    assert "timezone" not in result and "latitude" not in result
    assert session.minutes_elapsed == 1


def test_check_weather_surfaces_an_api_failure(monkeypatch) -> None:
    from dropzone import weather

    async def boom(lat, lon):
        raise RuntimeError("502 Bad Gateway")

    monkeypatch.setattr(weather, "fetch_current", boom)
    result = call(make_session(), "check_weather")
    assert "check_weather failed" in result["error"]
    assert result["suggestion"]


# --- read_sun -------------------------------------------------------------


def test_read_sun_gives_angles_and_a_utc_watch() -> None:
    session = make_session()
    result = call(session, "read_sun")

    assert -90 <= result["sun_elevation_deg"] <= 90
    assert 0 <= result["sun_azimuth_deg"] <= 360
    assert result["watch_reads"].endswith("UTC")
    assert result["daylight"] is True
    assert result["minutes_until_sunset"] > 0
    # The whole point: a UTC watch, and no timezone to convert it with.
    assert "timezone" not in result
    assert "local_time" not in result and "WEST" not in str(result)


def test_read_sun_at_night_reports_no_daylight() -> None:
    session = make_session(minutes=24 * 60)
    session.spend_minutes(11 * 60)  # 22:00 UTC in Lisbon: well after dark
    result = call(session, "read_sun")
    assert result["daylight"] is False
    assert result["sun_elevation_deg"] < 0


# --- survey_terrain -------------------------------------------------------


def test_survey_terrain_summarises_slope_in_eight_directions() -> None:
    session = make_session()
    result = call(session, "survey_terrain")

    assert result["elevation_m"] == 30
    assert set(result["directions_500m"]) == {
        "north", "north-east", "east", "south-east",
        "south", "south-west", "west", "north-west",
    }
    # The fixture's ground rises to the north.
    assert "climbs" in result["directions_500m"]["north"]["slope"]
    assert result["directions_500m"]["south"]["elevation_change_m"] < 0
    assert session.minutes_elapsed == 5


def test_survey_terrain_handles_a_missing_reading(monkeypatch) -> None:
    from dropzone import elevation

    async def nothing(points):
        return [None] * len(points)

    monkeypatch.setattr(elevation, "fetch_elevations", nothing)
    result = call(make_session(), "survey_terrain")
    assert "elevation service" in result["error"]
    assert result["suggestion"]


# --- spot_wildlife --------------------------------------------------------


def test_spot_wildlife_returns_names_and_groups_only() -> None:
    session = make_session()
    result = call(session, "spot_wildlife")

    assert result["species_seen"] == [
        {"common_name": "European Robin", "group": "bird"},
        {"common_name": "Olive", "group": "plant"},
    ]
    assert session.minutes_elapsed == 10


def test_spot_wildlife_with_no_records_is_still_a_clue(monkeypatch) -> None:
    from dropzone import wildlife

    async def empty(lat, lon, radius_km, limit):
        return []

    monkeypatch.setattr(wildlife, "fetch_species", empty)
    result = call(make_session(), "spot_wildlife")
    assert result["species_seen"] == []
    assert "clue" in result["note"]


# --- inspect_road ---------------------------------------------------------


def test_inspect_road_gives_driving_side_and_units() -> None:
    session = make_session()
    look = call(session, "look_around", radius_m=500)
    road = next(f for f in look["features_by_group"]["roads"]
                if f["category"] == "residential street")

    result = call(session, "inspect_road", feature_id=road["id"])
    assert result["traffic_drives_on"] == "right"      # PT
    assert result["speed_limit_unit"] == "km/h"
    assert result["surface"] == "sett"
    assert result["lanes"] == 1
    assert result["has_posted_speed_limit"] is True
    assert "Remédios" not in str(result)
    assert "PT" not in str(result)


def test_inspect_road_rejects_something_that_is_not_a_road() -> None:
    session = make_session()
    look = call(session, "look_around", radius_m=500)
    cafe = next(f for f in look["features_by_group"]["businesses"]
                if f["category"] == "cafe")

    result = call(session, "inspect_road", feature_id=cafe["id"])
    assert "not a road" in result["error"]
    assert "roads" in result["suggestion"]


def test_driving_side_table_handles_both_sides() -> None:
    from dropzone import tables

    assert tables.drives_on("JP") == "left"
    assert tables.drives_on("DE") == "right"
    assert tables.drives_on(None) == "unknown"
    assert tables.speed_unit("US") == "mph"
    assert tables.speed_unit("FR") == "km/h"


# --- listen ---------------------------------------------------------------


def test_listen_reports_only_sourced_sounds() -> None:
    session = make_session()
    result = call(session, "listen")

    sounds = {entry["sound"] for entry in result["sounds"]}
    assert "road traffic" in sounds
    assert "trains" in sounds        # the tram line in the fixture
    assert "a bell" in sounds        # place_of_worship
    assert "wind" in sounds          # 18.5 km/h in the weather fixture
    assert "surf" in sounds          # the coastline sits just inside 400 m
    # The town in the fixture is 1.4 km off: too far to hear.
    assert "voices and trade" not in sounds
    for entry in result["sounds"]:
        assert entry["direction"]
    assert session.minutes_elapsed == 2


def test_listen_in_an_empty_place_reports_silence(monkeypatch) -> None:
    from dropzone import weather

    async def nothing(query: str, timeout_s: float = 15.0):
        return []

    async def calm(lat, lon):
        return {field: 0 for field in weather.CURRENT_FIELDS}

    monkeypatch.setattr(overpass, "run_query", nothing)
    monkeypatch.setattr(weather, "fetch_current", calm)
    result = call(make_session(), "listen")
    assert result["sounds"] == []
    assert "Silence" in result["note"]


# --- greet_local ----------------------------------------------------------


def test_greet_local_returns_a_greeting_in_script() -> None:
    session = make_session()
    result = call(session, "greet_local")

    assert result["greeting_heard"] == "olá"
    assert result["writing_system"] == "Latin"
    assert result["greetings_left"] == 2
    # The language is never named.
    assert "portug" not in str(result).lower()
    assert session.minutes_elapsed == 5


def test_greet_local_runs_out_of_uses() -> None:
    session = make_session()
    for _ in range(3):
        call(session, "greet_local")
    result = call(session, "greet_local")
    assert "no greetings left" in result["error"]
    assert result["suggestion"]


def test_greet_local_needs_somebody_nearby(monkeypatch) -> None:
    session = make_session()
    # Move far from the fixture's shops, keeping only distant features in range.
    session.move_to(session.true_lat + 0.01, session.true_lon + 0.01)
    result = call(session, "greet_local")
    assert "nobody about" in result["error"]
    assert result["suggestion"]
    assert session.greet_uses_left == 3, "a failed greeting must not burn a use"


# --- scan_horizon ---------------------------------------------------------


def test_scan_horizon_lists_distant_landmarks() -> None:
    session = make_session()
    result = call(session, "scan_horizon")

    groups = result["landmarks_by_group"]
    assert "settlements" in groups
    assert groups["settlements"][0]["category"] == "town"
    assert "km" in groups["settlements"][0]["distance"]
    assert session.minutes_elapsed == 10
    assert "Almada" not in str(result)


def test_scan_horizon_range_shrinks_in_fog(monkeypatch) -> None:
    from dropzone import weather

    clear = call(make_session(), "scan_horizon")

    async def foggy(lat, lon):
        current = dict.fromkeys(weather.CURRENT_FIELDS, 0)
        current["visibility"] = 2000.0
        return current

    monkeypatch.setattr(weather, "fetch_current", foggy)
    fogged = call(make_session(), "scan_horizon")

    assert int(fogged["visible_range"].split()[0]) < int(clear["visible_range"].split()[0])
    assert fogged["limited_by"] == "weather"


# --- move -----------------------------------------------------------------


def test_move_updates_position_and_spends_time() -> None:
    session = make_session()
    result = call(session, "move", direction="north", distance_m=1000)

    assert result["walked"] == "1000 m north"
    assert result["minutes_spent"] >= 12          # 5 km/h plus the climb penalty
    assert "north" in result["position"]
    assert session.distance_walked_m == 1000
    # No coordinates anywhere in the result.
    assert "38." not in str(result) and "-9." not in str(result)


def test_move_accepts_a_bearing_and_caps_the_distance() -> None:
    session = make_session()
    result = call(session, "move", bearing_deg=225, distance_m=9000)
    assert result["bearing_deg"] == 225.0
    assert result["walked"].startswith("3000 m")
    assert "Capped" in result["note"]


def test_move_without_a_direction_explains_itself() -> None:
    result = call(make_session(), "move", distance_m=500)
    assert "no direction" in result["error"]
    assert "bearing_deg" in result["suggestion"]


def test_move_rejects_gibberish_directions() -> None:
    result = call(make_session(), "move", direction="widdershins", distance_m=500)
    assert "unrecognised direction" in result["error"]


def test_move_reaches_the_extraction_point() -> None:
    session = make_session(mode="escape")
    # Extraction sits 0.027 degrees north, about 3 km.
    for _ in range(2):
        result = call(session, "move", direction="north", distance_m=1500)
    assert result["extraction_reached"] is True
    assert session.extraction_reached is True
    assert "extraction_note" in result


# --- radio_check ----------------------------------------------------------


def test_radio_check_points_at_extraction_and_drains_battery() -> None:
    session = make_session(mode="escape")
    result = call(session, "radio_check")

    assert result["beacon_bearing"] == "north"
    assert 2800 < result["beacon_distance_m"] < 3200
    assert result["charges_left"] == 3
    assert session.minutes_elapsed == 2


def test_radio_check_runs_the_battery_flat() -> None:
    session = make_session(mode="escape")
    for _ in range(4):
        call(session, "radio_check")
    result = call(session, "radio_check")
    assert "battery flat" in result["error"]
    assert result["suggestion"]


def test_radio_check_is_useless_in_locate_mode() -> None:
    result = call(make_session(mode="locate"), "radio_check")
    assert "no extraction point" in result["error"]
    assert "Locate" in result["suggestion"]


# --- check_status ---------------------------------------------------------


def test_check_status_is_free_and_hides_the_truth() -> None:
    session = make_session()
    result = call(session, "check_status")

    assert result["minutes_remaining"] == 360
    assert result["offset_from_drop"] == "at the drop point"
    assert result["guesses_left"] == 3
    assert result["radio_charges_left"] == 4
    assert session.minutes_elapsed == 0
    for forbidden in ("true_lat", "country", "38.", "-9.", "Portugal", "PT"):
        assert forbidden not in str(result)


# --- submit_guess ---------------------------------------------------------


def test_submit_guess_returns_a_rounded_distance() -> None:
    session = make_session(mode="locate")
    result = call(session, "submit_guess", place_name="Porto")

    assert result["guesses_left"] == 2
    assert result["distance_from_truth"].endswith("km")
    assert result["verdict"] == "the right country, roughly"
    assert "city" in result["answer_must"]
    assert "final" not in result


def test_submit_guess_grades_the_same_guess_against_the_difficulty() -> None:
    """Lisbon is 1.4 km from the truth: dead on for a city, merely close in god."""
    easy = make_session(mode="locate")
    god = make_session(mode="locate")
    god.difficulty = "god"

    assert call(easy, "submit_guess", place_name="Lisbon")["verdict"] == "dead on"
    god_guess = call(god, "submit_guess", place_name="Lisbon")
    assert god_guess["verdict"] == "close enough to count"
    assert "neighbourhood" in god_guess["answer_must"]

    # Porto is 275 km out: the wrong answer at any precision. Fresh sessions,
    # because a correct guess has already ended the two above.
    assert call(make_session(mode="locate"), "submit_guess", place_name="Porto")["verdict"] \
        == "the right country, roughly"


def test_third_guess_ends_the_run_and_reveals() -> None:
    session = make_session(mode="locate")
    call(session, "submit_guess", place_name="Tokyo")
    call(session, "submit_guess", place_name="Porto")
    result = call(session, "submit_guess", place_name="Lisbon")

    assert result["final"] is True
    assert result["result"]["outcome"] == "success"
    assert result["result"]["true_location"] == "Alfama, Lisboa, Portugal"
    assert session.game_over is True


def test_unresolvable_guess_does_not_cost_an_attempt() -> None:
    session = make_session(mode="locate")
    result = call(session, "submit_guess", place_name="Atlantis-on-Sea")
    assert "could not find" in result["error"]
    assert session.guesses_left() == 3


def test_guessing_is_refused_in_escape_mode() -> None:
    result = call(make_session(mode="escape"), "submit_guess", place_name="Lisbon")
    assert "not part of an Escape run" in result["error"]


# --- dispatcher -----------------------------------------------------------


def test_unknown_tool_is_reported_with_the_valid_names() -> None:
    result = call(make_session(), "teleport")
    assert "no such tool" in result["error"]
    assert "look_around" in result["suggestion"]


def test_timed_tools_are_refused_once_the_clock_is_out() -> None:
    session = make_session(minutes=10)
    session.spend_minutes(10)

    assert "no game time left" in call(session, "look_around")["error"]
    # Free tools still work, so the player can see what happened.
    assert "minutes_remaining" in call(session, "check_status")


def test_string_arguments_are_coerced_to_numbers() -> None:
    session = make_session()
    assert call(session, "look_around", radius_m="700")["radius_m"] == 700


def test_unparseable_argument_is_reported_not_raised() -> None:
    result = call(make_session(), "look_around", radius_m="a long way")
    assert "must be a integer" in result["error"]


def test_every_tool_has_a_description_mentioning_its_cost() -> None:
    for name, spec in tools.REGISTRY.items():
        assert len(spec.description) > 80, name
        assert spec.parameters["type"] == "object", name
        if spec.time_cost_min:
            assert "minute" in spec.description, name
        else:
            assert "free" in spec.description.lower() \
                or "per kilometre" in spec.description, name


def test_all_fourteen_tools_are_registered() -> None:
    assert set(tools.REGISTRY) == {
        "look_around", "read_sign", "check_weather", "read_sun", "survey_terrain",
        "spot_wildlife", "inspect_road", "listen", "greet_local", "scan_horizon",
        "move", "radio_check", "check_status", "submit_guess",
    }


def test_a_correct_guess_ends_a_locate_run_immediately() -> None:
    """The run is over when the player is right, not when they run out of tries."""
    session = make_session(mode="locate")
    result = call(session, "submit_guess", place_name="Lisbon")

    assert result["final"] is True
    assert result["result"]["outcome"] == "success"
    assert result["result"]["reason"] == "guessed_correctly"
    assert session.game_over is True
    # Two guesses were still in hand, and the unused time still scores.
    assert result["result"]["guesses_made"] == ["Lisbon"]
    assert session.guesses_left() == 2
    assert result["result"]["score_breakdown"]["time_remaining"] > 150
    # And nothing further is playable.
    assert "game is over" in call(session, "look_around")["error"]


def test_a_wrong_guess_leaves_a_locate_run_going() -> None:
    session = make_session(mode="locate")
    result = call(session, "submit_guess", place_name="Porto")

    assert "final" not in result
    assert session.game_over is False
    assert session.guesses_left() == 2


def test_a_correct_guess_alone_does_not_end_an_expedition() -> None:
    """Expedition wants both: being right is only half the job."""
    session = make_session(mode="expedition")
    result = call(session, "submit_guess", place_name="Lisbon")

    assert "final" not in result
    assert session.located is True
    assert session.game_over is False
    # Walking to the pickup is what finishes it.
    assert "features_found" in call(session, "look_around")


def test_god_mode_does_not_end_on_a_city_level_guess() -> None:
    session = make_session(mode="locate")
    session.difficulty = "god"
    result = call(session, "submit_guess", place_name="Lisbon")

    # 1.4 km away: inside god mode's 3 km, so this one does win.
    assert result["final"] is True
    assert result["result"]["outcome"] == "success"

    # Porto, 275 km out, does not.
    other = make_session(mode="locate")
    other.difficulty = "god"
    assert "final" not in call(other, "submit_guess", place_name="Porto")
    assert other.game_over is False
