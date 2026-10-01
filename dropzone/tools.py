"""The game's tools.

Each tool is a plain async function whose first argument is the session. It
returns a JSON-serialisable dict, and on failure returns
`{"error": ..., "suggestion": ...}` so the model always has a next move. The
`@tool` decorator wraps every function so nothing raises into the agent loop.

The whitelisting rule from the spec is absolute here: results are assembled
field by field out of computed values. No API response, and no Overpass
element, is ever forwarded with fields removed.
"""

from __future__ import annotations

import datetime as dt
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import elevation as elev
from . import geo, geocode, overpass, scoring, scripts, sun, tables, weather, wildlife
from .overpass import OverpassError
from .session import EXTRACTION_RADIUS_M, GameSession, Guess

UTC = dt.timezone.utc

LOOK_AROUND_CAP = 25
NEAR_FEATURE_M = 150.0
GREET_RANGE_M = 100.0
LISTEN_RANGE_M = 400.0
HORIZON_MAX_M = 25_000
HORIZON_PER_CATEGORY = 3
# What counts as a horizon landmark. A tower that is also tagged as a public
# clock classifies as the clock, which is not something you see from 5 km.
HORIZON_GROUPS = frozenset({"terrain", "water", "settlements", "structures", "transport"})
WALK_SPEED_KMH = 5.0
MAX_MOVE_M = 3000


@dataclass
class ToolSpec:
    """A tool's provider-neutral definition."""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Awaitable[dict[str, Any]]]
    time_cost_min: float | None

    def schema(self) -> dict[str, Any]:
        """JSON-Schema form, which each provider adapter reshapes as needed."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


REGISTRY: dict[str, ToolSpec] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    time_cost_min: float | None = None,
) -> Callable:
    """Register a function as a tool and give it the never-raise guarantee."""

    def decorate(func: Callable[..., Awaitable[dict[str, Any]]]) -> Callable:
        async def guarded(session: GameSession, **kwargs: Any) -> dict[str, Any]:
            try:
                result = await func(session, **kwargs)
            except OverpassError as exc:
                return {
                    "error": f"map service unavailable ({exc})",
                    "suggestion": "Try again, or call look_around with a smaller radius_m.",
                }
            except TypeError as exc:
                return {
                    "error": f"bad arguments for {name}: {exc}",
                    "suggestion": "Check the tool's parameters and call it again.",
                }
            except Exception as exc:  # noqa: BLE001 - tools must not raise into the loop
                return {
                    "error": f"{name} failed: {type(exc).__name__}: {exc}",
                    "suggestion": "Try a different tool, or try this one again in a moment.",
                }
            if isinstance(result, dict) and "error" not in result:
                result.setdefault("time_cost", _cost_text(session, name))
            return result

        guarded.__name__ = func.__name__
        guarded.__doc__ = func.__doc__
        REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            func=guarded,
            time_cost_min=time_cost_min,
        )
        return guarded

    return decorate


def _cost_text(session: GameSession, name: str) -> str:
    """How much game time a tool charged, for display in the field log."""
    spec = REGISTRY.get(name)
    if not spec or not spec.time_cost_min:
        return "free"
    return f"{spec.time_cost_min:g} min"


async def call_tool(session: GameSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a model-requested tool call."""
    spec = REGISTRY.get(name)
    if spec is None:
        return {
            "error": f"no such tool: {name}",
            "suggestion": f"Use one of: {', '.join(sorted(REGISTRY))}.",
        }
    if session.game_over:
        return {
            "error": "the game is over",
            "suggestion": "Tell the player the run has ended; no further actions are possible.",
        }
    if session.out_of_time() and spec.time_cost_min:
        return {
            "error": "no game time left",
            "suggestion": "The clock has run out. Only check_status and submit_guess remain.",
        }
    clean = _coerce_args(spec, args)
    if isinstance(clean, dict) and "error" in clean:
        return clean
    return await spec.func(session, **clean)


