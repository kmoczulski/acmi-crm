"""One auto-fit planner for flex-industrial buildings AND explosives (hazard) buildings on the same site.

siteplan.py lays out warehouses / manufacturing / flex under a zone's setbacks, FAR, coverage and impervious limits;
qd.py sizes and places magazines and operating buildings so their quantity-distance arcs stay legal. This module
runs both on one program list:

1. Hazard buildings (type "magazine" = IMD spacing, "operating" = ILD spacing) go first, inside the zoning envelope
   (setbacks, flood / wetland exclusion), each deep enough that its IBD arc stays on the property and at least its
   separation from the other hazard buildings.
2. Flex-industrial buildings then fill what is left. Every hazard building's arc is carved out of the buildable
   area for each flex building, by the distance that building is exposed at (default IBD: an unrelated inhabited
   building; "ptrd", "ild" or "none" per program line). The hazard buildings count toward FAR, coverage and ISR.
3. Each line reports what limited it (zoning, QD arc, flood / wetland exclusion, FAR / coverage / ISR), and each
   hazard building gets its Max NEWQD given the final layout.

Screening only, not an explosives safety submission: site plans need DDESB review. 360-degree arcs (ECM arcs are
really directional); zoning numbers are as good as the zoning file they came from.
"""
import math

from shapely import make_valid
from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.prepared import prep

from . import qd, siteplan

FT = qd.FT_PER_M
HAZARD_TYPES = {"magazine": "Magazine / storage (IMD)", "operating": "Operating building (ILD)"}
EXPOSURES = {"ibd": "IBD (unrelated / public)", "ptrd": "PTRD (public road)", "ild": "ILD (inside the operation)",
             "none": "No QD distance"}
DEFAULT_EXPOSURE = {"lab": "ild"}  # everything else is an unrelated inhabited building until told otherwise
HAZARD_HEIGHT_FT = 20              # one-story magazine / operating bay, for the zone height check
DISCLAIMER = ("Screening only, not a DDESB explosives safety submission: DoD 6055.09-M V3.E3 distances as 360-degree "
              "arcs, zoning from the zoning file (confirm with the code), no road, utility or stormwater design.")


def _grid(env, step):
    minx, miny, maxx, maxy = env.bounds
    pts = []
    y = miny + step / 2
    while y < maxy:
        x = minx + step / 2
        while x < maxx:
            pts.append((x, y))
            x += step
        y += step
    return pts


def _hazard_dict(item, i):
    kind = item["type"]
    b = {k: item.get(k) for k in ("sqft", "length", "rotation", "hd", "newqd", "pes", "barricaded", "mce", "k")
         if item.get(k) is not None}
    b.setdefault("hd", "HD 1.1")
    b.setdefault("pes", "ECM Front" if kind == "magazine" else "Aboveground structure")
    b.setdefault("sqft", 1000 if kind == "magazine" else 10000)
    b.setdefault("newqd", 10000 if kind == "magazine" else 500)
    b.setdefault("k", 6 if kind == "magazine" else 11)
    b["kind"] = kind
    b["name"] = item.get("name") or f"{'Magazine' if kind == 'magazine' else 'Operating'} {i + 1}"
    b["dist"] = qd.building_distances(b)
    return b


def _candidates(env, env_prep, b, boundary, step, placed):
    """[(x, y, edge_m, footprint)] where b's footprint sits wholly inside env and clears the hazard buildings placed.

    edge_m is the distance from the footprint to the property line (arcs are measured from the footprint edge).
    The second list is the same without the spacing test, for reporting how much room the envelope has at all.
    """
    open_, spaced = [], []
    for x, y in _grid(env, step):
        if not env_prep.contains(Point(x, y)):
            continue
        fp = qd.footprint(b, x, y)
        if not env_prep.contains(fp):
            continue
        cand = (x, y, boundary.distance(fp), fp)
        open_.append(cand)
        if all(fp.distance(pfp) * FT >= max(b["dist"]["separation"], pb["dist"]["separation"])
               for pb, pfp in placed):
            spaced.append(cand)
    return open_, spaced


