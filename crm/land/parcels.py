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
from django.db.models import Q
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
KEEP_IF_BLANK = {"owner_name", "care_of", "mail_address", "mail_city", "mail_state", "mail_zip", "land_value",
                 "improvement_value", "market_value"}
# Parcels with team work on them: never pruned on a reload, and the one kept when several ids share a shape.
TEAM_WORK = (~Q(status="") | ~Q(notes="") | Q(company__isnull=False) | Q(sites__isnull=False)
             | Q(site_deals__isnull=False) | ~Q(qd_design={}) | ~Q(site_plan={}))
# A reload that returns fewer than this share of the stored parcels looks like a partial download: nothing is pruned
# unless force_prune (manage.py load_parcels --force-prune).
PRUNE_FLOOR = 0.8
# Below this a "parcel" is a sliver or a drawing error (Pinal's full windows held 44 of them at 0.00 ac).
MIN_PARCEL_ACRES = 0.01
# Keys made from an object id (Pinal "NAP<objectid>", NC "OID<objectid>") or an outline ("shp-..."); see unstable_key.
UNSTABLE_PREFIXES = ("NAP", "OID", "SHP-", "PT-")
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


def load_county(name_or_fips, *, min_acres=20, keep_zip=False, catalog=None, log=print, force_prune=False):
    """Load one county by name or FIPS. Returns the LandCounty."""
    md = counties.find(name_or_fips)
    if md:
        return load_arcgis_county(md, min_acres=min_acres, log=log, force_prune=force_prune)
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
        county = _load_texas_shapefile(next(work.rglob("*.shp")), entry, min_acres, log, force_prune=force_prune)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not keep_zip:
        zpath.unlink(missing_ok=True)
    return county


def _load_texas_shapefile(shp_path, entry, min_acres, log, force_prune=False):
    reader = shapefile.Reader(str(shp_path), encoding="utf-8", encodingErrors="replace")
    # Pass 1: attributes only. Group split polygons by property id; total each owner's acres.
    groups, first, total = defaultdict(list), {}, 0
    for i, rec in enumerate(reader.iterRecords()):
        total += 1
        rec = rec.as_dict()
        k = _text(rec.get("PROP_ID")) or _text(rec.get("GEO_ID")) or f"row-{i}"
        groups[k].append((i, _num(rec.get("GIS_AREA")) or 0.0))
        first.setdefault(k, rec)
    keep = {k: rows for k, rows in groups.items() if sum(a for _, a in rows) >= min_acres}
    sample = next(iter(first.values()), {})
    county, _ = LandCounty.objects.update_or_create(fips=entry["fips"], defaults={
        "name": entry["name"], "state": "TX", "region": region_for(entry["name"], "TX"), "source": _text(sample.get("SOURCE")), "release": entry["release"],
        "tax_year": _text(sample.get("TAX_YEAR")), "total_parcels": total, "min_acres": min_acres,
        "cad_search_url": CAD_SEARCH.get(entry["fips"], ""),
    })

    # Pass 2: shapes of the kept ones. Some appraisal files give several property ids the same polygon and
    # GIS_AREA (Hays: 10 ids on one 3,655 ac ranch); store those as one parcel.
    shapes = {}
    for k, rows in keep.items():
        geoms = []
        for i, _ in rows:
            shp = reader.shape(i)
            if shp.shapeType != shapefile.NULL and shp.points:
                geoms.append(shape(shp.__geo_interface__))
        if geoms:
            shapes[k] = geoms
    reader.close()
    team = set(Parcel.objects.filter(county=county).filter(TEAM_WORK).values_list("key", flat=True))
    shared = shared_shapes({k: (_shape_sig(g, sum(a for _, a in keep[k])),
                                (k in team, _num(first[k].get("LEGAL_AREA")) or 0)) for k, g in shapes.items()})
    sig_of = {k: rep for rep, others in shared.items() for k in (rep, *others)}
    merged_away = {k for others in shared.values() for k in others}

    # Owner totals count a shared shape once per owner.
    counted = set()

    def owner_pairs():
        for k, rows in groups.items():
            name = first[k].get("OWNER_NAME")
            if k in sig_of:
                if (owner_key(name), sig_of[k]) in counted:
                    continue
                counted.add((owner_key(name), sig_of[k]))
            yield name, sum(a for _, a in rows)

    owner_totals = _owner_totals(owner_pairs())
    log(f"{entry['name']}: {total:,} parcels, {len(keep) - len(merged_away):,} of {min_acres:g}+ acres"
        + (f" ({len(merged_away):,} property ids sharing another's shape merged in)" if merged_away else ""))

    def items():
        for k, geoms in shapes.items():
            if k in merged_away:
                continue
            rec, rows = first[k], keep[k]
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
            if k in shared:
                fields.update(_merged_fields(fields, [first[o] for o in shared[k]]))
            yield k, fields, geoms

    _save(county, items(), owner_totals, len(shapes) - len(merged_away), log, force_prune=force_prune)
    return county


