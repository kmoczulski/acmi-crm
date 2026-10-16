"""Load county parcels into Parcel.

Texas: the TxGIO StratMap county zip (shapefile). Maryland (Charles County): the county's own tax-parcel
service, plus its zoning and planning layers stamped onto each parcel (crm/land/counties.py).

Keeps parcels of `min_acres` or more (default 20). Re-loading a county updates the same parcels by
property id / account and keeps the team's status, notes, landowner link and live checks.
"""
import json
import shutil
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

import shapefile
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pyproj import Geod
from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from ..models import LandCounty, Parcel
from . import counties, layers, score
from .sources import arcgis_query, download, region_for, txgio_parcel_catalog

DATA_DIR = Path(getattr(settings, "ACMI_LAND_DATA_DIR", settings.BASE_DIR / "data" / "land"))
SIMPLIFY_DEG = 0.00003  # about 3 m
SQ_M_PER_ACRE = 4046.8564224
_geod = Geod(ellps="WGS84")

# Texas appraisal district property search pages, where known. {prop_id} is filled in.
CAD_SEARCH = {
    "48209": "https://esearch.hayscad.com/Property/View/{prop_id}",
    "48055": "https://esearch.caldwellcad.org/Property/View/{prop_id}",
    "48021": "https://esearch.bastropcad.org/Property/View/{prop_id}",
    "48187": "https://esearch.guadalupead.org/Property/View/{prop_id}",
    "48091": "https://esearch.comalad.org/Property/View/{prop_id}",
    "48031": "https://esearch.blancocad.com/Property/View/{prop_id}",
}

TX_TEXT_FIELDS = {
    "prop_id": "PROP_ID", "geo_id": "GEO_ID", "owner_name": "OWNER_NAME", "care_of": "NAME_CARE",
    "mail_city": "MAIL_CITY", "mail_state": "MAIL_STAT", "mail_zip": "MAIL_ZIP", "situs_city": "SITUS_CITY",
    "situs_zip": "SITUS_ZIP", "legal_desc": "LEGAL_DESC", "state_land_use": "STAT_LAND_",
    "local_land_use": "LOC_LAND_U", "year_built": "YEAR_BUILT", "tax_year": "TAX_YEAR",
}
# Fields the loaders own; everything the team or live checks set is left alone on re-load.
LOADED_FIELDS = [
    "prop_id", "geo_id", "owner_name", "care_of", "mail_address", "mail_city", "mail_state", "mail_zip",
    "situs_address", "situs_city", "situs_zip", "legal_desc", "acres", "legal_acres", "land_value",
    "improvement_value", "market_value", "state_land_use", "local_land_use", "year_built", "tax_year", "geometry",
    "lat", "lon", "min_lat", "min_lon", "max_lat", "max_lon", "in_city", "zoning", "zoning_class", "overlays", "near", "owner_acres",
    "owner_parcels", "score", "score_reasons", "killers",
]


def _text(value):
    value = "" if value is None else str(value).strip()
    return "" if value.lower() in ("null", "none", "0") and len(value) <= 4 else value


def _num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _address(*parts):
    return " ".join(p for p in (_text(x) for x in parts) if p and p != ",").strip(" ,")


def owner_key(name):
    return " ".join((name or "").upper().replace(",", " ").replace(".", " ").split())


def geodesic_acres(geom):
    return abs(_geod.geometry_area_perimeter(geom)[0]) / SQ_M_PER_ACRE


def load_county(name_or_fips, *, min_acres=20, keep_zip=False, catalog=None, log=print):
    """Load one county by name or FIPS. Returns the LandCounty."""
    md = counties.find(name_or_fips)
    if md:
        return load_arcgis_county(md, min_acres=min_acres, log=log)
    catalog = catalog or txgio_parcel_catalog()
    entry = catalog.get(str(name_or_fips).lower()) or next(
        (c for c in catalog.values() if c["fips"] == str(name_or_fips)), None)
    if not entry:
        raise ValueError(f"No parcel source for {name_or_fips!r} (Texas counties, or: {counties.names()})")
    zpath = DATA_DIR / "raw" / f"{entry['fips']}.zip"
    if not zpath.exists():
        log(f"{entry['name']}: downloading {entry['size'] / 1e6:,.0f} MB")
        download(entry["url"], zpath)
    work = Path(tempfile.mkdtemp(prefix=f"parcels_{entry['fips']}_"))
    try:
        with zipfile.ZipFile(zpath) as z:
            shp_names = [n for n in z.namelist() if n.lower().endswith((".shp", ".shx", ".dbf", ".prj", ".cpg"))]
            z.extractall(work, members=shp_names)
        county = _load_texas_shapefile(next(work.rglob("*.shp")), entry, min_acres, log)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not keep_zip:
        zpath.unlink(missing_ok=True)
    return county


