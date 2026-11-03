"""Counties loaded from their own ArcGIS services rather than a state file: Charles County, MD, and the
Huntsville, AL area (Limestone, Madison, Morgan) and Fayetteville, NC (Cumberland and neighbours), Arizona (Maricopa, Pinal) and Greene County, IN.

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
                       "fields": ["ZONING", "ZONE", "GENZONE"], "value": "ZONING",
                       "name_field": "ZONE", "general_field": "GENZONE"},
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


def in_windows(cfg, geom):
    """True when the shape's centre lies in one of the county's load-everything windows."""
    pt = geom.representative_point()
    return any(w[0] <= pt.x <= w[2] and w[1] <= pt.y <= w[3] for w in cfg.get("full_windows") or [])


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
        feats = arcgis_query(spec["url"], where=spec.get("where", "1=1"), fields=spec["fields"] or ["*"],
                             bbox=cfg["bbox"] if spec.get("clip") else None, simplify=0.00005, page_size=1000)
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
            value = spec.get("value_labels", {}).get(value, value)
            shares[value] = shares.get(value, 0) + part
        shares = {v: round(100 * s, 1) for v, s in shares.items() if s >= 0.01}
        if not shares:
            continue
        ordered = sorted(shares.items(), key=lambda kv: -kv[1])
        overlays[key] = {"label": spec["label"], "value": ordered[0][0], "pct": round(min(100, sum(shares.values())), 1),
                         "parts": dict(ordered)}
        if key == "zoning" or spec.get("refines"):
            labels = spec.get("value_labels", {})
            top = next((p for p in props if labels.get(str(p.get(spec["value"]) or "").strip(),
                                                         str(p.get(spec["value"]) or "").strip()) == ordered[0][0]), {})
            names = spec.get("names") or {}
            overlays[key]["name"] = (names.get(ordered[0][0]) or top.get(spec.get("name_field") or "", "") or "").title()
            overlays[key]["general"] = (top.get(spec.get("general_field") or "", "") or "").title()
            if spec.get("authority"):
                overlays[key]["authority"] = spec["authority"]
            zoning = " / ".join(v for v, _ in ordered[:3])
    # A city's own zoning layer replaces the county layer's bare city name ("ELOY") where the parcel is in that city.
    for key, (_, _, _, spec) in index.items():
        parent = spec.get("refines")
        if not parent or key not in overlays:
            continue
        refined = overlays.pop(key)
        if overlays.get(parent, {}).get("value") == spec["when_value"]:
            overlays[parent + "_county"] = overlays[parent]
            overlays[parent] = {**refined, "label": spec["label"]}
    if "zoning" in overlays:
        zoning = " / ".join(list(overlays["zoning"]["parts"])[:3])
    return zoning, overlays


# --- Per-county record hooks (Charles uses the SDAT helpers above) ----------------------------------------------

def owner_field(cfg):
    return cfg.get("owner_field", "OWNNAME1")


def count_fields(cfg):
    return cfg.get("count_fields", [cfg["key_field"], "OWNNAME1", "LANDAREA", "LUOM"])


def acres_of(cfg, rec):
    return (cfg.get("acres_fn") or record_acres)(rec)


def in_county(cfg, where):
    """Scope a where clause to the county when its parcels come from a multi-county (statewide) layer."""
    base = cfg.get("base_where")
    return f"({base}) AND ({where})" if base else where


def count_where(cfg):
    """Rows counted for owner totals: the whole county, or (huge counties) only the parcels big enough to matter."""
    return in_county(cfg, cfg.get("count_where", "1=1"))


def big_where(cfg, min_acres):
    return in_county(cfg, (cfg.get("big_where_fn") or big_parcel_where)(min_acres))


def record_fields(cfg, rec):
    return (cfg.get("fields_fn") or fields_from_record)(rec)


def record_key(cfg, rec):
    """The parcel's stored key. Alabama parcel numbers are kept as digits only ('05 07 36 0 000 001.000' ->
    '0507360000001000') so the three counties' different spellings line up."""
    if cfg.get("key_fn"):
        return cfg["key_fn"](rec)
    raw = _t(rec, cfg["key_field"])
    return re.sub(r"\D", "", raw) if cfg.get("digits_key") else raw


