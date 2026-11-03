"""A site's listing / broker-flyer facts: what the flyer says, entered on the site page and printed on the brief's
second page. Everything is optional; the brief shows "N/A" for what a site doesn't have.

Stored as JSON on LandSite.listing:
  text fields   source, location, size, price, taxes, zoning_claim, parcel_numbers, concept_plan
  lists         access, highlights, market, nearby, documents          (one item per line in the form)
  utilities     {label: text}   (the standard labels first, then any others)
  contacts      [{name, firm, phone, email, address}]
  drive_times   [[place, "distance / time"]]
  claims_check  [{claim, found, status}]   flyer statement against what the app found
"""

TEXT_FIELDS = (
    ("source", "Where the flyer came from", "e.g. Land Advisors flyer, via Arizona Commerce Authority, Oct 9 2026"),
    ("location", "Location", "e.g. SWC Burris Rd & SR 84, Casa Grande"),
    ("size", "Size", "e.g. ±320 acres"),
    ("price", "Price", "e.g. Submit, or Call for price"),
    ("taxes", "Taxes", "e.g. $49,587.28 (2021)"),
    ("zoning_claim", "Zoning per the flyer", "e.g. I-2, City of Casa Grande"),
    ("parcel_numbers", "Parcel numbers on the flyer", "e.g. 503-26-020E, 503-26-020D, 503-26-020F"),
    ("concept_plan", "Concept plan", "e.g. Lot 1 56 ac, 1,000,000 SF; Lot 2 47 ac, 400,000 SF"),
)
LIST_FIELDS = (
    ("access", "Access and traffic", "Interchanges, drive times to the nearest interchange, traffic counts, rail"),
    ("highlights", "Flyer highlights", "Selling points the flyer lists"),
    ("market", "Market", "Population, jobs, investment figures"),
    ("nearby", "Nearby employers and projects", "Neighbours the flyer names"),
    ("documents", "Documents in the flyer", "Surveys, studies, will-serve letters, links"),
)
UTILITY_LABELS = ("Electric", "Water", "Sewer", "Natural gas", "Telecom", "Irrigation")
CLAIM_STATUSES = ("confirmed", "partly confirmed", "differs", "note", "not checked")
BLANK_UTILITY_ROWS, BLANK_CONTACT_ROWS, BLANK_DRIVE_ROWS, BLANK_CLAIM_ROWS = 2, 2, 4, 3
CONTACT_FIELDS = ("name", "firm", "phone", "email", "address")


def display(listing):
    """The listing with every key present and utilities as ordered rows: for the brief and the form."""
    listing = listing or {}
    out = {k: (listing.get(k) or "") for k, _, _ in TEXT_FIELDS}
    out.update({k: list(listing.get(k) or []) for k, _, _ in LIST_FIELDS})
    util = dict(listing.get("utilities") or {})
    out["utility_rows"] = [(label, util.pop(label, "")) for label in UTILITY_LABELS] + [(k, v) for k, v in util.items()]
    out["contacts"] = [c for c in listing.get("contacts") or [] if c.get("name")]
    out["drive_times"] = [list(r) for r in listing.get("drive_times") or []]
    out["claims_check"] = list(listing.get("claims_check") or [])
    out["has_any"] = bool(listing)
    return out


def form_rows(listing):
    """display() plus blank rows to fill in, for the edit form."""
    d = display(listing)
    d["utility_rows"] = d["utility_rows"] + [("", "")] * BLANK_UTILITY_ROWS
    d["contacts"] = d["contacts"] + [{}] * max(BLANK_CONTACT_ROWS, 3 - len(d["contacts"]))
    d["drive_times"] = d["drive_times"] + [["", ""]] * BLANK_DRIVE_ROWS
    d["claims_check"] = d["claims_check"] + [{}] * BLANK_CLAIM_ROWS
    d["list_text"] = {k: "\n".join(d[k]) for k, _, _ in LIST_FIELDS}
    return d


def _rows(post, prefix, fields, limit=80):
    """Rows posted as <prefix>_<i>_<field> with consecutive i (blank rows included)."""
    rows = []
    for i in range(limit):
        if not any(f"{prefix}_{i}_{f}" in post for f in fields):
            break
        rows.append({f: (post.get(f"{prefix}_{i}_{f}") or "").strip() for f in fields})
    return rows


def parse(post):
    """The listing dict from a posted form; empty things are left out (they show as N/A)."""
    data = {}
    for key, _, _ in TEXT_FIELDS:
        value = " ".join((post.get(key) or "").split())
        if value:
            data[key] = value
    for key, _, _ in LIST_FIELDS:
        lines = [" ".join(line.split()) for line in (post.get(key) or "").splitlines() if line.strip()]
        if lines:
            data[key] = lines
    util = {r["label"]: r["value"] for r in _rows(post, "util", ("label", "value")) if r["label"] and r["value"]}
    if util:
        data["utilities"] = util
    contacts = [r for r in _rows(post, "contact", CONTACT_FIELDS) if r["name"]]
    if contacts:
        data["contacts"] = [{k: v for k, v in r.items() if v} for r in contacts]
    drive = [[r["place"], r["value"]] for r in _rows(post, "drive", ("place", "value")) if r["place"] and r["value"]]
    if drive:
        data["drive_times"] = drive
    claims = [r for r in _rows(post, "claim", ("claim", "found", "status")) if r["claim"]]
    if claims:
        data["claims_check"] = [{"claim": r["claim"], "found": r["found"],
                                 "status": r["status"] if r["status"] in CLAIM_STATUSES else "not checked"} for r in claims]
    return data
