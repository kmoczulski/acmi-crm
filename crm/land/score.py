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
    ("unzoned", "No zoning (unincorporated TX / AL)"),
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
# City of Huntsville districts (they also cover Huntsville's annexed land in Limestone and Morgan counties).
HUNTSVILLE_CODES = {
    "PI": "industrial", "CIP": "industrial", "AIP": "industrial", "IP": "industrial", "LI": "industrial",
    "HI": "industrial",
    "RP": "business_park", "RP2": "business_park", "RPA": "business_park", "RPC": "business_park",
    "RPW": "business_park",
    "C1": "commercial", "C2": "commercial", "C3": "commercial", "C4": "commercial", "C5": "commercial",
    "C6": "commercial", "O": "commercial",
    "RO2": "mixed_use", "PD": "planned", "PDH": "planned",
    "R1": "residential", "R1A": "residential", "R1B": "residential", "R1C": "residential", "R2": "residential",
    "R2A": "residential", "R2B": "residential", "R2C": "residential", "RA": "residential",
}
for _fips in ("01083", "01089", "01103"):
    ZONING_CODES[_fips] = HUNTSVILLE_CODES
# Pinal County zoning (unincorporated land); city names mean the city zones it.
PINAL_CODES = {
    "CI-B": "industrial", "CI-1": "industrial", "CI-2": "industrial", "I-1": "industrial", "I-2": "industrial",
    "I-3": "industrial", "CB-1": "commercial", "CB-2": "commercial", "C-1": "commercial", "C-2": "commercial",
    "C-3": "commercial", "O-1": "commercial", "O-2": "commercial", "AC-1": "mixed_use", "AC-2": "mixed_use",
    "AC-3": "mixed_use", "MPCMP": "planned", "L-MPC": "planned", "GR": "agricultural", "SR": "agricultural",
    "SH": "agricultural", "CAR": "agricultural", "CR-1A": "residential", "CR-1": "residential", "CR-2": "residential",
    "CR-3": "residential", "CR-4": "residential", "CR-5": "residential", "MH": "residential", "MHP": "residential",
    "RV": "residential", "RVP": "residential", "TR": "residential", "MD": "residential", "MR": "residential",
    "MH-8": "residential", "MH-435": "residential", "PM/RV": "residential",
    **{f"RU-{n}": "residential" for n in ("10", "5", "3.3", "2", "1.25", "C")},
    **{f"R-{n}": "residential" for n in ("43", "35", "20", "12", "9", "7")},
    **{city: "town" for city in ("APACHE JUNCTION", "CASA GRANDE", "COOLIDGE", "ELOY", "FLORENCE", "KEARNY", "MAMMOTH",
                                 "MARANA", "MARICOPA", "QUEEN CREEK", "SUPERIOR", "WINKELMAN")},
}
# City of Casa Grande and City of Eloy districts (Pinal parcels inside those cities show the city's own code).
PINAL_CODES.update({
    "I-1": "industrial", "I-2": "industrial", "B-1": "commercial", "B-2": "commercial", "B-3": "commercial",
    "B-4": "commercial", "CO": "commercial", "BP": "business_park", "C-1": "commercial", "C-2": "commercial",
    "PAD": "planned", "PAD-RV": "planned", "PF": "planned", "RR": "agricultural", "UR": "agricultural",
    "RR-5": "agricultural", "RR-20": "agricultural", "OSR": "conservation",
    **{f"R-{n}": "residential" for n in ("1", "1A", "2", "3", "4", "5")},
    **{f"R1-{n}": "residential" for n in ("6", "12", "43")},
})
ZONING_CODES["04021"] = PINAL_CODES
# Maricopa County zoning (unincorporated land, e.g. Tonopah).
ZONING_CODES["04013"] = {
    "IND-1": "industrial", "IND-2": "industrial", "IND-3": "industrial", "IND-2P": "industrial", "IND-3P": "industrial",
    "C-1": "commercial", "C-2": "commercial", "C-3": "commercial", "C-O": "commercial", "C-S": "commercial",
    "R-2": "residential", "R-3": "residential", "R-4": "residential", "R-5": "residential", "R1-6": "residential",
    "R1-7": "residential", "R1-8": "residential", "R1-10": "residential", "R1-18": "residential",
    "R1-35": "residential", "RU-43": "agricultural", "RU-70": "agricultural", "RU-190": "agricultural",
}
# States whose rural counties have no zoning ordinance (subdivision rules only).
NO_COUNTY_ZONING = ("TX", "AL")
_ZONE_WORDS = [("industr", "industrial"), ("employment", "business_park"), ("business park", "business_park"),
               ("mixed", "mixed_use"), ("planned unit", "planned"), ("commerc", "commercial"),
               ("business", "commercial"), ("agri", "agricultural"), ("rural", "agricultural"),
               ("conserv", "conservation"), ("resid", "residential"), ("town of", "town")]


