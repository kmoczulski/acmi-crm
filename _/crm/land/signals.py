"""Off-market signals: why an owner might sell land that isn't listed. Worked out from the stored owner, mailing and
last-transfer fields at every load and re-score, and kept apart from the buy-box score (Parcel.motivation is its own
sort).

- long_hold: last transfer 20+ years ago (only where the county publishes a transfer date)
- estate: estate, heirs, life estate, deceased in the owner name
- family: ET AL / ET UX / ET VIR (several family members on the title)
- trust: held in a trust
- out_of_state / absentee: owner mails from another state, or from outside the ZIP areas the county's land is in
- non_arms: the last transfer wasn't arms-length (inherited, gift, related parties), where the county says so
Tax-delinquent: none of the loaded counties publish it in their parcel services, so there is no signal for it.
"""
import re
import time
from datetime import date

from ..models import Parcel

LONG_HOLD_YEARS = 20
WEIGHTS = {"estate": 3, "long_hold": 2, "non_arms": 2, "out_of_state": 2, "absentee": 1, "family": 1, "trust": 1}
LABELS = {
    "long_hold": "Long hold", "estate": "Estate / heirs", "family": "Family (ET AL)", "trust": "Trust",
    "out_of_state": "Out-of-state owner", "absentee": "Absentee owner", "non_arms": "Inherited / non-arms-length",
}
FILTERS = [("long_hold", "Long hold (20+ yrs)"), ("estate", "Estate / heirs / life estate"),
           ("family", "Family (ET AL)"), ("trust", "Trust"), ("absentee", "Absentee or out-of-state owner"),
           ("non_arms", "Inherited / non-arms-length transfer")]

ESTATE = re.compile(r"\b(ESTATE|EST OF|HEIRS?|HRS OF|DECEASED|DEC'D|DECD|LIFE EST|L/E|DEVISEES?|SURVIVING)\b")
NOT_ESTATE = re.compile(r"\bREAL ESTATE\b|\bESTATE(S)? (HOLDINGS|DEVELOPMENT|PARTNERS|INVEST)")
FAMILY = re.compile(r"\bET\s?(AL|UX|VIR)\b")
TRUST = re.compile(r"\b(TRUST|TRUSTEES?|TRS|REV TR|LIV TR|LIVING TR|FAMILY TR|TR)\b")
TRUST_COMPANY = re.compile(r"\bTRUST (CO|COMPANY|BANK)\b|\bBANK\b")
AREA_CACHE_SECONDS = 300  # loads and re-scores reset it; a running web process re-reads it now and then
_area_cache = {}


def reset_cache():
    _area_cache.clear()


def area_zip3s(county_id):
    """First three ZIP digits of the land's own addresses in the county (situs ZIPs): mail from outside them is
    absentee. Empty where the county publishes no situs ZIPs, and then only out-of-state owners count."""
    hit = _area_cache.get(county_id)
    if hit is None or time.time() - hit[1] > AREA_CACHE_SECONDS:
        zips = Parcel.objects.filter(county_id=county_id).exclude(situs_zip="").values_list("situs_zip", flat=True)
        hit = _area_cache[county_id] = ({z.strip()[:3] for z in zips if re.match(r"\d{3}", z.strip())}, time.time())
    return hit[0]


def years_held(p, today=None):
    if not p.last_sale_date:
        return None
    today = today or date.today()
    return today.year - p.last_sale_date.year - ((today.month, today.day) < (p.last_sale_date.month, p.last_sale_date.day))


def compute(p, today=None, zip3s=None):
    """The parcel's signal codes, in a fixed order."""
    from .score import public_owner

    names = " ".join(" ".join((x or "").upper().split()) for x in (p.owner_name, p.care_of))
    if not names.strip() or public_owner(p.owner_name):
        return []
    out = []
    held = years_held(p, today)
    if held is not None and held >= LONG_HOLD_YEARS:
        out.append("long_hold")
    if ESTATE.search(NOT_ESTATE.sub("", names)):
        out.append("estate")
    if FAMILY.search(names):
        out.append("family")
    if TRUST.search(names) and not TRUST_COMPANY.search(names):
        out.append("trust")
    state = (p.county.state or "").upper() if p.county_id else ""
    mail_state = (p.mail_state or "").upper()
    if mail_state and state and mail_state != state:
        out.append("out_of_state")
    else:
        zip3s = area_zip3s(p.county_id) if zip3s is None and p.county_id else (zip3s or set())
        mz = re.sub(r"\D", "", p.mail_zip or "")[:3]
        if mz and len(mz) == 3 and zip3s and mz not in zip3s:
            out.append("absentee")
    if p.last_sale_arms_length is False:
        out.append("non_arms")
    return out


def apply(p, today=None, zip3s=None):
    codes = compute(p, today, zip3s)
    p.signals = f" {' '.join(codes)} " if codes else ""
    p.motivation = sum(WEIGHTS[c] for c in codes)
    return p


def apply_county(county):
    """Recompute and store signals for every parcel in a county (after a sale refresh)."""
    reset_cache()
    zip3s = area_zip3s(county.pk)
    batch = []
    for p in Parcel.objects.filter(county=county).select_related("county").only(
            "pk", "county", "owner_name", "care_of", "mail_state", "mail_zip", "last_sale_date",
            "last_sale_arms_length", "signals", "motivation"):
        before = (p.signals, p.motivation)
        apply(p, zip3s=zip3s)
        if (p.signals, p.motivation) != before:
            batch.append(p)
    Parcel.objects.bulk_update(batch, ["signals", "motivation"], batch_size=500)
    return len(batch)


def chips(p, today=None):
    """[(code, label, detail)] for the parcel card."""
    out = []
    for code in (p.signals or "").split():
        label, detail = LABELS.get(code, code), ""
        if code == "long_hold":
            label = f"Owned since {p.last_sale_date.year}" if p.last_sale_date else label
            detail = f"Last transfer {p.last_sale_date:%b %Y}, {years_held(p, today)} years ago"
        elif code == "out_of_state":
            label = f"Out-of-state owner ({p.mail_state})"
        elif code == "absentee":
            detail = "Mails from outside the ZIP areas this county's land is in"
        elif code == "non_arms":
            detail = p.last_sale_type or "The county marks the last transfer as not arms-length"
        elif code in ("estate", "family", "trust"):
            detail = "From the owner name on the tax roll"
        out.append((code, label, detail))
    return out


def filter_q(codes):
    """Q for parcels with any of these signals (the Signals filter on land search)."""
    from django.db.models import Q

    cond = Q()
    for code in codes:
        for c in (("absentee", "out_of_state") if code == "absentee" else (code,)):
            cond |= Q(signals__contains=f" {c} ")
    return cond