def plan(site_m, std, program, setback_ft=None, exclude_m=None, parking_mode="market", shrink=False, whole_m=None):
    """Place the whole program on site_m (shapely polygon, metres). program lines:

    flex: {"type": warehouse|manufacturing|flex|office|lab, count?, sf?, exposure?, height?, ...} as siteplan.layout.
    hazard: {"type": "magazine"|"operating", count?, name?, sqft?, hd?, newqd?, pes?, barricaded?, mce?, k?, height?}.
    whole_m: the whole property when site_m is only its largest piece (arcs are judged against all of it).
    shrink: lower a hazard building's NEWQD to the most it can hold where it stands when its arc won't fit.

    Returns a dict: envelope, flex / hazard buildings with their results, summary, limits, notes.
    """
    site_m = site_m if site_m.is_valid else make_valid(site_m)
    whole = whole_m if whole_m is not None else site_m
    boundary = whole.boundary
    setback = setback_ft if setback_ft is not None else max(std.get("front") or 0, std.get("side") or 0,
                                                            std.get("rear") or 0)
    env_zone = siteplan.envelope(site_m, setback)
    env = siteplan.envelope(site_m, setback, exclude_m)
    to_ac = siteplan.SQFT_PER_M2 / siteplan.SQFT_PER_ACRE
    site_ac = whole.area * to_ac
    losses = {"site_acres": round(site_ac, 1), "zoning_setback_acres": round(site_m.area * to_ac - env_zone.area * to_ac, 1),
              "exclusion_acres": round(env_zone.area * to_ac - env.area * to_ac, 1)}
    notes = []
    zone_h, zone_stories = std.get("height"), std.get("stories")

    # ---- program -> hazard buildings and flex lines --------------------------------------------------------
    hazards, flex_lines = [], []
    for item in program:
        t = item.get("type")
        if t in HAZARD_TYPES:
            for _ in range(max(1, int(item.get("count") or 1))):
                b = _hazard_dict(item, len(hazards))
                h_ft = float(item.get("height") or HAZARD_HEIGHT_FT)
                b["height_ft"] = h_ft
                if zone_h is not None and h_ft > zone_h:
                    b["height_ft"] = zone_h
                    notes.append(f"{b['name']}: height cut from {h_ft:g} to the zone's {zone_h:g} ft limit.")
                hazards.append(b)
        elif t in siteplan.BUILDING_TYPES:
            exposure = item.get("exposure") or DEFAULT_EXPOSURE.get(t, "ibd")
            if exposure not in EXPOSURES:
                exposure = "ibd"
            bt = siteplan.BUILDING_TYPES[t]
            stories = int(item.get("stories") or bt["stories"])
            if zone_stories is not None and stories > zone_stories:
                notes.append(f"{bt['label']}: {stories} stories cut to the zone's {zone_stories}.")
                stories = int(zone_stories)
            height = float(item.get("height") or bt.get("height") or 0)
            if zone_h is not None and height > zone_h:
                notes.append(f"{bt['label']}: {height:g} ft clear height cut to the zone's {zone_h:g} ft limit.")
                height = zone_h
            flex_lines.append({**item, "exposure": exposure, "stories": stories, "height_ft": height})

    # ---- 1. hazard buildings -------------------------------------------------------------------------------
    placed = []  # (building dict, footprint, position)
    out_hazard = [None] * len(hazards)
    env_prep = prep(env) if not env.is_empty else None
    zone_prep = prep(env_zone) if not env_zone.is_empty else None
    step = max(5.0, min(site_m.bounds[2] - site_m.bounds[0], site_m.bounds[3] - site_m.bounds[1]) / 50)
    order = sorted(range(len(hazards)), key=lambda i: -hazards[i]["dist"]["ibd"])
    for i in order:
        b = hazards[i]
        info = {"limits": []}
        out_hazard[i] = info
        if env_prep is None:
            info.update(placed=False)
            info["limits"].append({"kind": "zoning", "text": "No buildable area left inside the zoning setback"
                                   + (" and flood / wetland exclusion." if losses["exclusion_acres"] else ".")})
            continue
        open_, spaced = _candidates(env, env_prep, b, boundary, step, placed)
        if not open_:
            info.update(placed=False)
            kind = "exclusion" if (losses["exclusion_acres"] and zone_prep is not None
                                   and _candidates(env_zone, zone_prep, b, boundary, step * 2, [])[0]) else "zoning"
            info["limits"].append({"kind": kind, "text": "The footprint doesn't fit inside the "
                                   + ("zoning envelope once flood / wetland areas are removed." if kind == "exclusion"
                                      else f"zoning envelope ({setback:g} ft setback).")})
            continue
        pool = spaced or open_
        if not spaced:
            info["limits"].append({"kind": "qd", "text": "No spot keeps the required spacing to the other hazard "
                                                         "buildings; placed deepest, spacing conflict reported."})
        ibd_m = b["dist"]["ibd"] / FT
        clear = [c for c in pool if c[2] >= ibd_m]
        best = min(clear, key=lambda c: c[2]) if clear else max(pool, key=lambda c: c[2])
        if not clear and shrink:
            gaps = [(pb["name"], best[3].distance(pfp) * FT) for pb, pfp in placed]
            allowed, why = qd.max_newqd(b, best[2] * FT, gaps)
            if allowed and allowed < float(b["newqd"]):
                notes.append(f"{b['name']}: NEWQD cut from {float(b['newqd']):,.0f} to {allowed:,.0f} lb "
                             f"(limited by {why}).")
                b["newqd"] = allowed
                b["dist"] = qd.building_distances(b)
        info.update(placed=True, x=best[0], y=best[1], edge_m=best[2])
        placed.append((b, best[3]))
        b["_pos"] = (best[0], best[1])
        b["_fp"] = best[3]
        # Would the flood / wetland exclusion have bought more arc room? (deepest legal spot with and without it)
        if exclude_m is not None and zone_prep is not None and not exclude_m.is_empty:
            wide, _ = _candidates(env_zone, zone_prep, b, boundary, step * 2, [])
            if wide:
                deepest = max(c[2] for c in wide) * FT
                if deepest > best[2] * FT * 1.05 + 25:
                    info["limits"].append({"kind": "exclusion", "text": (
                        f"Deepest allowed spot is {best[2] * FT:,.0f} ft from the line, "
                        f"{deepest:,.0f} ft without the flood / wetland exclusion.")})

    # ---- 2. flex buildings around the arcs ---------------------------------------------------------------------
    on_site = [b for b in hazards if "_pos" in b]
    h_foot = sum(qd.footprint(b, *b["_pos"]).area for b in on_site) * siteplan.SQFT_PER_M2
    state = {"footprint": h_foot, "gfa": h_foot, "paved": 0.0}
    blocked = []
    arc_acres_in_env = {}
    for exp in ("ibd", "ptrd", "ild"):
        arcs = [b["_fp"].buffer(b["dist"][exp] / FT, 48) for b in on_site if b["dist"].get(exp)]
        arc_acres_in_env[exp] = unary_union(arcs) if arcs else None
    keepout = unary_union([b["_fp"].buffer(10 / FT) for b in on_site]) if on_site else None
    flex_out, flex_shapes, courts, all_placed = [], [], [], []
    base_excl = [g for g in (exclude_m,) if g is not None and not g.is_empty]
    for line in flex_lines:
        parts = list(base_excl) + list(blocked)
        arc = arc_acres_in_env.get(line["exposure"])
        if arc is not None:
            parts.append(arc)
        if keepout is not None:
            parts.append(keepout)
        excl = unary_union(parts) if parts else None
        shapes, summ = siteplan.layout(site_m, std, [line], setback_ft=setback, exclude_m=excl,
                                       parking_mode=parking_mode, stories_override=line["stories"], start=state)
        stop = summ["stops"][0] if summ.get("stops") else {"placed": 0, "reason": "no room"}
        for b in shapes["buildings"]:
            state["footprint"] += b["footprint"]
            state["gfa"] += b["gfa"]
            b["exposure"] = line["exposure"]
            b["height_ft"] = line["height_ft"]
            flex_shapes.append(b)
            all_placed.append(b)
            blocked.append(b["geom"])
        state["paved"] = summ["paved_sf"] if shapes["buildings"] else state["paved"]
        for c in shapes["parking"]:
            courts.append(c)
            blocked.append(c)
        limits = []
        asked = int(line.get("count") or 0)
        reason = stop["reason"]
        if reason in ("FAR", "lot coverage", "impervious (ISR)"):
            limits.append({"kind": "zoning", "text": f"Zoning {reason} limit reached."})
        elif reason == "no room" or (asked and stop["placed"] < asked and reason == "count reached"):
            limits.append(_no_room(line, losses, arc, env, exclude_m, setback))
        flex_out.append({"type": line["type"], "label": siteplan.BUILDING_TYPES[line["type"]]["label"],
                         "exposure": line["exposure"], "asked": asked, "placed": stop["placed"],
                         "gfa_sf": round(sum(b["gfa"] for b in shapes["buildings"])), "stories": line["stories"],
                         "height_ft": line["height_ft"], "limits": limits})

    # ---- 3. check the final layout -----------------------------------------------------------------------------
    hbuildings = [b for b in on_site]
    positions = [b["_pos"] for b in hbuildings]
    results, qd_summary = qd.evaluate(whole, hbuildings, positions) if hbuildings else ([], None)
    hazard_report = []
    for i, b in enumerate(hazards):
        info = out_hazard[i]
        row = {"name": b["name"], "type": b["kind"], "hd": b["hd"], "newqd": b["newqd"], "sqft": b["sqft"],
               "pes": b["pes"], "barricaded": b.get("barricaded"), "mce": b.get("mce"), "k": b.get("k"),
               "height_ft": b["height_ft"], "ibd_ft": b["dist"]["ibd"], "separation_ft": b["dist"]["separation"],
               "placed": bool(info.get("placed")), "limits": info["limits"]}
        if row["placed"]:
            j = hbuildings.index(b)
            r = results[j]
            gaps = [(o["name"], b["_fp"].distance(o["_fp"]) * FT) for o in hbuildings if o is not b]
            for fb in flex_shapes:
                key = fb["exposure"]
                if key != "none":
                    gaps.append((fb["label"], b["_fp"].distance(fb["geom"]) * FT, key))
            mx, why = qd.max_newqd(b, r["edge_ft"], gaps)
            row.update(x=b["_pos"][0], y=b["_pos"][1], edge_ft=r["edge_ft"], ibd_clear=r["ibd_clear"],
                       off_site_acres=r["off_site_acres"], conflicts=[{**c, "with_name": hbuildings[c["with"]]["name"]}
                                                                      for c in r["conflicts"]],
                       max_newqd=mx, max_by=why)
            if not r["ibd_clear"]:
                row["limits"].append({"kind": "qd", "text": (
                    f"IBD arc ({b['dist']['ibd']:,.0f} ft) runs {r['off_site_acres']:g} ac off the property: only "
                    f"{r['edge_ft']:,} ft of room at the best spot the zoning envelope allows.")})
            if mx is not None and mx < float(b["newqd"]) and r["ibd_clear"]:
                row["limits"].append({"kind": "qd", "text": f"At {float(b['newqd']):,.0f} lb a neighbour is inside the "
                                                            f"required spacing (max {mx:,.0f} lb: {why})."})
            if why == "IBD to the property line" and mx is not None and mx >= float(b["newqd"]):
                row["limits"].append({"kind": "qd", "text": f"Max NEWQD {mx:,.0f} lb is set by the IBD arc reaching "
                                                            f"the property line; zoning keeps it at least {setback:g} ft "
                                                            f"inside."})
        hazard_report.append(row)
    exposure_conflicts = []
    for fb in flex_shapes:
        if fb["exposure"] == "none":
            continue
        for b in on_site:
            need = b["dist"].get(fb["exposure"]) or 0
            have = b["_fp"].distance(fb["geom"]) * FT
            if have + 1e-6 < need:
                exposure_conflicts.append({"building": fb["label"], "hazard": b["name"], "need_ft": round(need),
                                           "have_ft": round(have)})
    summary = siteplan._summary(whole.area * siteplan.SQFT_PER_M2, env.area * siteplan.SQFT_PER_M2, std,
                                [{"type": "hazard", "label": "Hazard buildings", "footprint": h_foot, "gfa": h_foot,
                                  "stalls": 0}] * (1 if on_site else 0) + all_placed, [], state["paved"], setback)
    # Landscaped open space is approximated as the pervious share of the site.
    if std.get("open_space") is not None:
        pervious = 1 - summary["impervious_sf"] / (whole.area * siteplan.SQFT_PER_M2)
        summary["checks"].append({"key": "open_space", "label": "Open space (pervious share)", "value": round(pervious, 3),
                                  "limit": std["open_space"], "ok": pervious >= std["open_space"] - 1e-9,
                                  "min": True})
    summary.update(hazard_buildings=len(on_site), hazard_requested=len(hazards),
                   qd=qd_summary, exposure_conflicts=exposure_conflicts, losses=losses,
                   all_clear=bool(qd_summary["all_clear"] if qd_summary else True) and not exposure_conflicts
                   and all(c["ok"] for c in summary["checks"]) and len(on_site) == len(hazards))
    summary.pop("by_type", None)
    return {"envelope": env, "flex_shapes": flex_shapes, "courts": courts, "hazards": hazards, "hazard": hazard_report,
            "flex": flex_out, "summary": summary, "notes": notes, "disclaimer": DISCLAIMER}


def _no_room(line, losses, arc, env, exclude_m, setback):
    """What ate the room for a flex line that stopped for lack of space: say which of the three took the most."""
    arc_ac = 0.0
    if arc is not None and not env.is_empty:
        arc_ac = env.intersection(arc).area * siteplan.SQFT_PER_M2 / siteplan.SQFT_PER_ACRE
    cost = {"qd": arc_ac, "zoning": losses["zoning_setback_acres"], "exclusion": losses["exclusion_acres"]}
    kind = max(cost, key=cost.get)
    if cost[kind] <= 0:
        return {"kind": "zoning", "text": "No more room: the building depth and truck court don't fit what is left."}
    text = {"qd": f"No more room outside the QD arcs: the {line['exposure'].upper()} arcs take {arc_ac:,.0f} ac of the "
                  f"buildable area.",
            "zoning": f"No more room: the {setback:g} ft zoning setback removes {losses['zoning_setback_acres']:,.0f} ac "
                      f"and the rest is used.",
            "exclusion": f"No more room: flood / wetland areas remove {losses['exclusion_acres']:,.0f} ac."}[kind]
    return {"kind": kind, "text": text, "acres": {k: round(v, 1) for k, v in cost.items()}}
