"""Drive times from the public Valhalla router (OpenStreetMap roads, no key; free, fair-use).

route(): time and distance A -> B and B -> A with the road path.
isochrones(): areas reachable within N minutes of a point (the drive-time heat map).
Times are free-flow (no live traffic).
"""
from .sources import SESSION

VALHALLA = "https://valhalla1.openstreetmap.de"
DEFAULT_MINUTES = (15, 30, 45, 60)
MAX_CONTOURS = 4  # the public server allows 4 bands per request; more are fetched in batches


def _decode_polyline6(encoded):
    """Valhalla's encoded shape (precision 6) -> [[lon, lat], ...]."""
    coords, index, lat, lon = [], 0, 0, 0
    while index < len(encoded):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append([lon / 1e6, lat / 1e6])
    return coords


def _leg(a, b):
    resp = SESSION.post(f"{VALHALLA}/route", json={
        "locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
        "costing": "auto", "units": "miles", "directions_type": "none",
    }, timeout=60)
    data = resp.json()
    if "trip" not in data:
        raise RuntimeError(data.get("error") or "No route found")
    trip = data["trip"]
    return {
        "minutes": round(trip["summary"]["time"] / 60, 1),
        "miles": round(trip["summary"]["length"], 1),
        "highway": trip["summary"].get("has_highway"), "toll": trip["summary"].get("has_toll"),
        "path": {"type": "LineString", "coordinates": [c for leg in trip["legs"] for c in _decode_polyline6(leg["shape"])]},
    }


def route(a, b):
    """a, b = (lat, lon). Returns {"there": leg, "back": leg}."""
    return {"there": _leg(a, b), "back": _leg(b, a)}


def isochrones(point, minutes=DEFAULT_MINUTES, reverse=False):
    """GeoJSON FeatureCollection of drive-time bands around point = (lat, lon).

    reverse=True gives the areas that can reach the point within each time (drive *to* the site).
    """
    minutes = sorted({int(m) for m in minutes if 0 < int(m) <= 120})[:8] or list(DEFAULT_MINUTES)
    features = []
    for i in range(0, len(minutes), MAX_CONTOURS):
        resp = SESSION.post(f"{VALHALLA}/isochrone", json={
            "locations": [{"lat": point[0], "lon": point[1]}], "costing": "auto",
            "contours": [{"time": m} for m in minutes[i:i + MAX_CONTOURS]], "polygons": True, "denoise": 0.3,
            "generalize": 120, "reverse": bool(reverse),
        }, timeout=90)
        data = resp.json()
        if "features" not in data:
            raise RuntimeError(data.get("error") or "No drive-time area")
        for f in data["features"]:
            features.append({"type": "Feature", "geometry": f["geometry"],
                             "properties": {"minutes": f["properties"].get("contour")}})
    features.sort(key=lambda f: -(f["properties"]["minutes"] or 0))  # largest first so small bands draw on top
    return {"type": "FeatureCollection", "features": features}