def _shape_sig(geoms, acres):
    """Identity of a polygon for spotting the same shape filed under several property ids."""
    x0, y0, x1, y1 = zip(*(g.bounds for g in geoms))
    return round(min(x0), 5), round(min(y0), 5), round(max(x1), 5), round(max(y1), 5), round(acres, 1)


def shared_shapes(sigs):
    """{key: (shape signature, rank)} -> {kept key: [other keys with the same shape]}. The kept key has the
    highest rank (team work on it first, then the largest legal area)."""
    by_sig = defaultdict(list)
    for k, (sig, rank) in sigs.items():
        by_sig[sig].append((rank, k))
    out = {}
    for keys in by_sig.values():
        if len(keys) > 1:
            keys.sort(reverse=True)
            out[keys[0][1]] = sorted(k for _, k in keys[1:])
    return out


def same_shape_legal(fields, ids, owners, label="prop ids"):
    """The kept record's legal description with the other ids (and owners) on the same outline noted at the end."""
    owners = sorted({o for o in owners if o} - {fields.get("owner_name") or ""})
    note = f"[same shape as {label} {', '.join(ids)}" + (f"; also owned by {'; '.join(owners)}" if owners else "") + "]"
    note = note if len(note) <= 200 else note[:198] + "…]"
    legal = (fields.get("legal_desc") or "")[: 300 - len(note) - 1]
    return {"legal_desc": f"{legal} {note}".strip()}


def _merged_fields(fields, others):
    """Texas: note the other property ids (and owners) on the same shape in the kept record's legal description.
    Mapped acres stay the shape's; deed acres stay the kept id's (the largest)."""
    return same_shape_legal(fields, [_text(r.get("PROP_ID")) or _text(r.get("GEO_ID")) for r in others],
                            [_text(r.get("OWNER_NAME")) for r in others])


def unstable_key(key):
    """Keys made from an object id or an outline (no parcel number): they can change when the county republishes."""
    return (key or "").upper().startswith(UNSTABLE_PREFIXES)


def reload_sig(min_lon, min_lat, max_lon, max_lat, acres):
    """Identity of a stored outline across reloads (bounds to ~1 m, acres to 0.1)."""
    return round(min_lon, 5), round(min_lat, 5), round(max_lon, 5), round(max_lat, 5), round(acres or 0, 1)


