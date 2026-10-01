"""Server-side session state: the only place the truth is kept.

Everything secret lives on this object: the true position, the drop point, the
country code, the reveal label, the Overpass cache (which is full of names and
coordinates). Tools receive the session and read what they need. No tool takes a
coordinate as an argument and no tool returns one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from . import geo

UTC = dt.timezone.utc

Mode = Literal["locate", "escape", "expedition"]
MODES: tuple[str, ...] = ("locate", "escape", "expedition")

MAX_GUESSES = 3
RADIO_CHARGES = 4
GREET_USES = 3
EXTRACTION_RADIUS_M = 120.0


@dataclass
class Guess:
    """One `submit_guess` attempt."""

    place_name: str
    distance_m: float | None
    resolved: bool


@dataclass
class GameSession:
    """One game. Sessions share nothing: all mutable state hangs off here."""

    mode: Mode
    true_lat: float
    true_lon: float
    drop_lat: float
    drop_lon: float
    start_utc: dt.datetime
    deadline_utc: dt.datetime
    country_code: str | None = None
    reveal_label: str = "an undisclosed location"
    extraction_lat: float | None = None
    extraction_lon: float | None = None

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    clock_utc: dt.datetime = field(init=False)
    distance_walked_m: float = 0.0

    radio_battery: int = RADIO_CHARGES
    greet_uses_left: int = GREET_USES
    inventory: list[str] = field(
        default_factory=lambda: [
            "wristwatch (set to UTC)",
            "compass",
            "radio (4 charges)",
            "1 L water",
        ]
    )

    guesses: list[Guess] = field(default_factory=list)
    extraction_reached: bool = False
    game_over: bool = False
    outcome: str | None = None
    score: int | None = None
    ended_reason: str | None = None
    # The scorer's payload, kept so the frontend can fetch the reveal even if
    # the run ended several tool rounds ago.
    result_payload: dict[str, Any] | None = None

    history: list[dict[str, Any]] = field(default_factory=list)

    # Caches. Raw, unredacted, server-only.
    overpass_cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    features: dict[str, dict[str, Any]] = field(default_factory=dict)
    _feature_ids: dict[str, str] = field(default_factory=dict)
    _feature_counter: int = 0

    def __post_init__(self) -> None:
        self.clock_utc = self.start_utc

    # -- clock ------------------------------------------------------------

    @property
    def minutes_remaining(self) -> int:
        """Whole minutes of game time left, floored at zero."""
        left = (self.deadline_utc - self.clock_utc).total_seconds() / 60.0
        return max(0, int(left))

    @property
    def minutes_elapsed(self) -> int:
        """Whole minutes of game time spent so far."""
        return max(0, int((self.clock_utc - self.start_utc).total_seconds() / 60.0))

    @property
    def total_minutes(self) -> int:
        """The session's full time budget in minutes."""
        return max(1, int((self.deadline_utc - self.start_utc).total_seconds() / 60.0))

    def spend_minutes(self, minutes: float) -> None:
        """Advance the game clock. Running the clock out ends the game."""
        if minutes <= 0 or self.game_over:
            return
        self.clock_utc += dt.timedelta(minutes=minutes)
        if self.clock_utc >= self.deadline_utc:
            self.clock_utc = self.deadline_utc

    def out_of_time(self) -> bool:
        """True once the clock has reached the deadline."""
        return self.clock_utc >= self.deadline_utc

    def watch_time(self) -> str:
        """The player's watch, which reads UTC and nothing else."""
        return self.clock_utc.strftime("%Y-%m-%d %H:%M UTC")

    # -- position ---------------------------------------------------------

    @property
    def offset_text(self) -> str:
        """Where the player is, phrased as an offset from the drop point."""
        return geo.describe_offset(self.drop_lat, self.drop_lon, self.true_lat, self.true_lon)

    def move_to(self, lat: float, lon: float) -> None:
        """Place the player at a new true position (callers compute it)."""
        self.true_lat, self.true_lon = lat, lon

    # -- features ---------------------------------------------------------

    def feature_id_for(self, element: dict[str, Any]) -> str:
        """A stable, opaque id for an Overpass element within this session.

        Ids are per-session and sequential, so they carry no OSM identity that
        could be looked up outside the game.
        """
        key = f"{element.get('type')}/{element.get('id')}"
        existing = self._feature_ids.get(key)
        if existing:
            return existing
        self._feature_counter += 1
        fid = f"F{self._feature_counter:03d}"
        self._feature_ids[key] = fid
        return fid

    def remember_feature(self, fid: str, record: dict[str, Any]) -> None:
        """Cache the server-side record (tags and coordinates) for a feature."""
        self.features[fid] = record

    # -- status -----------------------------------------------------------

    def guesses_left(self) -> int:
        """Guesses still available in this session."""
        return max(0, MAX_GUESSES - len(self.guesses))

    def best_guess_distance_m(self) -> float | None:
        """Closest resolved guess so far, in metres."""
        distances = [g.distance_m for g in self.guesses if g.distance_m is not None]
        return min(distances) if distances else None

    def status(self) -> dict[str, Any]:
        """The status block shared by the HTTP response and `check_status`.

        Note what is absent: coordinates, the country, the reveal label.
        """
        body: dict[str, Any] = {
            "mode": self.mode,
            "game_time": self.watch_time(),
            "minutes_remaining": self.minutes_remaining,
            "time_remaining": _format_duration(self.minutes_remaining),
            "offset_from_drop": self.offset_text,
            "distance_walked": f"{self.distance_walked_m / 1000:.1f} km",
            "radio_charges_left": self.radio_battery,
            "guesses_left": self.guesses_left(),
            "inventory": list(self.inventory),
            "game_over": self.game_over,
        }
        if self.mode in ("escape", "expedition"):
            body["extraction_reached"] = self.extraction_reached
        if self.game_over:
            body["outcome"] = self.outcome
            body["score"] = self.score
        return body


def _format_duration(minutes: int) -> str:
    """'4 h 05 min' style formatting for a minute count."""
    if minutes <= 0:
        return "no time left"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} h {mins:02d} min"
    if hours:
        return f"{hours} h"
    return f"{mins} min"
