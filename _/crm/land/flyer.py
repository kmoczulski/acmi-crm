"""Flyer intake: drop a broker's flyer (PDF or image) on a site and Claude fills the listing details (what the flyer
says) and the claims table (each factual claim against what the CRM found about the land: acres, zoning, floodplain,
wetlands, utilities, distances). Only blank listing fields are filled, and claims already in the table stay.
"""
import base64
import json

from django.utils import timezone

from . import listing, score
from .tracker import site_deals

MAX_BYTES = 25 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
SUFFIXES = (".pdf", *IMAGE_TYPES)  # what a flyer may be (and what opens in the browser rather than downloading)
WRONG_TYPE = "Upload the flyer as a PDF or an image (PNG or JPG)."
NEAR_LABELS = (("highway", "a major highway"), ("rail", "rail"), ("transmission", "a transmission line"),
               ("substation", "a substation"), ("gas_pipeline", "a gas pipeline"), ("military", "a military installation"))

SYSTEM = """You read land listing flyers for ACMI Properties, which assembles land for manufacturing campuses for \
defense, aerospace and energetics companies. First pull out what the flyer says, for the site's listing record: \
location, size, price, taxes, zoning per the flyer, parcel numbers, concept plan; access and traffic, highlights, \
market figures, nearby employers and projects, documents the flyer lists; utilities by type as the flyer states \
them; broker contacts; drive times. Keep the flyer's own figures and wording where it matters. Leave out anything \
the flyer doesn't say (empty string or empty list).

Then check the flyer's factual claims (acreage, zoning, utilities, floodplain or wetlands, access, rail, distances) \
against the facts from the CRM below. One row per claim: the claim as the flyer makes it, what the CRM found, and a \
status: "confirmed", "partly confirmed", "differs", "not checked" when the CRM has nothing to check it against, or \
"note" for a caveat worth flagging. Never invent a fact that neither the flyer nor the CRM gives."""

_STR = {"type": "string"}
_LIST = {"type": "array", "items": {"type": "string"}}


def _obj(*fields):
    return {"type": "object", "properties": {f: _STR for f in fields}, "required": list(fields),
            "additionalProperties": False}


SCHEMA = {
    "type": "object",
    "properties": {
        **{key: _STR for key, _, _ in listing.TEXT_FIELDS},
        **{key: _LIST for key, _, _ in listing.LIST_FIELDS},
        "utilities": {"type": "array", "items": _obj("label", "value")},
        "contacts": {"type": "array", "items": _obj(*listing.CONTACT_FIELDS)},
        "drive_times": {"type": "array", "items": _obj("place", "value")},
        "claims_check": {"type": "array", "items": {
            "type": "object",
            "properties": {"claim": _STR, "found": _STR,
                           "status": {"type": "string", "enum": list(listing.CLAIM_STATUSES)}},
            "required": ["claim", "found", "status"], "additionalProperties": False}},
    },
    "required": [key for key, _, _ in listing.TEXT_FIELDS] + [key for key, _, _ in listing.LIST_FIELDS]
    + ["utilities", "contacts", "drive_times", "claims_check"],
    "additionalProperties": False,
}


def crm_facts(site):
    """What the CRM knows about the site's land, for checking the flyer's claims."""
    deals = [d for d in site_deals(site) if d.included]
    acres = sum(d.acres or 0 for d in deals)
    lines = [f"SITE: {site.name}; {acres:,.1f} acres across {len(deals)} parcel{'s' if len(deals) != 1 else ''}"
             + (f"; {site.campus.code} campus" if site.campus_id else "")]
    for d in deals:
        p, c = d.parcel, d.parcel.checks or {}
        bits = [f"{d.acres:,.1f} ac" + (" (trimmed portion)" if d.trimmed else "")]
        if p.owner_name:
            bits.append(f"owner {p.owner_name}")
        if p.situs_address:
            bits.append(f"address {p.situs_address}" + (f", {p.situs_city}" if p.situs_city else ""))
        if p.zoning:
            bits.append(f"zoning {p.zoning}")
        use = score.land_use_label(p.state_land_use) if p.state_land_use else ""
        if use:
            bits.append(f"land use {use}")
        flood = c.get("flood") or {}
        if "sfha_pct" in flood:
            bits.append(f"FEMA 100-year floodplain {flood['sfha_pct']}%" + (
                f" (zones {', '.join(flood.get('zones') or [])})" if flood.get("zones") else ""))
        wet = c.get("wetlands") or {}
        if "pct" in wet:
            bits.append(f"mapped wetlands {wet['pct']}%")
        if (c.get("net_usable") or {}).get("acres") is not None:
            bits.append(f"net usable {c['net_usable']['acres']} ac")
        for key in ("electric", "water", "sewer", "gas"):
            providers = (c.get(key) or {}).get("providers") or []
            if providers:
                bits.append(f"{key} service area: {', '.join(providers[:3])}")
        for layer, label in NEAR_LABELS:
            n = (p.near or {}).get(layer)
            if n and n.get("miles") is not None:
                bits.append(f"{n['miles']:.1f} mi to {label}" + (f" ({n['name']})" if n.get("name") else ""))
        lines.append(f"- Parcel {p.prop_id or p.key} ({p.county.name} County, {p.county.state}): " + "; ".join(bits))
    if not deals:
        lines.append("(No parcels on the site yet, so there is nothing to check the claims against.)")
    return "\n".join(lines)