def _coerce_args(spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown arguments and coerce the types models most often fumble."""
    accepted = set(inspect.signature(spec.func).parameters) - {"session", "kwargs"}
    schema_props: dict[str, Any] = spec.parameters.get("properties", {})
    clean: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key not in accepted and key not in schema_props:
            continue
        wanted = (schema_props.get(key) or {}).get("type")
        try:
            if wanted == "integer" and value is not None:
                value = int(float(value))
            elif wanted == "number" and value is not None:
                value = float(value)
        except (TypeError, ValueError):
            return {
                "error": f"{key} must be a {wanted}, got {value!r}",
                "suggestion": "Call the tool again with a numeric value.",
            }
        clean[key] = value
    return clean


def all_schemas() -> list[dict[str, Any]]:
    """Every tool schema, in registration order."""
    return [spec.schema() for spec in REGISTRY.values()]


# ---------------------------------------------------------------------------
# Shared Overpass access
# ---------------------------------------------------------------------------


async def _local_elements(session: GameSession, radius_m: int) -> list[dict[str, Any]]:
    """Overpass elements around the player, reusing any wider cached sweep."""
    key = overpass.cache_key(session.true_lat, session.true_lon, "local")
    cached = session.overpass_cache.get(key)
    cached_radius = session.overpass_cache.get(key + overpass.RADIUS_SUFFIX)
    if cached is not None and isinstance(cached_radius, int) and cached_radius >= radius_m:
        return cached
    elements = await overpass.run_query(
        overpass.local_query(session.true_lat, session.true_lon, radius_m)
    )
    session.overpass_cache[key] = elements
    session.overpass_cache[key + overpass.RADIUS_SUFFIX] = radius_m  # type: ignore[assignment]
    return elements


def _measure(session: GameSession, elements: list[dict[str, Any]], radius_m: float) -> list[dict[str, Any]]:
    """Turn raw elements into measured, classified, named-stripped records.

    Each record keeps its raw tags and coordinates for server-side use by
    `read_sign` / `inspect_road`; the caller decides which keys to publish.
    """
    out: list[dict[str, Any]] = []
    for element in elements:
        point = overpass.element_latlon(element)
        if point is None:
            continue
        tags = element.get("tags") or {}
        classified = overpass.classify(tags)
        if classified is None:
            continue
        category, group = classified
        distance = geo.haversine_m(session.true_lat, session.true_lon, point[0], point[1])
        if distance > radius_m:
            continue
        bearing = geo.bearing_deg(session.true_lat, session.true_lon, point[0], point[1])
        fid = session.feature_id_for(element)
        record = {
            "id": fid,
            "category": category,
            "group": group,
            "distance_m": distance,
            "bearing_deg": bearing,
            "_lat": point[0],
            "_lon": point[1],
            "_tags": tags,
        }
        session.remember_feature(fid, record)
        out.append(record)
    out.sort(key=lambda r: r["distance_m"])
    return out


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """Whitelist a measured record down to what the player may see."""
    return {
        "id": record["id"],
        "category": record["category"],
        "distance": geo.snap_distance(record["distance_m"]),
        "bearing": geo.compass_16(record["bearing_deg"]),
    }


def _name_of(tags: dict[str, str]) -> str | None:
    """The feature's own name, for server-side inspection only."""
    for key in ("name", "name:en", "official_name", "alt_name", "int_name"):
        value = tags.get(key)
        if value:
            return str(value)
    return None


# ---------------------------------------------------------------------------
# Observation tools
# ---------------------------------------------------------------------------


@tool(
    name="look_around",
    description=(
        "Survey the mapped features around you and return each one's category, "
        "rough distance, compass bearing and a short feature id. Returns up to 25 "
        "features, nearest first, grouped by kind (roads, buildings, water, "
        "businesses and so on). Names are never returned: use read_sign on a "
        "feature id to learn about its lettering, or inspect_road on a road. "
        "This is the main way to find out what is physically around the player. "
        "Costs 5 minutes of game time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "radius_m": {
                "type": "integer",
                "description": "Search radius in metres, 100 to 2000. Start around 500.",
                "minimum": 100,
                "maximum": 2000,
            }
        },
    },
    time_cost_min=5,
)
async def look_around(session: GameSession, radius_m: int = 500) -> dict[str, Any]:
    """Nearby mapped features by category, distance and bearing. No names."""
    radius_m = max(100, min(2000, int(radius_m)))
    elements = await _local_elements(session, radius_m)
    session.spend_minutes(5)

    measured = _measure(session, elements, radius_m)
    shown = measured[:LOOK_AROUND_CAP]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in shown:
        grouped.setdefault(record["group"], []).append(_public(record))

    return {
        "radius_m": radius_m,
        "features_found": len(measured),
        "features_shown": len(shown),
        "features_by_group": grouped,
        "note": (
            "Nothing mapped within this radius."
            if not shown
            else "Feature ids are valid for this session only."
        ),
    }