def zoning_class(p):
    """The parcel's zoning bucket (see ZONING_CLASSES)."""
    county = getattr(p, "county", None)
    zone = (p.overlays or {}).get("zoning")
    if zone:
        code = (zone.get("value") or "").upper().strip()
        codes = ZONING_CODES.get(getattr(county, "fips", ""), {})
        if code in codes:
            return codes[code]
        base = code.split(" / ")[0].strip()  # 'I-2 / CR' = I-2 with a Casa Grande overlay
        if base in codes:
            return codes[base]
        text = f"{zone.get('name', '')} {zone.get('general', '')}".lower()
        return next((cls for word, cls in _ZONE_WORDS if word in text), "")
    if (getattr(county, "state", "") or "TX") in NO_COUNTY_ZONING:
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
        use = add(0, "Tax-exempt owner (public, school or church): fewer sellers, but counties do sell land", "warn")
    elif groups & {"A", "B", "M", "O"}:
        use = add(2, "Residential use on the tax roll")
    else:
        use = add(4, "Land use not given by the appraisal district", "warn")
    if p.in_city:
        add(0, f"Inside {p.in_city} city limits: check city zoning" if state != "MD"
            else f"Inside the Town of {p.in_city}: town zoning applies")
    elif state == "TX":
        use += add(5, "Unincorporated: Texas counties have no zoning (subdivision rules still apply)")
    elif state == "AL":
        use += add(5, "Unincorporated: no county zoning (subdivision rules only); water, sewer and fire usually "
                      "need annexation or an extension agreement")
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

    # Incentive programs (from the live check): Opportunity Zone, NMTC, Enterprise Zone, FTZ...
    programs = (checks.get("incentives") or {}).get("programs") or []
    bonus = 0
    for prog in programs:
        if prog.get("points"):
            bonus += add(prog["points"], f"{prog['label']}" + (f": {prog['area']}" if prog.get("area") else ""))
    score += min(bonus, 5)

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
    # A public or institutional owner is a warning, not a deal-killer: counties do sell land (e.g. Livingston Rd).
    ca = ov.get("critical_area") or {}
    if ca.get("pct", 0) >= 30 and ca.get("value") == "RCA":
        killers.append(f"{ca['pct']:.0f}% in the Chesapeake Bay Critical Area (RCA): little development allowed")
    elif ca:
        add(0, f"{ca['pct']:.0f}% in the Chesapeake Bay Critical Area ({ca['value']})", "warn")

    score = max(0, min(100, round(score)))
    if killers:
        score = min(score, KILLER_CAP)
    return score, reasons, killers


def methodology():
    """The buy-box score written out for the reports' definitions page: [{part, max, how: [lines]}] plus notes.

    Built from the same constants the scorer uses where there are any. KEEP IT IN STEP WITH screen(): when a threshold
    or a point value changes there, change it here (a test checks the headline numbers)."""
    zp = ZONING_POINTS
    return {
        "parts": [
            {"part": "Size", "max": 25, "how": [
                "Uses net usable acres (after floodplain and wetlands) once the live checks have run, otherwise gross acres.",
                "250 acres or more: 25. 100 to 249: 22. 50 to 99: 15. Under 50: 0, since a small tract only matters as part "
                "of an assemblage (5 points if the same owner holds 50+ acres across several parcels in the county)."]},
            {"part": "Land use and entitlement", "max": 25, "how": [
                "Where a zoning map covers the land, its district sets the points: "
                + "; ".join(f"{ZONING_LABELS[k].split(' (')[0].lower()} {v}" for k, v in
                            sorted(zp.items(), key=lambda kv: -kv[1]) if k in ZONING_LABELS) + ".",
                "Where there is no zoning layer, the tax-roll land use is used: industrial 20, farm or rural 12, commercial "
                "or vacant 8, residential 2, tax-exempt 0, not given 4.",
                "Add-ons: unincorporated land in Texas or Alabama with no county zoning +5; county Technology Corridor +5; "
                "Maryland Priority Funding Area +3. The part is capped at 25."]},
            {"part": "Power", "max": 20, "how": [
                "Distance to the nearest transmission line (100 kV and up): within 1 mile 14, within 3 miles 9, within 6 miles 4.",
                "Distance to the nearest substation: within 2 miles 6, within 5 miles 3."]},
            {"part": "Access", "max": 15, "how": [
                "Nearest major highway: within 1 mile 8, within 3 miles 5, within 6 miles 2.",
                "Nearest rail line: within 1 mile 7, within 3 miles 4, within 6 miles 1."]},
            {"part": "Defense demand", "max": 10, "how": [
                "Distance to the nearest military installation: within 25 miles 10, within 50 miles 6, within 100 miles 2.",
                "Navy Military Awareness Zone +5; HUBZone +5. The part is capped at 10."]},
            {"part": "Utilities", "max": 5, "how": [
                "Water provider found +2; sewer provider found +2; a county water or sewer service category of 1 to 3 +2 each; "
                "natural gas pipeline within 3 miles +1. Capped at 5."]},
            {"part": "Incentives", "max": 5, "how": [
                "Points set by program (Opportunity Zone 3, New Markets Tax Credit 2, Foreign-Trade Zone 1, state programs "
                "where they exist). Capped at 5."]},
            {"part": "Owner signals", "max": 5, "how": [
                "Owner mails to another state +3 (often a passive owner); owned by an entity, trust or estate +2."]},
            {"part": "Improvements", "max": -5, "how": [
                "Minus 5 when buildings are worth over $250,000 and over 30% of the property's value."]},
        ],
        "killers": [
            f"{FLOOD_KILL_PCT}% or more of the parcel in the FEMA 100-year floodplain.",
            f"{FLOODWAY_KILL_PCT}% or more in a FEMA floodway.",
            f"{WETLAND_KILL_PCT}% or more in mapped wetlands (NWI).",
            "A Superfund (NPL) site within half a mile.",
            "30% or more in the Chesapeake Bay Critical Area's resource conservation area (Maryland).",
        ],
        "notes": [
            "The parts add up to 110 at most and the total is held to 0 to 100.",
            f"If any deal-killer applies, the score is held at {KILLER_CAP} or below however well the rest scores.",
            "A site's score is the acre-weighted average of its parcels' scores. Each parcel card lists the exact reasons "
            "and points behind its number.",
            "The score ranks sites for a first look only. It does not see power capacity, safety-arc (QD) clearance, "
            "title, environmental history, or whether the land is for sale.",
        ],
    }


def apply(parcel):
    parcel.zoning_class = zoning_class(parcel)
    parcel.score, parcel.score_reasons, parcel.killers = screen(parcel)
    return parcel
