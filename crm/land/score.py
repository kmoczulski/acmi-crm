"""Buy-box screen for a parcel: a 0-100 score with its reasons, and any deal-killers.

Follows ACMI's defense-site buy-box: 50-500+ net usable acres, industrial or ag land with a path to
industrial use, high-voltage power, heavy-truck or rail access, a defense demand driver, and no floodway,
material wetlands or contamination. The score only ranks parcels for a first look: power capacity,
QD-arc clearance, zoning detail and environmental status always need confirming by a person.
"""
import re

# Texas Comptroller (PTAD) state property categories.
LAND_USE = {
    "A": "Single-family home", "B": "Multifamily", "C": "Vacant lot", "D": "Farm/ranch (ag-valued)",
    "E": "Rural land", "F1": "Commercial", "F2": "Industrial", "G": "Oil, gas, minerals",
    "J": "Utility", "L": "Business property", "M": "Mobile home", "O": "Residential inventory",
    "S": "Special inventory", "X": "Tax-exempt",
}
ENTITY_WORDS = re.compile(r"\b(LLC|L\.L\.C|LP|L\.P|LTD|INC|CORP|CO|COMPANY|PARTNERS(HIP)?|TRUST|TR|TRUSTEE|ESTATE|EST|"
                          r"HOLDINGS|INVESTMENTS?|PROPERTIES|LAND|RANCH|FAMILY)\b")

# Zoning buckets shared across counties and towns (each county's codes map onto these).
ZONING_CLASSES = [
    ("industrial", "Industrial"),
    ("business_park", "Business park / employment"),
    ("commercial", "Commercial"),
    ("mixed_use", "Mixed use"),
    ("planned", "Planned development (PUD)"),
    ("agricultural", "Agricultural / rural"),
    ("residential", "Residential"),
    ("conservation", "Conservation / protected"),
    ("public", "Public / institutional"),
    ("town", "Town or city zoning (not mapped)"),
    ("unzoned", "No zoning (unincorporated TX)"),
]
ZONING_LABELS = dict(ZONING_CLASSES)
# Points for the land-use / entitlement part of the screen, by bucket.
ZONING_POINTS = {"industrial": 22, "business_park": 16, "commercial": 10, "mixed_use": 6, "planned": 5,
                 "agricultural": 5, "residential": 0, "conservation": 0, "public": 0, "town": 4}
ZONING_NOTES = {
    "industrial": "", "business_park": ": industrial-flex often allowed, check the use table",
    "commercial": ": industrial would need a rezoning", "mixed_use": ": industrial unlikely without a rezoning",
    "planned": ": governed by its PUD plan", "agricultural": ": needs a rezoning, a buy-box risk",
    "residential": ": rezoning to industrial unlikely", "conservation": ": protected, little development",
    "public": "", "town": ": check the town zoning map",
}
# County codes -> bucket. Codes not listed fall back to keywords in the zone's name.
ZONING_CODES = {
    "24017": {  # Charles County, MD. Its own "general" labels file Business Park under Mixed Use.
        "IG": "industrial", "IH": "industrial",
        "BP": "business_park", "PEP": "business_park", "ABP": "business_park", "HVE": "business_park",
        "CER": "business_park",
        "CB": "commercial", "CC": "commercial", "CN": "commercial", "CV": "commercial", "HVC": "commercial",
        "HVG": "commercial",
        "MX": "mixed_use", "CMR": "mixed_use", "CRR": "mixed_use", "TOD": "mixed_use", "WPC": "mixed_use",
        "RO": "mixed_use", "AUC": "mixed_use", "WC": "mixed_use",
        "PUD": "planned", "PRD": "planned", "PMH": "planned",
        "RL": "residential", "RR": "residential", "RV": "residential", "RM": "residential", "RH": "residential",
        "HVR": "residential",
        "AC": "agricultural", "RC": "agricultural",
        "WCD": "conservation",
        "INDIAN HEAD": "town", "LA PLATA": "town",
    },
}
_ZONE_WORDS = [("industr", "industrial"), ("employment", "business_park"), ("business park", "business_park"),
               ("mixed", "mixed_use"), ("planned unit", "planned"), ("commerc", "commercial"),
               ("business", "commercial"), ("agri", "agricultural"), ("rural", "agricultural"),
               ("conserv", "conservation"), ("resid", "residential"), ("town of", "town")]


