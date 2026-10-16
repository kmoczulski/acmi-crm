"""A site's real footprint: included parcels only, each trimmed to the portion the site needs.

Kellen's case: an assemblage needs only part of a parcel (a trail bisects it, and only the side that joins the
neighbouring parcels matters). The site page lets him draw cut lines across the parcel; the pieces that touch the
rest of the assemblage are kept by default and he can flip any piece.
"""
import json

from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import LineString, mapping, shape
from shapely.ops import split, transform, unary_union

_to_albers = Transformer.from_crs(4326, 5070, always_xy=True).transform
SQM_PER_ACRE = 4046.8564224
EXTEND_M = 60  # cut lines run this far past their first and last clicks, so a line ending on the edge still cuts


def acres(geom):
    return transform(_to_albers, geom).area / SQM_PER_ACRE


def clean(geom):
    return geom if geom.is_valid else make_valid(geom)


def union(geoms):
    """Merge neighbouring shapes into one boundary (a hair of buffer closes slivers between parcels)."""
    geoms = [clean(g) for g in geoms if g is not None and not g.is_empty]
    if not geoms:
        return None
    return unary_union([g.buffer(0.00001) for g in geoms]).buffer(-0.00001)


def deal_shape(deal):
    return clean(shape(json.loads(deal.geometry_json())))


def parts(site, ids=None):
    """[(deal, shape)] for the site's included parcels (optionally only these parcel ids), trimmed."""
    from .tracker import site_deals

    wanted = None if ids is None else set(ids)
    return [(d, deal_shape(d)) for d in site_deals(site)
            if d.included and (wanted is None or d.parcel_id in wanted)]


def site_geometry(site, ids=None):
    return union([g for _, g in parts(site, ids)])


def site_acres(site):
    from .tracker import site_deals

    return round(sum(d.acres for d in site_deals(site) if d.included), 1)


def _extend(coords):
    """Stretch the line's first and last segments by EXTEND_M (degrees scaled by latitude)."""
    import math

    if len(coords) < 2:
        return coords
    lat = coords[0][1]
    m_lon, m_lat = 111320 * math.cos(math.radians(lat)), 110540

    def push(a, b):  # move b further from a
        dx, dy = (b[0] - a[0]) * m_lon, (b[1] - a[1]) * m_lat
        d = math.hypot(dx, dy) or 1
        return (b[0] + dx / d * EXTEND_M / m_lon, b[1] + dy / d * EXTEND_M / m_lat)

    return [push(coords[1], coords[0])] + list(coords[1:-1]) + [push(coords[-2], coords[-1])]


def cut(geom, lines):
    """Split a parcel by cut lines ([[lon, lat], ...] each). Returns the pieces, largest first."""
    pieces = [g for g in getattr(clean(geom), "geoms", [clean(geom)]) if g.geom_type == "Polygon"]
    for coords in lines:
        coords = [tuple(map(float, c[:2])) for c in coords if len(c) >= 2]
        if len(coords) < 2:
            continue
        blade = LineString(_extend(coords))
        out = []
        for p in pieces:
            try:
                out.extend(g for g in split(p, blade).geoms if g.geom_type == "Polygon")
            except Exception:  # GEOS can refuse odd blades; keep the piece whole
                out.append(p)
        pieces = out
    pieces = [p for p in pieces if acres(p) >= 0.01]
    return sorted(pieces, key=lambda p: -p.area)


def default_keep(pieces, neighbours):
    """Keep the pieces that touch the rest of the site; if none do, the largest."""
    if neighbours is not None and not neighbours.is_empty:
        near = neighbours.buffer(0.00003)  # ~3 m: parcel lines rarely meet exactly
        keep = [i for i, p in enumerate(pieces) if p.intersects(near)]
        if keep:
            return keep
    return [0] if pieces else []


def preview(deal, lines):
    """Pieces of a site parcel cut by `lines`, with acres and which ones to keep by default."""
    parcel = clean(shape(json.loads(deal.parcel.geometry)))
    pieces = cut(parcel, lines)
    others = site_geometry(deal.site, ids=None)
    if others is not None:
        mine = deal_shape(deal)
        others = clean(others).difference(mine.buffer(0.00001)) if deal.included else others
    if deal.trimmed and lines == deal.portion_cuts:
        # Re-opening a saved trim: show the pieces that were kept.
        saved = clean(shape(json.loads(deal.portion)))
        keep = [i for i, p in enumerate(pieces) if saved.contains(p.representative_point())]
    else:
        keep = default_keep(pieces, others)
    return {"pieces": [{"i": i, "acres": round(acres(p), 2), "keep": i in keep, "geometry": mapping(p)}
                       for i, p in enumerate(pieces)],
            "parcel_acres": round(deal.parcel.acres, 2)}


def apply(deal, lines, keep):
    """Store the kept pieces as the parcel's portion for this site (all pieces kept = whole parcel)."""
    parcel = clean(shape(json.loads(deal.parcel.geometry)))
    pieces = cut(parcel, lines)
    kept = [p for i, p in enumerate(pieces) if i in set(keep)]
    if not kept or len(kept) == len(pieces):
        reset(deal)
        return deal
    geom = unary_union(kept)
    deal.portion = json.dumps(mapping(geom))
    # Scale to the county's acreage so a trimmed parcel never reads larger than the tax roll says.
    measured_whole = acres(parcel) or 1
    deal.portion_acres = round(deal.parcel.acres * acres(geom) / measured_whole, 2)
    deal.portion_cuts = lines
    deal.save(update_fields=["portion", "portion_acres", "portion_cuts", "updated_at"])
    return deal


def reset(deal):
    deal.portion, deal.portion_acres, deal.portion_cuts = "", None, []
    deal.save(update_fields=["portion", "portion_acres", "portion_cuts", "updated_at"])
    return deal
