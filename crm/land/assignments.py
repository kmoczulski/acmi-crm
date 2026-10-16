"""Client land-search assignments: the diligence report structure, ranking, and what a guest may see.

The report layout follows ACMI's Word due diligence reports and the Site Assessment workbook: ten rated
categories, a master checklist, utility gap table, permit matrix, ROM to pad-ready, priority actions.
It is stored as JSON on AssignmentSite.report; anything left blank falls back to what the CRM knows.
"""
import secrets
from datetime import date

RATINGS = [("low", "Low Risk"), ("moderate", "Moderate"), ("elevated", "Elevated"), ("na", "N/A")]
RATING_LABEL = dict(RATINGS)
STATUSES = ["Not Started", "In Progress", "Complete", "N/A"]
INCENTIVE = ["Yes", "Partial", "No", "TBD"]

CATEGORIES = [
    ("land", "Land Assembly and Title"),
    ("zoning", "Zoning and Entitlements"),
    ("qd", "QD Arc and DDESB Siting"),
    ("environmental", "Environmental"),
    ("geotech", "Geotechnical and Topography"),
    ("flood", "Floodplain and Hydrology"),
    ("electric", "Electric Power"),
    ("water", "Water and Wastewater"),
    ("gas", "Natural Gas"),
    ("rail", "Rail and Transportation"),
]
CATEGORY_LABEL = dict(CATEGORIES)

OVERVIEW = [
    ("location", "Location"), ("address", "Address"), ("coordinates", "Coordinates"), ("total_area", "Total area"),
    ("parcel_ids", "Parcel identifiers"), ("zoning", "Zoning"), ("opportunity_zone", "Opportunity Zone"),
    ("rail", "Rail"), ("prior_use", "Prior use"), ("activity", "Current on-site activity"),
]

# Narrative sections: (key, heading)
SECTIONS = [
    ("land", "Land, Title, and Entitlements"),
    ("environmental", "Environmental, Geotechnical, and Hydrology"),
    ("utilities", "Utilities"),
    ("transport", "Rail and Transportation"),
    ("permitting", "Permitting Summary and Site Readiness Timeline"),
]

UTILITIES = ["Electric", "Water", "Wastewater", "Natural Gas", "Fiber", "Rail"]

PERMITS = [
    "Special Use Permit (if required)", "Rezoning (if required)", "Site plan approval", "Erosion and sediment control",
    "Stormwater", "Wetlands / stream impacts (if any)", "Utility extension agreements", "Building permits",
]

ROM = [
    ("Clearing and mass grading", "Partial"), ("Electric substation and service extension", "Yes"),
    ("Gas line extension", "Yes"), ("Water and sewer connections and upsizing", "Yes"),
    ("Rail siding construction or expansion", "Yes"), ("Roadway and access improvements", "Yes"),
    ("Stormwater infrastructure", "Partial"), ("QD arc buffer acquisition or easement", "No"),
    ("Floodplain corridor buffers", "No"), ("Soft costs, permits, and contingency", "No"),
]