def load_arcgis_county(cfg, *, min_acres=20, log=print, force_prune=False):
    """A county that publishes its tax parcels as a queryable ArcGIS service (Charles County, MD)."""
    log(f"{cfg['name']}: reading owner totals")
    every = arcgis_query(cfg["parcels_url"], where=counties.count_where(cfg), fields=counties.count_fields(cfg),
                         return_geometry=False, page_size=2000)
    total = len(every)
    owner_totals = _owner_totals((_text(f["properties"].get(counties.owner_field(cfg))),
                                  counties.acres_of(cfg, f["properties"]) or 0) for f in every)
    log(f"{cfg['name']}: {total:,} parcels; downloading the {min_acres:g}+ acre ones")
    feats = arcgis_query(cfg["parcels_url"], where=counties.big_where(cfg, min_acres * 0.8), fields=["*"],
                         simplify=SIMPLIFY_DEG, page_size=1000)
    have = {f.get("id") for f in feats if f.get("id") is not None}
    for west, south, east, north in cfg.get("full_windows") or []:
        log(f"{cfg['name']}: every parcel in the window {west}, {south} to {east}, {north}")
        extra = arcgis_query(cfg["parcels_url"], where=counties.in_county(cfg, "1=1"), fields=["*"],
                             bbox=(west, south, east, north), simplify=SIMPLIFY_DEG, page_size=1000)
        for f in extra:
            if f.get("id") is None or f["id"] not in have:
                feats.append(f)
                have.add(f.get("id"))
    county, _ = LandCounty.objects.update_or_create(fips=cfg["fips"], defaults={
        "name": cfg["name"], "state": cfg["state"], "region": region_for(cfg["name"], cfg["state"]), "source": cfg["source"], "release": cfg["release"],
        "total_parcels": total, "min_acres": min_acres, "cad_search_url": cfg.get("cad_search_url", ""),
    })
    overlay_index = counties.overlay_index(cfg, log)
    groups = defaultdict(list)
    for f in feats:
        key = counties.record_key(cfg, f["properties"])
        # Rights-of-way and other shapes without a tax account ("NO ID") aren't parcels anyone can sell.
        if f.get("geometry") and key and key.upper() != "NO ID":
            groups[key].append(f)

    kept = {}
    for k, fs in groups.items():
        geoms = [shape(f["geometry"]) for f in fs]
        fields = counties.record_fields(cfg, fs[0]["properties"])
        geom = unary_union([g if g.is_valid else make_valid(g) for g in geoms])
        fields["acres"] = round(geodesic_acres(geom), 2)
        if fields["acres"] < MIN_PARCEL_ACRES:  # zero-area slivers and drawing errors, not land anyone can buy
            continue
        if fields["acres"] < min_acres and not counties.in_windows(cfg, geom):
            continue
        kept[k] = (fields, geoms, geom)
    # Several accounts on one identical outline (records without a parcel number, condo-style splits): store the
    # outline once, as the Texas loader does. Kept: the one with team work, else a real parcel number, else the
    # highest value; the others are noted in its legal description and pruned like any vanished parcel.
    team = set(Parcel.objects.filter(county__fips=cfg["fips"]).filter(TEAM_WORK).values_list("key", flat=True))
    shared = shared_shapes({k: (_shape_sig([g], f["acres"]), (k in team, not unstable_key(k),
                                                              f.get("market_value") or f.get("land_value") or 0))
                            for k, (f, _, g) in kept.items()})
    merged_away = {o for others in shared.values() for o in others}
    if merged_away:
        log(f"{cfg['name']}: {len(merged_away):,} records share another's outline; each outline is stored once")

    def items():
        for k, (fields, geoms, geom) in kept.items():
            if k in merged_away:
                continue
            if k in shared:
                fields.update(same_shape_legal(fields, shared[k], [kept[o][0].get("owner_name") for o in shared[k]],
                                               label="parcel ids"))
            fields["zoning"], fields["overlays"] = counties.stamp(overlay_index, geom)
            yield k, fields, geoms

    _save(county, items(), owner_totals, len(kept) - len(merged_away), log, force_prune=force_prune)
    return county


def _owner_totals(pairs):
    totals = defaultdict(lambda: [0.0, 0])
    for name, acres in pairs:
        o = owner_key(name)
        if o and o != "NULL":
            totals[o][0] += acres or 0
            totals[o][1] += 1
    return totals


def assign_fields(p, fields):
    """Copy loaded fields onto a parcel: text cut to the column size, and owner data a file added (load_owners)
    kept when the source has it blank."""
    for field, value in fields.items():
        if value in ("", None) and p.pk and field in KEEP_IF_BLANK and getattr(p, field):
            continue
        f = Parcel._meta.get_field(field)
        if isinstance(value, str) and getattr(f, "max_length", None):
            value = value[: f.max_length]
        setattr(p, field, value)


