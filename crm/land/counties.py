"""Counties loaded from their own ArcGIS services rather than a state file: Charles County, MD.

Each config names the tax-parcel layer and the county planning layers stamped onto every parcel at load
(zoning, water/sewer service category, Chesapeake Bay Critical Area, military zones...).
"""
import re

from shapely import STRtree, make_valid
from shapely.geometry import shape

from .layers import to_albers
from .sources import arcgis_query

CHARLES = "https://services7.arcgis.com/3BMWkdyrt45RNCrq/arcgis/rest/services"

COUNTIES = {
    "24017": {
        "fips": "24017", "name": "Charles", "state": "MD", "bbox": (-77.35, 38.25, -76.65, 38.70), "aliases": ["charles", "charles county", "charles md"],
        "source": "Charles County Government GIS (SDAT tax parcels)", "release": "Charles County live service",
        "cad_search_url": ("https://sdat.dat.maryland.gov/RealProperty/Pages/viewdetails.aspx"
                           "?County={c}&SearchType=ACCT&District={d}&AccountNumber={a}"),
        "parcels_url": f"{CHARLES}/Tax_Parcels/FeatureServer/0", "key_field": "ACCTID",
        "overlays": {
            "zoning": {"label": "Zoning", "url": f"{CHARLES}/Zoning/FeatureServer/0",
                       "fields": ["ZONING", "ZONE", "GENZONE"], "value": "ZONING"},
            "water_category": {"label": "Water service category", "url": f"{CHARLES}/Water_and_Sewer_Category_Designations/FeatureServer/1",
                               "fields": ["Category"], "value": "Category"},
            "sewer_category": {"label": "Sewer service category", "url": f"{CHARLES}/Water_and_Sewer_Category_Designations/FeatureServer/0",
                               "fields": ["Category"], "value": "Category"},
            "critical_area": {"label": "Chesapeake Bay Critical Area",
                              "url": f"{CHARLES}/Charles_County_Chesapeake_Bay_Critical_Area_/FeatureServer/2",
                              "fields": ["Designation"], "value": "Designation"},
            "military_zone": {"label": "Military Awareness Zone", "url": f"{CHARLES}/Military_Awareness_Zone/FeatureServer/0",
                              "fields": [], "value": None},
            "navy_noise": {"label": "Navy noise contour", "url": f"{CHARLES}/Navy_Noise_Contours/FeatureServer/0",
                           "fields": ["ZONE"], "value": "ZONE"},
            "pfa": {"label": "Priority Funding Area", "url": f"{CHARLES}/Priority_Funding_Areas/FeatureServer/0",
                    "fields": [], "value": None},
            "tech_corridor": {"label": "Technology Corridor", "url": f"{CHARLES}/TechnologyCorridor_Boundary/FeatureServer/0",
                              "fields": [], "value": None},
            "septic_tier": {"label": "Septic tier", "url": f"{CHARLES}/Septic_Tiers_Adopted_July_12_2016/FeatureServer/0",
                            "fields": ["TIER"], "value": "TIER"},
        },
    },
}

# Maryland SDAT land use -> the Texas-style codes the scorer understands.
MD_LAND_USE = {"A": "D", "I": "F2", "C": "F1", "CC": "F1", "CR": "F1", "RC": "F1", "EC": "X", "E": "X",
               "R": "A", "TH": "A", "U": "A", "M": "B", "CA": "F1", "MA": "E"}

# First three ZIP digits -> state, for owners' mailing addresses (Maryland records have no state field).
_ZIP3 = [
    (5, "NY"), (9, "PR"), (27, "MA"), (29, "RI"), (38, "NH"), (49, "ME"), (54, "VT"), (55, "MA"), (59, "VT"),
    (69, "CT"), (89, "NJ"), (99, "AE"), (149, "NY"), (196, "PA"), (199, "DE"), (200, "DC"), (201, "VA"),
    (205, "DC"), (219, "MD"), (246, "VA"), (268, "WV"), (289, "NC"), (299, "SC"), (319, "GA"), (349, "FL"),
    (369, "AL"), (385, "TN"), (397, "MS"), (399, "GA"), (427, "KY"), (458, "OH"), (479, "IN"), (499, "MI"),
    (528, "IA"), (549, "WI"), (567, "MN"), (569, "DC"), (577, "SD"), (588, "ND"), (599, "MT"), (629, "IL"),
    (658, "MO"), (679, "KS"), (693, "NE"), (714, "LA"), (729, "AR"), (749, "OK"), (799, "TX"), (816, "CO"),
    (831, "WY"), (838, "ID"), (847, "UT"), (865, "AZ"), (884, "NM"), (885, "TX"), (898, "NV"), (961, "CA"),
    (968, "HI"), (979, "OR"), (994, "WA"), (999, "AK"),
]


def zip_state(zipcode):
    digits = re.sub(r"\D", "", zipcode or "")[:3]
    if len(digits) < 3:
        return ""
    n = int(digits)
    return next((st for top, st in _ZIP3 if n <= top), "")