@tool(
    name="read_sign",
    description=(
        "Walk up to a mapped feature and study the lettering on its sign. Returns "
        "the writing system (Latin, Cyrillic, Arabic, Devanagari, Hangul, Han and "
        "so on), how many characters the name has, whether it carries diacritics, "
        "the use of upper and lower case, and the first two characters only. The "
        "full name is never returned. Use it to narrow down the region's language. "
        "The feature must be within 150 metres, so call look_around first and move "
        "closer if needed. Costs 3 minutes of game time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "feature_id": {
                "type": "string",
                "description": "A feature id from a previous look_around, e.g. 'F003'.",
            }
        },
        "required": ["feature_id"],
    },
    time_cost_min=3,
)
async def read_sign(session: GameSession, feature_id: str) -> dict[str, Any]:
    """The writing system and shape of a feature's name, never the name."""
    record = session.features.get(str(feature_id).strip().upper())
    if record is None:
        return {
            "error": f"unknown feature id {feature_id!r}",
            "suggestion": "Call look_around first and use an id from its result.",
        }
    distance = geo.haversine_m(session.true_lat, session.true_lon, record["_lat"], record["_lon"])
    if distance > NEAR_FEATURE_M:
        return {
            "error": f"too far to read: about {geo.snap_distance(distance)} away",
            "suggestion": (
                f"Move {geo.compass_16(geo.bearing_deg(session.true_lat, session.true_lon, record['_lat'], record['_lon']))} "
                f"about {int(distance)} m, then read the sign again."
            ),
        }

    name = _name_of(record["_tags"])
    session.spend_minutes(3)
    if not name:
        return {
            "feature_id": record["id"],
            "category": record["category"],
            "signed": False,
            "note": "This feature carries no sign or lettering.",
        }
    return {
        "feature_id": record["id"],
        "category": record["category"],
        "signed": True,
        "writing_system": scripts.detect_script(name),
        "character_count": len(name),
        "word_count": len(name.split()),
        "has_diacritics": scripts.has_diacritics(name),
        "letter_case": scripts.describe_case(name),
        "first_two_characters": name[:2],
    }


@tool(
    name="check_weather",
    description=(
        "Read the current weather where you stand: temperature in Celsius, what "
        "the sky is doing, precipitation, cloud cover, and wind speed with the "
        "compass direction it is blowing from. Useful early, since temperature "
        "and wind narrow down both hemisphere and season. Costs 1 minute of game "
        "time."
    ),
    time_cost_min=1,
)
async def check_weather(session: GameSession) -> dict[str, Any]:
    """Current conditions as described phrases plus the raw numbers."""
    current = await weather.fetch_current(session.true_lat, session.true_lon)
    session.spend_minutes(1)

    wind_from = current.get("wind_direction_10m")
    return {
        "conditions": weather.describe_code(current.get("weather_code")),
        "feels": weather.describe_temperature(current.get("temperature_2m")),
        "temperature_c": current.get("temperature_2m"),
        "apparent_temperature_c": current.get("apparent_temperature"),
        "relative_humidity_pct": current.get("relative_humidity_2m"),
        "sky": weather.describe_cloud(current.get("cloud_cover")),
        "cloud_cover_pct": current.get("cloud_cover"),
        "precipitation_mm_last_hour": current.get("precipitation"),
        "snowfall_cm_last_hour": current.get("snowfall"),
        "wind": weather.describe_wind(current.get("wind_speed_10m")),
        "wind_speed_kmh": current.get("wind_speed_10m"),
        "wind_gusts_kmh": current.get("wind_gusts_10m"),
        "wind_blowing_from": geo.compass_16(wind_from) if wind_from is not None else "unknown",
    }