# --- Huntsville area, Alabama ---------------------------------------------------------------------------------
# Parcels: Limestone County via the City of Huntsville's combined parcel service (the county's own public layer
# leaves out the land Huntsville annexed, i.e. the whole Greenbrier / I-565 corridor); Madison County via the City of Madison's copy (it carries acres and
# values); Morgan County via the City of Huntsville's combined parcel service. Zoning: City of Huntsville's
# districts, which also cover the land Huntsville annexed into Limestone (most of the Greenbrier / I-565 PI and
# CIP corridor). Decatur, Athens, Madison and the towns publish no zoning service, and the unincorporated
# county has no zoning ordinance, so those parcels show their town (from city limits) or "No zoning".
HSV = "https://maps.huntsvilleal.gov/server/rest/services"
HUNTSVILLE_ZONES = {
    "PI": "Planned Industrial", "CIP": "Corporate Interstate Park", "AIP": "Airport Industrial Park",
    "IP": "Industrial Park", "LI": "Light Industry", "HI": "Heavy Industry",
    "RP": "Research Park", "RP2": "Research Park 2", "RPA": "Research Park A", "RPC": "Research Park C",
    "RPW": "Research Park West",
    "C1": "Neighborhood Business C-1", "C2": "Commercial C-2", "C3": "Commercial C-3", "C4": "Highway Business C-4",
    "C5": "Commercial C-5", "C6": "Commercial C-6", "O": "Office",
    "PD": "Planned Development", "PDH": "Planned Development Housing",
    "R1": "Residence 1", "R1A": "Residence 1-A", "R1B": "Residence 1-B", "R1C": "Residence 1-C",
    "R2": "Residence 2", "R2A": "Residence 2-A", "R2B": "Residence 2-B", "R2C": "Residence 2-C",
    "RA": "Residence-Agriculture", "RO2": "Residential Office 2",
}
AL_OVERLAYS = {
    "zoning": {"label": "Zoning (City of Huntsville)", "url": f"{HSV}/Zoning/ZoningDistricts/MapServer/5",
               "fields": ["Zoning"], "value": "Zoning", "names": HUNTSVILLE_ZONES, "authority": "City of Huntsville"},
}
AL_ACCESS = "Parcel geometry and tax roll published by the county Revenue Commissioner"


def _mail_state(rec, state_field, zip_field):
    """Two-letter state: the rolls sometimes spell it out ('TEXAS'), so fall back to the ZIP code."""
    st = _t(rec, state_field).upper()
    return st if re.fullmatch(r"[A-Z]{2}", st) else zip_state(_t(rec, zip_field))


def _num_al(rec, k):
    v = rec.get(k)
    return float(v) if isinstance(v, (int, float)) else None


def _al_land_use(assessment_class, exempt=False, improved=False, use_value=False):
    """Alabama assessment class -> the Texas-style codes the scorer understands.
    Class I utility, II commercial / industrial / other, III farm, forest and homes (current-use land = ag)."""
    if exempt:
        return "X"
    c = re.sub(r"\D", "", assessment_class or "")[:1]
    if c == "1":
        return "J"
    if c == "2":
        return "F1"
    if c == "3" or use_value:
        return "E" if improved else "D"
    return ""


def _limestone_fields(rec):
    mail = " ".join(x for x in [_t(rec, "MailAddress1"), _t(rec, "MailAddress2")] if x)
    legal = " ".join(x for x in [_t(rec, "SubDiv1"), ("Lot " + _t(rec, "Lot1")) if _t(rec, "Lot1") else "",
                                 ("Blk " + _t(rec, "Block1")) if _t(rec, "Block1") else ""] if x)
    imp = _num_al(rec, "TotalImpValue")
    return {
        "prop_id": _t(rec, "ParcelNo"), "geo_id": _t(rec, "PID"),
        "owner_name": _t(rec, "OwnerName"), "care_of": _t(rec, "OwnerName2"),
        "mail_address": mail, "mail_city": _t(rec, "MailCity"), "mail_zip": _t(rec, "MailZip"),
        "mail_state": _mail_state(rec, "MailState", "MailZip"),
        "situs_address": _t(rec, "PropertyAddr1"), "situs_city": _t(rec, "PropertyCity"), "situs_zip": _t(rec, "PropertyZip"),
        "legal_desc": legal, "legal_acres": _num_al(rec, "DeededAcreage") or _num_al(rec, "CalcAcreage"),
        "state_land_use": _al_land_use(_t(rec, "AssessmentClass"), improved=bool(imp)),
        "local_land_use": ("Class " + _t(rec, "AssessmentClass")) if _t(rec, "AssessmentClass") else "",
        "year_built": "", "land_value": _num_al(rec, "TotalLandValue"), "improvement_value": imp,
        "market_value": _num_al(rec, "TotalValue"), "tax_year": _t(rec, "RecordYear"),
    }


