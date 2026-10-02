"""End-of-game results. The server decides these; the model only narrates them.

Scores are out of 1000 per objective, so Expedition tops out at 2000.
"""

from __future__ import annotations

import math
from typing import Any

from . import geo
from .session import PRECISION, GameSession

MAX_OBJECTIVE_POINTS = 1000


def guess_points(distance_m: float | None, difficulty: str = "easy") -> int:
    """Points for the best guess: 1000 at zero error, decaying with distance.

    How fast it decays is the difficulty. On easy the curve is forgiving enough
    that naming the right city scores near full marks; on hard it is tight
    enough that only the right neighbourhood does.
    """
    if distance_m is None:
        return 0
    decay_km = PRECISION[difficulty]["decay_km"]
    return round(MAX_OBJECTIVE_POINTS * math.exp(-(distance_m / 1000.0) / decay_km))


def verdict(distance_m: float, difficulty: str = "easy") -> str:
    """How a guess reads back to the player, scaled to the target precision.

    The near bands are relative to what counts as a win, so "close enough" means
    the same thing to the player on either difficulty. The far bands are
    absolute, because being on the wrong continent is the wrong continent.
    """
    win_m = PRECISION[difficulty]["win_m"]
    if distance_m <= win_m * 0.25:
        return "dead on"
    if distance_m <= win_m:
        return "close enough to count"
    if distance_m <= win_m * 8:
        return "the right area, but not precise enough"
    if distance_m <= 150_000:
        return "the right region"
    if distance_m <= 600_000:
        return "the right country, roughly"
    if distance_m <= 3_000_000:
        return "the wrong part of the world"
    return "the wrong continent"


def escape_points(session: GameSession) -> int:
    """Points for the extraction objective.

    Reaching it pays 600 plus up to 400 for the time left on the clock. Missing
    it pays up to 250 for how close the player got.
    """
    if session.extraction_reached:
        fraction_left = session.minutes_remaining / session.total_minutes
        return round(600 + 400 * max(0.0, min(1.0, fraction_left)))
    if session.extraction_lat is None or session.extraction_lon is None:
        return 0
    gap_m = geo.haversine_m(
        session.true_lat, session.true_lon, session.extraction_lat, session.extraction_lon
    )
    start_gap_m = geo.haversine_m(
        session.drop_lat, session.drop_lon, session.extraction_lat, session.extraction_lon
    )
    if start_gap_m <= 0:
        return 0
    closed = 1.0 - gap_m / start_gap_m
    return round(250 * max(0.0, min(1.0, closed)))


def time_bonus(session: GameSession) -> int:
    """Up to 200 points for finishing a Locate run with time to spare."""
    fraction_left = session.minutes_remaining / session.total_minutes
    return round(200 * max(0.0, min(1.0, fraction_left)))


def finalise(session: GameSession, reason: str) -> dict[str, Any]:
    """Score the session, mark it over, and build the reveal payload.

    `reason` is one of 'out_of_time', 'guesses_spent', 'extracted', 'resigned'.
    This is the one and only place the true location is allowed out.
    """
    best = session.best_guess_distance_m()
    breakdown: dict[str, int] = {}

    if session.mode == "locate":
        breakdown["location"] = guess_points(best, session.difficulty)
        breakdown["time_remaining"] = time_bonus(session)
    elif session.mode == "escape":
        breakdown["extraction"] = escape_points(session)
    else:
        breakdown["extraction"] = escape_points(session)
        breakdown["location"] = guess_points(best, session.difficulty)

    score = sum(breakdown.values())

    win_m = session.precision["win_m"]
    located = best is not None and best <= win_m
    if session.mode == "locate":
        won = located
    elif session.mode == "escape":
        won = session.extraction_reached
    else:
        won = session.extraction_reached and located

    session.game_over = True
    session.score = score
    session.outcome = "success" if won else "failure"
    session.ended_reason = reason

    payload = {
        "mode": session.mode,
        "difficulty": session.difficulty,
        "outcome": session.outcome,
        "reason": reason,
        "score": score,
        "score_breakdown": breakdown,
        "best_guess_error": (
            f"{geo.round_sig(best / 1000.0):g} km" if best is not None else "no valid guess"
        ),
        "guesses_made": [g.place_name for g in session.guesses],
        "needed_to_be_within": f"{win_m // 1000} km",
        "extraction_reached": session.extraction_reached,
        "time_used": f"{session.minutes_elapsed // 60} h {session.minutes_elapsed % 60:02d} min",
        "true_location": session.reveal_label,
        "true_coordinates": f"{session.true_lat:.4f}, {session.true_lon:.4f}",
        "drop_coordinates": f"{session.drop_lat:.4f}, {session.drop_lon:.4f}",
    }
    session.result_payload = payload
    return payload