# Master checklist (Appendix A): (ref, block, item, verification method)
CHECKLIST = [
    ("A-1", "Land and Title", "Chain of title and ownership verification", "Deed records; title company"),
    ("A-2", "Land and Title", "ALTA/NSPS land title survey", "Licensed surveyor"),
    ("A-3", "Land and Title", "Title commitment: exception documents reviewed", "Title company; outside counsel"),
    ("A-4", "Land and Title", "Easement and encumbrance inventory with map overlay", "Survey; title exceptions"),
    ("A-5", "Land and Title", "Existing lease terms and boundary confirmation", "Recorded lease; owner disclosure"),
    ("A-6", "Land and Title", "Owner-name full-portfolio search", "County assessor; Secretary of State"),
    ("B-1", "Zoning and Entitlements", "Zoning verification letter", "County planning department"),
    ("B-2", "Zoning and Entitlements", "Permitted use analysis against program", "Ordinance review; county staff"),
    ("B-3", "Zoning and Entitlements", "Special Use Permit requirements mapped", "County ordinance; pre-application meeting"),
    ("B-4", "Zoning and Entitlements", "Subdivision feasibility confirmed", "County subdivision ordinance"),
    ("B-5", "Zoning and Entitlements", "Overlay district screen (floodplain, AICUZ/JLUS)", "County GIS; installation JLUS"),
    ("B-6", "Zoning and Entitlements", "Incentive designation verification (Opportunity Zone, HUBZone)", "Treasury/CDFI map; SBA HUBZone map"),
    ("C-1", "QD Arc and DDESB Siting", "NEW quantity and hazard division confirmed for every case", "Client process data; DoD 6055.09-M"),
    ("C-2", "QD Arc and DDESB Siting", "Required IBD, PTRD, and ILD computed for each QD case", "DDESB standards; siting engineer"),
    ("C-3", "QD Arc and DDESB Siting", "Receptor inventory within the governing arc radius", "County GIS parcel and structure layers"),
    ("C-4", "QD Arc and DDESB Siting", "Arc closure within parcel boundary tested", "Survey; GIS overlay"),
    ("C-5", "QD Arc and DDESB Siting", "Buyout, easement, or road relocation path scoped", "Owner outreach; county DOT"),
    ("C-6", "QD Arc and DDESB Siting", "All case arcs rerun after any program change", "Siting engineer"),
    ("D-1", "Environmental", "Phase I ESA obtained and reviewed", "Environmental consultant report"),
    ("D-2", "Environmental", "Historical use research", "Sanborn maps; county records; interviews"),
    ("D-3", "Environmental", "Remediation closure documentation reviewed", "Closure reports; regulator correspondence"),
    ("D-4", "Environmental", "Wetlands delineation", "Field delineation; USACE coordination; NWI overlay"),
    ("D-5", "Environmental", "Threatened and endangered species screen", "State natural heritage database"),
    ("D-6", "Environmental", "Cultural resources screen", "SHPO consultation"),
    ("D-7", "Environmental", "Phase I refresh prior to closing", "Environmental consultant"),
    ("E-1", "Geotechnical and Site Conditions", "Published soils and bearing capacity review", "NRCS soils data; prior reports"),
    ("E-2", "Geotechnical and Site Conditions", "Topographic model and cut/fill analysis", "Survey; civil engineer"),
    ("E-3", "Geotechnical and Site Conditions", "Confirmatory borings at building pads", "Geotechnical program"),
    ("E-4", "Geotechnical and Site Conditions", "Floodplain and floodway corridor mapping", "FEMA NFHL; FIRM panels; county GIS"),
    ("E-5", "Geotechnical and Site Conditions", "Stormwater management concept", "Civil engineer; county requirements"),
    ("F-1", "Utilities", "Provider confirmation, all utilities", "Direct provider meetings"),
    ("F-2", "Utilities", "Electric capacity, timeline, and commitment deadline", "Utility correspondence"),
    ("F-3", "Utilities", "Water and sewer will-serve letters", "Municipal utility"),
    ("F-4", "Utilities", "Process water quality and volume testing", "Sampling and lab analysis"),
    ("F-5", "Utilities", "Gas extension routing and binding estimate", "Gas utility engineering"),
    ("F-6", "Utilities", "Fiber service and redundancy review", "Broadband provider"),
    ("F-7", "Utilities", "Utility easements mapped on survey", "ALTA survey; provider records"),
    ("G-1", "Transportation", "Rail service verification and siding inspection", "Rail carrier; field inspection"),
    ("G-2", "Transportation", "Rail expansion feasibility and cost", "Carrier engineering consultation"),
    ("G-3", "Transportation", "Highway access and turning movements", "Civil engineer; state DOT"),
    ("H-1", "Permitting and Schedule", "Permit matrix with authorities and durations", "Agency confirmation"),
    ("H-2", "Permitting and Schedule", "Critical dates register (electric commitment, hearings)", "Project controls"),
    ("H-3", "Permitting and Schedule", "Pad-ready timeline with sequencing", "Integrated schedule"),
    ("I-1", "Cost and Incentives", "Site readiness ROM by cost category", "Estimates and provider quotes"),
    ("I-2", "Cost and Incentives", "Incentive-eligible infrastructure cost isolation", "ROM breakdown"),
    ("I-3", "Cost and Incentives", "Local and state incentive program screening", "EDO consultation"),
]

RANKING_RULE = [
    "Any site with an Elevated rating sorts below every site without one.",
    "Among the rest, the fewest Moderate ratings come first.",
    "Tie-break on schedule-critical exposure: Moderates sitting on an externally imposed deadline rank below "
    "Moderates that are cost items ACMI controls.",
    "Tie-break on ROM to pad-ready, net of incentive-eligible cost (lower first).",
    "Final tie-break on net usable acreage headroom above the program requirement (more first).",
]


def new_token():
    return secrets.token_urlsafe(24)