def _save(county, items, owner_totals, expected, log, prune=True, force_prune=False):
    """Upsert parcels from (key, fields, geometries). prune: drop parcels no longer in the source, except those with
    team work on them (TEAM_WORK), and only when the source didn't come back suspiciously short (PRUNE_FLOOR)."""
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
        assign_fields(p, fields)
        p.mail_state = (p.mail_state or "")[:2]
        p.geometry = json.dumps(mapping(geom))
        pt = geom.representative_point()
        p.lat, p.lon = pt.y, pt.x
        p.min_lon, p.min_lat, p.max_lon, p.max_lat = geom.bounds
        o = owner_totals.get(owner_key(p.owner_name))
        if o:
            p.owner_acres, p.owner_parcels = round(o[0], 1), o[1]
        elif not (p.pk and p.owner_name and p.owner_acres):
            p.owner_acres, p.owner_parcels = None, None
        p.near, p.in_city = layers.measure(geom)
        score.apply(p)
        seen.add(k)
        (to_update if p.pk else to_create).append(p)
        if n % 2000 == 0:
            log(f"  {n:,} / {expected:,}")
    # A parcel keyed by an object id gets a new key when the county republishes. If a new record has exactly the
    # outline of a stored one whose unstable key vanished, it is the same land: move the stored parcel (and the team's
    # work on it) to the new key instead of pruning it and creating a stranger.
    rekeys = []
    if prune:
        vanished = {}
        for key in set(existing) - seen:
            if unstable_key(key):
                old = existing[key]
                vanished.setdefault(reload_sig(old.min_lon, old.min_lat, old.max_lon, old.max_lat, old.acres), old)
        if vanished:
            still_new = []
            for p in to_create:
                old = vanished.pop(reload_sig(p.min_lon, p.min_lat, p.max_lon, p.max_lat, p.acres), None)
                if old is None:
                    still_new.append(p)
                    continue
                for f in LOADED_FIELDS:
                    setattr(old, f, getattr(p, f))
                rekeys.append((old.pk, old.key, p.key))
                to_update.append(old)
            to_create = still_new
    with transaction.atomic():
        for pk, _old, new in rekeys:
            Parcel.objects.filter(pk=pk).update(key=new)
        if rekeys:
            log(f"{county.name}: {len(rekeys):,} parcels kept their record under a new key "
                f"(e.g. {rekeys[0][1]} -> {rekeys[0][2]})")
        Parcel.objects.bulk_create(to_create, batch_size=500)
        Parcel.objects.bulk_update(to_update, LOADED_FIELDS, batch_size=500)
        gone = sorted(set(existing) - seen) if prune else []
        if gone and len(seen) < PRUNE_FLOOR * len(existing) and not force_prune:
            log(f"{county.name}: the source returned {len(seen):,} parcels against {len(existing):,} stored, so the "
                f"{len(gone):,} missing ones were kept (a partial download?). If the drop is real, re-run with "
                "--force-prune.")
            gone = []
        for i in range(0, len(gone), 500):
            chunk = gone[i:i + 500]
            protected = set(Parcel.objects.filter(county=county, key__in=chunk).filter(TEAM_WORK)
                            .values_list("key", flat=True))
            Parcel.objects.filter(county=county, key__in=[k for k in chunk if k not in protected]).delete()
        county.kept_parcels = county.parcels.count()
        county.loaded_at = timezone.now()
        county.save()
    log(f"{county.name}: saved {len(to_create):,} new, {len(to_update):,} updated")


def loaded_county(name):
    """A loaded county by config name ('Pinal AZ', 'fayetteville'), 'Name ST', a name only one county has, or FIPS.
    Covers the Texas counties too, which come from TxGIO files rather than a configured service."""
    cfg = counties.find(name)
    if cfg:
        return LandCounty.objects.filter(fips=cfg["fips"]).first()
    text = name.strip()
    if text.isdigit():
        return LandCounty.objects.filter(fips=text).first()
    head, _, state = text.rpartition(" ")
    if head and len(state) == 2:
        return LandCounty.objects.filter(name__iexact=head, state__iexact=state).first()
    matches = list(LandCounty.objects.filter(name__iexact=text)[:2])
    return matches[0] if len(matches) == 1 else None


def clean_stored(county=None, commit=False):
    """Tidy parcels already stored the way loads now do: remove slivers under MIN_PARCEL_ACRES and keep each
    identical outline once (the kept one notes the others in its legal description). Parcels with team work are
    never removed. Returns ({county label: Counter}, number removed); nothing changes unless commit."""
    from collections import Counter

    qs = Parcel.objects.select_related("county").defer(
        "geometry", "overlays", "near", "checks", "score_reasons", "killers", "qd_design", "site_plan")
    if county is not None:
        qs = qs.filter(county=county)
    team = set(qs.filter(TEAM_WORK).values_list("pk", flat=True))
    report, remove, notes, groups = defaultdict(Counter), [], {}, defaultdict(list)
    for p in qs.iterator(chunk_size=5000):
        label = f"{p.county.name} {p.county.state}"
        if p.acres < MIN_PARCEL_ACRES:
            if p.pk in team:
                report[label]["slivers kept (team work)"] += 1
            else:
                remove.append(p.pk)
                report[label]["slivers"] += 1
            continue
        groups[(label, reload_sig(p.min_lon, p.min_lat, p.max_lon, p.max_lat, p.acres))].append(p)
    for (label, _sig), ps in groups.items():
        if len(ps) < 2:
            continue
        ps.sort(key=lambda p: (p.pk in team, not unstable_key(p.key), p.market_value or p.land_value or 0, -p.pk),
                reverse=True)
        keep, others = ps[0], ps[1:]
        dropped = [o for o in others if o.pk not in team]
        if len(others) > len(dropped):
            report[label]["duplicates kept (team work)"] += len(others) - len(dropped)
        if not dropped:
            continue
        remove.extend(o.pk for o in dropped)
        report[label]["duplicate outlines"] += len(dropped)
        if "[same shape as" not in (keep.legal_desc or ""):
            notes[keep.pk] = same_shape_legal({"owner_name": keep.owner_name, "legal_desc": keep.legal_desc},
                                              [o.key for o in dropped], [o.owner_name for o in dropped],
                                              label="parcel ids")["legal_desc"]
    if commit:
        with transaction.atomic():
            for pk, legal in notes.items():
                Parcel.objects.filter(pk=pk).update(legal_desc=legal)
            for i in range(0, len(remove), 500):
                Parcel.objects.filter(pk__in=remove[i:i + 500]).delete()
    return report, len(remove)


