"""Quick site plans (TestFit-style screening): zoning standards, a buildable envelope and an auto layout of
flex-industrial buildings with parking, checked against the zone's FAR, coverage and impervious limits.

Standards for Charles County, MD are transcribed from the County Code, Chapter 297 (Supp 15, Jul 2025) for
industrial uses (Permissible Use 7.00.000) unless noted. Everything is editable in the planner, so a rezoning
can be tested by picking another zone or typing other numbers. Screening only: confirm with the code and
the County before relying on it (bufferyards, forest conservation, stormwater and road corridor rules apply).
"""
import math

from shapely import make_valid
from shapely.affinity import rotate
from shapely.geometry import box

FT_PER_M = 3.280839895
SQFT_PER_M2 = FT_PER_M ** 2
SQFT_PER_ACRE = 43560

CHARLES_SOURCE = "Charles County Code ch. 297 (Supp 15, Jul 2025)"
# zone -> standards for industrial use. ft / ratios; None = no limit in the schedule.
STANDARDS = {
    "24017": {
        "IG": {"label": "IG · General Industrial", "front": 50, "side": 6, "rear": 10, "height": 36, "stories": 3,
               "far": 0.50, "isr": 0.75, "coverage": None, "open_space": None, "min_lot_ac": 1,
               "source": f"{CHARLES_SOURCE}, Fig. VI-6", "note": "ISR may rise to 0.85 with regional (off-site) stormwater."},
        "IH": {"label": "IH · Heavy Industrial", "front": 50, "side": 6, "rear": 10, "height": 50, "stories": 4,
               "far": 0.50, "isr": 0.60, "coverage": None, "open_space": None, "min_lot_ac": 3,
               "source": f"{CHARLES_SOURCE}, Fig. VI-6", "note": ""},
        "BP": {"label": "BP · Business Park", "front": 50, "side": 20, "rear": 25, "height": 60, "stories": 5,
               "far": 0.35, "isr": 0.70, "coverage": None, "open_space": None, "min_lot_ac": 2, "min_tract_ac": 10,
               "source": f"{CHARLES_SOURCE}, Fig. VI-5", "note": "Flex space (use 9.10.100) may reach 0.50 FAR. Setbacks apply to the BP tract perimeter."},
        "ABP": {"label": "ABP · Aviation Business Park", "front": 50, "side": 20, "rear": 25, "height": 36, "stories": 3,
                "far": 0.35, "isr": 0.70, "coverage": None, "open_space": None, "min_lot_ac": 2,
                "source": f"{CHARLES_SOURCE}, Fig. VI-5", "note": "Energetics permitted by right (per MEIH land review)."},
        "PEP": {"label": "PEP · Planned Employment & Industrial Park", "front": 75, "side": 40, "rear": 50, "height": 40,
                "stories": 3, "far": 0.40, "isr": 0.60, "coverage": None, "open_space": None, "min_lot_ac": 3,
                "source": f"{CHARLES_SOURCE}, Fig. VII-4",
                "note": "In mixed-use / employment areas of an adopted subarea plan: 30 / 6 / 10 ft and ISR 0.85."},
        "CC": {"label": "CC · Community Commercial", "front": 30, "side": 6, "rear": 10, "height": 40, "stories": 3,
               "far": 0.40, "isr": 0.85, "coverage": None, "open_space": None, "min_lot_ac": 1, "min_tract_ac": 5,
               "source": f"{CHARLES_SOURCE}, Fig. VI-5", "note": ""},
        "CB": {"label": "CB · Central Business", "front": 25, "side": 6, "rear": 10, "height": 60, "stories": 5,
               "far": 0.60, "isr": 0.90, "coverage": None, "open_space": None, "min_lot_ac": 3,
               "source": f"{CHARLES_SOURCE}, Fig. VI-5", "note": ""},
        "CN": {"label": "CN · Neighborhood Commercial", "front": 30, "side": 6, "rear": 10, "height": 36, "stories": 3,
               "far": 0.30, "isr": 0.80, "coverage": None, "open_space": None, "min_lot_ac": None,
               "source": f"{CHARLES_SOURCE}, Fig. VI-5", "note": ""},
        "WCD": {"label": "WCD · Watershed Conservation District", "front": 75, "side": 40, "rear": 50, "height": 40,
                "stories": 3, "far": None, "isr": 0.08, "coverage": 0.10, "open_space": None, "min_lot_ac": 3,
                "source": f"{CHARLES_SOURCE}, Fig. VI-10 (all other permitted uses)",
                "note": "8% impervious and 10% coverage: industrial needs a rezoning."},
        "AC": {"label": "AC · Agricultural Conservation", "front": 75, "side": 40, "rear": 50, "height": 40, "stories": 3,
               "far": None, "isr": None, "coverage": None, "open_space": None, "min_lot_ac": 3,
               "source": f"{CHARLES_SOURCE}, Fig. VI-1 (all other permitted uses)",
               "note": "Industrial uses mostly not permitted: a rezoning scenario."},
        "RC": {"label": "RC · Rural Conservation", "front": 50, "side": 30, "rear": 50, "height": 36, "stories": 3,
               "far": 0.20, "isr": None, "coverage": None, "open_space": None, "min_lot_ac": 3,
               "source": f"{CHARLES_SOURCE}, Fig. VI-2 (institutional/utilities)",
               "note": "Industrial uses not permitted: a rezoning scenario."},
    },
}
NO_ZONING = {"label": "No zoning (edit to match deed restrictions / city code)", "front": 50, "side": 25, "rear": 25,
             "height": 45, "stories": 2, "far": None, "isr": 0.70, "coverage": None, "open_space": None,
             "min_lot_ac": None, "source": "ACMI screening default (unincorporated Texas has no county zoning)",
             "note": "Texas counties have no zoning outside city limits; county subdivision, fire code and any deed "
                     "restrictions still apply. Numbers are a starting point: edit them."}
