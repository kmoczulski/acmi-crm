"""Explosives quantity-distance (QD) screening, ported from "QD CALCS v20 - ACMI Site Screen.xlsx".

DoD 6055.09-M (DESB standards), Volume 3, Enclosure 3, English units (feet, pounds). Gives IMD, ILD, PTRD
and IBD for HD 1.1, 1.2.1, 1.2.2, 1.2.3, 1.3 and 1.4, then lays buildings out on a parcel (fit) and checks
a layout (evaluate): IBD arcs on the property, and separation between buildings.

Screening only, not an explosives safety submission: site plans need DDESB review.
"""
import math

from shapely import make_valid
from shapely.geometry import Point
from shapely.ops import transform, unary_union

FT_PER_M = 3.280839895
SQFT_PER_ACRE = 43560

HAZARD_DIVISIONS = ["HD 1.1", "HD 1.2.1", "HD 1.2.2", "HD 1.2.3", "HD 1.3", "HD 1.4"]
PES_TYPES = ["Open (no structure)", "Aboveground structure", "ECM Front", "ECM Side", "ECM Rear"]
# The site-screen sheet's single "Aboveground or Open" row uses the Structure column of the hazardous
# fragment table; colleagues' plans use the Open column for small open-air quantities (5 lb -> 419 ft).
PES_ALIASES = {"Aboveground or Open": "Aboveground structure"}
BUILDING_KINDS = {"magazine": "Magazine / storage (IMD)", "operating": "Operating building (ILD)"}

# Regulatory constants (sheet section 4d).
IBD_FRAG_FLOOR = 1250
PTRD_FRAG_FLOOR = 750
ILD_MIN_UNBARRICADED = 66
ILD_MIN_BARRICADED = 33
ILD_K_UNBARRICADED = 18
ILD_K_BARRICADED = 9
IBD_K_LARGE = 50
HD121_IMD = 300
HD122_IMD = 100
HD123_IM_K = 11


def _cbrt(w):
    return w ** (1 / 3)