def _morgan_fields(rec):
    mail = " ".join(x for x in [_t(rec, "MailAdd1"), _t(rec, "MailAdd2")] if x)
    imp = _num_al(rec, "TotalImpValue")
    return {
        "prop_id": _t(rec, "PARCELID"), "geo_id": _t(rec, "PIN"),
        "owner_name": _t(rec, "Owner"), "care_of": _t(rec, "Owner2"),
        "mail_address": mail, "mail_city": _t(rec, "MailCity"), "mail_zip": _t(rec, "MailZip"),
        "mail_state": _mail_state(rec, "MailState", "MailZip"),
        "situs_address": " ".join(x for x in [_t(rec, "PropAddr1"), _t(rec, "PropAddr2")] if x),
        "situs_city": _t(rec, "PropCity"), "situs_zip": _t(rec, "PropZip"),
        "legal_desc": _t(rec, "LegalDescription")[:500], "legal_acres": _num_al(rec, "TotalAcres") or _num_al(rec, "CalcAcres"),
        "state_land_use": _al_land_use(_t(rec, "AssessmentClass"), improved=bool(imp)),
        "local_land_use": ("Class " + _t(rec, "AssessmentClass")) if _t(rec, "AssessmentClass") else "",
        "year_built": "", "land_value": _num_al(rec, "TotalLandValue"), "improvement_value": imp,
        "market_value": _num_al(rec, "TotalValue"), "tax_year": _t(rec, "RecordYear"),
    }


def _madison_fields(rec):
    """City of Madison's copy of the Madison County roll: mailing address is one line 'street, CITY, ST zip'."""
    parts = [x.strip() for x in _t(rec, "MailingAddress").split(",")]
    street = ", ".join(parts[:-2]) if len(parts) >= 3 else (parts[0] if parts else "")
    city = parts[-2] if len(parts) >= 3 else ""
    m = re.match(r"([A-Z]{2})\s+([\d-]+)", parts[-1]) if parts else None
    imp = _num_al(rec, "TotalBuildingValue")
    return {
        "prop_id": _t(rec, "ParcelNum"), "geo_id": _t(rec, "PIN"),
        "owner_name": _t(rec, "PropertyOwner"), "care_of": "" if _t(rec, "AccountOwner") == _t(rec, "PropertyOwner") else _t(rec, "AccountOwner")[:200],
        "mail_address": street, "mail_city": city, "mail_zip": m.group(2) if m else "",
        "mail_state": m.group(1) if m else zip_state(parts[-1] if parts else ""),
        "situs_address": _t(rec, "PropertyAddress"), "situs_city": "", "situs_zip": "",
        "legal_desc": _t(rec, "PropertyDescription").replace("\r\n", " ").replace("\n", " ")[:500],
        "legal_acres": _num_al(rec, "Acres"),
        "state_land_use": _al_land_use("", exempt=bool(_t(rec, "ExemptCode")), improved=bool(imp),
                                       use_value=bool(_num_al(rec, "TotalUseValue"))),
        "local_land_use": _t(rec, "TaxDistrict")[:20],
        "year_built": "", "land_value": _num_al(rec, "TotalLandValue"), "improvement_value": imp,
        "market_value": _num_al(rec, "TotalAppraisedValue"), "tax_year": _t(rec, "TaxYear"),
    }


def _acres_field(field):
    return lambda rec: rec.get(field) if isinstance(rec.get(field), (int, float)) else None


def _acres_where(field):
    return lambda min_acres: f"{field} >= {min_acres:.1f} OR {field} IS NULL"


COUNTIES.update({
    "01083": {
        "fips": "01083", "name": "Limestone", "state": "AL", "bbox": (-87.25, 34.55, -86.75, 35.01),
        "aliases": ["limestone al", "limestone county al", "limestone, al"],
        "source": "Limestone County, AL tax parcels (City of Huntsville combined parcel service)",
        "release": "City of Huntsville live service",
        "parcels_url": f"{HSV}/Boundaries/CombinedParcels/MapServer/7",
        "key_field": "ParcelNo", "digits_key": True, "owner_field": "OwnerName",
        "count_fields": ["ParcelNo", "OwnerName", "CalcAcreage"], "acres_fn": _acres_field("CalcAcreage"),
        "big_where_fn": _acres_where("CalcAcreage"), "fields_fn": _limestone_fields, "overlays": AL_OVERLAYS,
    },
    "01089": {
        "fips": "01089", "name": "Madison", "state": "AL", "bbox": (-86.80, 34.48, -86.25, 35.01),
        "aliases": ["madison al", "madison county al", "madison, al"],
        "source": "Madison County, AL tax parcels (City of Madison GIS copy)", "release": "City of Madison live service",
        "parcels_url": "https://maps.madisonal.gov/server/rest/services/Development_Dashboard_Base_MIL1/MapServer/1",
        "key_field": "ParcelNum", "digits_key": True, "owner_field": "PropertyOwner",
        "count_fields": ["ParcelNum", "PropertyOwner", "Acres"], "acres_fn": _acres_field("Acres"),
        "big_where_fn": _acres_where("Acres"), "fields_fn": _madison_fields, "overlays": AL_OVERLAYS,
    },
    "01103": {
        "fips": "01103", "name": "Morgan", "state": "AL", "bbox": (-87.15, 34.29, -86.55, 34.70),
        "aliases": ["morgan", "morgan al", "morgan county al", "morgan, al"],
        "source": "Morgan County, AL tax parcels (City of Huntsville combined parcel service)",
        "release": "City of Huntsville live service",
        "parcels_url": f"{HSV}/Boundaries/CombinedParcels/MapServer/10",
        "key_field": "PID", "digits_key": True, "owner_field": "Owner",
        "count_fields": ["PID", "Owner", "CalcAcres"], "acres_fn": _acres_field("CalcAcres"),
        "big_where_fn": _acres_where("CalcAcres"), "fields_fn": _morgan_fields, "overlays": AL_OVERLAYS,
    },
})


