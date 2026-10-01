"""OpenStreetMap Overpass client, plus the tag -> category vocabulary.

Two rules hold this file together:

1. Raw Overpass elements (which carry coordinates and names) stay in the
   server-side session cache. Only `classify()` output and geometry computed
   from them is ever handed to a tool result.
2. A feature's category comes from a closed vocabulary. An unrecognised tag
   value degrades to a generic word rather than being passed through, so a
   value we never anticipated cannot leak a name.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

USER_AGENT = "Dropzone/0.1 (course project; tool-calling geography game)"

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

TIMEOUT_S = 15.0
# A 25 km sweep is a much bigger job than a street-corner one, so it is allowed
# longer. The client timeout must exceed the timeout declared inside the query,
# or we always give up before the server answers.
HORIZON_TIMEOUT_S = 30.0

# ---------------------------------------------------------------------------
# Category vocabulary
# ---------------------------------------------------------------------------

# Closed vocabulary: OSM tag value -> (category, group). Anything not listed
# falls back to GENERIC_BY_KEY.
VOCAB: dict[str, dict[str, tuple[str, str]]] = {
    "amenity": {
        "cafe": ("cafe", "businesses"),
        "restaurant": ("restaurant", "businesses"),
        "fast_food": ("fast food stand", "businesses"),
        "bar": ("bar", "businesses"),
        "pub": ("pub", "businesses"),
        "biergarten": ("beer garden", "businesses"),
        "bank": ("bank", "businesses"),
        "atm": ("cash machine", "businesses"),
        "pharmacy": ("pharmacy", "businesses"),
        "fuel": ("fuel station", "businesses"),
        "marketplace": ("open-air market", "businesses"),
        "bureau_de_change": ("currency exchange", "businesses"),
        "school": ("school", "civic"),
        "kindergarten": ("kindergarten", "civic"),
        "university": ("university building", "civic"),
        "college": ("college building", "civic"),
        "library": ("library", "civic"),
        "hospital": ("hospital", "civic"),
        "clinic": ("clinic", "civic"),
        "doctors": ("doctor's surgery", "civic"),
        "police": ("police station", "civic"),
        "fire_station": ("fire station", "civic"),
        "post_office": ("post office", "civic"),
        "townhall": ("town hall", "civic"),
        "courthouse": ("courthouse", "civic"),
        "community_centre": ("community hall", "civic"),
        "place_of_worship": ("place of worship", "civic"),
        "grave_yard": ("graveyard", "civic"),
        "parking": ("car park", "transport"),
        "bicycle_parking": ("cycle parking", "transport"),
        "bus_station": ("bus station", "transport"),
        "taxi": ("taxi rank", "transport"),
        "ferry_terminal": ("ferry terminal", "transport"),
        "fountain": ("fountain", "street furniture"),
        "bench": ("bench", "street furniture"),
        "drinking_water": ("drinking water tap", "street furniture"),
        "waste_basket": ("litter bin", "street furniture"),
        "toilets": ("public toilets", "street furniture"),
        "shelter": ("shelter", "street furniture"),
        "clock": ("public clock", "street furniture"),
    },
    "shop": {
        "bakery": ("bakery", "businesses"),
        "butcher": ("butcher", "businesses"),
        "greengrocer": ("greengrocer", "businesses"),
        "supermarket": ("supermarket", "businesses"),
        "convenience": ("corner shop", "businesses"),
        "clothes": ("clothing shop", "businesses"),
        "hairdresser": ("hairdresser", "businesses"),
        "laundry": ("laundry", "businesses"),
        "hardware": ("hardware shop", "businesses"),
        "car_repair": ("garage", "businesses"),
        "florist": ("florist", "businesses"),
        "books": ("bookshop", "businesses"),
        "alcohol": ("off-licence", "businesses"),
        "tobacco": ("tobacconist", "businesses"),
        "mobile_phone": ("phone shop", "businesses"),
        "pharmacy": ("pharmacy", "businesses"),
    },
    "highway": {
        "motorway": ("motorway", "roads"),
        "trunk": ("major road", "roads"),
        "primary": ("primary road", "roads"),
        "secondary": ("secondary road", "roads"),
        "tertiary": ("minor through road", "roads"),
        "unclassified": ("small road", "roads"),
        "residential": ("residential street", "roads"),
        "living_street": ("shared-surface street", "roads"),
        "service": ("service lane", "roads"),
        "track": ("farm or forest track", "paths"),
        "path": ("path", "paths"),
        "footway": ("footway", "paths"),
        "pedestrian": ("pedestrianised street", "paths"),
        "steps": ("flight of steps", "paths"),
        "cycleway": ("cycle track", "paths"),
        "bridleway": ("bridleway", "paths"),
        "bus_stop": ("bus stop", "transport"),
        "crossing": ("pedestrian crossing", "street furniture"),
        "traffic_signals": ("traffic signals", "street furniture"),
        "street_lamp": ("street lamp", "street furniture"),
    },
    "railway": {
        "rail": ("railway line", "transport"),
        "light_rail": ("light rail line", "transport"),
        "subway": ("metro line", "transport"),
        "tram": ("tram line", "transport"),
        "station": ("railway station", "transport"),
        "halt": ("railway halt", "transport"),
        "tram_stop": ("tram stop", "transport"),
        "subway_entrance": ("metro entrance", "transport"),
        "level_crossing": ("level crossing", "transport"),
        "abandoned": ("disused railway", "transport"),
        "disused": ("disused railway", "transport"),
    },
    "natural": {
        "water": ("body of water", "water"),
        "coastline": ("coastline", "water"),
        "beach": ("beach", "water"),
        "spring": ("spring", "water"),
        "wetland": ("wetland", "water"),
        "bay": ("bay", "water"),
        "tree": ("tree", "vegetation"),
        "tree_row": ("row of trees", "vegetation"),
        "wood": ("woodland", "vegetation"),
        "scrub": ("scrub", "vegetation"),
        "grassland": ("grassland", "vegetation"),
        "heath": ("heath", "vegetation"),
        "sand": ("sand", "terrain"),
        "bare_rock": ("bare rock", "terrain"),
        "scree": ("scree", "terrain"),
        "cliff": ("cliff", "terrain"),
        "peak": ("summit", "terrain"),
        "ridge": ("ridge", "terrain"),
        "saddle": ("saddle", "terrain"),
        "volcano": ("volcano", "terrain"),
        "glacier": ("glacier", "terrain"),
    },
    "waterway": {
        "river": ("river", "water"),
        "stream": ("stream", "water"),
        "canal": ("canal", "water"),
        "drain": ("drainage channel", "water"),
        "ditch": ("ditch", "water"),
        "waterfall": ("waterfall", "water"),
        "dam": ("dam", "water"),
        "weir": ("weir", "water"),
        "riverbank": ("riverbank", "water"),
    },
    "leisure": {
        "park": ("park", "open space"),
        "garden": ("garden", "open space"),
        "playground": ("playground", "open space"),
        "pitch": ("sports pitch", "open space"),
        "sports_centre": ("sports centre", "open space"),
        "swimming_pool": ("swimming pool", "open space"),
        "stadium": ("stadium", "open space"),
        "golf_course": ("golf course", "open space"),
        "nature_reserve": ("nature reserve", "open space"),
        "marina": ("marina", "water"),
        "common": ("common land", "open space"),
    },
    "tourism": {
        "hotel": ("hotel", "businesses"),
        "hostel": ("hostel", "businesses"),
        "guest_house": ("guest house", "businesses"),
        "museum": ("museum", "civic"),
        "gallery": ("gallery", "civic"),
        "attraction": ("visitor attraction", "civic"),
        "viewpoint": ("viewpoint", "open space"),
        "information": ("information board", "street furniture"),
        "picnic_site": ("picnic site", "open space"),
        "camp_site": ("campsite", "open space"),
        "artwork": ("public artwork", "street furniture"),
    },
    "landuse": {
        "farmland": ("cultivated field", "agriculture"),
        "farmyard": ("farmyard", "agriculture"),
        "meadow": ("meadow", "agriculture"),
        "orchard": ("orchard", "agriculture"),
        "vineyard": ("vineyard", "agriculture"),
        "plant_nursery": ("plant nursery", "agriculture"),
        "allotments": ("allotments", "agriculture"),
        "paddy": ("paddy field", "agriculture"),
        "aquaculture": ("fish ponds", "agriculture"),
        "forest": ("managed forest", "vegetation"),
        "residential": ("residential area", "built-up"),
        "commercial": ("commercial area", "built-up"),
        "retail": ("retail area", "built-up"),
        "industrial": ("industrial area", "built-up"),
        "quarry": ("quarry", "terrain"),
        "cemetery": ("cemetery", "civic"),
    },
    "place": {
        "city": ("city", "settlements"),
        "town": ("town", "settlements"),
        "village": ("village", "settlements"),
        "hamlet": ("hamlet", "settlements"),
        "suburb": ("suburb", "settlements"),
        "neighbourhood": ("neighbourhood", "settlements"),
        "isolated_dwelling": ("isolated dwelling", "settlements"),
        "farm": ("farm", "settlements"),
        "island": ("island", "terrain"),
        "islet": ("islet", "terrain"),
    },
    "man_made": {
        "tower": ("tower", "structures"),
        "mast": ("mast", "structures"),
        "water_tower": ("water tower", "structures"),
        "windmill": ("windmill", "structures"),
        "watermill": ("watermill", "structures"),
        "lighthouse": ("lighthouse", "structures"),
        "chimney": ("chimney", "structures"),
        "silo": ("silo", "structures"),
        "storage_tank": ("storage tank", "structures"),
        "pier": ("pier", "structures"),
        "bridge": ("bridge", "structures"),
        "pipeline": ("pipeline", "structures"),
        "surveillance": ("camera", "street furniture"),
    },
    "building": {
        "church": ("church building", "civic"),
        "chapel": ("chapel", "civic"),
        "cathedral": ("cathedral", "civic"),
        "mosque": ("mosque", "civic"),
        "temple": ("temple", "civic"),
        "synagogue": ("synagogue", "civic"),
        "shrine": ("shrine", "civic"),
        "train_station": ("station building", "transport"),
        "school": ("school building", "civic"),
        "hospital": ("hospital building", "civic"),
        "farm": ("farmhouse", "agriculture"),
        "barn": ("barn", "agriculture"),
        "greenhouse": ("greenhouse", "agriculture"),
        "warehouse": ("warehouse", "built-up"),
        "apartments": ("apartment block", "built-up"),
        "house": ("house", "built-up"),
        "hut": ("hut", "built-up"),
        "ruins": ("ruined building", "built-up"),
    },
    "historic": {
        "monument": ("monument", "civic"),
        "memorial": ("memorial", "civic"),
        "castle": ("castle", "civic"),
        "fort": ("fort", "civic"),
        "ruins": ("ruins", "civic"),
        "archaeological_site": ("archaeological site", "civic"),
        "church": ("historic church", "civic"),
        "city_gate": ("city gate", "civic"),
        "wayside_shrine": ("wayside shrine", "civic"),
    },
    "aeroway": {
        "aerodrome": ("airfield", "transport"),
        "runway": ("runway", "transport"),
        "helipad": ("helipad", "transport"),
    },
    "barrier": {
        "wall": ("wall", "boundaries"),
        "fence": ("fence", "boundaries"),
        "hedge": ("hedge", "boundaries"),
        "gate": ("gate", "boundaries"),
        "city_wall": ("city wall", "boundaries"),
    },
}

# Used when a key is present but its value is outside the vocabulary above.
GENERIC_BY_KEY = {
    "amenity": ("public amenity", "civic"),
    "shop": ("shop", "businesses"),
    "highway": ("road or path", "roads"),
    "railway": ("railway feature", "transport"),
    "natural": ("natural feature", "terrain"),
    "waterway": ("watercourse", "water"),
    "leisure": ("leisure ground", "open space"),
    "tourism": ("visitor facility", "civic"),
    "landuse": ("land parcel", "built-up"),
    "place": ("populated place", "settlements"),
    "man_made": ("structure", "structures"),
    "building": ("building", "built-up"),
    "historic": ("historic site", "civic"),
    "aeroway": ("aviation feature", "transport"),
    "barrier": ("barrier", "boundaries"),
}

# Priority order: a way tagged both highway and building is described as a road.
KEY_ORDER = [
    "place", "natural", "waterway", "railway", "aeroway", "highway", "amenity",
    "shop", "tourism", "historic", "man_made", "leisure", "landuse", "building",
    "barrier",
]

NAME_KEYS = ("name", "name:en", "int_name", "official_name", "alt_name", "loc_name",
             "old_name", "short_name", "nat_name", "reg_name", "addr:street",
             "addr:city", "ref", "operator", "brand", "network", "destination")


def classify(tags: dict[str, str]) -> tuple[str, str] | None:
    """Map OSM tags onto (category, group) from the closed vocabulary."""
    for key in KEY_ORDER:
        value = tags.get(key)
        if value in (None, "no"):
            continue
        entry = VOCAB.get(key, {}).get(value)
        if entry:
            return entry
        return GENERIC_BY_KEY.get(key, ("unmapped feature", "other"))
    return None


def element_latlon(element: dict[str, Any]) -> tuple[float, float] | None:
    """Representative point of an Overpass element (node coords or way centre)."""
    if "lat" in element and "lon" in element:
        return float(element["lat"]), float(element["lon"])
    center = element.get("center")
    if center:
        return float(center["lat"]), float(center["lon"])
    return None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def local_query(lat: float, lon: float, radius_m: int) -> str:
    """Overpass QL for features near the player.

    Wide radii drop the small stuff (benches, shops, individual trees) so the
    response stays answerable inside the timeout.
    """
    r = int(radius_m)
    a = f"(around:{r},{lat:.6f},{lon:.6f})"
    parts = [
        f"nwr{a}[highway];",
        f"nwr{a}[railway];",
        f"nwr{a}[waterway];",
        f"nwr{a}[natural];",
        f"nwr{a}[place];",
        f"nwr{a}[aeroway];",
        f"nwr{a}[man_made];",
        f"nwr{a}[historic];",
        f"nwr{a}[leisure];",
        f"nwr{a}[landuse];",
        f'nwr{a}["building"~"^(church|chapel|cathedral|mosque|temple|synagogue|shrine|train_station|school|hospital|farm|barn|greenhouse|ruins)$"];',
    ]
    if r <= 1200:
        parts += [f"nwr{a}[amenity];", f"nwr{a}[shop];", f"nwr{a}[tourism];", f"nwr{a}[barrier];"]
    body = "\n  ".join(parts)
    return f"[out:json][timeout:15];\n(\n  {body}\n);\nout center tags qt;"


def horizon_query(lat: float, lon: float, radius_m: int) -> str:
    """Overpass QL for large landmarks only, used by `scan_horizon`.

    Deliberately frugal. Peaks, settlements and towers are nodes, so they are
    asked for as nodes; ways over a 25 km radius are what makes this query
    expensive, so coastline, lakes and rivers are gathered from a tighter radius
    and only when named.
    """
    r = int(radius_m)
    w = min(r, 12_000)
    far = f"(around:{r},{lat:.6f},{lon:.6f})"
    near = f"(around:{w},{lat:.6f},{lon:.6f})"
    body = "\n  ".join([
        f'node{far}["natural"~"^(peak|volcano)$"];',
        f'node{far}["place"~"^(city|town|village)$"];',
        f'node{far}["man_made"~"^(lighthouse|tower|mast|water_tower)$"];',
        f'nwr{far}["aeroway"="aerodrome"];',
        f'way{near}["natural"~"^(coastline|glacier)$"];',
        # The name is a server-side significance filter, to keep farm ponds out
        # of a horizon sweep. It is never returned.
        f'way{near}["natural"="water"]["name"];',
        f'way{near}["waterway"="river"]["name"];',
    ])
    return f"[out:json][timeout:25];\n(\n  {body}\n);\nout center tags qt;"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def cache_key(lat: float, lon: float, kind: str) -> str:
    """Per-session cache key at ~100 m resolution, per the spec's etiquette.

    Three decimal places of latitude is about 110 m, so a player pottering
    around one street re-uses one Overpass response.
    """
    return f"{kind}:{lat:.3f}:{lon:.3f}"


RADIUS_SUFFIX = ":radius"


class OverpassError(RuntimeError):
    """Raised when every mirror fails; tools turn this into an error dict."""


async def run_query(query: str, timeout_s: float = TIMEOUT_S) -> list[dict[str, Any]]:
    """POST a query, trying each mirror in turn. Returns raw elements.

    Overpass answers 429 when an IP is over quota and 504 when a query is too
    heavy, and it reports both as 200-with-an-error-page often enough that the
    body has to be checked too. Each mirror gets two attempts with a short
    backoff before we move on.
    """
    last_error = "no mirrors reachable"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), headers=headers) as client:
        for mirror in MIRRORS:
            for attempt in range(2):
                try:
                    response = await client.post(mirror, content=query.encode("utf-8"))
                    if response.status_code in (429, 504):
                        last_error = f"HTTP {response.status_code} (rate limited or overloaded)"
                        await asyncio.sleep(1.0 + attempt)
                        continue
                    response.raise_for_status()
                    payload = response.json()
                except httpx.HTTPStatusError as exc:
                    last_error = f"HTTP {exc.response.status_code}"
                    break
                except ValueError:
                    # An HTML error page where JSON was promised.
                    last_error = "malformed response"
                    await asyncio.sleep(0.5)
                    continue
                except Exception as exc:  # noqa: BLE001 - mirror fallback is the point
                    last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                    break
                if "remark" in payload and not payload.get("elements"):
                    last_error = f"overpass remark: {payload['remark']}"
                    break
                return list(payload.get("elements", []))
    raise OverpassError(last_error)
