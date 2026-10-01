"""iNaturalist observations client (no API key).

The species list is the clue; everything else in the response is a leak. We ask
for a species count (which is already aggregated, so no observation has a
location in it) and keep only the common name and the taxonomic group.
"""

from __future__ import annotations

import httpx

SPECIES_COUNTS_URL = "https://api.inaturalist.org/v1/observations/species_counts"
TIMEOUT = httpx.Timeout(15.0)
USER_AGENT = "Dropzone/0.1 (course project; tool-calling geography game)"

# iNaturalist iconic taxon -> the word the player sees.
GROUPS = {
    "Aves": "bird",
    "Mammalia": "mammal",
    "Reptilia": "reptile",
    "Amphibia": "amphibian",
    "Actinopterygii": "fish",
    "Elasmobranchii": "fish",
    "Mollusca": "mollusc",
    "Arachnida": "arachnid",
    "Insecta": "insect",
    "Plantae": "plant",
    "Fungi": "fungus",
    "Protozoa": "protozoan",
    "Chromista": "algae",
    "Animalia": "animal",
}


async def fetch_species(lat: float, lon: float, radius_km: float, limit: int) -> list[dict[str, str]]:
    """Commonly recorded species near a point, most-observed first.

    Returns a list of `{"common_name": ..., "group": ...}` and nothing else.
    """
    params = {
        "lat": f"{lat:.4f}",
        "lng": f"{lon:.4f}",
        "radius": f"{radius_km:g}",
        "quality_grade": "research",
        "per_page": str(max(limit * 4, 30)),
        "locale": "en",
    }
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
        response = await client.get(SPECIES_COUNTS_URL, params=params)
        response.raise_for_status()
        results = response.json().get("results", []) or []

    species: list[dict[str, str]] = []
    for row in results:
        taxon = row.get("taxon") or {}
        # Species rank only: a genus or family tells the player far less.
        if taxon.get("rank") not in ("species", "subspecies", "variety"):
            continue
        name = taxon.get("preferred_common_name") or taxon.get("name")
        if not name:
            continue
        group = GROUPS.get(taxon.get("iconic_taxon_name"), "other")
        species.append({"common_name": str(name), "group": group})
        if len(species) >= limit:
            break
    return species