def _hd11_table(w):
    """Section 4b: per PES orientation, IBD/PTRD governing and ILD (unbarricaded, barricaded)."""
    ln = math.log(w) if w > 0 else 0
    c = _cbrt(w)

    def ibd_blast(k_small, a, b):
        return k_small * c if w < 100000 else (a * w ** b if w < 250001 else IBD_K_LARGE * c)

    rows = {}
    # Aboveground or open.
    blast = ibd_blast(40, 2.42, 0.577)
    frag = 200 if w < 32 else (389 * ln - 1133.9 if w < 451 else (IBD_FRAG_FLOOR if w < 30445 else 0))
    pfrag = 120 if w < 32 else (0.6 * (389 * ln - 1133.9) if w < 451 else (PTRD_FRAG_FLOOR if w < 30396 else 0))
    rows["Aboveground structure"] = {
        "ibd_blast": blast, "ibd_frag": frag, "ptrd_blast": 0.6 * blast, "ptrd_frag": pfrag,
        "ild_unbarricaded": max(ILD_MIN_UNBARRICADED, ILD_K_UNBARRICADED * c),
        "ild_barricaded": max(ILD_MIN_BARRICADED, ILD_K_BARRICADED * c),
    }
    # Open (no structure): hazardous fragment distance, Table V3.E3.T2 "Open" column, for 450 lb and under.
    open_row = dict(rows["Aboveground structure"])
    if w < 451:
        hfd = 236 if w < 0.51 else (79.2 * ln + 291.3 if w < 100 else 389 * ln - 1133.9)
        open_row.update(ibd_frag=hfd, ptrd_frag=0.6 * hfd)
    rows["Open (no structure)"] = open_row
    # Earth-covered magazine: front.
    blast = ibd_blast(35, 0.3955, 0.7227)
    if w < 300001:
        bar = 10 * c
    elif w < 500001:
        bar = (13.659 - 1.6479e-05 * w + 1.4358e-11 * w ** 2) * c
    else:
        bar = 9 * c
    rows["ECM Front"] = {
        "ibd_blast": blast, "ibd_frag": 500 if w < 151 else (700 if w < 451 else (IBD_FRAG_FLOOR if w < 45436 else 0)),
        "ptrd_blast": 0.6 * blast, "ptrd_frag": 300 if w < 151 else (420 if w < 451 else (PTRD_FRAG_FLOOR if w < 45554 else 0)),
        "ild_unbarricaded": max(ILD_MIN_UNBARRICADED, 18 * c), "ild_barricaded": max(ILD_MIN_BARRICADED, bar),
    }
    # ECM side.
    if w < 300000:
        unb, bar = 16 * c, 7 * c
    elif w < 400001:
        unb, bar = (9.9683 + 2.0135e-05 * w) * c, (1.0848 + 1.986e-05 * w) * c
    else:
        unb, bar = 18 * c, 9 * c
    rows["ECM Side"] = {
        "ibd_blast": blast, "ibd_frag": 250 if w < 451 else (IBD_FRAG_FLOOR if w < 45436 else 0),
        "ptrd_blast": 0.6 * blast, "ptrd_frag": 150 if w < 451 else (PTRD_FRAG_FLOOR if w < 45554 else 0),
        "ild_unbarricaded": max(ILD_MIN_UNBARRICADED, unb), "ild_barricaded": max(ILD_MIN_BARRICADED, bar),
    }
    # ECM rear.
    blast = 25 * c if w < 100000 else (0.004125 * w ** 1.0898 if w < 250001 else IBD_K_LARGE * c)
    if w < 100000:
        unb = 12 * c
    elif w < 300001:
        unb = (11.521 + 1.9918e-06 * w + 2.0947e-11 * w ** 2) * c
    elif w < 400001:
        unb = (1.9389 + 4.0227e-05 * w) * c
    else:
        unb = 18 * c
    if w < 300000:
        bar = 6 * c
    elif w < 400001:
        bar = (-3.059 + 3.0228e-05 * w) * c
    else:
        bar = 9 * c
    rows["ECM Rear"] = {
        "ibd_blast": blast, "ibd_frag": 250 if w < 451 else (IBD_FRAG_FLOOR if w < 107027 else 0),
        "ptrd_blast": 0.6 * blast, "ptrd_frag": 150 if w < 451 else (PTRD_FRAG_FLOOR if w < 106974 else 0),
        "ild_unbarricaded": max(ILD_MIN_UNBARRICADED, unb), "ild_barricaded": max(ILD_MIN_BARRICADED, bar),
    }
    for r in rows.values():
        r["ibd"] = max(r["ibd_blast"], r["ibd_frag"])
        r["ptrd"] = max(r["ptrd_blast"], r["ptrd_frag"])
    return rows