def _num(v):
    """A float from user text like '$1,200,000' or '640.5'; None if blank or not a number."""
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def prefill(site):
    """What the CRM already knows about a site, as report overview fields (all values are strings)."""
    from . import score, trim

    deals = list(site.parcel_deals.select_related("parcel", "parcel__county").filter(included=True))
    if not deals:
        trim.site_acres(site)  # makes sure each parcel has its deal row
        deals = list(site.parcel_deals.select_related("parcel", "parcel__county").filter(included=True))
    parcels = [d.parcel for d in deals]
    counties = sorted({f"{p.county.name}, {p.county.state}" for p in parcels})
    zoning = sorted({p.zoning for p in parcels if p.zoning})
    uses = sorted({score.land_use_label(p.state_land_use) for p in parcels if p.state_land_use} - {None, ""})
    first = next((p for p in parcels if p.situs_address), None)
    acres = round(sum(d.acres for d in deals), 1)
    coords = ""
    try:
        geom = trim.site_geometry(site)
        if geom is not None and not geom.is_empty:
            c = geom.centroid
            coords = f"{c.y:.6f}, {c.x:.6f}"
    except Exception:  # noqa: BLE001 - geometry is a nicety; the report works without it
        pass
    return {
        "location": "; ".join(counties),
        "address": f"{first.situs_address}, {first.situs_city}".strip(", ") if first else "",
        "coordinates": coords,
        "total_area": f"{acres:,.1f} acres across {len(parcels)} parcel{'' if len(parcels) == 1 else 's'}" if parcels else "",
        "parcel_ids": "\n".join(p.prop_id or p.key for p in parcels),
        "zoning": "; ".join(zoning),
        "prior_use": "; ".join(uses),
        "gross_acres": acres,
    }


def blank_report():
    return {
        "overview": {}, "summary": "", "market": "", "sections": {}, "categories": {}, "net": {},
        "utilities": [], "permits": [], "rom": [], "actions": [], "dates": [], "checklist": {},
    }


def _rows(post, prefix, fields, n):
    """Rows posted as <prefix>_<i>_<field>; empty rows are dropped."""
    out = []
    for i in range(n):
        row = {f: (post.get(f"{prefix}_{i}_{f}") or "").strip() for f in fields}
        if any(row.values()):
            out.append(row)
    return out


def parse_form(post):
    """Report JSON from the editor form."""
    cats = {}
    for key, _ in CATEGORIES:
        cats[key] = {
            "rating": post.get(f"cat_{key}_rating", ""),
            "finding": (post.get(f"cat_{key}_finding") or "").strip(),
            "mitigation": (post.get(f"cat_{key}_mitigation") or "").strip(),
            "owner": (post.get(f"cat_{key}_owner") or "").strip(),
            "confirmed": bool(post.get(f"cat_{key}_confirmed")),
            "critical": bool(post.get(f"cat_{key}_critical")),
        }
    return {
        "overview": {k: (post.get(f"ov_{k}") or "").strip() for k, _ in OVERVIEW},
        "summary": (post.get("summary") or "").strip(),
        "market": (post.get("market") or "").strip(),
        "sections": {k: (post.get(f"sec_{k}") or "").strip() for k, _ in SECTIONS},
        "categories": cats,
        "net": {k: (post.get(f"net_{k}") or "").strip() for k in ("wetlands", "floodway", "easements", "qd")},
        "utilities": _rows(post, "ut", ["utility", "provider", "infrastructure", "available", "committed", "gap", "action"], len(UTILITIES) + 2),
        "permits": _rows(post, "pm", ["approval", "authority", "duration", "note"], len(PERMITS) + 2),
        "rom": _rows(post, "rom", ["category", "basis", "amount", "incentive"], len(ROM) + 2),
        "actions": _rows(post, "pa", ["action", "why", "owner", "deadline"], 6),
        "dates": _rows(post, "dt", ["item", "date", "consequence", "owner", "critical"], 4),
        "checklist": {
            ref: {"status": post.get(f"ck_{ref}_status", "Not Started"), "note": (post.get(f"ck_{ref}_note") or "").strip()}
            for ref, *_ in CHECKLIST
        },
    }


