"""Stored map layers and each parcel's distance to them.

`load_layers()` copies bases, transmission lines, substations, gas pipelines, rail, major highways and
city limits for a region into LandFeature. `measure()` then gives every parcel its nearest of each
(miles, name, details) and the city whose limits it falls in (blank = unincorporated, i.e. no zoning
in Texas).
"""
import json
from functools import lru_cache

from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import mapping, shape
from shapely.ops import transform

from ..models import LandFeature
from .sources import LIMIT_LAYERS, REGIONS, STORED_LAYERS, arcgis_query

METERS_PER_MILE = 1609.344
# NAD83 / Conus Albers: metres, good enough for distances and area shares in Texas and Maryland alike.
_to_albers = Transformer.from_crs(4326, 5070, always_xy=True).transform


def to_albers(geom):
    return transform(_to_albers, geom)


def load_layers(layers=None, regions=None, log=print):
    """Replace the stored features of each layer with a fresh copy for every region. Returns {layer: count}.

    regions: only refresh these regions (the other regions' features stay); layers limited to other regions
    are skipped."""
    from django.db.models import Q

    counts = {}
    for key in layers or STORED_LAYERS:
        spec = STORED_LAYERS[key]
        todo = [r for r in regions if r in (spec.get("regions") or REGIONS)] if regions else (spec.get("regions") or REGIONS)
        if not todo:
            continue
        fields = [spec["name"], *spec["fields"]]
        features = []
        for region in todo:
            features += arcgis_query(spec["url"], where=spec["where"], fields=fields, bbox=REGIONS[region],
                                     simplify=spec["simplify"] or None)
        rows = []
        for f in features:
            if not f.get("geometry"):
                continue
            geom = shape(f["geometry"])
            if geom.is_empty:
                continue
            props = f.get("properties") or {}
            minx, miny, maxx, maxy = geom.bounds
            rows.append(LandFeature(
                layer=key, name=str(props.get(spec["name"]) or "")[:255],
                attrs={k: props.get(k) for k in spec["fields"] if props.get(k) not in (None, "")},
                geometry=json.dumps(mapping(geom)), min_lon=minx, min_lat=miny, max_lon=maxx, max_lat=maxy,
            ))
        stale = LandFeature.objects.filter(layer=key)
        if regions:
            inside = Q()
            for r in todo:
                w, so, e, n = REGIONS[r]
                inside |= Q(max_lon__gte=w, min_lon__lte=e, max_lat__gte=so, min_lat__lte=n)
            stale = stale.filter(inside)
        stale.delete()
        LandFeature.objects.bulk_create(rows, batch_size=500)
        counts[key] = len(rows)
        log(f"{spec['label']}: {len(rows)}")
    _index.cache_clear()
    return counts


@lru_cache(maxsize=None)
def _index(layer):
    """(STRtree over Albers geometries, list of features) for one stored layer."""
    feats = list(LandFeature.objects.filter(layer=layer).only("name", "attrs", "geometry"))
    geoms = [to_albers(shape(json.loads(f.geometry))) for f in feats]
    return (STRtree(geoms) if geoms else None), geoms, feats


def reset_cache():
    _index.cache_clear()


def measure(geom_wgs84):
    """Distances from one parcel (shapely, WGS84) to each stored layer, plus its city.

    Returns (near, city) where near = {layer: {"miles", "name", ...attrs}}.
    """
    geom = to_albers(geom_wgs84)
    near, city = {}, ""
    for layer in STORED_LAYERS:
        tree, geoms, feats = _index(layer)
        if tree is None:
            continue
        if layer in LIMIT_LAYERS:
            hits = tree.query(geom.representative_point(), predicate="intersects")
            if len(hits) and not city:
                city = feats[int(hits[0])].name
            continue
        idx, dist = tree.query_nearest(geom, return_distance=True, all_matches=False)
        if not len(idx):
            continue
        feat = feats[int(idx[0])]
        near[layer] = {"miles": round(float(dist[0]) / METERS_PER_MILE, 2), "name": feat.name, **feat.attrs}
    return near, city


def layer_counts():
    from django.db.models import Count

    return dict(LandFeature.objects.values_list("layer").annotate(n=Count("id")))