def distances(hd="HD 1.1", newqd=0.0, pes="Aboveground structure", barricaded=False, mce=0.0, imd_k=11.0):
    """{"imd", "ild", "ptrd", "ibd"} in feet, plus "governing" and "range" notes (sheet sections 2-3)."""
    w = float(newqd or 0)
    mce = float(mce or 0)
    if hd not in HAZARD_DIVISIONS:
        raise ValueError(f"Unknown hazard division {hd!r}")
    pes = PES_ALIASES.get(pes, pes)
    if pes not in PES_TYPES:
        pes = "Aboveground structure"
    if w <= 0:
        return {"imd": 0, "ild": 0, "ptrd": 0, "ibd": 0, "governing": "", "range": "Enter a NEWQD greater than zero."}
    ln = math.log(w)
    lnm = math.log(mce) if mce > 0 else 0
    governing = f"Distance taken directly from the DoD 6055.09-M table for {hd}."
    if hd == "HD 1.1":
        row = _hd11_table(w)[pes]
        out = {"imd": float(imd_k) * _cbrt(w),
               "ild": row["ild_barricaded"] if barricaded else row["ild_unbarricaded"],
               "ptrd": row["ptrd"], "ibd": row["ibd"]}
        governing = (f"Fragment distance governs IBD - the {IBD_FRAG_FLOOR:,} ft floor applies."
                     if row["ibd_frag"] >= row["ibd_blast"] else "Blast overpressure governs IBD - K-factor scaling.")
    elif hd == "HD 1.2.1":
        from_new = 200 if w < 72 else 237.559 * ln - 735.186 - 4.274 * ln ** 2
        from_mce = 200 if mce < 32 else (389 * lnm - 1133.9 if mce < 451 else 1250)
        ibd = max(from_new, from_mce)
        out = {"imd": HD121_IMD, "ild": 200 if ibd < 555.5 else 0.36 * ibd, "ptrd": 200 if ibd < 333.3 else 0.6 * ibd,
               "ibd": ibd}
    elif hd == "HD 1.2.2":
        ibd = 100 if w < 20 else 101.649 - 15.934 * ln + 5.173 * ln ** 2
        out = {"imd": HD122_IMD, "ild": 100 if ibd < 277.8 else 0.36 * ibd, "ptrd": 100 if ibd < 166.7 else 0.6 * ibd,
               "ibd": ibd}
    elif hd == "HD 1.2.3":
        if w < 96001:
            from_new = math.exp(2.47 + 0.2368 * ln + 0.00384 * ln ** 2)
        elif w < 1000001:
            from_new = math.exp(7.2297 - 0.5984 * ln + 0.04046 * ln ** 2)
        else:
            from_new = 8 * _cbrt(w)
        from_mce = 200 if mce < 32 else (389 * lnm - 1133.9 if mce < 451 else 1250)
        ibd = max(from_new, from_mce)
        out = {"imd": HD123_IM_K * _cbrt(mce), "ild": 0.36 * ibd, "ptrd": 0.6 * ibd, "ibd": ibd}
    elif hd == "HD 1.3":
        if w < 1006:
            agm = 50
        elif w < 84001:
            agm = math.exp(2.0325 + 0.2488 * ln + 0.00313 * ln ** 2)
        elif w < 1000001:
            agm = math.exp(4.338 - 0.1695 * ln + 0.0221 * ln ** 2)
        else:
            agm = 5 * _cbrt(w)
        if w < 1079:
            ib = 75
        elif w < 96001:
            ib = math.exp(2.47 + 0.2368 * ln + 0.00384 * ln ** 2)
        elif w < 1000001:
            ib = math.exp(7.2297 - 0.5984 * ln + 0.04046 * ln ** 2)
        else:
            ib = 8 * _cbrt(w)
        out = {"imd": agm, "ild": agm, "ptrd": ib, "ibd": ib}
    else:  # HD 1.4
        small = w < 3000
        out = {"imd": 50 if small else 100, "ild": 50 if small else 100, "ptrd": 75 if small else 100,
               "ibd": 75 if small else 100}
    out = {k: round(v, 1) for k, v in out.items()}
    out["governing"] = governing
    out["range"] = ("NEWQD exceeds 500,000 lb - beyond the published HD 1.1 table range. K50 extrapolation shown; "
                    "confirm with DDESB.") if w > 500000 else "NEWQD is within the published table range."
    return out


def arc_acres(radius_ft):
    return math.pi * radius_ft ** 2 / SQFT_PER_ACRE


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def dimensions_ft(b):
    """(length, width) in feet: the building's floor area as a square, or length x (area / length) when a length
    is given. (0, 0) if no area."""
    sqft = _num(b.get("sqft"))
    if sqft <= 0:
        return 0.0, 0.0
    length = _num(b.get("length"))
    if length <= 0:
        return math.sqrt(sqft), math.sqrt(sqft)
    return length, sqft / length


def footprint(b, x, y):
    """Footprint (metres) of a building's floor area centred on x, y; a point if no area is given.

    Length runs east-west, turned clockwise by "rotation" degrees.
    """
    length, width = dimensions_ft(b)
    if length <= 0:
        return Point(x, y)
    from shapely.affinity import rotate
    from shapely.geometry import box

    hl, hw = length / 2 / FT_PER_M, width / 2 / FT_PER_M
    rect = box(x - hl, y - hw, x + hl, y + hw)
    angle = _num(b.get("rotation"))
    return rotate(rect, -angle, origin=(x, y)) if angle else rect


def _reach_m(b):
    """Centre-to-corner distance of the footprint, metres: the most it can stick out in any direction."""
    length, width = dimensions_ft(b)
    return math.hypot(length, width) / 2 / FT_PER_M