def zoning_class(p):
    """The parcel's zoning bucket (see ZONING_CLASSES)."""
    county = getattr(p, "county", None)
    zone = (p.overlays or {}).get("zoning")
    if zone:
        code = (zone.get("value") or "").upper()
        codes = ZONING_CODES.get(getattr(county, "fips", ""), {})
        if code in codes:
            return codes[code]
        text = f"{zone.get('name', '')} {zone.get('general', '')}".lower()
        return next((cls for word, cls in _ZONE_WORDS if word in text), "")
    if (getattr(county, "state", "") or "TX") == "TX":
        return "town" if p.in_city else "unzoned"
    return "town" if p.in_city else ""


FLOOD_KILL_PCT = 30
FLOODWAY_KILL_PCT = 10
WETLAND_KILL_PCT = 20
KILLER_CAP = 25

# Always unconfirmed by public data; shown on every parcel card.
ALWAYS_UNCONFIRMED = [
    "Power capacity at the nearest substation (call the utility)",
    "QD-arc / safety separation from homes, schools and public roads",
    "Zoning or subdivision rules, and any deed restrictions",
    "Phase I environmental",
    "Whether it is under contract or listed",
]


def land_use_codes(raw):
    """'E1,D2' -> ['E1', 'D2']."""
    return [c.strip().upper() for c in re.split(r"[,;/ ]+", raw or "") if c.strip()]


def land_use_label(raw):
    labels = []
    for code in land_use_codes(raw):
        label = LAND_USE.get(code[:2]) or LAND_USE.get(code[:1])
        if label and label not in labels:
            labels.append(label)
    return ", ".join(labels)


def road_name(hw):
    """NHS sign 'U77' -> 'US 77', 'I35' -> 'I-35', 'S71' -> 'SH 71'."""
    sign = (hw.get("name") or "").strip()
    m = re.match(r"^([IUS])(\d+\w*)$", sign)
    if m:
        return {"I": "I-", "U": "US ", "S": "SH "}[m.group(1)] + m.group(2)
    return (hw.get("LNAME") or "").strip() or sign or "a major highway"


def substation_name(sub):
    name = (sub.get("name") or "").strip()
    return "an unnamed" if not name or name.upper().startswith(("UNKNOWN", "NOT AVAILABLE")) else name.title()


def _band(miles, bands):
    """bands = [(max miles, points), ...] in ascending order."""
    if miles is None:
        return 0
    for limit, pts in bands:
        if miles <= limit:
            return pts
    return 0


