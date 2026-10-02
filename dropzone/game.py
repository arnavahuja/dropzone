"""Game setup: choosing a drop point, placing extraction, starting the clock."""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path
from typing import Any

from . import geo, geocode, overpass, sun
from .session import DIFFICULTIES, GameSession, Mode

UTC = dt.timezone.utc

DROPS_PATH = Path(__file__).resolve().parent.parent / "data" / "drops.json"

JITTER_M = 300.0
MIN_FEATURES_WITHIN_1KM = 5
MAX_SEED_ATTEMPTS = 6

DEFAULT_BUDGET_MIN = 6 * 60
MIN_BUDGET_MIN = 3 * 60

EXTRACTION_MIN_M = 2000.0
EXTRACTION_MAX_M = 5000.0

# Easy drops only in cities most people could place on a map. Hard and god use
# the whole list, which is still cities - no villages, no farmland - just ones
# you might have to think about.
EASY_FAME = "famous"

_drops_cache: list[dict[str, Any]] | None = None


def load_drops() -> list[dict[str, Any]]:
    """Read the server-only seed file. Never exposed over HTTP."""
    global _drops_cache
    if _drops_cache is None:
        with DROPS_PATH.open(encoding="utf-8") as fh:
            _drops_cache = json.load(fh)["drops"]
    return _drops_cache


def jitter(lat: float, lon: float, rng: random.Random, radius_m: float = JITTER_M) -> tuple[float, float]:
    """Nudge a seed point by up to `radius_m` so repeat plays differ."""
    bearing = rng.uniform(0, 360)
    distance = rng.uniform(0, radius_m)
    return geo.destination(lat, lon, bearing, distance)


async def _count_features(lat: float, lon: float) -> tuple[int, list[dict[str, Any]]]:
    """How many classifiable features sit within 1 km, plus the raw elements."""
    elements = await overpass.run_query(overpass.local_query(lat, lon, 1000))
    count = 0
    for element in elements:
        point = overpass.element_latlon(element)
        if point is None:
            continue
        if overpass.classify(element.get("tags") or {}) is None:
            continue
        if geo.haversine_m(lat, lon, point[0], point[1]) <= 1000:
            count += 1
    return count, elements


def candidate_drops(difficulty: str) -> list[dict[str, Any]]:
    """The seeds eligible for a difficulty.

    Easy draws only on the famous cities. Hard and god draw on all of them, so
    the city can be one you have to work for - but it is always a real city with
    a name worth guessing.
    """
    seeds = load_drops()
    if difficulty != "easy":
        return seeds
    return [seed for seed in seeds if seed.get("fame") == EASY_FAME] or seeds


async def pick_drop(
    rng: random.Random, difficulty: str = "easy"
) -> tuple[dict[str, Any], float, float, list[dict[str, Any]]]:
    """Choose a seed, jitter it, and check it has enough mapped detail."""
    seeds = candidate_drops(difficulty)
    tried: set[int] = set()
    last: tuple[dict[str, Any], float, float] | None = None

    for _ in range(MAX_SEED_ATTEMPTS):
        index = rng.randrange(len(seeds))
        if index in tried:
            continue
        tried.add(index)
        seed = seeds[index]
        lat, lon = jitter(seed["lat"], seed["lon"], rng)
        last = (seed, lat, lon)
        try:
            count, elements = await _count_features(lat, lon)
        except overpass.OverpassError:
            # Overpass is down. The seeds are hand-picked, so trust the seed
            # rather than refusing to start a game.
            return seed, lat, lon, []
        if count >= MIN_FEATURES_WITHIN_1KM:
            return seed, lat, lon, elements

    seed, lat, lon = last  # type: ignore[misc]
    return seed, lat, lon, []


async def place_extraction(
    lat: float, lon: float, rng: random.Random
) -> tuple[float, float]:
    """A point 2-5 km away, snapped onto a mapped road or path if we can find one.

    Snapping matters: an extraction point in the middle of a lake or a cliff face
    is not reachable on foot.
    """
    bearings = [rng.uniform(0, 360)]
    bearings += [(bearings[0] + offset) % 360 for offset in (72, 144, 216, 288)]

    for bearing in bearings:
        distance = rng.uniform(EXTRACTION_MIN_M, EXTRACTION_MAX_M)
        target = geo.destination(lat, lon, bearing, distance)
        query = (
            f"[out:json][timeout:15];\n"
            f'way(around:600,{target[0]:.6f},{target[1]:.6f})'
            f'["highway"~"^(residential|unclassified|tertiary|secondary|primary|living_street|'
            f'service|track|path|footway|pedestrian|cycleway|bridleway)$"];\n'
            f"out center;"
        )
        try:
            elements = await overpass.run_query(query)
        except overpass.OverpassError:
            return target
        candidates = []
        for element in elements:
            point = overpass.element_latlon(element)
            if point is None:
                continue
            from_drop = geo.haversine_m(lat, lon, point[0], point[1])
            if EXTRACTION_MIN_M <= from_drop <= EXTRACTION_MAX_M:
                candidates.append((geo.haversine_m(target[0], target[1], point[0], point[1]), point))
        if candidates:
            candidates.sort(key=lambda row: row[0])
            return candidates[0][1]

    bearing = rng.uniform(0, 360)
    return geo.destination(lat, lon, bearing, rng.uniform(EXTRACTION_MIN_M, EXTRACTION_MAX_M))