def _clean(value):
    return " ".join((value or "").split())


def merge(current, found):
    """(listing, [what was filled]): the flyer's facts added where the listing is blank; its claims appended unless the
    table already has them."""
    out, filled = dict(current or {}), []

    def fill(key, value, label):
        if value and not out.get(key):
            out[key] = value
            filled.append(label)

    for key, label, _ in listing.TEXT_FIELDS:
        fill(key, _clean(found.get(key)), label)
    for key, label, _ in listing.LIST_FIELDS:
        fill(key, [_clean(x) for x in found.get(key) or [] if _clean(x)], label)
    utilities = dict(out.get("utilities") or {})
    for row in found.get("utilities") or []:
        label, value = _clean(row.get("label")), _clean(row.get("value"))
        if label and value and label not in utilities:
            utilities[label] = value
    if utilities != (out.get("utilities") or {}):
        out["utilities"] = utilities
        filled.append("Utilities")
    fill("contacts", [{k: _clean(v) for k, v in c.items() if _clean(v)} for c in found.get("contacts") or []
                      if _clean(c.get("name"))], "Contacts")
    fill("drive_times", [[_clean(r.get("place")), _clean(r.get("value"))] for r in found.get("drive_times") or []
                         if _clean(r.get("place")) and _clean(r.get("value"))], "Drive times")
    have = {_clean(c.get("claim")).lower() for c in out.get("claims_check") or []}
    new = [{"claim": _clean(c["claim"]), "found": _clean(c.get("found")),
            "status": c.get("status") if c.get("status") in listing.CLAIM_STATUSES else "not checked"}
           for c in found.get("claims_check") or [] if _clean(c.get("claim")) and _clean(c["claim"]).lower() not in have]
    if new:
        out["claims_check"] = list(out.get("claims_check") or []) + new
        filled.append(f"{len(new)} claim{'s' if len(new) != 1 else ''} checked")
    return out, filled


def read(site, data, filename, user=None):
    """Have Claude read the flyer and fill the site's listing. Returns what was filled. Raises contracts.ai.Unavailable."""
    from ..contracts import ai

    if len(data) > MAX_BYTES:
        raise ai.Unavailable("Flyers up to 25 MB.")
    suffix = "." + filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    encoded = base64.standard_b64encode(data).decode()
    if suffix == ".pdf":
        block = {"type": "document", "title": filename,
                 "source": {"type": "base64", "media_type": "application/pdf", "data": encoded}}
    elif suffix in IMAGE_TYPES:
        block = {"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[suffix], "data": encoded}}
    else:
        raise ai.Unavailable(WRONG_TYPE)
    content = [block, {"type": "text", "text": "FACTS FROM THE CRM (not from the flyer)\n" + crm_facts(site)}]
    message = ai._stream(SYSTEM, content, 8000, {"effort": ai.EFFORT,
                                                 "format": {"type": "json_schema", "schema": SCHEMA}})
    if message.stop_reason == "max_tokens":
        raise ai.Unavailable("Claude's reading of the flyer was cut off. Try again.")
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        found = json.loads(text)
    except ValueError as exc:
        raise ai.Unavailable("Claude's reading of the flyer couldn't be read. Try again.") from exc
    site.listing, filled = merge(site.listing, found)
    site.flyer_read_at = timezone.now()
    site.save(update_fields=["listing", "flyer_read_at", "updated_at"])
    return filled