@tool(
    name="read_sun",
    description=(
        "Sight the sun with compass and watch. Returns the sun's elevation above "
        "the horizon in degrees, its compass azimuth, minutes until sunset, and "
        "the time on the player's wristwatch, which is set to UTC. A careful "
        "player can derive longitude from when the sun peaks and latitude from "
        "how high it climbs, so this is the most informative tool in the kit. "
        "Costs 1 minute of game time."
    ),
    time_cost_min=1,
)
async def read_sun(session: GameSession) -> dict[str, Any]:
    """Sun elevation and azimuth now, plus the UTC watch time."""
    now = session.clock_utc
    elevation_deg, azimuth_deg = sun.sun_position(session.true_lat, session.true_lon, now)
    session.spend_minutes(1)

    setting = sun.next_sunset(session.true_lat, session.true_lon, now)
    rising = sun.next_sunrise(session.true_lat, session.true_lon, now)
    return {
        "watch_reads": session.watch_time(),
        "sun_elevation_deg": round(elevation_deg, 1),
        "sun_position": sun.describe_elevation(elevation_deg),
        "sun_azimuth_deg": round(azimuth_deg, 1),
        "sun_bearing": geo.compass_16(azimuth_deg),
        "daylight": elevation_deg > -0.833,
        "minutes_until_sunset": (
            int((setting - now).total_seconds() // 60) if setting else None
        ),
        "minutes_until_sunrise": (
            int((rising - now).total_seconds() // 60) if rising else None
        ),
        "note": (
            "Sun never crosses the horizon in the next few days."
            if setting is None and rising is None
            else "The watch is set to UTC, not to local time."
        ),
    }


@tool(
    name="survey_terrain",
    description=(
        "Read the shape of the land. Returns the elevation in metres where you "
        "stand, and how the ground 500 metres away in each of the eight compass "
        "directions compares, summarised as rises, falls or stays level. Use it to "
        "tell a valley floor from a ridge, a coastal plain from a plateau. Costs 5 "
        "minutes of game time."
    ),
    time_cost_min=5,
)
async def survey_terrain(session: GameSession) -> dict[str, Any]:
    """Elevation here plus slope in eight directions."""
    points = [(session.true_lat, session.true_lon)]
    bearings = [b * 45.0 for b in range(8)]
    for bearing in bearings:
        points.append(geo.destination(session.true_lat, session.true_lon, bearing, 500.0))

    values = await elev.fetch_elevations(points)
    session.spend_minutes(5)

    here = values[0]
    if here is None:
        return {
            "error": "elevation service returned nothing for this position",
            "suggestion": "Try survey_terrain again, or use scan_horizon instead.",
        }

    directions: dict[str, Any] = {}
    neighbours: list[float] = []
    for bearing, value in zip(bearings, values[1:]):
        word = geo.compass_8(bearing)
        if value is None:
            directions[word] = {"slope": "unknown", "elevation_change_m": None}
            continue
        neighbours.append(value)
        directions[word] = {
            "slope": elev.describe_slope(here, value, 500.0),
            "elevation_change_m": round(value - here),
        }

    if neighbours:
        lowest, highest = min(neighbours), max(neighbours)
        if highest - lowest < 10:
            landform = "flat ground"
        elif here <= lowest + 5:
            landform = "the bottom of a valley or basin"
        elif here >= highest - 5:
            landform = "a summit or ridge line"
        elif here - lowest > 40:
            landform = "a slope well above the surrounding low ground"
        else:
            landform = "undulating ground"
    else:
        landform = "unknown"

    return {
        "elevation_m": round(here),
        "near_sea_level": here < 20,
        "landform": landform,
        "directions_500m": directions,
    }


@tool(
    name="spot_wildlife",
    description=(
        "Sit still and note what is living around you. Returns up to 8 species "
        "that have been recorded nearby and often, each with its common English "
        "name and group (bird, plant, mammal, insect and so on). Species ranges "
        "are strong regional evidence, so this narrows a continent fast. No "
        "locations or place names are returned. Costs 10 minutes of game time."
    ),
    time_cost_min=10,
)
async def spot_wildlife(session: GameSession) -> dict[str, Any]:
    """Commonly recorded nearby species: common name and group only."""
    species = await wildlife.fetch_species(session.true_lat, session.true_lon, 10.0, 8)
    session.spend_minutes(10)
    if not species:
        return {
            "species_seen": [],
            "note": "Nothing has been recorded around here. No records is itself a clue.",
        }
    return {
        "species_seen": species,
        "species_count": len(species),
        "note": "Species commonly recorded within about 10 km, most-recorded first.",
    }


@tool(
    name="inspect_road",
    description=(
        "Crouch at the roadside and study the road itself. Returns its surface, "
        "lane count, road class, which side of the road traffic drives on, and "
        "whether speed limits are posted in km/h or mph. Road names and route "
        "numbers are never returned. Driving side plus signage units splits the "
        "world into a few candidate regions, so this pairs well with read_sign. "
        "Pass a road id from look_around, and be within 150 metres of it. Costs 3 "
        "minutes of game time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "feature_id": {
                "type": "string",
                "description": "The id of a road or path from look_around, e.g. 'F001'.",
            }
        },
        "required": ["feature_id"],
    },
    time_cost_min=3,
)
async def inspect_road(session: GameSession, feature_id: str) -> dict[str, Any]:
    """Road surface, class, lanes, driving side and speed-sign units."""
    record = session.features.get(str(feature_id).strip().upper())
    if record is None:
        return {
            "error": f"unknown feature id {feature_id!r}",
            "suggestion": "Call look_around first and pass an id from the roads group.",
        }
    if record["group"] not in ("roads", "paths"):
        return {
            "error": f"{record['id']} is a {record['category']}, not a road",
            "suggestion": "Pick an id from the 'roads' or 'paths' group in look_around.",
        }
    distance = geo.haversine_m(session.true_lat, session.true_lon, record["_lat"], record["_lon"])
    if distance > NEAR_FEATURE_M:
        return {
            "error": f"too far from that road: about {geo.snap_distance(distance)} away",
            "suggestion": (
                f"Move {geo.compass_16(geo.bearing_deg(session.true_lat, session.true_lon, record['_lat'], record['_lon']))} "
                f"about {int(distance)} m first."
            ),
        }

    tags: dict[str, str] = record["_tags"]
    session.spend_minutes(3)

    lanes = tags.get("lanes")
    maxspeed = tags.get("maxspeed")
    return {
        "feature_id": record["id"],
        "road_class": record["category"],
        "surface": tags.get("surface", "unrecorded, looks like the local default"),
        "lanes": int(lanes) if lanes and lanes.isdigit() else None,
        "lit": tags.get("lit") == "yes",
        "oneway": tags.get("oneway") == "yes",
        "has_posted_speed_limit": bool(maxspeed),
        "speed_limit_unit": tables.speed_unit(session.country_code),
        "traffic_drives_on": tables.drives_on(session.country_code),
        "sidewalk": tags.get("sidewalk", "none recorded"),
    }