# --- Fayetteville, North Carolina (Cumberland, plus Hoke, Harnett and Moore around Fort Liberty / Bragg) -----------------------
# Parcels: NC OneMap's statewide parcel layer (the county assessor's data, standardised), filtered to Cumberland.
# Its `gisacres` field is unusable (square degrees), so acres come from the recorded acreage, with the layer's
# own shape area (state-plane square feet) as the fallback. Parcels with no PIN are keyed by their object id.
# No public zoning service has been found yet, so Cumberland parcels carry no zoning overlay (city limits only).
NC_PARCELS = "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/FeatureServer/1"


def _nc_acres(rec):
    rec_acres = rec.get("recareano")
    if isinstance(rec_acres, (int, float)) and rec_acres > 0:
        return float(rec_acres)
    area = rec.get("Shape__Area")
    return area / 43560 if isinstance(area, (int, float)) and area > 0 else None


def _nc_land_use(desc, owner=""):
    """Cumberland use descriptions ('R101-RES', 'C353-SHOP/RE', 'AG...') -> the Texas-style codes the scorer uses."""
    d = (desc or "").strip().upper()
    if not d:
        return ""
    if re.search(r"EXEMPT|GOVERN|CHURCH|SCHOOL|MUNICIPAL|COUNTY|STATE-OWN|FEDERAL", d):
        return "X"
    return {"R": "A", "C": "F1", "I": "F2", "A": "D", "F": "D", "T": "D", "V": "D", "M": "B"}.get(d[:1], "")


def _nc_fields(rec):
    imp, land, total = _num_al(rec, "improvval"), _num_al(rec, "landval"), _num_al(rec, "parval")
    mail = " ".join(x for x in [_t(rec, "mailadd"), _t(rec, "munit")] if x)
    return {
        "prop_id": _t(rec, "parno"), "geo_id": _t(rec, "altparno"),
        "owner_name": _t(rec, "ownname"), "care_of": _t(rec, "ownname2"),
        "mail_address": mail, "mail_city": _t(rec, "mcity"), "mail_zip": _t(rec, "mzip"),
        "mail_state": _mail_state(rec, "mstate", "mzip"),
        "situs_address": _t(rec, "siteadd"), "situs_city": _t(rec, "scity"), "situs_zip": _t(rec, "szip"),
        "legal_desc": _t(rec, "legdecfull")[:500], "legal_acres": _nc_acres(rec),
        "state_land_use": _nc_land_use(_t(rec, "parusedesc")), "local_land_use": _t(rec, "parusedesc")[:20],
        "year_built": "" if not _t(rec, "structyear") or _t(rec, "structyear") == "0" else _t(rec, "structyear"),
        "land_value": land, "improvement_value": imp, "market_value": total, "tax_year": "",
    }


def _nc_key(rec):
    return _t(rec, "parno") or (f"OID{rec['objectid']}" if rec.get("objectid") else "")


def _nc_county(fips, name, bbox, aliases):
    return {
        "fips": fips, "name": name, "state": "NC", "bbox": bbox, "aliases": aliases,
        "source": f"{name} County, NC tax parcels (NC OneMap statewide parcels)", "release": "NC OneMap live service",
        "parcels_url": NC_PARCELS, "base_where": f"cntyname = '{name}'",
        "key_field": "parno", "key_fn": _nc_key, "owner_field": "ownname",
        "count_fields": ["parno", "ownname", "recareano", "Shape__Area"], "acres_fn": _nc_acres,
        "big_where_fn": lambda a: f"recareano >= {a:.1f} OR recareano IS NULL OR recareano = 0",
        "fields_fn": _nc_fields, "overlays": {},
    }


