"""Geodesy, offset wording and scoring."""

from __future__ import annotations

import pytest

from dropzone import geo, scoring
from tests.conftest import make_session


def test_haversine_known_distances() -> None:
    # Lisbon to Porto, about 274 km.
    d = geo.haversine_m(38.7223, -9.1393, 41.1579, -8.6291)
    assert 270_000 < d < 280_000

    # London to New York, about 5570 km.
    d = geo.haversine_m(51.5074, -0.1278, 40.7128, -74.0060)
    assert 5_520_000 < d < 5_620_000

    # A degree of latitude at the equator is about 111 km.
    assert 110_500 < geo.haversine_m(0, 0, 1, 0) < 111_500

    assert geo.haversine_m(10, 20, 10, 20) == pytest.approx(0.0)


def test_haversine_is_symmetric() -> None:
    there = geo.haversine_m(1.1, 2.2, -33.9, 151.2)
    back = geo.haversine_m(-33.9, 151.2, 1.1, 2.2)
    assert there == pytest.approx(back)


def test_bearings_point_the_right_way() -> None:
    assert geo.bearing_deg(0, 0, 1, 0) == pytest.approx(0.0, abs=0.1)
    assert geo.bearing_deg(0, 0, 0, 1) == pytest.approx(90.0, abs=0.1)
    assert geo.bearing_deg(0, 0, -1, 0) == pytest.approx(180.0, abs=0.1)
    assert geo.bearing_deg(0, 0, 0, -1) == pytest.approx(270.0, abs=0.1)


def test_destination_round_trips_with_haversine() -> None:
    lat, lon = geo.destination(45.0, 9.0, 37.0, 2500.0)
    assert geo.haversine_m(45.0, 9.0, lat, lon) == pytest.approx(2500.0, rel=1e-4)
    assert geo.bearing_deg(45.0, 9.0, lat, lon) == pytest.approx(37.0, abs=0.1)


def test_destination_wraps_the_antimeridian() -> None:
    _, lon = geo.destination(0.0, 179.99, 90.0, 5000.0)
    assert -180.0 <= lon <= 180.0
    assert lon < 0  # crossed into the western hemisphere


def test_compass_words() -> None:
    assert geo.compass_16(0) == "north"
    assert geo.compass_16(45) == "north-east"
    assert geo.compass_16(112.5) == "east-south-east"
    assert geo.compass_16(359) == "north"
    assert geo.compass_8(100) == "east"
    assert geo.compass_8(225) == "south-west"


def test_round_sig_keeps_two_figures() -> None:
    assert geo.round_sig(1234.0) == 1200
    assert geo.round_sig(0.0456) == pytest.approx(0.046)
    assert geo.round_sig(98765.0) == 99000
    assert geo.round_sig(0.0) == 0.0


def test_describe_offset_never_contains_coordinates() -> None:
    text = geo.describe_offset(38.7121, -9.13, 38.7221, -9.12)
    assert "38." not in text and "-9." not in text
    assert "north" in text
    assert geo.describe_offset(10.0, 10.0, 10.0, 10.0) == "at the drop point"


def test_guess_points_decay_with_distance() -> None:
    assert scoring.guess_points(0) == 1000
    assert scoring.guess_points(None) == 0
    near = scoring.guess_points(10_000)
    far = scoring.guess_points(2_000_000)
    assert 900 < near < 1000
    assert far < 20
    assert near > far


def test_locate_scoring_combines_guess_and_time() -> None:
    session = make_session(mode="locate", minutes=360)
    session.guesses.append(
        type("G", (), {"place_name": "Lisbon", "distance_m": 1500.0, "resolved": True})()
    )
    session.spend_minutes(60)  # five hours left of six

    result = scoring.finalise(session, "guesses_spent")
    assert session.game_over is True
    assert result["outcome"] == "success"
    assert result["score_breakdown"]["location"] > 990
    assert result["score_breakdown"]["time_remaining"] == pytest.approx(167, abs=2)
    assert result["true_location"] == "Alfama, Lisboa, Portugal"


def test_escape_scoring_rewards_arriving_early() -> None:
    quick = make_session(mode="escape")
    quick.extraction_reached = True
    quick.spend_minutes(60)

    slow = make_session(mode="escape")
    slow.extraction_reached = True
    slow.spend_minutes(300)

    assert scoring.escape_points(quick) > scoring.escape_points(slow)
    assert scoring.finalise(quick, "extracted")["outcome"] == "success"


def test_failed_escape_scores_partial_progress() -> None:
    session = make_session(mode="escape")
    # Extraction is 0.027 degrees of latitude north; walk roughly half way.
    session.move_to(session.true_lat + 0.0135, session.true_lon)
    points = scoring.escape_points(session)
    assert 100 < points < 160

    result = scoring.finalise(session, "out_of_time")
    assert result["outcome"] == "failure"
    assert result["extraction_reached"] is False