def _load_texas_shapefile(shp_path, entry, min_acres, log):
    reader = shapefile.Reader(str(shp_path), encoding="utf-8", encodingErrors="replace")
    # Pass 1: attributes only. Group split polygons by property id; total each owner's acres.
    groups, first, total = defaultdict(list), {}, 0
    for i, rec in enumerate(reader.iterRecords()):
        total += 1
        rec = rec.as_dict()
        k = _text(rec.get("PROP_ID")) or _text(rec.get("GEO_ID")) or f"row-{i}"
        groups[k].append((i, _num(rec.get("GIS_AREA")) or 0.0))
        first.setdefault(k, rec)
    owner_totals = _owner_totals((first[k].get("OWNER_NAME"), sum(a for _, a in rows)) for k, rows in groups.items())
    keep = {k: rows for k, rows in groups.items() if sum(a for _, a in rows) >= min_acres}
    log(f"{entry['name']}: {total:,} parcels, {len(keep):,} of {min_acres:g}+ acres")
    sample = next(iter(first.values()), {})
    county, _ = LandCounty.objects.update_or_create(fips=entry["fips"], defaults={
        "name": entry["name"], "state": "TX", "region": region_for(entry["name"]), "source": _text(sample.get("SOURCE")), "release": entry["release"],
        "tax_year": _text(sample.get("TAX_YEAR")), "total_parcels": total, "min_acres": min_acres,
        "cad_search_url": CAD_SEARCH.get(entry["fips"], ""),
    })

    def items():
        for k, rows in keep.items():
            geoms = []
            for i, _ in rows:
                shp = reader.shape(i)
                if shp.shapeType != shapefile.NULL and shp.points:
                    geoms.append(shape(shp.__geo_interface__))
            if not geoms:
                continue
            rec = first[k]
            fields = {f: _text(rec.get(src)) for f, src in TX_TEXT_FIELDS.items()}
            fields.update(
                mail_address=_address(rec.get("MAIL_LINE1"), rec.get("MAIL_LINE2")),
                situs_address=_address(rec.get("SITUS_NUM"), rec.get("SITUS_STRE"), rec.get("SITUS_ST_1"),
                                       rec.get("SITUS_ST_2")),
                acres=round(sum(a for _, a in rows), 2), legal_acres=_num(rec.get("LEGAL_AREA")),
                land_value=int(_num(rec.get("LAND_VALUE")) or 0) or None,
                improvement_value=int(_num(rec.get("IMP_VALUE")) or 0) or None,
                market_value=int(_num(rec.get("MKT_VALUE")) or 0) or None,
            )
            yield k, fields, geoms

    _save(county, items(), owner_totals, len(keep), log)
    return county


def load_arcgis_county(cfg, *, min_acres=20, log=print):
    """A county that publishes its tax parcels as a queryable ArcGIS service (Charles County, MD)."""
    log(f"{cfg['name']}: reading owner totals")
    every = arcgis_query(cfg["parcels_url"], fields=[cfg["key_field"], "OWNNAME1", "LANDAREA", "LUOM"],
                         return_geometry=False, page_size=2000)
    total = len(every)
    owner_totals = _owner_totals((f["properties"].get("OWNNAME1"), counties.record_acres(f["properties"]) or 0)
                                 for f in every)
    log(f"{cfg['name']}: {total:,} parcels; downloading the 20+ acre ones")
    feats = arcgis_query(cfg["parcels_url"], where=counties.big_parcel_where(min_acres * 0.8), fields=["*"],
                         simplify=SIMPLIFY_DEG, page_size=1000)
    county, _ = LandCounty.objects.update_or_create(fips=cfg["fips"], defaults={
        "name": cfg["name"], "state": cfg["state"], "region": region_for(cfg["name"]), "source": cfg["source"], "release": cfg["release"],
        "total_parcels": total, "min_acres": min_acres, "cad_search_url": cfg.get("cad_search_url", ""),
    })
    overlay_index = counties.overlay_index(cfg, log)
    groups = defaultdict(list)
    for f in feats:
        if f.get("geometry"):
            groups[_text(f["properties"].get(cfg["key_field"])) or str(f["properties"].get("FID"))].append(f)

    def items():
        for k, fs in groups.items():
            geoms = [shape(f["geometry"]) for f in fs]
            rec = fs[0]["properties"]
            fields = counties.fields_from_record(rec)
            geom = unary_union([g if g.is_valid else make_valid(g) for g in geoms])
            fields["acres"] = round(geodesic_acres(geom), 2)
            if fields["acres"] < min_acres:
                continue
            fields["zoning"], fields["overlays"] = counties.stamp(overlay_index, geom)
            yield k, fields, geoms

    _save(county, items(), owner_totals, len(groups), log)
    return county


