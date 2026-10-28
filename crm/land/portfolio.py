"""Owner groups: stored parcels that look like one owner's holdings, even when each sits in a different LLC.

The rule (docs/HANDOFF.md says the same):
- Two parcels are in one group when their owner names are the same after normalising (parcels.owner_key) and they
  are in the same county, or when their owners mail to the same address (street + 5-digit ZIP, ignoring case,
  punctuation, suite / unit numbers and street-word spellings), in any county.
- An address only links owners when it looks like an owner's own: not when the c/o line is a tax service, bank,
  law firm or accountant (GENERIC_CARE), not when more than MAX_OWNERS_PER_ADDRESS different owner names use it
  (a registered agent, a management company), and a PO box only up to MAX_OWNERS_PER_PO_BOX names.
- Links chain: A and B share a name, B and C an address, so A, B and C are one group.
Parcels with no other stored parcel in their group have a blank portfolio_key. Rebuilt after every load, owner-file
join and sale refresh (all counties at once, since addresses link across counties).
"""
import hashlib
import re
from collections import defaultdict

from django.db import transaction

from ..models import Parcel
from .parcels import owner_key

MAX_OWNERS_PER_ADDRESS = 5
MAX_OWNERS_PER_PO_BOX = 3
GENERIC_CARE = re.compile(r"\b(TAX|TAXES|ESCROW|BANK|MORTGAGE|LAW|LAWYERS?|ATTORNEYS?|ATTY|ESQ|CPA|ACCOUNTING|"
                          r"CORELOGIC|LERETA|REGISTERED AGENT|CORPORATION SERVICE|CT CORPORATION)\b")
NO_OWNER = {"", "NULL", "UNKNOWN", "UNKNOWN OWNER", "OWNER UNKNOWN", "NOT AVAILABLE", "N/A", "NA"}
_WORDS = {
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "BOULEVARD": "BLVD",
    "HIGHWAY": "HWY", "COURT": "CT", "PLACE": "PL", "PARKWAY": "PKWY", "CIRCLE": "CIR", "TERRACE": "TER",
    "TRAIL": "TRL", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "NORTHEAST": "NE", "NORTHWEST": "NW",
    "SOUTHEAST": "SE", "SOUTHWEST": "SW", "FARM TO MARKET": "FM", "COUNTY ROAD": "CR",
}
# Not RM: in Texas that's a road (RM 2325).
_UNIT = re.compile(r"\b(STE|SUITE|UNIT|APT|APARTMENT|FL|FLOOR|ROOM|BLDG|BUILDING|DEPT)\b\s*[A-Z0-9-]*")
_PO = re.compile(r"\b(P\s?O|POST OFFICE|POST OFF)\s*(BOX|BX|DRAWER)\b|\bBOX\b")


def address_key(street, zipcode):
    """'1200 N. Main Street, Suite 400' + '20646-0807' -> '1200 N MAIN ST|20646'. Blank when either part is."""
    zip5 = re.sub(r"\D", "", zipcode or "")[:5]
    text = (street or "").upper().replace("#", " UNIT ")
    text = re.sub(r"[.,;:'\"]", " ", text)
    text = _PO.sub(" PO BOX ", text)
    text = _UNIT.sub(" ", text)
    for long, short in _WORDS.items():
        text = re.sub(rf"\b{long}\b", short, text)
    text = " ".join(text.split())
    return f"{text}|{zip5}" if text and len(zip5) == 5 else ""


def _find(parent, x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def groups(rows):
    """rows: (pk, county_id, owner_name, care_of, mail_address, mail_zip) -> [set of pks], groups of 2+ only."""
    parent = {}
    by_name, by_address, names_at = defaultdict(list), defaultdict(list), defaultdict(set)
    for pk, county_id, owner, care_of, street, zipcode in rows:
        parent[pk] = pk
        name = owner_key(owner)
        if name not in NO_OWNER:
            by_name[(county_id, name)].append(pk)
        addr = address_key(street, zipcode)
        if addr and not GENERIC_CARE.search((care_of or "").upper()):
            by_address[addr].append(pk)
            names_at[addr].add(name)

    def union(pks):
        root = _find(parent, pks[0])
        for pk in pks[1:]:
            other = _find(parent, pk)
            if other != root:
                parent[other] = root

    for pks in by_name.values():
        if len(pks) > 1:
            union(pks)
    for addr, pks in by_address.items():
        limit = MAX_OWNERS_PER_PO_BOX if addr.startswith("PO BOX") else MAX_OWNERS_PER_ADDRESS
        if len(pks) > 1 and len(names_at[addr] - NO_OWNER) <= limit:
            union(pks)
    out = defaultdict(set)
    for pk in parent:
        out[_find(parent, pk)].add(pk)
    return [g for g in out.values() if len(g) > 1]


def rebuild(log=None):
    """Recompute every stored parcel's owner group. Returns the number of groups."""
    rows = list(Parcel.objects.values_list("pk", "county_id", "owner_name", "care_of", "mail_address", "mail_zip"))
    info = {pk: (key, fips, acres) for pk, key, fips, acres in
            Parcel.objects.values_list("pk", "key", "county__fips", "acres")}
    new = {}
    found = groups(rows)
    for g in found:
        seed = min(f"{info[pk][1]}:{info[pk][0]}" for pk in g)
        key = "g" + hashlib.sha1(seed.encode()).hexdigest()[:12]
        acres = round(sum(info[pk][2] or 0 for pk in g), 1)
        for pk in g:
            new[pk] = (key, len(g), acres)
    changed = []
    for p in Parcel.objects.only("pk", "portfolio_key", "portfolio_parcels", "portfolio_acres").iterator(chunk_size=5000):
        want = new.get(p.pk, ("", None, None))
        if (p.portfolio_key, p.portfolio_parcels, p.portfolio_acres) != want:
            p.portfolio_key, p.portfolio_parcels, p.portfolio_acres = want
            changed.append(p)
    with transaction.atomic():
        Parcel.objects.bulk_update(changed, ["portfolio_key", "portfolio_parcels", "portfolio_acres"], batch_size=1000)
    if log:
        log(f"Owner groups: {len(found):,} groups of 2+ parcels; {len(changed):,} parcels changed")
    return len(found)


def members(parcel, limit=25):
    """The other parcels in this parcel's owner group, largest first."""
    if not parcel.portfolio_key:
        return []
    return list(Parcel.objects.filter(portfolio_key=parcel.portfolio_key).exclude(pk=parcel.pk)
                .select_related("county").order_by("-acres")[:limit])


def summary(parcel):
    """{parcels, acres, counties, others, more} for the parcel card, or None outside a group."""
    if not parcel.portfolio_key:
        return None
    others = members(parcel)
    counties = sorted({f"{p.county.name} {p.county.state}" for p in others} | {f"{parcel.county.name} {parcel.county.state}"})
    return {"parcels": parcel.portfolio_parcels or len(others) + 1, "acres": parcel.portfolio_acres,
            "counties": counties, "others": others,
            "more": max(0, (parcel.portfolio_parcels or 0) - 1 - len(others)),
            "names": sorted({p.owner_name for p in others if p.owner_name and p.owner_name != parcel.owner_name})}