def build(entry, assignment=None):
    """Everything a report page needs: the saved report merged with CRM prefill, plus computed totals."""
    assignment = assignment or entry.assignment
    rep = {**blank_report(), **(entry.report or {})}
    pre = prefill(entry.site)
    overview = [(label, (rep["overview"].get(k) or pre.get(k) or "")) for k, label in OVERVIEW]

    cats = []
    for key, label in CATEGORIES:
        c = rep["categories"].get(key, {})
        cats.append({"key": key, "label": label, "rating": c.get("rating", ""), "rating_label": RATING_LABEL.get(c.get("rating", ""), "Not yet rated"),
                     "finding": c.get("finding", ""), "mitigation": c.get("mitigation", ""), "owner": c.get("owner", ""),
                     "confirmed": c.get("confirmed", False), "critical": c.get("critical", False)})
    rated = [c for c in cats if c["rating"] in ("low", "moderate", "elevated")]
    counts = {"elevated": sum(c["rating"] == "elevated" for c in cats), "moderate": sum(c["rating"] == "moderate" for c in cats),
              "low": sum(c["rating"] == "low" for c in cats), "unrated": sum(not c["rating"] for c in cats)}
    critical_moderate = sum(c["rating"] == "moderate" and c["critical"] for c in cats)

    gross = pre["gross_acres"]
    less = {k: _num(rep["net"].get(k)) or 0 for k in ("wetlands", "floodway", "easements", "qd")}
    net_acres = round(gross - sum(less.values()), 1) if gross else None
    floor = float(assignment.acreage_floor) if assignment.acreage_floor is not None else None
    headroom = round(net_acres - floor, 1) if net_acres is not None and floor is not None else None

    rom_rows = [{**r, "value": _num(r.get("amount"))} for r in rep["rom"]]
    rom_total = sum(r["value"] or 0 for r in rom_rows)
    rom_eligible = sum(r["value"] or 0 for r in rom_rows if r.get("incentive") == "Yes")
    rom_net = round(rom_total - rom_eligible) if rom_total else None

    if counts["elevated"]:
        verdict, tone = "Elevated risk: resolve before commitment", "elevated"
    elif not rated:
        verdict, tone = "Not yet rated", "na"
    elif counts["moderate"]:
        verdict, tone = f"{counts['moderate']} moderate item{'' if counts['moderate'] == 1 else 's'} with mitigation paths", "moderate"
    else:
        verdict, tone = "No material impediment identified", "low"

    checklist = []
    for ref, block, item, how in CHECKLIST:
        st = rep["checklist"].get(ref, {})
        checklist.append({"ref": ref, "block": block, "item": item, "how": how,
                          "status": st.get("status") or "Not Started", "note": st.get("note", "")})
    done = sum(c["status"] == "Complete" for c in checklist)
    active = sum(c["status"] != "N/A" for c in checklist)

    return {
        "entry": entry, "site": entry.site, "overview": overview, "pre": pre, "report": rep, "cats": cats, "counts": counts,
        "critical_moderate": critical_moderate, "gross": gross, "less": less, "net_acres": net_acres, "floor": floor,
        "headroom": headroom, "rom_rows": rom_rows, "rom_total": rom_total or None, "rom_eligible": rom_eligible or None,
        "rom_net": rom_net, "verdict": verdict, "tone": tone, "checklist": checklist,
        "checklist_done": done, "checklist_active": active,
        "sections": [(label, rep["sections"].get(k, "")) for k, label in SECTIONS],
        "gap_rows": rep["utilities"], "permits": rep["permits"], "actions": rep["actions"], "dates": rep["dates"],
    }


def rank(assignment, guest=False):
    """[(position, report context, pinned)] best first, per the ranking rule above. Pinned sites keep their set position."""
    entries = assignment.entries.select_related("site").all()
    if guest:
        entries = entries.filter(show_to_guest=True)
    ctxs = [build(e, assignment) for e in entries]

    def key(c):
        group = 1 if c["counts"]["elevated"] else (2 if c["counts"]["unrated"] == len(CATEGORIES) else 0)
        return (group, c["counts"]["moderate"], c["critical_moderate"],
                c["rom_net"] if c["rom_net"] is not None else float("inf"),
                -(c["headroom"] if c["headroom"] is not None else -1e9), c["site"].name.lower())

    ordered = sorted(ctxs, key=key)
    for c in sorted([c for c in ordered if c["entry"].rank_override], key=lambda c: c["entry"].rank_override):
        ordered.remove(c)
        ordered.insert(min(c["entry"].rank_override, len(ordered) + 1) - 1, c)
    return [(i, c, bool(c["entry"].rank_override)) for i, c in enumerate(ordered, 1)]


def guest_assignment(token):
    """The assignment a share link opens, or None when the link is wrong, switched off or expired."""
    from ..models import Assignment

    if not token or len(token) < 16:
        return None
    a = Assignment.objects.filter(share_token=token).first()
    return a if a and a.share_active() else None


def today():
    return date.today()