def _owner_totals(pairs):
    totals = defaultdict(lambda: [0.0, 0])
    for name, acres in pairs:
        o = owner_key(name)
        if o and o != "NULL":
            totals[o][0] += acres or 0
            totals[o][1] += 1
    return totals


def _save(county, items, owner_totals, expected, log):
    """Upsert parcels from (key, fields, geometries) and drop untouched parcels no longer in the source."""
    existing = {p.key: p for p in Parcel.objects.filter(county=county)}
    layers.reset_cache()
    to_create, to_update, seen = [], [], set()
    for n, (k, fields, geoms) in enumerate(items, 1):
        geoms = [g if g.is_valid else make_valid(g) for g in geoms]
        geom = unary_union(geoms) if len(geoms) > 1 else geoms[0]
        geom = geom.simplify(SIMPLIFY_DEG, preserve_topology=True)
        if geom.is_empty:
            continue
        p = existing.get(k) or Parcel(county=county, key=k)
        p.county = county
        for field, value in fields.items():
            f = Parcel._meta.get_field(field)
            if isinstance(value, str) and getattr(f, "max_length", None):
                value = value[: f.max_length]
            setattr(p, field, value)
        p.mail_state = (p.mail_state or "")[:2]
        p.geometry = json.dumps(mapping(geom))
        pt = geom.representative_point()
        p.lat, p.lon = pt.y, pt.x
        p.min_lon, p.min_lat, p.max_lon, p.max_lat = geom.bounds
        o = owner_totals.get(owner_key(p.owner_name))
        p.owner_acres, p.owner_parcels = (round(o[0], 1), o[1]) if o else (None, None)
        p.near, p.in_city = layers.measure(geom)
        score.apply(p)
        seen.add(k)
        (to_update if p.pk else to_create).append(p)
        if n % 2000 == 0:
            log(f"  {n:,} / {expected:,}")
    with transaction.atomic():
        Parcel.objects.bulk_create(to_create, batch_size=500)
        Parcel.objects.bulk_update(to_update, LOADED_FIELDS, batch_size=500)
        gone = set(existing) - seen
        Parcel.objects.filter(county=county, key__in=gone, status="", notes="", company__isnull=True).delete()
        county.kept_parcels = county.parcels.count()
        county.loaded_at = timezone.now()
        county.save()
    log(f"{county.name}: saved {len(to_create):,} new, {len(to_update):,} updated")


def rescore(queryset=None, remeasure=False, log=print):
    """Recompute scores (and optionally distances) after new layers, checks or buy-box changes."""
    qs = queryset if queryset is not None else Parcel.objects.all()
    layers.reset_cache()
    batch, n = [], 0
    fields = ["score", "score_reasons", "killers", "zoning_class"] + (["near", "in_city"] if remeasure else [])
    for p in qs.select_related("county").iterator(chunk_size=1000):
        if remeasure:
            p.near, p.in_city = layers.measure(shape(json.loads(p.geometry)))
        score.apply(p)
        batch.append(p)
        if len(batch) >= 1000:
            Parcel.objects.bulk_update(batch, fields)
            n += len(batch)
            batch = []
            log(f"  {n:,}")
    Parcel.objects.bulk_update(batch, fields)
    return n + len(batch)