# Parking: Charles County Code Fig. XX-1 (uses 7.01.100, 7.02.200, 9.01.000): 1 space per 300 sf of office and
# display area + 1 per employee at maximum shift.
CODE_PARKING = {"office_sf_per_space": 300, "per_employee": 1,
                "source": "Charles County Code ch. 297 Fig. XX-1: 1 space / 300 sf office + 1 / employee (max shift)"}

# Building types for flex industrial. Defaults are ACMI screening assumptions (edit per deal).
BUILDING_TYPES = {
    "warehouse": {"label": "Warehouse / distribution", "depth": 300, "max_length": 800, "stories": 1, "court": 185,
                  "office_pct": 5, "emp_per_ksf": 0.3, "market_ratio": 0.5, "color": "#6d7a8a"},
    "manufacturing": {"label": "Manufacturing", "depth": 240, "max_length": 600, "stories": 1, "court": 150,
                      "office_pct": 10, "emp_per_ksf": 1.0, "market_ratio": 1.5, "color": "#c0603a"},
    "flex": {"label": "Flex / R&D", "depth": 160, "max_length": 450, "stories": 1, "court": 120,
             "office_pct": 25, "emp_per_ksf": 1.5, "market_ratio": 2.5, "color": "#3a7bd5"},
    "office": {"label": "Office", "depth": 120, "max_length": 240, "stories": 2, "court": 130,
               "office_pct": 100, "emp_per_ksf": 3.3, "market_ratio": 3.5, "color": "#7b4fd6"},
    "lab": {"label": "Lab / energetics process", "depth": 120, "max_length": 300, "stories": 1, "court": 140,
            "office_pct": 20, "emp_per_ksf": 1.0, "market_ratio": 2.0, "color": "#d6a21e"},
}
STALL_SF = 350          # one stall incl. its share of aisles
COURT_PAVED_SHARE = 0.85  # how much of a truck court / drive band ends up paved


def standards_for(fips, zone_code):
    zone = (STANDARDS.get(fips) or {}).get((zone_code or "").upper())
    if zone:
        return {"code": zone_code.upper(), **zone}
    return {"code": "", **NO_ZONING}


def zone_choices(fips):
    return [{"code": c, **v} for c, v in (STANDARDS.get(fips) or {}).items()]