def arcs_m(b, x, y):
    """{distance name: polygon in metres}: each distance measured out from the building's footprint."""
    fp = footprint(b, x, y)
    return {k: fp.buffer(b["dist"][k] / FT_PER_M, 64) for k in ("ibd", "ptrd", "ild", "imd") if b["dist"].get(k)}


def building_distances(b):
    """Distances for one building dict from the UI (keys: hd, newqd, pes, barricaded, mce, k)."""
    d = distances(b.get("hd", "HD 1.1"), b.get("newqd", 0), b.get("pes", PES_TYPES[0]),
                  str(b.get("barricaded", "")).lower() in ("1", "true", "yes", "on"), b.get("mce", 0), b.get("k", 11))
    d["separation"] = d["imd"] if b.get("kind", "magazine") == "magazine" else d["ild"]
    d["ibd_acres"] = round(arc_acres(d["ibd"]), 1)
    return d


# ---------------------------------------------------------------------------
# Layout on a parcel. Geometry is in a local metric projection supplied by the caller.
# ---------------------------------------------------------------------------


MAX_NEWQD = 500000  # top of the published HD 1.1 table


def max_newqd(b, edge_ft, gaps):
    """Largest NEWQD (lb) for building b where it stands: its IBD arc stays on the property (edge_ft from the
    footprint to the property line; None = no parcel) and its own separation fits each neighbour.

    gaps: [(name, edge-to-edge ft)]. Returns (pounds, what limits it). Distances grow with weight, so this scans
    up a log scale and then narrows the last step down; 0 means even 1 lb doesn't fit.
    """
    def limit(w):
        d = building_distances({**b, "newqd": w})
        if edge_ft is not None and d["ibd"] > edge_ft:
            return "IBD to the property line"
        for name, have in gaps:
            if d["separation"] > have:
                return f"spacing to {name}"
        return None

    if edge_ft is None and not gaps:
        return None, ""
    if limit(1):
        return 0, limit(1)
    lo, hi, steps = 1.0, None, 120
    for k in range(1, steps + 1):
        w = MAX_NEWQD ** (k / steps)
        if limit(w):
            hi = w
            break
        lo = w
    if hi is None:
        return MAX_NEWQD, "table limit"
    for _ in range(30):
        mid = (lo + hi) / 2
        if limit(mid):
            hi = mid
        else:
            lo = mid
    step = 1 if lo < 100 else 10
    return int(lo // step * step), limit(hi)


def evaluate(parcel_m, buildings, positions_m, with_max=False):
    """Check a layout. parcel_m: shapely polygon (metres) or None. positions_m: [(x, y)] per building centre.

    Distances run from each building's footprint edge. Returns per-building results and a summary: IBD arc
    acres off the parcel, spacing conflicts between buildings, and how much of the parcel the arcs encumber.
    with_max adds each building's largest NEWQD where it stands ("max_newqd", "max_by").
    """
    results, arcs = [], []
    prints = [footprint(b, x, y) for b, (x, y) in zip(buildings, positions_m)]
    for b, fp in zip(buildings, prints):
        ibd = fp.buffer(b["dist"]["ibd"] / FT_PER_M, 64) if b["dist"].get("ibd") else None
        off = ibd.difference(parcel_m).area if (parcel_m is not None and ibd is not None) else 0
        if ibd is not None:
            arcs.append(ibd)
        inside = parcel_m.contains(fp) if parcel_m is not None else True
        edge_ft = parcel_m.boundary.distance(fp) * FT_PER_M if parcel_m is not None else None
        results.append({"off_site_acres": round(off * FT_PER_M ** 2 / SQFT_PER_ACRE, 1),
                        "edge_ft": round(edge_ft) if edge_ft is not None else None, "inside": inside,
                        "ibd_clear": inside and off * FT_PER_M ** 2 / SQFT_PER_ACRE < 1, "conflicts": []})
    for i in range(len(buildings)):
        for j in range(i + 1, len(buildings)):
            need = max(buildings[i]["dist"]["separation"], buildings[j]["dist"]["separation"])
            if need <= 0:
                continue
            have = prints[i].distance(prints[j]) * FT_PER_M
            if have < need:
                note = {"with": j, "need_ft": round(need), "have_ft": round(have)}
                results[i]["conflicts"].append(note)
                results[j]["conflicts"].append({**note, "with": i})
    for i, (b, r) in enumerate(zip(buildings, results) if with_max else ()):
        if not r["inside"]:
            r["max_newqd"], r["max_by"] = None, "building is off the parcel"
            continue
        gaps = [(buildings[j].get("name") or f"#{j + 1}", prints[i].distance(prints[j]) * FT_PER_M)
                for j in range(len(buildings)) if j != i]
        r["max_newqd"], r["max_by"] = max_newqd(b, r["edge_ft"], gaps)
    union = unary_union(arcs) if arcs else None
    to_ac = FT_PER_M ** 2 / SQFT_PER_ACRE
    summary = {"arc_acres": round(union.area * to_ac, 1) if union else 0}
    if parcel_m is not None:
        site = parcel_m.area * to_ac
        on = union.intersection(parcel_m).area * to_ac if union else 0
        summary.update(parcel_acres=round(site, 1), encumbered_acres=round(on, 1),
                       off_site_acres=round(summary["arc_acres"] - on, 1),
                       unencumbered_acres=round(max(0, site - on), 1),
                       encumbered_pct=round(100 * on / site, 1) if site else 0)
    summary["all_clear"] = all(r["ibd_clear"] and not r["conflicts"] for r in results)
    return results, summary


def fit(parcel_m, buildings, step_m=None):
    """Place buildings inside the parcel: each IBD arc on the property where possible, buildings at least
    their separation apart. Largest IBD first, each as deep inside the remaining room as possible.

    Returns [(x, y)] in the same metric projection.
    """
    parcel_m = parcel_m if parcel_m.is_valid else make_valid(parcel_m)
    order = sorted(range(len(buildings)), key=lambda i: -buildings[i]["dist"]["ibd"])
    minx, miny, maxx, maxy = parcel_m.bounds
    step = step_m or max(5.0, min(maxx - minx, maxy - miny) / 60)
    grid = []
    y = miny + step / 2
    while y < maxy:
        x = minx + step / 2
        while x < maxx:
            p = Point(x, y)
            if parcel_m.contains(p):
                grid.append((x, y, parcel_m.boundary.distance(p)))
            x += step
        y += step
    if not grid:
        c = parcel_m.representative_point()
        return [(c.x, c.y)] * len(buildings)
    placed = {}
    for i in order:
        d = buildings[i]["dist"]
        reach = _reach_m(buildings[i])
        # Centre-to-boundary room needed for the IBD arc, allowing for the footprint's corners.
        ibd_m = d["ibd"] / FT_PER_M + reach if d["ibd"] else reach

        def ok(pt):
            for j, (px, py) in placed.items():
                sep = max(d["separation"], buildings[j]["dist"]["separation"]) / FT_PER_M
                need = sep + reach + _reach_m(buildings[j])  # edge to edge, worst case for two rectangles
                if math.dist(pt[:2], (px, py)) < need:
                    return False
            return True

        candidates = [g for g in grid if ok(g)]
        if not candidates:
            candidates = grid  # no room: overlap is reported as a conflict
        # Prefer spots whose IBD arc stays on the parcel (edge distance >= IBD); among those, hug the
        # boundary least wastefully (closest to IBD), leaving the middle for the others. Otherwise go deepest.
        clear = [g for g in candidates if g[2] >= ibd_m]
        if clear:
            best = min(clear, key=lambda g: g[2])
        else:
            best = max(candidates, key=lambda g: g[2])
        placed[i] = best[:2]
    return [placed[i] for i in range(len(buildings))]


def projector(lat, lon):
    """(to_metres, to_wgs84) transforms for a local azimuthal equidistant projection centred on lat/lon."""
    from pyproj import Transformer

    crs = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m"
    fwd = Transformer.from_crs(4326, crs, always_xy=True).transform
    inv = Transformer.from_crs(crs, 4326, always_xy=True).transform
    return (lambda g: transform(fwd, g)), fwd, inv
