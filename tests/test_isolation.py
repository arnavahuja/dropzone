"""Two sessions must share nothing: not position, clock, caches or history."""

from __future__ import annotations

import pytest

from dropzone import tools
from tests.conftest import make_session, run

pytestmark = pytest.mark.usefixtures("offline")


def call(session, name, **args):
    return run(tools.call_tool(session, name, args))


def test_moving_in_one_session_does_not_move_the_other() -> None:
    a = make_session(mode="escape")
    b = make_session(mode="escape")

    call(a, "move", direction="north", distance_m=2000)

    assert a.true_lat != b.true_lat
    assert b.true_lat == b.drop_lat and b.true_lon == b.drop_lon
    assert b.offset_text == "at the drop point"
    assert a.distance_walked_m == 2000
    assert b.distance_walked_m == 0


def test_clocks_and_consumables_are_per_session() -> None:
    a = make_session(mode="escape")
    b = make_session(mode="escape")

    call(a, "look_around", radius_m=500)
    call(a, "radio_check")
    call(a, "greet_local")

    assert a.minutes_elapsed == 12
    assert b.minutes_elapsed == 0
    assert a.radio_battery == 3 and b.radio_battery == 4
    assert a.greet_uses_left == 2 and b.greet_uses_left == 3


def test_caches_and_feature_ids_are_per_session() -> None:
    a = make_session()
    b = make_session()

    call(a, "look_around", radius_m=500)
    assert a.overpass_cache and not b.overpass_cache
    assert a.features and not b.features

    # The same feature in a fresh session gets its own id space, and a's ids
    # are meaningless there.
    result = call(b, "read_sign", feature_id=next(iter(a.features)))
    assert "unknown feature id" in result["error"]


def test_guesses_and_endings_are_per_session() -> None:
    a = make_session(mode="locate")
    b = make_session(mode="locate")

    for place in ("Tokyo", "Porto", "Lisbon"):
        call(a, "submit_guess", place_name=place)

    assert a.game_over is True
    assert b.game_over is False
    assert b.guesses_left() == 3
    # And a finished session refuses further tools without touching b.
    assert "game is over" in call(a, "check_weather")["error"]
    assert "temperature_c" in call(b, "check_weather")


def test_histories_do_not_bleed() -> None:
    a = make_session()
    b = make_session()
    a.history.append({"role": "user", "content": "look around"})
    assert b.history == []


def test_different_drops_have_different_truths() -> None:
    a = make_session(lat=35.0, lon=135.0)
    b = make_session(lat=-33.0, lon=151.0)

    weather_a = call(a, "check_weather")
    weather_b = call(b, "check_weather")
    assert weather_a and weather_b           # both answered
    assert (a.true_lat, a.true_lon) != (b.true_lat, b.true_lon)