for _args in [
    ("37051", "Cumberland", (-79.1, 34.8, -78.4, 35.4), ["fayetteville", "fayetteville nc"]),
    ("37093", "Hoke", (-79.4, 34.8, -78.9, 35.25), []),
    ("37085", "Harnett", (-79.1, 35.15, -78.5, 35.6), []),
    ("37125", "Moore", (-79.8, 35.0, -79.25, 35.5), []),
]:
    COUNTIES[_args[0]] = _nc_county(*_args[:3], aliases=[f"{_args[1].lower()} nc", f"{_args[1].lower()} county nc",
                                                          f"{_args[1].lower()}, nc"] + _args[3])


# Hoke County publishes its own tax-parcel service with owners and mailing addresses (NC OneMap's Hoke records have
# those blank), so Hoke is loaded from it. Its TWN_PIN is the same 12-digit parcel number NC OneMap uses.
HOKE_PARCELS = "https://maps.hokecounty.org/server/rest/services/Parcels/MapServer/0"


def _hoke_acres(rec):
    for k in ("TOTAL_ACRE", "CAL_ACRES"):
        v = rec.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _hoke_fields(rec):
    return {
        "prop_id": _t(rec, "TWN_PIN"), "geo_id": _t(rec, "PPN_2").replace(".0", ""),
        "owner_name": _t(rec, "OWNER_NAME"), "care_of": _t(rec, "OWNER_NA_1"),
        "mail_address": " ".join(x for x in [_t(rec, "MAILING_AD"), _t(rec, "MAILING__1")] if x),
        "mail_city": _t(rec, "CITY"), "mail_state": _mail_state(rec, "STATE_OR_C", "ZIP_CODE"), "mail_zip": _t(rec, "ZIP_CODE"),
        "situs_address": " ".join(x for x in [_t(rec, "STREET_NUM"), _t(rec, "STREET_NAM")] if x) or _t(rec, "PHY_ADDRES"),
        "situs_city": "", "situs_zip": "",
        "legal_desc": _t(rec, "NOTE_1")[:500], "legal_acres": _hoke_acres(rec),
        "state_land_use": "", "local_land_use": "",
        "year_built": "", "land_value": None, "improvement_value": None, "market_value": _num_al(rec, "COST_TOTAL"),
        "tax_year": "",
    }


COUNTIES["37093"] = dict(
    COUNTIES["37093"], source="Hoke County, NC tax parcels (county GIS)", release="Hoke County live service",
    parcels_url=HOKE_PARCELS, base_where="1=1", key_field="TWN_PIN", key_fn=None, owner_field="OWNER_NAME",
    count_fields=["TWN_PIN", "OWNER_NAME", "TOTAL_ACRE", "CAL_ACRES"], acres_fn=_hoke_acres,
    big_where_fn=lambda a: f"TOTAL_ACRE >= {a:.1f} OR CAL_ACRES >= {a:.1f} OR TOTAL_ACRE IS NULL OR TOTAL_ACRE = 0",
    fields_fn=_hoke_fields,
)


# --- Arizona I-10 corridor: Maricopa (Tonopah / Wintersburg Rd) and Pinal (Casa Grande, Eloy) -----------------------
# Maricopa: the County Assessor's own parcel service (1.76M parcels, so owner totals are counted over parcels of an
# acre or more only, and `total_parcels` is that count). Pinal: the county's own TaxParcels service (gis.pinal.gov;
# the older host gismaps.pinalcountyaz.gov is dead) plus its county zoning layer.
AZ_MARICOPA = "https://gis.mcassessor.maricopa.gov/arcgis/rest/services/Parcels/MapServer/0"
SQFT_ACRE = 43560


def _az_key(field):
    return lambda rec: "".join(ch for ch in _t(rec, field).upper() if ch.isalnum())


def _pinal_key(rec):
    """Parcel number, or NAP<objectid> for the assessor's placeholder 'NAP' (land with no parcel number assigned yet:
    new splits, state / federal / tribal land, rights-of-way). Those keys are not stable if the county republishes."""
    key = "".join(ch for ch in _t(rec, "PARCELID").upper() if ch.isalnum())
    return key if key not in ("", "NAP") else (f"NAP{rec['OBJECTID']}" if rec.get("OBJECTID") is not None else "")


def _money(rec, k):
    digits = re.sub(r"[^\d.]", "", _t(rec, k))
    return float(digits) if digits else None


def _az_land_use(puc):
    """Maricopa property use code -> the Texas-style codes the scorer uses (0000s vacant / farm, 01xx-09xx homes,
    5xxx utilities, 9xxx exempt; the rest is commercial / industrial / other). Only the first digits are confirmed."""
    puc = (puc or "").strip()
    if not puc:
        return ""
    if puc.startswith("00"):
        return "D"
    if puc[0] == "0":
        return "A"
    return {"5": "J", "9": "X"}.get(puc[0], "F1")