def plan_clock(lat: float, lon: float, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """`(start, deadline)`: 6 h, or until sunset, but never under 3 h.

    A night-time drop is shifted forward to the morning rather than played blind.
    """
    start = sun.morning_start(lat, lon, now)
    deadline = start + dt.timedelta(minutes=DEFAULT_BUDGET_MIN)
    setting = sun.next_sunset(lat, lon, start)
    if setting is not None and setting < deadline:
        deadline = max(setting, start + dt.timedelta(minutes=MIN_BUDGET_MIN))
    return start, deadline


def coarsen_label(label: str) -> str:
    """Drop a seed label's leading neighbourhood, leaving city and country.

    "Spaccanapoli, Naples, Italy" becomes "Naples, Italy".
    """
    parts = [part.strip() for part in label.split(",")]
    return ", ".join(parts[1:]) if len(parts) >= 3 else label


def compose_reveal(label: str, difficulty: str) -> str:
    """The end-of-run reveal, at the granularity the run was played at.

    Built from the seed label, which is written in English, so the result page
    never shows a name in a script the player cannot read. God mode reveals the
    neighbourhood it asked for; easy and hard reveal the city.
    """
    return label if difficulty == "god" else coarsen_label(label)


async def new_game(
    mode: str, seed: int | None = None, difficulty: str = "easy"
) -> GameSession:
    """Build a fully initialised session for a mode and difficulty."""
    if mode not in ("locate", "escape", "expedition"):
        raise ValueError(f"unknown mode: {mode}")
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty: {difficulty}")

    rng = random.Random(seed)
    drop_seed, lat, lon, elements = await pick_drop(rng, difficulty)
    start, deadline = plan_clock(lat, lon, dt.datetime.now(UTC))

    reveal = compose_reveal(
        drop_seed.get("label", "an undisclosed location"), difficulty
    )

    session = GameSession(
        mode=mode,  # type: ignore[arg-type]
        true_lat=lat,
        true_lon=lon,
        drop_lat=lat,
        drop_lon=lon,
        start_utc=start,
        deadline_utc=deadline,
        difficulty=difficulty,  # type: ignore[arg-type]
        reveal_label=reveal,
    )

    # The drop check already pulled a 1 km sweep. Prime the session cache with
    # it so the player's first look_around costs Overpass nothing: the service
    # is rate limited per IP, and a game opens with several queries already.
    if elements:
        key = overpass.cache_key(lat, lon, "local")
        session.overpass_cache[key] = elements
        session.overpass_cache[key + overpass.RADIUS_SUFFIX] = 1000  # type: ignore[assignment]

    # Resolve the country once, server-side. Only the code is kept, and only
    # `tables.py` ever reads it.
    try:
        code = await geocode.reverse_country_code(lat, lon)
        if code:
            session.country_code = code
    except Exception:  # noqa: BLE001 - the tools that need it degrade gracefully
        pass

    if mode in ("escape", "expedition"):
        try:
            extraction = await place_extraction(lat, lon, rng)
        except Exception:  # noqa: BLE001
            extraction = geo.destination(lat, lon, rng.uniform(0, 360), 3000.0)
        session.extraction_lat, session.extraction_lon = extraction

    return session


OPENING = {
    "locate": (
        "The engine note fades to the north and you are alone. No map, no phone, no idea. "
        "A watch on your wrist reads UTC and a compass needle settles. You have a few hours "
        "of daylight and three guesses. Work out where on earth you are."
    ),
    "escape": (
        "The engine note fades and you are alone. There is an extraction point somewhere "
        "within five kilometres, a radio with four charges to find its bearing, and not "
        "much daylight. Get there."
    ),
    "expedition": (
        "The engine note fades and you are alone. There is an extraction point within five "
        "kilometres, a radio with four charges, and a question nobody has answered: where "
        "is this? Reach the pickup, then name the place."
    ),
}


def opening_text(session: GameSession) -> str:
    """The deterministic opening narration, written by the server not the model."""
    status = session.status()
    lines = [OPENING[session.mode], ""]
    if session.mode != "escape":
        lines.append(
            f"Difficulty: {session.difficulty}. To answer, {session.precision['answer']}."
        )
    lines.append(
        f"Watch: {status['game_time']}. Daylight remaining: {status['time_remaining']}."
    )
    lines.append(
        "Try: 'look around', 'listen', 'where is the sun', 'walk north-east 800 metres'."
    )
    return "\n".join(lines)


def describe_mode_hint(mode: Mode) -> str:
    """One line reminding the model what the player is trying to do."""
    return {
        "locate": "The player must work out where they are. Three guesses.",
        "escape": "The player must reach the extraction point before the clock runs out.",
        "expedition": "The player must reach extraction and then name the location.",
    }[mode]
