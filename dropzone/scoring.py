"""End-of-game results. The server decides these; the model only narrates them.

Scores are out of 1000 per objective, so Expedition tops out at 2000.
"""

from __future__ import annotations

import math
from typing import Any

from . import geo
from .session import GameSession

MAX_OBJECTIVE_POINTS = 1000

# A guess 400 km out is worth about a third of a perfect one.
GUESS_DECAY_KM = 400.0


def guess_points(distance_m: float | None) -> int:
    """Points for the best guess: 1000 at zero error, decaying with distance."""
    if distance_m is None:
        return 0
    return round(MAX_OBJECTIVE_POINTS * math.exp(-(distance_m / 1000.0) / GUESS_DECAY_KM))


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
        breakdown["location"] = guess_points(best)
        breakdown["time_remaining"] = time_bonus(session)
    elif session.mode == "escape":
        breakdown["extraction"] = escape_points(session)
    else:
        breakdown["extraction"] = escape_points(session)
        breakdown["location"] = guess_points(best)

    score = sum(breakdown.values())

    if session.mode == "locate":
        won = best is not None and best <= 50_000
    elif session.mode == "escape":
        won = session.extraction_reached
    else:
        won = session.extraction_reached and best is not None and best <= 50_000

    session.game_over = True
    session.score = score
    session.outcome = "success" if won else "failure"
    session.ended_reason = reason

    payload = {
        "mode": session.mode,
        "outcome": session.outcome,
        "reason": reason,
        "score": score,
        "score_breakdown": breakdown,
        "best_guess_error": (
            f"{geo.round_sig(best / 1000.0):g} km" if best is not None else "no valid guess"
        ),
        "guesses_made": [g.place_name for g in session.guesses],
        "extraction_reached": session.extraction_reached,
        "time_used": f"{session.minutes_elapsed // 60} h {session.minutes_elapsed % 60:02d} min",
        "true_location": session.reveal_label,
        "true_coordinates": f"{session.true_lat:.4f}, {session.true_lon:.4f}",
        "drop_coordinates": f"{session.drop_lat:.4f}, {session.drop_lon:.4f}",
    }
    session.result_payload = payload
    return payload