def _maricopa_acres(rec):
    size = rec.get("LAND_SIZE")
    return size / SQFT_ACRE if isinstance(size, (int, float)) and size > 0 else None


def _maricopa_fields(rec):
    street = " ".join(x for x in [_t(rec, "PHYSICAL_STREET_NUM"), _t(rec, "PHYSICAL_STREET_DIR"),
                                  _t(rec, "PHYSICAL_STREET_NAME"), _t(rec, "PHYSICAL_STREET_TYPE")] if x)
    legal = " ".join(x for x in [_t(rec, "SUBNAME"), ("Lot " + _t(rec, "LOT_NUM")) if _t(rec, "LOT_NUM") else ""] if x)
    year = _t(rec, "CONST_YEAR")
    return {
        "prop_id": _t(rec, "APN_DASH") or _t(rec, "APN"), "geo_id": _t(rec, "APN"),
        "owner_name": _t(rec, "OWNER_NAME"), "care_of": _t(rec, "INCAREOF"),
        "mail_address": " ".join(x for x in [_t(rec, "MAIL_ADDR1"), _t(rec, "MAIL_ADDR2")] if x),
        "mail_city": _t(rec, "MAIL_CITY"), "mail_zip": _t(rec, "MAIL_ZIP"),
        "mail_state": _mail_state(rec, "MAIL_STATE", "MAIL_ZIP"),
        "situs_address": street, "situs_city": _t(rec, "PHYSICAL_CITY"), "situs_zip": _t(rec, "PHYSICAL_ZIP"),
        "legal_desc": legal[:500], "legal_acres": _maricopa_acres(rec),
        "state_land_use": _az_land_use(_t(rec, "PUC")),
        "local_land_use": ("PUC " + _t(rec, "PUC")) if _t(rec, "PUC") else "",
        "year_built": "" if year in ("", "0", "0000") else year,
        "land_value": None, "improvement_value": None, "market_value": _money(rec, "FCV_CUR"),
        "tax_year": _t(rec, "TAX_YR_CUR"),
    }


# Areas (west, south, east, north) where every parcel is loaded whatever its size, so a site can be assembled from
# small neighbouring lots and unassigned ("NAP") tracts. Everywhere else the 20-acre cutoff applies (loading all of
# Maricopa's 1.76M parcels is not practical). Add a window here, then re-run load_parcels for the county.
PINAL_FULL_WINDOWS = [
    (-111.80, 32.72, -111.52, 32.95),  # I-10 / I-8 interchange, Casa Grande (Barnes Farm) down to Eloy (Petersen Park)
]
MARICOPA_FULL_WINDOWS = [
    (-113.02, 33.36, -112.84, 33.50),  # Wintersburg Rd & I-10, Tonopah
]
PINAL_GIS = "https://gis.pinal.gov/mapping/rest/services"
PINAL_ZONES = {  # Zoning_2010 layer coded values (codes 51-62 mean "zoned by that city or town")
    1: "CR-1A", 2: "CR-1", 3: "CR-2", 4: "CR-3", 5: "CR-4", 6: "CR-5", 7: "SR", 8: "SH", 9: "CAR", 10: "GR", 11: "MH",
    12: "RV", 13: "MHP", 14: "RVP", 15: "TR", 16: "CB-1", 17: "CB-2", 18: "CI-B", 19: "CI-1", 20: "CI-2",
    21: "Undesignated", 22: "Multiple", 23: "RU-10", 24: "RU-5", 25: "RU-3.3", 26: "RU-2", 27: "RU-1.25", 28: "RU-C",
    29: "R-43", 30: "R-35", 31: "R-20", 32: "R-12", 33: "R-9", 34: "R-7", 35: "MD", 36: "MR", 37: "AC-1", 38: "AC-2",
    39: "AC-3", 40: "O-1", 41: "O-2", 42: "C-1", 43: "C-2", 44: "C-3", 45: "I-1", 46: "I-2", 47: "I-3", 48: "MH-8",
    49: "MH-435", 50: "PM/RV", 51: "APACHE JUNCTION", 52: "CASA GRANDE", 53: "COOLIDGE", 54: "ELOY", 55: "FLORENCE",
    56: "KEARNY", 57: "MAMMOTH", 58: "MARANA", 59: "MARICOPA", 60: "QUEEN CREEK", 61: "SUPERIOR", 62: "WINKELMAN",
    63: "MPCMP", 64: "L-MPC",
}
CASA_GRANDE_ZONING = "https://rogue.casagrandeaz.gov/arcgis/rest/services/Planning/Zoning/MapServer/0"
ELOY_ZONING = "https://services7.arcgis.com/VfOwt1fngp5cVBlk/arcgis/rest/services/EloyZoning_view_Public/FeatureServer/0"
PINAL_OVERLAYS = {
    "zoning": {"label": "Zoning (Pinal County)", "url": f"{PINAL_GIS}/Zoning_2010/MapServer/0", "fields": ["ZONING"],
               "value": "ZONING", "value_labels": {str(k): v for k, v in PINAL_ZONES.items()},
               "authority": "Pinal County (city names = zoned by that city)"},
    # Land the county layer hands to a city is zoned by that city: use the city's own layer there.
    "zoning_casa_grande": {"label": "Zoning (City of Casa Grande)", "url": CASA_GRANDE_ZONING, "fields": ["Zoning"],
                           "value": "Zoning", "authority": "City of Casa Grande", "refines": "zoning",
                           "when_value": "CASA GRANDE"},
    "zoning_eloy": {"label": "Zoning (City of Eloy)", "url": ELOY_ZONING, "fields": ["Zoning", "ShortDescription"],
                    "value": "Zoning", "name_field": "ShortDescription", "authority": "City of Eloy",
                    "refines": "zoning", "when_value": "ELOY"},
}
MARICOPA_PLANNET = "https://gis.maricopa.gov/arcgis/rest/services/PND/PlanNet/MapServer"
MARICOPA_OVERLAYS = {
    "zoning": {"label": "Zoning (Maricopa County)", "url": f"{MARICOPA_PLANNET}/11", "fields": ["ZONE", "JURIS"],
               "value": "ZONE", "general_field": "JURIS", "authority": "Maricopa County (unincorporated land only)",
               "clip": True},
}