def rescore(queryset=None, remeasure=False, log=print):
    """Recompute scores (and optionally distances) after new layers, checks or buy-box changes."""
    qs = queryset if queryset is not None else Parcel.objects.all()
    layers.reset_cache()
    batch, n = [], 0
    fields = ["score", "score_reasons", "killers", "zoning_class"] + (["near", "in_city"] if remeasure else [])
    # By id, not the default -score order: SQLite doesn't isolate a running read from writes on the same connection,
    # so walking the score index while rewriting scores visited ~1,500 parcels twice (and miscounted).
    for p in qs.select_related("county").order_by("pk").iterator(chunk_size=1000):
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


def normalize_account(fips, value):
    """Account ids as people write them -> the county's key. Charles (SDAT): '906022677', '06266428',
    '7043589' and '09-003134' all become 10 digits, '09' + district + account."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if fips.startswith("04"):  # Arizona APNs keep their letter suffix: '506-45-029h' -> '50645029H'
        return "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    if fips == "24017" and digits:
        if len(digits) == 10:
            return digits
        if len(digits) == 9:
            return "0" + digits
        if len(digits) == 8:
            return "09" + digits
        if len(digits) == 7:
            return "090" + digits
    return digits


def ensure_parcels(county_key, accounts, log=print):
    """Make sure these county accounts are stored as parcels (any size), fetching any that are missing from
    the county's own service. Returns {account: Parcel}. Charles County only for now."""
    cfg = counties.find(county_key)
    if not cfg:
        raise ValueError(f"No live parcel service for {county_key!r}")
    wanted = {normalize_account(cfg["fips"], a) for a in accounts if a}
    county = LandCounty.objects.filter(fips=cfg["fips"]).first()
    have = {p.key: p for p in Parcel.objects.filter(county=county, key__in=wanted)} if county else {}
    missing = sorted(wanted - set(have))
    if missing:
        if county is None:
            county = LandCounty.objects.create(fips=cfg["fips"], name=cfg["name"], state=cfg["state"],
                                               region=region_for(cfg["name"], cfg["state"]), source=cfg["source"],
                                               cad_search_url=cfg.get("cad_search_url", ""))
        feats = []
        for i in range(0, len(missing), 50):
            chunk = ", ".join(f"'{a}'" for a in missing[i:i + 50])
            feats += arcgis_query(cfg["parcels_url"], where=counties.in_county(cfg, f"{cfg['key_field']} IN ({chunk})"), fields=["*"],
                                  simplify=SIMPLIFY_DEG)
        index = counties.overlay_index(cfg, log=lambda *_: None)
        owners = _owner_totals(Parcel.objects.filter(county=county).values_list("owner_name", "acres"))
        groups = defaultdict(list)
        for f in feats:
            if f.get("geometry"):
                groups[counties.record_key(cfg, f["properties"])].append(f)

        def items():
            for k, fs in groups.items():
                geoms = [shape(f["geometry"]) for f in fs]
                fields = counties.record_fields(cfg, fs[0]["properties"])
                geom = unary_union([g if g.is_valid else make_valid(g) for g in geoms])
                fields["acres"] = round(geodesic_acres(geom), 2)
                fields["zoning"], fields["overlays"] = counties.stamp(index, geom)
                yield k, fields, geoms

        _save(county, items(), owners, len(groups), log, prune=False)
        not_found = set(missing) - set(groups)
        if not_found:
            log(f"Not in the county service: {', '.join(sorted(not_found))}")
    return {p.key: p for p in Parcel.objects.filter(county__fips=cfg["fips"], key__in=wanted)}