def parking_required(btype, gfa_sf, mode="market", ratio=None, office_pct=None, emp_per_ksf=None):
    """Stalls for one building. mode 'market' = ratio per 1,000 sf; 'code' = Charles County formula."""
    t = BUILDING_TYPES.get(btype, BUILDING_TYPES["flex"])
    if mode == "code":
        office = gfa_sf * (t["office_pct"] if office_pct is None else office_pct) / 100
        emp = gfa_sf / 1000 * (t["emp_per_ksf"] if emp_per_ksf is None else emp_per_ksf)
        return math.ceil(office / CODE_PARKING["office_sf_per_space"] + emp * CODE_PARKING["per_employee"])
    r = t["market_ratio"] if ratio is None else ratio
    return math.ceil(gfa_sf / 1000 * r)


def envelope(site_m, setback_ft, exclude_m=None):
    """Buildable area: the site pulled in by the perimeter setback, minus excluded areas (flood, wetlands)."""
    env = site_m.buffer(-setback_ft / FT_PER_M, join_style=2)
    if exclude_m is not None and not exclude_m.is_empty:
        env = env.difference(exclude_m.buffer(15 / FT_PER_M))
    env = env if env.is_valid else make_valid(env)
    return env


def _main_angle(geom):
    """Angle (degrees) of the longest side of the minimum rotated rectangle: buildings run along it."""
    mrr = geom.minimum_rotated_rectangle
    if mrr.geom_type != "Polygon":
        return 0.0
    pts = list(mrr.exterior.coords)
    best = max(range(4), key=lambda i: math.dist(pts[i], pts[i + 1]))
    (x1, y1), (x2, y2) = pts[best], pts[best + 1]
    return math.degrees(math.atan2(y2 - y1, x2 - x1))


def layout(site_m, std, program, setback_ft=None, exclude_m=None, parking_mode="market", stories_override=None):
    """Lay buildings out in rows inside the envelope. program: [{type, count?, sf?, ratio?, office_pct?, emp_per_ksf?}].

    Each row is a building band (depth) followed by a court band (truck court / parking / drive). Buildings are
    added while the zone's FAR, coverage and impervious limits allow. Returns (shapes in metres, summary).
    """
    setback = setback_ft if setback_ft is not None else max(std.get("front") or 0, std.get("side") or 0,
                                                            std.get("rear") or 0)
    site_area_sf = site_m.area * SQFT_PER_M2
    env = envelope(site_m, setback, exclude_m)
    if env.is_empty:
        return {"envelope": env, "buildings": [], "parking": []}, _summary(site_area_sf, 0, std, [], [], 0, setback)
    angle = _main_angle(env)
    origin = env.centroid
    frame = rotate(env, -angle, origin=origin)  # work with buildings running along the x axis
    minx, miny, maxx, maxy = frame.bounds
    far, cov, isr = std.get("far"), std.get("coverage"), std.get("isr")
    placed, courts = [], []
    footprint = gfa = paved = 0.0
    y = miny
    queue = []
    for item in program:
        t = BUILDING_TYPES.get(item.get("type"), BUILDING_TYPES["flex"])
        count = int(item.get("count") or 0)  # 0 = as many as fit
        queue.append({**item, "t": t, "left": count if count > 0 else 10 ** 6,
                      "target_sf": float(item.get("sf") or 0)})
    qi = 0
    while y < maxy and qi < len(queue):
        q = queue[qi]
        t = q["t"]
        depth = float(q.get("depth") or t["depth"]) / FT_PER_M
        court = float(q.get("court") or t["court"]) / FT_PER_M
        max_len = float(q.get("max_length") or t["max_length"]) / FT_PER_M
        stories = int(stories_override or q.get("stories") or t["stories"])
        band = box(minx, y, maxx, y + depth)
        row = band.intersection(frame)
        row_added = False
        if not row.is_empty:
            for piece in getattr(row, "geoms", [row]):
                px0, _, px1, _ = piece.bounds
                x = px0
                while x + 30 / FT_PER_M < px1 and q["left"] > 0:
                    length = min(max_len, px1 - x)
                    if q["target_sf"]:
                        built = sum(b["gfa"] for b in placed if b["type"] == q.get("type"))
                        remaining = q["target_sf"] - built
                        if remaining <= 0:
                            q["left"] = 0
                            break
                        length = min(length, remaining / stories / (depth * SQFT_PER_M2))
                    if length * FT_PER_M < 80:
                        break
                    # Try the full length, then shorter buildings; if none fit here, slide along 50 ft.
                    rect = None
                    for share in (1.0, 0.75, 0.5, 0.35):
                        trial_len = length * share
                        if trial_len * FT_PER_M < 150 and share < 1.0:
                            break
                        trial = box(x, y, x + trial_len, y + depth)
                        if frame.contains(trial.buffer(-0.01)):
                            rect, length = trial, trial_len
                            break
                    if rect is None:
                        x += 50 / FT_PER_M
                        continue
                    if rect is not None:
                        b_fp = rect.area * SQFT_PER_M2
                        b_gfa = b_fp * stories
                        stalls = parking_required(q.get("type"), b_gfa, parking_mode, q.get("ratio"),
                                                  q.get("office_pct"), q.get("emp_per_ksf"))
                        court_rect = box(x, y + depth, x + length, y + depth + court).intersection(frame)
                        court_paved = court_rect.area * SQFT_PER_M2 * COURT_PAVED_SHARE
                        add_paved = max(court_paved, stalls * STALL_SF)
                        ok = True
                        if far is not None and (gfa + b_gfa) / site_area_sf > far + 1e-9:
                            ok = False
                        if cov is not None and (footprint + b_fp) / site_area_sf > cov + 1e-9:
                            ok = False
                        if isr is not None and (footprint + b_fp + paved + add_paved) / site_area_sf > isr + 1e-9:
                            ok = False
                        if not ok:
                            q["left"] = 0  # limits reached for this type; later (smaller) types may still fit
                            break
                        placed.append({"geom": rect, "type": q.get("type"), "label": t["label"], "stories": stories,
                                       "footprint": b_fp, "gfa": b_gfa, "stalls": stalls, "color": t["color"]})
                        courts.append(court_rect)
                        footprint += b_fp
                        gfa += b_gfa
                        paved += add_paved
                        q["left"] -= 1
                        row_added = True
                    x += length + 60 / FT_PER_M  # 60 ft fire lane / side yard between buildings
        y += depth + court if row_added else 20 / FT_PER_M
        if q["left"] <= 0:
            qi += 1
    back = lambda g: rotate(g, angle, origin=origin)  # noqa: E731
    shapes = {
        "envelope": env,
        "buildings": [{**{k: v for k, v in b.items() if k != "geom"}, "geom": back(b["geom"])} for b in placed],
        "parking": [back(c) for c in courts if not c.is_empty],
    }
    return shapes, _summary(site_area_sf, env.area * SQFT_PER_M2, std, placed, courts, paved, setback)