def _pinal_acres(rec):
    for k in ("GROSSAC", "STATEDAREA"):
        v = rec.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    sf = rec.get("LANDSF")
    return sf / SQFT_ACRE if isinstance(sf, (int, float)) and sf > 0 else None


def _pinal_land_use(rec):
    t = f"{_t(rec, 'CLASSDSCRP')} {_t(rec, 'USEDSCRP')}".upper()
    if "GOVERN" in t or "NON-PROFIT" in t and "VACANT" not in t:
        return "X"
    if "AG LAND" in t or "AGRIC" in t:
        return "D"
    if "VACANT" in t:
        return "F1"
    if "MANUFACT" in t or "INDUSTR" in t or "FOREIGN TRADE" in t:
        return "F2"
    if "COMMERCIAL" in t or "SHOPPING" in t or "GOLF" in t:
        return "F1"
    if "RESID" in t or "RENTAL" in t or "LIHTC" in t:
        return "A"
    return ""


def _pinal_fields(rec):
    """Pinal County assessor roll (the county's own TaxParcels service)."""
    year = _t(rec, "RESYRBLT").replace(".0", "")
    return {
        "prop_id": "" if _t(rec, "PARCELID") == "NAP" else _t(rec, "PARCELID"), "geo_id": _t(rec, "PARCELID"),
        "owner_name": _t(rec, "OWNERNME1"), "care_of": _t(rec, "OWNERNME2"),
        "mail_address": _t(rec, "PSTLADDRESS"), "mail_city": _t(rec, "PSTLCITY"),
        "mail_state": _mail_state(rec, "PSTLSTATE", "PSTLZIP5"), "mail_zip": _t(rec, "PSTLZIP5"),
        "situs_address": _t(rec, "SITEADDRESS"), "situs_city": "", "situs_zip": "",
        "legal_desc": _t(rec, "PRPRTYDSCRP")[:500], "legal_acres": _pinal_acres(rec),
        "state_land_use": _pinal_land_use(rec), "local_land_use": (_t(rec, "USEDSCRP") or _t(rec, "CLASSDSCRP"))[:20],
        "year_built": "" if year in ("", "0") else year,
        "land_value": _num_al(rec, "LNDVALUE"), "improvement_value": None, "market_value": None, "tax_year": "",
    }