def screen(p):
    """Score a Parcel (or any object with the same fields). Returns (score, reasons, killers)."""
    reasons, killers = [], []
    near, checks = p.near or {}, p.checks or {}

    def add(pts, text, kind=None):
        reasons.append({"pts": pts, "text": text, "kind": kind or ("good" if pts > 0 else "warn")})
        return pts

    def miles(layer):
        return (near.get(layer) or {}).get("miles")

    # Size, on net usable acres once flood and wetlands have been checked.
    net = (checks.get("net_usable") or {}).get("acres")
    acres = net if net is not None else p.acres
    basis = "net usable" if net is not None else "gross"
    size = 0
    if acres >= 250:
        size = add(25, f"{acres:,.0f} {basis} acres")
    elif acres >= 100:
        size = add(22, f"{acres:,.0f} {basis} acres")
    elif acres >= 50:
        size = add(15, f"{acres:,.0f} {basis} acres")
    else:
        add(0, f"{acres:,.0f} {basis} acres: under 50, only as part of an assemblage")
        if (p.owner_acres or 0) >= 50 and (p.owner_parcels or 0) > 1:
            size = add(5, f"Owner holds {p.owner_acres:,.0f} ac across {p.owner_parcels} parcels in the county")
    score = size

    # Land use and entitlement path. County zoning, where published, outranks the tax-roll land use.
    state = getattr(getattr(p, "county", None), "state", "") or "TX"
    ov = p.overlays or {}
    codes = land_use_codes(p.state_land_use)
    groups = {c[:2] if c[:1] == "F" else c[:1] for c in codes}
    zone = ov.get("zoning")
    zclass = zoning_class(p)
    if zone and zclass in ZONING_POINTS:
        label = f"{zone['value']}" + (f", {zone['name']}" if zone.get("name") else "")
        pts = ZONING_POINTS[zclass]
        kind = "bad" if zclass in ("residential", "conservation") else (None if pts >= 10 else "warn")
        use = add(pts, f"Zoned {ZONING_LABELS[zclass].split(' (')[0].lower()} ({label}){ZONING_NOTES[zclass]}", kind)
        if len(zone.get("parts") or {}) > 1:
            add(0, "Split zoning: " + ", ".join(f"{k} {v:.0f}%" for k, v in zone["parts"].items()))
    elif zone:
        use = add(4, f"Zoning {zone['value']}", "warn")
    elif "F2" in groups:
        use = add(20, "Industrial use on the tax roll")
    elif groups & {"D", "E"}:
        use = add(12, "Farm/ranch or rural land: needs a path to industrial use")
    elif groups & {"F1", "C"}:
        use = add(8, "Commercial or vacant land")
    elif "X" in groups:
        use = add(0, "Tax-exempt owner (public, school or church): unlikely seller", "bad")
    elif groups & {"A", "B", "M", "O"}:
        use = add(2, "Residential use on the tax roll")
    else:
        use = add(4, "Land use not given by the appraisal district", "warn")
    if p.in_city:
        add(0, f"Inside {p.in_city} city limits: check city zoning" if state == "TX"
            else f"Inside the Town of {p.in_city}: town zoning applies")
    elif state == "TX":
        use += add(5, "Unincorporated: Texas counties have no zoning (subdivision rules still apply)")
    if ov.get("tech_corridor"):
        use += add(5, "In the county Technology Corridor")
    if ov.get("pfa"):
        use += add(3, "Priority Funding Area: eligible for state infrastructure money")
    score += min(use, 25)

    # Power.
    t = miles("transmission")
    tline = near.get("transmission") or {}
    kv = tline.get("VOLT_CLASS") or tline.get("VOLTAGE")
    power = _band(t, [(1, 14), (3, 9), (6, 4)])
    if t is not None:
        add(power, f"{t:.1f} mi to a {kv} kV transmission line" if kv else f"{t:.1f} mi to a transmission line",
            None if power else "warn")
    s = miles("substation")
    if s is not None:
        sub = near.get("substation") or {}
        pts = _band(s, [(2, 6), (5, 3)])
        volt = sub.get("MAX_VOLTAG")
        volt = f" ({volt:g} kV)" if isinstance(volt, (int, float)) and volt > 0 else ""
        power += add(pts, f"{s:.1f} mi to {substation_name(sub)} substation{volt}", None if pts else "warn")
    score += power

    # Access.
    h, r = miles("highway"), miles("rail")
    access = 0
    if h is not None:
        hw = near.get("highway") or {}
        road = road_name(hw)
        pts = _band(h, [(1, 8), (3, 5), (6, 2)])
        access += add(pts, f"{h:.1f} mi to {road}", None if pts else "warn")
    if r is not None:
        rr = near.get("rail") or {}
        pts = _band(r, [(1, 7), (3, 4), (6, 1)])
        access += add(pts, f"{r:.1f} mi to rail ({rr.get('name') or 'railroad'})", None if pts else "warn")
    score += access

    # Defense demand driver.
    m = miles("military")
    defense = 0
    if m is not None:
        base = (near.get("military") or {}).get("name") or "a military installation"
        pts = _band(m, [(25, 10), (50, 6), (100, 2)])
        defense += add(pts, f"{m:.0f} mi to {base}", None if pts else "warn")
    if ov.get("military_zone"):
        defense += add(5, "In the Navy's Military Awareness Zone (base neighbours are reviewed by the Navy)")
    if ov.get("navy_noise"):
        add(0, f"Navy noise contour: {ov['navy_noise']['value']} complaint risk")
    hub = checks.get("hubzone") or {}
    if hub.get("qualified"):
        defense += add(5, "In a HUBZone")
    score += min(defense, 10)

    # Water, sewer, gas.
    util = 0
    if checks.get("water", {}).get("providers"):
        util += add(2, "Water provider (CCN): " + ", ".join(checks["water"]["providers"]))
    elif "water" in checks:
        add(0, "No water CCN: well or new service needed")
    if checks.get("sewer", {}).get("providers"):
        util += add(2, "Sewer provider (CCN): " + ", ".join(checks["sewer"]["providers"]))
    elif "sewer" in checks:
        add(0, "No sewer CCN: septic or on-site treatment needed")
    for key, letter, what in (("water_category", "W", "Water"), ("sewer_category", "S", "Sewer")):
        cat = (ov.get(key) or {}).get("value", "")
        digit = re.search(r"\d", cat)
        if digit and int(digit.group()) <= 3:
            util += add(2, f"{what} service category {cat}: existing or planned public {what.lower()}")
        elif cat:
            add(0, f"{what} service category {cat}: no public {what.lower()} planned")
    g = miles("gas_pipeline")
    if g is not None and g <= 3:
        util += add(1, f"{g:.1f} mi to a natural gas pipeline ({(near.get('gas_pipeline') or {}).get('name') or ''})".replace(" ()", ""))
    score += min(util, 5)

    # Owner attention signals.
    owner = 0
    if p.mail_state and p.mail_state.upper() not in (state, ""):
        owner += add(3, f"Owner mails to {p.mail_state}: passive, out-of-state owner")
    if ENTITY_WORDS.search((p.owner_name or "").upper()):
        owner += add(2, "Owned by an entity, trust or estate")
    score += min(owner, 5)

    # Improvements.
    if (p.improvement_value or 0) > 250_000 and (p.market_value or 0) and p.improvement_value > 0.3 * p.market_value:
        score += add(-5, f"Improved: ${p.improvement_value:,.0f} of buildings", "warn")

    # Deal-killers from the live checks.
    flood = checks.get("flood") or {}
    if flood.get("sfha_pct", 0) >= FLOOD_KILL_PCT:
        killers.append(f"{flood['sfha_pct']:.0f}% in the FEMA 100-year floodplain")
    if flood.get("floodway_pct", 0) >= FLOODWAY_KILL_PCT:
        killers.append(f"{flood['floodway_pct']:.0f}% in a FEMA floodway")
    wet = checks.get("wetlands") or {}
    if wet.get("pct", 0) >= WETLAND_KILL_PCT:
        killers.append(f"{wet['pct']:.0f}% mapped wetlands (NWI)")
    for site in (checks.get("epa") or {}).get("sites", []):
        if site.get("npl") and site.get("miles", 9) <= 0.5:
            killers.append(f"Superfund (NPL) site {site['miles']:.1f} mi away: {site['name']}")
    if "X" in groups:
        killers.append("Tax-exempt (public or institutional) owner")
    ca = ov.get("critical_area") or {}
    if ca.get("pct", 0) >= 30 and ca.get("value") == "RCA":
        killers.append(f"{ca['pct']:.0f}% in the Chesapeake Bay Critical Area (RCA): little development allowed")
    elif ca:
        add(0, f"{ca['pct']:.0f}% in the Chesapeake Bay Critical Area ({ca['value']})", "warn")

    score = max(0, min(100, round(score)))
    if killers:
        score = min(score, KILLER_CAP)
    return score, reasons, killers


def apply(parcel):
    parcel.zoning_class = zoning_class(parcel)
    parcel.score, parcel.score_reasons, parcel.killers = screen(parcel)
    return parcel