@tool(
    name="listen",
    description=(
        "Stand still, close your eyes and listen for a couple of minutes. Reports "
        "only sounds with a real source: traffic from mapped roads, trains from "
        "mapped railways, running water, surf, bells from a church, a call to "
        "prayer from a mosque, and wind from the live weather, each with a rough "
        "direction. Silence is a valid and meaningful answer. Cheap, and a good "
        "first move before spending time on a wide look_around. Costs 2 minutes of "
        "game time."
    ),
    time_cost_min=2,
)
async def listen(session: GameSession) -> dict[str, Any]:
    """Audible sources derived from nearby mapped features and the wind."""
    elements = await _local_elements(session, max(500, int(LISTEN_RANGE_M)))
    measured = _measure(session, elements, LISTEN_RANGE_M)
    session.spend_minutes(2)

    sounds: list[dict[str, str]] = []

    def nearest(predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        for record in measured:
            if predicate(record):
                return record
        return None

    # Traffic, graded by the biggest road in earshot and how close it is.
    road_weights = {
        "motorway": 5, "major road": 4, "primary road": 4, "secondary road": 3,
        "minor through road": 2, "small road": 2, "residential street": 1,
        "shared-surface street": 1, "service lane": 1, "pedestrianised street": 0,
    }
    traffic_score = 0.0
    traffic_record = None
    for record in measured:
        weight = road_weights.get(record["category"])
        if not weight:
            continue
        score = weight * max(0.1, 1.0 - record["distance_m"] / LISTEN_RANGE_M)
        if score > traffic_score:
            traffic_score, traffic_record = score, record
    if traffic_record is not None:
        level = (
            "constant" if traffic_score >= 3.5
            else "steady" if traffic_score >= 2.0
            else "intermittent" if traffic_score >= 0.8
            else "faint"
        )
        sounds.append({
            "sound": "road traffic",
            "level": level,
            "direction": geo.compass_8(traffic_record["bearing_deg"]),
        })

    rail = nearest(lambda r: r["category"] in (
        "railway line", "light rail line", "metro line", "tram line",
        "railway station", "tram stop", "railway halt", "level crossing",
    ))
    if rail:
        sounds.append({
            "sound": "trains",
            "level": "close" if rail["distance_m"] < 150 else "distant",
            "direction": geo.compass_8(rail["bearing_deg"]),
        })

    water = nearest(lambda r: r["category"] in (
        "river", "stream", "canal", "waterfall", "weir", "fountain",
    ))
    if water:
        sounds.append({
            "sound": "running water",
            "level": "clear" if water["distance_m"] < 120 else "faint",
            "direction": geo.compass_8(water["bearing_deg"]),
        })

    surf = nearest(lambda r: r["category"] in ("coastline", "beach", "bay"))
    if surf:
        sounds.append({
            "sound": "surf",
            "level": "loud" if surf["distance_m"] < 200 else "a steady wash",
            "direction": geo.compass_8(surf["bearing_deg"]),
        })

    worship = nearest(lambda r: r["category"] in (
        "place of worship", "church building", "chapel", "cathedral", "historic church",
    ))
    if worship:
        sounds.append({
            "sound": "a bell",
            "level": "a single stroke on the hour",
            "direction": geo.compass_8(worship["bearing_deg"]),
        })

    mosque = nearest(lambda r: r["category"] == "mosque")
    if mosque:
        sounds.append({
            "sound": "a call to prayer",
            "level": "amplified, carrying over the rooftops",
            "direction": geo.compass_8(mosque["bearing_deg"]),
        })

    market = nearest(lambda r: r["category"] in ("open-air market", "marketplace", "bus station"))
    if market:
        sounds.append({
            "sound": "voices and trade",
            "level": "busy",
            "direction": geo.compass_8(market["bearing_deg"]),
        })

    # Wind comes from the live weather rather than the map.
    try:
        current = await weather.fetch_current(session.true_lat, session.true_lon)
        speed = current.get("wind_speed_10m")
        direction = current.get("wind_direction_10m")
        if speed is not None and float(speed) >= 12:
            sounds.append({
                "sound": "wind",
                "level": weather.describe_wind(speed),
                "direction": (
                    geo.compass_8(direction) if direction is not None else "all around"
                ),
            })
    except Exception:  # noqa: BLE001 - wind is a bonus, not the point of the tool
        pass

    return {
        "range_m": int(LISTEN_RANGE_M),
        "sounds": sounds,
        "note": (
            "Silence, apart from your own breathing. Nothing mechanical in earshot."
            if not sounds
            else "Only sounds with a traceable source are reported."
        ),
    }


@tool(
    name="greet_local",
    description=(
        "Greet a passer-by and listen to how they answer. Returns the local word "
        "for hello, written in its native script. The language is never named: "
        "recognising the word and its script is the point. Requires a shop, cafe, "
        "market or similar within 100 metres, so there is somebody to greet. Three "
        "uses per game. Costs 5 minutes of game time."
    ),
    time_cost_min=5,
)
async def greet_local(session: GameSession) -> dict[str, Any]:
    """A local's greeting, in script, with no language name attached."""
    if session.greet_uses_left <= 0:
        return {
            "error": "no greetings left (3 per game)",
            "suggestion": "Rely on read_sign and spot_wildlife for language and region clues.",
        }

    elements = await _local_elements(session, 500)
    measured = _measure(session, elements, GREET_RANGE_M)
    sociable = [
        r for r in measured
        if r["group"] == "businesses"
        or r["category"] in ("open-air market", "bus station", "railway station",
                             "school", "place of worship", "community hall")
    ]
    if not sociable:
        wider = [
            r for r in _measure(session, elements, 500)
            if r["group"] == "businesses" or r["category"] in ("open-air market", "bus station")
        ]
        hint = (
            f"The nearest is about {geo.snap_distance(wider[0]['distance_m'])} "
            f"{geo.compass_16(wider[0]['bearing_deg'])}; move there and try again."
            if wider
            else "Nothing sociable within 500 m. Walk towards buildings or a road first."
        )
        return {"error": "nobody about to greet within 100 m", "suggestion": hint}

    word = tables.greeting_for(session.country_code)
    session.greet_uses_left -= 1
    session.spend_minutes(5)
    if not word:
        return {
            "greeting_heard": None,
            "greetings_left": session.greet_uses_left,
            "note": "They nod back without speaking.",
        }
    return {
        "greeting_heard": word,
        "writing_system": scripts.detect_script(word),
        "greetings_left": session.greet_uses_left,
        "greeted_at": f"a {sociable[0]['category']} {geo.compass_8(sociable[0]['bearing_deg'])} of you",
        "note": "They return your greeting in the local tongue and move on.",
    }


@tool(
    name="scan_horizon",
    description=(
        "Shade your eyes and scan the far distance for large landmarks within 25 "
        "km: peaks, coastline, lakes, big rivers, towns and cities by size class, "
        "tall structures, each with a bearing and distance. Names are never "
        "returned. Range shrinks in haze, rain or fog, and from low ground where "
        "you cannot see out. Expensive but it frames the whole region. Costs 10 "
        "minutes of game time."
    ),
    time_cost_min=10,
)
async def scan_horizon(session: GameSession) -> dict[str, Any]:
    """Big landmarks within a visibility- and elevation-limited range."""
    visibility_m: float | None = None
    try:
        current = await weather.fetch_current(session.true_lat, session.true_lon)
        if current.get("visibility") is not None:
            visibility_m = float(current["visibility"])
    except Exception:  # noqa: BLE001 - fall back to the full range
        visibility_m = None

    # How much the player can see out is partly where they stand.
    relief_factor = 1.0
    relief_note = "level ground"
    try:
        ring = [(session.true_lat, session.true_lon)] + [
            geo.destination(session.true_lat, session.true_lon, b * 45.0, 5000.0)
            for b in range(8)
        ]
        values = await elev.fetch_elevations(ring)
        here = values[0]
        others = [v for v in values[1:] if v is not None]
        if here is not None and others:
            relative = here - (sum(others) / len(others))
            if relative > 150:
                relief_factor, relief_note = 1.0, "high ground, a clear view out"
            elif relative > 20:
                relief_factor, relief_note = 0.9, "slightly raised ground"
            elif relative > -40:
                relief_factor, relief_note = 0.6, "level ground, horizon close in"
            else:
                relief_factor, relief_note = 0.35, "low ground, hemmed in by higher land"
    except Exception:  # noqa: BLE001 - relief is a modifier, not a requirement
        pass

    range_m = HORIZON_MAX_M * relief_factor
    if visibility_m is not None:
        range_m = min(range_m, max(1500.0, visibility_m))
    range_m = max(1500.0, min(float(HORIZON_MAX_M), range_m))

    key = overpass.cache_key(session.true_lat, session.true_lon, "horizon")
    elements = session.overpass_cache.get(key)
    if elements is None:
        elements = await overpass.run_query(
            overpass.horizon_query(session.true_lat, session.true_lon, HORIZON_MAX_M),
            timeout_s=overpass.HORIZON_TIMEOUT_S,
        )
        session.overpass_cache[key] = elements
    session.spend_minutes(10)

    measured = _measure(session, elements, range_m)

    # Anything inside a few hundred metres is not a horizon landmark. Cities
    # map dozens of near-identical masts and towers, which would crowd out the
    # peak that actually tells the player something, so each kind of landmark
    # gets at most three entries, nearest first.
    grouped: dict[str, list[dict[str, Any]]] = {}
    per_category: dict[str, int] = {}
    landmarks = 0
    for record in measured:
        if record["distance_m"] <= 400:
            continue
        if record["group"] not in HORIZON_GROUPS:
            continue
        category = record["category"]
        if per_category.get(category, 0) >= HORIZON_PER_CATEGORY:
            continue
        per_category[category] = per_category.get(category, 0) + 1
        grouped.setdefault(record["group"], []).append({
            "category": category,
            "distance": f"{record['distance_m'] / 1000:.1f} km",
            "bearing": geo.compass_16(record["bearing_deg"]),
        })
        landmarks += 1
        if landmarks >= 20:
            break

    return {
        "visible_range": f"{range_m / 1000:.0f} km",
        "limited_by": (
            "weather" if visibility_m is not None and visibility_m < HORIZON_MAX_M * relief_factor
            else "terrain and the curve of the earth"
        ),
        "vantage": relief_note,
        "landmarks_by_group": grouped,
        "note": (
            "Nothing large enough to pick out at this range."
            if not landmarks
            else "Distances are estimates by eye."
        ),
    }


# ---------------------------------------------------------------------------
# Movement, state and guessing
# ---------------------------------------------------------------------------


@tool(
    name="move",
    description=(
        "Walk in a direction. Give either a compass direction ('north-east') or a "
        "bearing in degrees, plus a distance in metres, up to 3000 per call. "
        "Returns your new offset from the drop point, the time the walk took, and "
        "whether you reached the extraction point. Walking is 5 km/h on the flat "
        "and slower uphill, so this is the most expensive thing you can do: about "
        "12 minutes per kilometre."
    ),
    parameters={
        "type": "object",
        "properties": {
            "distance_m": {
                "type": "integer",
                "description": "How far to walk, in metres. Maximum 3000 per call.",
                "minimum": 10,
                "maximum": MAX_MOVE_M,
            },
            "direction": {
                "type": "string",
                "description": "Compass direction, e.g. 'north', 'south-west', 'ENE'.",
            },
            "bearing_deg": {
                "type": "number",
                "description": "Bearing in degrees clockwise from north. Use instead of direction.",
                "minimum": 0,
                "maximum": 360,
            },
        },
        "required": ["distance_m"],
    },
    time_cost_min=None,
)
async def move(
    session: GameSession,
    distance_m: int,
    direction: str | None = None,
    bearing_deg: float | None = None,
) -> dict[str, Any]:
    """Walk a bearing and distance; updates position and spends game time."""
    if direction is None and bearing_deg is None:
        return {
            "error": "no direction given",
            "suggestion": "Call move again with direction (e.g. 'north-east') or bearing_deg.",
        }
    if bearing_deg is None:
        key = str(direction).strip().lower().replace("_", "-")
        if key not in geo.DIRECTION_ALIASES:
            return {
                "error": f"unrecognised direction {direction!r}",
                "suggestion": "Use a compass word like 'north', 'south-west', or a bearing_deg.",
            }
        bearing = geo.DIRECTION_ALIASES[key]
    else:
        bearing = float(bearing_deg) % 360.0

    distance = int(distance_m)
    capped = distance > MAX_MOVE_M
    distance = max(0, min(MAX_MOVE_M, distance))
    if distance < 10:
        return {
            "error": "that distance is too short to bother walking",
            "suggestion": "Walk at least 10 m, or use look_around where you stand.",
        }

    start = (session.true_lat, session.true_lon)
    end = geo.destination(start[0], start[1], bearing, float(distance))

    # Naismith's rule: flat-ground time plus a penalty for climbing.
    minutes = distance / 1000.0 * (60.0 / WALK_SPEED_KMH)
    climb_m = 0.0
    try:
        values = await elev.fetch_elevations([start, end])
        if values[0] is not None and values[1] is not None:
            climb_m = max(0.0, values[1] - values[0])
            minutes += climb_m / 10.0
    except Exception:  # noqa: BLE001 - terrain penalty is best-effort
        pass

    minutes = round(minutes)
    session.move_to(end[0], end[1])
    session.distance_walked_m += distance
    session.spend_minutes(minutes)

    result: dict[str, Any] = {
        "walked": f"{distance} m {geo.compass_16(bearing)}",
        "bearing_deg": round(bearing, 1),
        "minutes_spent": minutes,
        "climb_m": round(climb_m),
        "position": session.offset_text,
        "time_remaining": session.status()["time_remaining"],
    }
    if capped:
        result["note"] = f"Capped at {MAX_MOVE_M} m for one walk; call move again to continue."

    if session.extraction_lat is not None and session.extraction_lon is not None:
        gap = geo.haversine_m(
            session.true_lat, session.true_lon, session.extraction_lat, session.extraction_lon
        )
        reached = gap <= EXTRACTION_RADIUS_M
        result["extraction_reached"] = reached
        if reached and not session.extraction_reached:
            session.extraction_reached = True
            result["extraction_note"] = (
                "You are standing on the extraction point. A marker panel, a flattened "
                "patch of ground, and the smell of aviation fuel."
            )
    result["time_cost"] = f"{minutes} min"
    return result


@tool(
    name="radio_check",
    description=(
        "Key the radio and listen for the extraction beacon. Returns the bearing "
        "and distance to the extraction point. The battery holds only 4 charges "
        "for the whole game, so use them to correct your course rather than to "
        "confirm what you already know. Only useful in Escape and Expedition. "
        "Costs 2 minutes of game time."
    ),
    time_cost_min=2,
)
async def radio_check(session: GameSession) -> dict[str, Any]:
    """Bearing and distance to extraction, at the cost of one battery charge."""
    if session.extraction_lat is None or session.extraction_lon is None:
        return {
            "error": "no extraction point in this mode",
            "suggestion": "This is a Locate run. Work on where you are, not on getting out.",
        }
    if session.radio_battery <= 0:
        return {
            "error": "radio battery flat",
            "suggestion": "Navigate by the bearing from your last radio_check and by move.",
        }

    session.radio_battery -= 1
    session.spend_minutes(2)
    distance = geo.haversine_m(
        session.true_lat, session.true_lon, session.extraction_lat, session.extraction_lon
    )
    bearing = geo.bearing_deg(
        session.true_lat, session.true_lon, session.extraction_lat, session.extraction_lon
    )
    return {
        "beacon_bearing": geo.compass_16(bearing),
        "beacon_bearing_deg": round(bearing),
        "beacon_distance_m": int(round(distance / 10) * 10),
        "charges_left": session.radio_battery,
        "extraction_reached": distance <= EXTRACTION_RADIUS_M,
    }


@tool(
    name="check_status",
    description=(
        "Check your own kit and clock: time remaining, how far and in which "
        "direction you are from the drop point, inventory, guesses left and radio "
        "charges. Free, costs no game time, so call it whenever the player asks "
        "where things stand."
    ),
    time_cost_min=None,
)
async def check_status(session: GameSession) -> dict[str, Any]:
    """Clock, position offset, inventory, guesses and battery. Free."""
    return session.status()


@tool(
    name="submit_guess",
    description=(
        "Submit the player's answer for where they are: a place name, as specific "
        "as they like ('Lisbon', 'Alfama, Lisbon', 'northern Vietnam'). Returns "
        "how far the guess is from the truth, rounded, and how many guesses "
        "remain. Three guesses per game, and the third ends the run and reveals "
        "the true location. Only call this when the player clearly commits to a "
        "guess. Free."
    ),
    parameters={
        "type": "object",
        "properties": {
            "place_name": {
                "type": "string",
                "description": "The place the player is guessing, in plain words.",
            }
        },
        "required": ["place_name"],
    },
    time_cost_min=None,
)
async def submit_guess(session: GameSession, place_name: str) -> dict[str, Any]:
    """Distance from the truth for a guessed place name."""
    if session.mode == "escape":
        return {
            "error": "guessing is not part of an Escape run",
            "suggestion": "Focus on radio_check and move to reach the extraction point.",
        }
    if session.guesses_left() <= 0:
        return {
            "error": "no guesses left",
            "suggestion": "The run is decided. Narrate the outcome.",
        }

    name = str(place_name).strip()
    if not name:
        return {
            "error": "empty guess",
            "suggestion": "Ask the player to name a place.",
        }

    hit = await geocode.geocode_place(name)
    if hit is None:
        # A guess we cannot place on the map does not burn an attempt.
        return {
            "error": f"could not find anywhere called {name!r}",
            "suggestion": "Ask the player for a better-known or more specific place name.",
            "guesses_left": session.guesses_left(),
        }

    distance = geo.haversine_m(
        session.true_lat, session.true_lon, float(hit["lat"]), float(hit["lon"])
    )
    session.guesses.append(Guess(place_name=name, distance_m=distance, resolved=True))

    km = geo.round_sig(distance / 1000.0)
    verdict = (
        "spot on" if distance < 2_000
        else "very close" if distance < 25_000
        else "the right region" if distance < 150_000
        else "the right country, roughly" if distance < 600_000
        else "the wrong part of the world" if distance < 3_000_000
        else "the wrong continent"
    )
    result: dict[str, Any] = {
        "guess": name,
        "distance_from_truth": f"{km:g} km",
        "verdict": verdict,
        "guesses_left": session.guesses_left(),
    }

    if session.guesses_left() <= 0:
        result["final"] = True
        result["result"] = scoring.finalise(session, "guesses_spent")
    return result
