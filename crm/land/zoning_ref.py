"""Zoning district reference: what each zoning code means, what it allows and its main standards, per jurisdiction.

The data are JSON files in crm/land/zoning_ref/ (one per jurisdiction), researched from the published zoning codes. Each
district records its purpose, permitted uses, conditional uses, standards and the code section it came from. Fields the
research could not confirm are null and are shown as "not captured", never guessed. Files whose text came from a
third-party copy of the code (a "mirror") are flagged so a reader knows to check the city or county's own code.

A parcel's jurisdiction comes from its zoning overlay's authority (a city layer, the county layer) or, for Charles County,
its county. `district_for(parcel)` returns the matching district, `references(parcels)` the districts to print for a report.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "zoning_ref"

# Zoning overlay authority text -> data file. (Authority strings come from crm/land/counties.py overlays.)
AUTHORITY_FILES = {
    "Maricopa County (unincorporated land only)": "maricopa_county",
    "City of Casa Grande": "casa_grande",
    "City of Eloy": "eloy",
    "Pinal County (city names = zoned by that city)": "pinal_county",
    "City of Huntsville": "huntsville_al",
    "Miami-Dade County (unincorporated land only)": "miami_dade_county",
    "City of Homestead": "homestead_fl",
    "City of Florida City": "florida_city_fl",
    "Town of Cleveland, UT": "cleveland_ut",
    "City and County of Honolulu (Land Use Ordinance)": "honolulu_hi",
    "Green River, UT": "green_river_ut",
    "Castle Dale, UT": "castle_dale_ut",
    "Huntington, UT": "huntington_ut",
    "Emery, UT": "emery_town_ut",
    "Clawson, UT": "clawson_ut",
    "Orangeville, UT": "orangeville_ut",
    "Ferron, UT": "ferron_ut",
    "Elmo, UT": "elmo_ut",
}
COUNTY_FILES = {"24017": "charles_county_md"}  # counties whose zoning layer carries no authority text

# Counties we load parcels for but hold no zoning layer for: what to say instead of a district.
NO_LAYER = {
    "TX": "Texas counties have no zoning authority; a city zones only land inside its limits, and no city zoning layer is loaded here.",
    "NC": "No zoning layer is loaded for this North Carolina county, so the district is not shown. Ask the county or city planning office.",
    "IN": "No zoning layer is loaded for this Indiana county, so the district is not shown. Ask the county or city planning office.",
    "UT": "Utah publishes zoning only for incorporated towns; unincorporated Emery County land has no public zoning layer, so the district is not shown. Ask the county planning office.",
}


@lru_cache(maxsize=None)
def load(key):
    path = DATA_DIR / f"{key}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data["key"] = key
    data["by_code"] = {d["code"].upper(): d for d in data.get("districts", [])}
    blob = f"{data.get('edition', '')} {data.get('code_url', '')}".lower()
    data["mirror"] = "zoneomics" in blob or "mirror" in blob
    return data


def jurisdiction_key(parcel):
    zone = (parcel.overlays or {}).get("zoning") or {}
    authority = zone.get("authority") or ""
    if authority in AUTHORITY_FILES:
        return AUTHORITY_FILES[authority]
    return COUNTY_FILES.get(getattr(getattr(parcel, "county", None), "fips", ""))


def _candidates(code):
    code = (code or "").strip().upper()
    base = code.split(" / ")[0].strip()
    yield code
    yield base
    yield re.sub(r"\s+(RUPD|IUPD|PAD|CR)$", "", base)  # an overlay tacked on the district code
    yield re.sub(r"P$", "", base)  # 'IND-3p' style suffix


def _code_of(parcel):
    zone = (parcel.overlays or {}).get("zoning") or {}
    return (parcel.zoning or "").split(" / ")[0].strip() or zone.get("value") or ""


def find(key, code):
    """(data, district) for a code in a jurisdiction's file, or (data, None)."""
    data = load(key) if key else None
    if not data:
        return None, None
    for c in _candidates(code):
        if c in data["by_code"]:
            return data, data["by_code"][c]
    for c in _candidates(code):  # one entry covering several labels, e.g. "ELOY" inside "CASA GRANDE / ELOY / COOLIDGE / ..."
        for entry in data["districts"]:
            if " / " in entry["code"] and c in {part.strip().upper() for part in entry["code"].split(" / ")}:
                return data, entry
    return data, None


def district_for(parcel):
    """{"jurisdiction", "code", "district", "mirror", ...} for a parcel's zoning, or None when nothing is documented."""
    key = jurisdiction_key(parcel)
    code = _code_of(parcel)
    if not key or not code:
        return None
    data, district = find(key, code)
    if not data or not district:
        return None
    return {"key": key, "code": code, "jurisdiction": data["jurisdiction"], "code_name": data.get("code_name", ""),
            "code_url": data.get("code_url", ""), "mirror": data["mirror"], "district": district}


def references(parcels):
    """The zoning districts to print for these parcels: [{... district_for ...}] unique by jurisdiction and code, biggest
    acreage first, plus {"undocumented": [(jurisdiction or county, code)]} and the no-layer notes."""
    seen, out, missing, no_layer = {}, [], [], {}
    for p in sorted(parcels, key=lambda p: -(p.acres or 0)):
        ref = district_for(p)
        key = jurisdiction_key(p)
        code = _code_of(p)
        if ref:
            ident = (ref["key"], ref["district"]["code"])
            if ident not in seen:
                seen[ident] = ref
                out.append(ref)
        elif code and key:
            missing.append((load(key)["jurisdiction"] if load(key) else key, code))
        elif not code and p.county.state in NO_LAYER:
            no_layer[p.county.name + ", " + p.county.state] = NO_LAYER[p.county.state]
    return {"districts": out, "undocumented": sorted(set(missing)), "no_layer": no_layer,
            "caveats": {r["key"]: load(r["key"]).get("caveats", []) for r in out}}
