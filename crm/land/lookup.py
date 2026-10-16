"""Live checks for one parcel, pulled from the source agencies on demand (nothing to keep refreshed).

flood (FEMA NFHL), wetlands (USFWS NWI), water and sewer providers + mapped lines (PUC Texas CCN),
electric provider (PUC Texas: investor-owned, co-op, municipal), HUBZone (SBA) and EPA cleanup sites
within a mile. Results are cached on Parcel.checks; `run_checks` re-runs them all.

Also `identify()`: what the state's parcel map has at a point, for parcels not stored locally (any size).
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor

from django.utils import timezone
from shapely import make_valid
from shapely.geometry import Point, mapping, shape
from shapely.ops import unary_union

from . import score
from .layers import METERS_PER_MILE, to_albers
from .sources import (ELECTRIC_AREAS, ELECTRIC_NATIONAL, EPA_CLEANUPS, FEMA_FLOOD, NWI_WETLANDS, SBA_HUBZONE, SESSION,
                      SEWER_CCN, SEWER_LINES, TX_PARCELS_MAPSERVER, WATER_CCN, WATER_LINES, arcgis_query)

SQ_M_PER_ACRE = 4046.8564224


def esri_polygon(geom, tolerance=0.0001):
    """Shapely polygon -> Esri JSON geometry string (simplified so the request stays small)."""
    geom = geom.simplify(tolerance, preserve_topology=True)
    polys = getattr(geom, "geoms", [geom])
    rings = []
    for poly in polys:
        if poly.geom_type != "Polygon":
            continue
        rings.append([list(c) for c in poly.exterior.coords][::-1])  # Esri wants clockwise outer rings
        rings.extend([list(c) for c in ring.coords] for ring in poly.interiors)
    return json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}})


def _shapes(features):
    out = []
    for f in features:
        if f.get("geometry"):
            g = shape(f["geometry"])
            out.append((g if g.is_valid else make_valid(g), f.get("properties") or {}))
    return out


def _safe_intersection(a, b):
    try:
        return a.intersection(b)
    except Exception:  # GEOS topology error on slivers
        return make_valid(a).buffer(0).intersection(make_valid(b).buffer(0))


def _pct(parcel_a, pieces):
    """Percent of the parcel (Albers) covered by the union of WGS84 pieces, and the covered geometry."""
    if not pieces:
        return 0.0, None
    covered = _safe_intersection(unary_union([make_valid(to_albers(g)) for g in pieces]), parcel_a)
    return round(100 * covered.area / parcel_a.area, 1) if parcel_a.area else 0.0, covered


def check_flood(geom, parcel_a):
    feats = _shapes(arcgis_query(FEMA_FLOOD, fields=["FLD_ZONE", "ZONE_SUBTY", "SFHA_TF"],
                                 geometry=esri_polygon(geom), geometry_type="esriGeometryPolygon"))
    sfha = [g for g, p in feats if p.get("SFHA_TF") == "T"]
    floodway = [g for g, p in feats if "FLOODWAY" in (p.get("ZONE_SUBTY") or "").upper()]
    zone_500 = [g for g, p in feats if "0.2 PCT" in (p.get("ZONE_SUBTY") or "").upper()]
    sfha_pct, sfha_geom = _pct(parcel_a, sfha)
    zones = sorted({p.get("FLD_ZONE") for _, p in feats if p.get("FLD_ZONE")})
    return {
        "sfha_pct": sfha_pct, "floodway_pct": _pct(parcel_a, floodway)[0], "x500_pct": _pct(parcel_a, zone_500)[0],
        "zones": zones, "mapped": bool(feats),
    }, sfha_geom


# Large water bodies are left out: only streams, ponds and freshwater wetlands count against a site.
EXCLUDED_WETLANDS = ("Lake", "Estuarine and Marine Deepwater", "Estuarine and Marine Wetland")
WETLANDS_WHERE = "WETLAND_TYPE NOT IN ({})".format(", ".join(f"'{t}'" for t in EXCLUDED_WETLANDS))


def check_wetlands(geom, parcel_a):
    # NWI's server fails on detailed polygons, so ask for the bounding box and clip here.
    feats = _shapes(arcgis_query(NWI_WETLANDS, where=WETLANDS_WHERE, fields=["WETLAND_TYPE", "ATTRIBUTE"], bbox=geom.bounds, simplify=0.00005,
                                 page_size=500))
    pct, covered = _pct(parcel_a, [g for g, _ in feats])
    by_type = {}
    for g, p in feats:
        t = p.get("WETLAND_TYPE") or "Wetland"
        part = _safe_intersection(to_albers(g), parcel_a).area / SQ_M_PER_ACRE
        by_type[t] = round(by_type.get(t, 0) + part, 1)
    return {"pct": pct, "types": {k: v for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]) if v >= 0.1}}, covered


def _name(value):
    """Blank for the placeholders some layers use instead of a name."""
    value = (value or "").strip()
    return "" if value.upper() in ("NA", "N/A", "NOT AVAILABLE", "NONE", "NULL", "UNKNOWN") else value


def _ccn(url, geom, parcel_a):
    feats = _shapes(arcgis_query(url, fields=["UTILITY", "DBA_NAME", "CCN_NO", "CCN_TYPE", "STATUS"],
                                 geometry=esri_polygon(geom), geometry_type="esriGeometryPolygon"))
    rows = {}
    for g, p in feats:
        name = _name(p.get("DBA_NAME")) or _name(p.get("UTILITY")) or "Unnamed utility"
        pct = round(100 * to_albers(g).intersection(parcel_a).area / parcel_a.area, 1) if parcel_a.area else 0
        row = rows.setdefault(name, {"name": name, "ccn": p.get("CCN_NO"), "type": p.get("CCN_TYPE"), "pct": 0})
        row["pct"] = min(100, round(row["pct"] + pct, 1))
    rows = sorted(rows.values(), key=lambda r: -r["pct"])
    return {"providers": [r["name"] for r in rows], "details": rows}


def _lines(url, geom, parcel_a, miles=1):
    feats = _shapes(arcgis_query(url, fields=["UTILITY", "DBA_NAME", "FACILITY"], geometry=esri_polygon(geom),
                                 geometry_type="esriGeometryPolygon", distance=miles, max_records=500))
    best = None
    for g, p in feats:
        d = to_albers(g).distance(parcel_a) / METERS_PER_MILE
        if best is None or d < best["miles"]:
            best = {"miles": round(d, 2), "name": _name(p.get("DBA_NAME")) or _name(p.get("UTILITY")),
                    "facility": p.get("FACILITY")}
    return best


def check_water(geom, parcel_a):
    out = _ccn(WATER_CCN, geom, parcel_a)
    out["nearest_line"] = _lines(WATER_LINES, geom, parcel_a)
    return out, None


def check_sewer(geom, parcel_a):
    out = _ccn(SEWER_CCN, geom, parcel_a)
    out["nearest_line"] = _lines(SEWER_LINES, geom, parcel_a)
    return out, None


def _point(geom):
    pt = geom.representative_point()
    return json.dumps({"x": pt.x, "y": pt.y, "spatialReference": {"wkid": 4326}})


def check_electric_national(geom, parcel_a):
    providers = []
    for f in arcgis_query(ELECTRIC_NATIONAL, fields=["NAME", "TYPE", "TELEPHONE", "WEBSITE", "CNTRL_AREA"],
                          geometry=_point(geom), geometry_type="esriGeometryPoint", return_geometry=False):
        p = f.get("properties") or {}
        clean = lambda v: "" if (v or "").upper() == "NOT AVAILABLE" else v  # noqa: E731
        providers.append({"name": (p.get("NAME") or "").title(), "kind": (p.get("TYPE") or "").capitalize(),
                          "phone": clean(p.get("TELEPHONE")), "website": clean(p.get("WEBSITE")),
                          "grid": p.get("CNTRL_AREA")})
    return {"providers": providers, "note": "Federal (HIFLD) territories overlap at the edges: confirm with the utility."}, None


def check_electric(geom, parcel_a):
    point = _point(geom)
    providers = []
    for kind, url in ELECTRIC_AREAS.items():
        for f in arcgis_query(url, fields=["COMPANY_NAME", "COMPANY_WEBSITE", "MAIN_PHONE",
                                           "INDEPENDENT_SYSTEM_OPERATOR_REGIONAL_TRANSMISSION_ORGANIZATION"],
                              geometry=point, geometry_type="esriGeometryPoint", return_geometry=False):
            p = f.get("properties") or {}
            providers.append({
                "name": p.get("COMPANY_NAME"), "kind": kind, "phone": p.get("MAIN_PHONE"),
                "website": p.get("COMPANY_WEBSITE"),
                "grid": p.get("INDEPENDENT_SYSTEM_OPERATOR_REGIONAL_TRANSMISSION_ORGANIZATION"),
            })
    return {"providers": providers}, None


def check_hubzone(geom, parcel_a):
    pt = geom.representative_point()
    resp = SESSION.get(SBA_HUBZONE, params={"latlng": f"{pt.y:.6f},{pt.x:.6f}"}, timeout=30, headers={
        "Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest"})
    resp.raise_for_status()
    m = re.search(r'id=\\?"hubzone-status\\?"[^>]*aria-label=\\?"([^"\\]+)', resp.text)
    status = m.group(1) if m else ""
    if not status:
        raise RuntimeError("SBA map did not return a status")
    return {"status": status, "qualified": status.lower().startswith(("qualified", "redesignated"))}, None


def check_epa(geom, parcel_a):
    feats = arcgis_query(EPA_CLEANUPS, fields=["PRIMARY_NAME", "MAP_SYMBOL_CODE", "SF_NPL_CODE", "FD_SF_NPL_IND",
                                               "LATITUDE", "LONGITUDE", "URL", "CITY_NAME"],
                         geometry=esri_polygon(geom), geometry_type="esriGeometryPolygon", distance=1,
                         return_geometry=False, max_records=50)
    kinds = {"S": "Superfund", "B": "Brownfield", "R": "RCRA cleanup", "F": "Federal facility", "U": "Leaking tank"}
    sites = []
    for f in feats:
        p = f.get("properties") or {}
        if p.get("LATITUDE") is None:
            continue
        d = to_albers(Point(p["LONGITUDE"], p["LATITUDE"])).distance(parcel_a) / METERS_PER_MILE
        sites.append({
            "name": (p.get("PRIMARY_NAME") or "").strip() or "Unnamed site", "miles": round(d, 2),
            "kind": kinds.get(p.get("MAP_SYMBOL_CODE"), "Cleanup site"),
            "npl": bool(p.get("SF_NPL_CODE") or p.get("FD_SF_NPL_IND") == "Y"), "url": p.get("URL") or "",
        })
    return {"sites": sorted(sites, key=lambda s: s["miles"])}, None


CHECKS = {
    "flood": check_flood, "wetlands": check_wetlands, "water": check_water, "sewer": check_sewer,
    "electric": check_electric, "hubzone": check_hubzone, "epa": check_epa,
}
# Texas-only sources (PUC Texas). Elsewhere, electric comes from HIFLD and water/sewer from county layers.
TEXAS_ONLY = ("water", "sewer")


def checks_for(state):
    if state == "TX":
        return dict(CHECKS)
    out = {k: v for k, v in CHECKS.items() if k not in TEXAS_ONLY}
    out["electric"] = check_electric_national
    return out


def run_checks(parcel, only=None):
    """Run the live checks (in parallel), store them on the parcel, re-score, save. Returns parcel.checks."""
    geom = shape(json.loads(parcel.geometry))
    parcel_a = to_albers(geom)
    available = checks_for(parcel.county.state)
    names = [n for n in (only or available) if n in available]
    results, covered = dict(parcel.checks or {}), {}

    def run(name):
        try:
            data, geom_hit = available[name](geom, parcel_a)
            return name, data, geom_hit
        except Exception as exc:  # one agency being down shouldn't sink the rest
            return name, {"error": str(exc)[:200]}, None

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        for name, data, geom_hit in pool.map(run, names):
            results[name] = data
            if geom_hit is not None:
                covered[name] = geom_hit
    # Net usable = gross minus the 100-year floodplain and mapped wetlands (not easements or QD arcs).
    if "error" not in results.get("flood", {}) and "error" not in results.get("wetlands", {}):
        lost = [g for g in covered.values() if g is not None and not g.is_empty]
        lost_area = unary_union(lost).area if lost else 0
        share = lost_area / parcel_a.area if parcel_a.area else 0
        results["net_usable"] = {"acres": round(parcel.acres * (1 - share), 1), "lost_pct": round(100 * share, 1)}
    parcel.checks = results
    parcel.checked_at = timezone.now()
    score.apply(parcel)
    parcel.save(update_fields=["checks", "checked_at", "score", "score_reasons", "killers", "zoning_class", "updated_at"])
    return results


def identify(lat, lon):
    """The parcel at a point, any size: {"county": {fips, name, state}, "fields": Parcel fields, "geometry"}.

    Texas: the state's StratMap map service (identify works there; searching doesn't).
    Charles County, MD: the county's tax-parcel service.
    """
    from . import counties

    for cfg in counties.COUNTIES.values():
        west, south, east, north = cfg["bbox"]
        if not (west <= lon <= east and south <= lat <= north):
            continue
        feats = arcgis_query(cfg["parcels_url"], fields=["*"], geometry=json.dumps(
            {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}), geometry_type="esriGeometryPoint",
            max_records=1)
        if feats and feats[0].get("geometry"):
            fields = counties.fields_from_record(feats[0]["properties"])
            fields["acres"] = round(_geod_acres(shape(feats[0]["geometry"])), 2)
            return {"county": {"fips": cfg["fips"], "name": cfg["name"], "state": cfg["state"],
                               "cad_search_url": cfg.get("cad_search_url", "")},
                    "fields": fields, "geometry": feats[0]["geometry"]}
    if not (25.8 <= lat <= 36.6 and -106.7 <= lon <= -93.5):
        return None
    d = 0.01
    resp = SESSION.get(f"{TX_PARCELS_MAPSERVER}/identify", params={
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "sr": 4326, "tolerance": 1,
        "mapExtent": f"{lon - d},{lat - d},{lon + d},{lat + d}", "imageDisplay": "800,800,96", "layers": "all:0",
        "returnGeometry": "true", "f": "json",
    }, timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    hit = results[0]
    a = {k.upper(): (None if v in ("Null", "") else v) for k, v in (hit.get("attributes") or {}).items()}
    geom = None
    rings = (hit.get("geometry") or {}).get("rings")
    if rings:
        from shapely.geometry import Polygon
        from shapely.ops import orient

        polys = [Polygon(r) for r in rings if len(r) >= 4]
        outers = [p for p in polys if not p.exterior.is_ccw] or polys
        geom = unary_union([orient(p) for p in outers])
        geom = mapping(geom if geom.is_valid else make_valid(geom))

    def t(k):
        v = a.get(k)
        return "" if v is None else str(v).strip()

    def n(k):
        try:
            return int(float(a.get(k))) or None
        except (TypeError, ValueError):
            return None

    try:
        acres = float(a.get("GIS_AREA") or 0)
    except ValueError:
        acres = 0.0
    fields = {
        "prop_id": "" if t("PROP_ID") == "0" else t("PROP_ID"), "geo_id": t("GEO_ID"),
        "owner_name": t("OWNER_NAME"), "care_of": t("NAME_CARE"),
        "mail_address": " ".join(x for x in [t("MAIL_LINE1"), t("MAIL_LINE2")] if x),
        "mail_city": t("MAIL_CITY"), "mail_state": t("MAIL_STAT")[:2], "mail_zip": t("MAIL_ZIP"),
        "situs_address": t("SITUS_ADDR").strip(" ,"), "situs_city": t("SITUS_CITY"), "situs_zip": t("SITUS_ZIP"),
        "legal_desc": t("LEGAL_DESC"), "acres": round(acres, 2),
        "legal_acres": float(a["LEGAL_AREA"]) if (a.get("LEGAL_AREA") or "0") not in ("0", "") else None,
        "land_value": n("LAND_VALUE"), "improvement_value": n("IMP_VALUE"), "market_value": n("MKT_VALUE"),
        "state_land_use": t("STAT_LAND_USE"), "local_land_use": t("LOC_LAND_USE"), "tax_year": t("TAX_YEAR"),
        "year_built": t("YEAR_BUILT"),
    }
    return {"county": {"fips": t("FIPS"), "name": t("COUNTY").title(), "state": "TX", "source": t("SOURCE")},
            "fields": fields, "geometry": geom}


def _geod_acres(geom):
    from pyproj import Geod

    return abs(Geod(ellps="WGS84").geometry_area_perimeter(geom)[0]) / SQ_M_PER_ACRE