def find(name_or_fips):
    key = str(name_or_fips).strip().lower()
    for cfg in COUNTIES.values():
        if key == cfg["fips"] or key in cfg["aliases"]:
            return cfg
    return None


def names():
    return ", ".join(c["name"] for c in COUNTIES.values())


def record_acres(rec):
    area = rec.get("LANDAREA")
    if not isinstance(area, (int, float)):
        return None
    return area / 43560 if (rec.get("LUOM") or "").strip() == "S" else area


def big_parcel_where(min_acres):
    """Records likely to be min_acres or more (exact acres come from the geometry afterwards)."""
    return (f"(LUOM = 'A' AND LANDAREA >= {min_acres}) OR (LUOM = 'S' AND LANDAREA >= {min_acres * 43560:.0f}) "
            f"OR LUOM = ' ' OR LUOM IS NULL")


def _t(rec, k):
    v = rec.get(k)
    return "" if v is None else str(v).strip()


def fields_from_record(rec):
    """Charles County / SDAT tax parcel record -> Parcel fields."""
    lu = _t(rec, "DESCLU")
    code = (re.search(r"\(([A-Z]+)\)", lu) or [None, "A" if lu.upper().startswith("AGRI") else ""])[1]
    owner2 = _t(rec, "OWNNAME2")
    premises = " ".join(x for x in [_t(rec, "PREMSNUM"), _t(rec, "PREMSDIR"), _t(rec, "PREMSNAM"), _t(rec, "PREMSTYP")] if x)
    legal = " ".join(x for x in [_t(rec, "LEGAL1"), _t(rec, "LEGAL2"), _t(rec, "LEGAL3")] if x)
    return {
        "prop_id": _t(rec, "ACCTID"), "geo_id": "-".join(x for x in [_t(rec, "MAP"), _t(rec, "GRID"), _t(rec, "PARCEL")] if x),
        "owner_name": _t(rec, "OWNNAME1"), "care_of": owner2,
        "mail_address": _t(rec, "ADDRESS"), "mail_city": _t(rec, "CITY"), "mail_zip": _t(rec, "ZIPCODE"),
        "mail_state": zip_state(_t(rec, "ZIPCODE")),
        "situs_address": premises, "situs_city": _t(rec, "PREMCITY"), "situs_zip": _t(rec, "PREMZIP"),
        "legal_desc": legal, "legal_acres": record_acres(rec),
        "state_land_use": MD_LAND_USE.get(code, ""), "local_land_use": code or lu[:20],
        "year_built": "" if _t(rec, "YEARBLT") in ("0000", "0") else _t(rec, "YEARBLT"),
        "land_value": None, "improvement_value": None, "market_value": None, "tax_year": "",
    }


def _valid(g):
    return g if g.is_valid else make_valid(g)


def overlay_index(cfg, log=print):
    """{overlay: (STRtree, [albers geoms], [props])} for each county planning layer."""
    out = {}
    for key, spec in cfg["overlays"].items():
        feats = arcgis_query(spec["url"], fields=spec["fields"] or ["*"], simplify=0.00005, page_size=1000)
        geoms, props = [], []
        for f in feats:
            if not f.get("geometry"):
                continue
            g = shape(f["geometry"])
            geoms.append(_valid(to_albers(g)))
            props.append(f.get("properties") or {})
        out[key] = (STRtree(geoms) if geoms else None, geoms, props, spec)
        log(f"  {spec['label']}: {len(geoms)} areas")
    return out


def stamp(index, geom_wgs84):
    """(zoning text, overlays dict) for one parcel: each layer's dominant value and % of the parcel covered."""
    parcel = _valid(to_albers(geom_wgs84))
    area = parcel.area or 1
    overlays, zoning = {}, ""
    for key, (tree, geoms, props, spec) in index.items():
        if tree is None:
            continue
        shares = {}
        for i in tree.query(parcel, predicate="intersects"):
            try:
                part = geoms[int(i)].intersection(parcel).area / area
            except Exception:  # GEOS topology errors on slivers: fall back to a zero-width buffer
                part = geoms[int(i)].buffer(0).intersection(parcel.buffer(0)).area / area
            value = str(props[int(i)].get(spec["value"]) or "").strip() if spec["value"] else "Yes"
            shares[value] = shares.get(value, 0) + part
        shares = {v: round(100 * s, 1) for v, s in shares.items() if s >= 0.01}
        if not shares:
            continue
        ordered = sorted(shares.items(), key=lambda kv: -kv[1])
        overlays[key] = {"label": spec["label"], "value": ordered[0][0], "pct": round(min(100, sum(shares.values())), 1),
                         "parts": dict(ordered)}
        if key == "zoning":
            top = next((p for i, p in enumerate(props) if str(p.get("ZONING") or "").strip() == ordered[0][0]), {})
            overlays[key]["name"] = (top.get("ZONE") or "").title()
            overlays[key]["general"] = (top.get("GENZONE") or "").title()
            zoning = " / ".join(v for v, _ in ordered[:3])
    return zoning, overlays