COUNTIES.update({
    "04013": {
        "fips": "04013", "name": "Maricopa", "state": "AZ", "bbox": (-113.35, 32.5, -111.0, 34.05),
        "aliases": ["maricopa", "maricopa az", "maricopa county az", "maricopa, az", "tonopah"],
        "source": "Maricopa County Assessor parcels", "release": "Maricopa County Assessor live service",
        "parcels_url": AZ_MARICOPA, "key_field": "APN", "key_fn": _az_key("APN"), "owner_field": "OWNER_NAME",
        "count_where": f"LAND_SIZE >= {SQFT_ACRE}",
        "count_fields": ["APN", "OWNER_NAME", "LAND_SIZE"], "acres_fn": _maricopa_acres,
        "big_where_fn": lambda a: f"LAND_SIZE >= {a * SQFT_ACRE:.0f}", "fields_fn": _maricopa_fields,
        "overlays": MARICOPA_OVERLAYS, "full_windows": MARICOPA_FULL_WINDOWS,
    },
    "04021": {
        "fips": "04021", "name": "Pinal", "state": "AZ", "bbox": (-112.9, 32.35, -110.4, 33.5),
        "aliases": ["pinal", "pinal az", "pinal county az", "pinal, az", "casa grande", "eloy"],
        "source": "Pinal County, AZ assessor parcels (county GIS)", "release": "Pinal County live service",
        "parcels_url": f"{PINAL_GIS}/TaxParcels/MapServer/3", "key_field": "PARCELID", "key_fn": _pinal_key,
        "full_windows": PINAL_FULL_WINDOWS,
        "owner_field": "OWNERNME1", "count_where": "GROSSAC > 0",
        "count_fields": ["PARCELID", "OWNERNME1", "GROSSAC", "STATEDAREA", "LANDSF"], "acres_fn": _pinal_acres,
        "big_where_fn": lambda a: (f"GROSSAC >= {a:.1f} OR STATEDAREA >= {a:.1f} OR LANDSF >= {a * SQFT_ACRE:.0f}"),
        "fields_fn": _pinal_fields, "overlays": PINAL_OVERLAYS,
    },
})


# --- Greene County, Indiana (NSWC Crane) -----------------------------------------------------------------------
# Parcels: IndianaMap's statewide parcel layer (IGIO, from the county's own GIS), filtered by county FIPS. It carries
# no owner names, acreage or values (Indiana publishes those only through each county's own viewer, here WTHGIS), so
# acres come from the geometry and owner / value fields stay blank until a county source is found.
# The layer's SHAPE__Area is in square degrees: ~9.4e9 m2 per square degree at Crane's latitude (38.9 N).
IN_PARCELS = "https://gisdata.in.gov/server/rest/services/Hosted/Parcel_Boundaries_of_Indiana_Current/FeatureServer/0"
IN_ACRES_PER_SQDEG = 9.4e9 / 4046.86


def _in_acres(rec):
    area = rec.get("SHAPE__Area")
    return area * IN_ACRES_PER_SQDEG if isinstance(area, (int, float)) and area > 0 else None


def _in_land_use(code):
    """Indiana DLGF property class -> the Texas-style codes the scorer uses (1xx agricultural, 2xx mineral,
    3xx industrial, 4xx residential, 5xx commercial, 6xx exempt, 8xx utility). Only the hundreds digit is used."""
    c = (code or "").strip()
    return {"1": "D", "2": "F2", "3": "F2", "4": "A", "5": "F1", "6": "X", "8": "J"}.get(c[:1], "")


def _in_fields(rec):
    code = _t(rec, "dlgf_prop_class_code")
    return {
        "prop_id": _t(rec, "parcel_id"), "geo_id": _t(rec, "local_id"),
        "owner_name": "", "care_of": "", "mail_address": "", "mail_city": "", "mail_zip": "", "mail_state": "",
        "situs_address": _t(rec, "prop_add") or _t(rec, "dlgf_prop_address"),
        "situs_city": _t(rec, "prop_city") or _t(rec, "dlgf_prop_address_city") or _t(rec, "esri_poname"),
        "situs_zip": _t(rec, "prop_zip") or _t(rec, "dlgf_prop_address_zip") or _t(rec, "esri_zip"),
        "legal_desc": "", "legal_acres": None,
        "state_land_use": _in_land_use(code), "local_land_use": ("Class " + code) if code else "",
        "year_built": "", "land_value": None, "improvement_value": None, "market_value": None, "tax_year": "",
    }


COUNTIES["18055"] = {
    "fips": "18055", "name": "Greene", "state": "IN", "bbox": (-87.4, 38.6, -86.6, 39.2),
    "aliases": ["greene in", "greene county in", "greene, in", "greene indiana", "greene county indiana", "crane"],
    "source": "Greene County, IN parcels (IndianaMap statewide parcels; no owner data)",
    "release": "IndianaMap live service",
    "parcels_url": IN_PARCELS, "base_where": "county_fips = '18055'",
    "key_field": "parcel_id", "owner_field": "tax_special",  # no owner field: a column that is always empty
    "count_fields": ["parcel_id", "tax_special", "SHAPE__Area"], "acres_fn": _in_acres,
    "big_where_fn": lambda a: f"SHAPE__Area >= {a / IN_ACRES_PER_SQDEG:.9f}", "fields_fn": _in_fields, "overlays": {},
}