def _summary(site_sf, env_sf, std, placed, courts, paved_sf, setback):
    footprint = sum(b["footprint"] for b in placed)
    gfa = sum(b["gfa"] for b in placed)
    stalls = sum(b["stalls"] for b in placed)
    by_type = {}
    for b in placed:
        row = by_type.setdefault(b["type"], {"label": b["label"], "buildings": 0, "gfa": 0, "stalls": 0})
        row["buildings"] += 1
        row["gfa"] += b["gfa"]
        row["stalls"] += b["stalls"]
    impervious = footprint + paved_sf
    checks = []
    for key, value, limit, label in (("far", gfa / site_sf if site_sf else 0, std.get("far"), "FAR"),
                                     ("coverage", footprint / site_sf if site_sf else 0, std.get("coverage"), "Lot coverage"),
                                     ("isr", impervious / site_sf if site_sf else 0, std.get("isr"), "Impervious (ISR)")):
        checks.append({"key": key, "label": label, "value": round(value, 3), "limit": limit,
                       "ok": limit is None or value <= limit + 1e-9})
    return {
        "site_acres": round(site_sf / SQFT_PER_ACRE, 1), "envelope_acres": round(env_sf / SQFT_PER_ACRE, 1),
        "setback_ft": setback, "buildings": len(placed), "footprint_sf": round(footprint), "gfa_sf": round(gfa),
        "stalls": stalls, "paved_sf": round(paved_sf), "impervious_sf": round(impervious),
        "gfa_per_acre": round(gfa / (site_sf / SQFT_PER_ACRE)) if site_sf else 0,
        "by_type": list(by_type.values()), "checks": checks,
        "max_gfa_by_far": round(std["far"] * site_sf) if std.get("far") else None,
    }
