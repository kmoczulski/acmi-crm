"""Signed leases as numbers: the terms (typed, or read from the lease as amended by Claude, with the language behind
each), the rent today, the renewal notice deadline, the rent roll with its weighted average lease term (WALT), and the
critical dates the terms imply (expiration, renewal notice, rent commencement), kept in step with them."""
import calendar
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from ..models import Contract, ContractDate, LeaseTerms

TERM_FIELDS = [  # (field, kind)
    ("premises_sf", "int"), ("land_acres", "dec"), ("commencement", "date"), ("rent_start", "date"),
    ("expiration", "date"), ("base_rent", "dec"), ("escalation_pct", "dec"), ("free_months", "int"),
    ("renewals", "int"), ("renewal_years", "dec"), ("renewal_notice_months", "int"), ("security_deposit", "dec"),
    ("ti_allowance", "dec"), ("guarantor", "text"),
]
AUTO = "From the lease terms"  # the `rule` that marks critical dates this module keeps in step with the terms
NOTICE_SOON_MONTHS = 18  # a renewal notice deadline this close is flagged on the rent roll

TERMS_SYSTEM = """You read a lease, and any amendments in the order they took effect (later ones control), for ACMI \
Properties' rent roll. Give each business term as it stands now, once. For each term you find, give its value, the \
number of the document it comes from, and the sentence that sets it copied word for word. Skip terms the lease doesn't \
state.

Values: square feet, acres, dollars and percents as plain numbers; months and counts as whole numbers; dates as \
YYYY-MM-DD, only when the lease fixes the date (skip a date that hangs on an event). base_rent is the annual base \
rent in the first lease year: convert monthly rent or rent per square foot to annual dollars and say how in "note". \
escalation_pct is the yearly increase in percent. renewal_notice_months is how many months before expiration the \
tenant must give notice to renew."""

TERMS_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": [f for f, _ in TERM_FIELDS]},
                    "value": {"type": "string"},
                    "document": {"type": "integer"},
                    "quote": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["field", "value", "document", "quote", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["terms"],
    "additionalProperties": False,
}


def add_months(d, months):
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def notice_deadline(t):
    """The last day to give notice of renewal, when the lease sets one."""
    if t and t.expiration and t.renewal_notice_months:
        return add_months(t.expiration, -t.renewal_notice_months)
    return None


def rent_now(t, today=None):
    """Annual base rent today: the first-year rent stepped up by the escalation for each full lease year since rent
    commencement (the first-year rent until then). None without a base rent."""
    if t is None or t.base_rent is None:
        return None
    today = today or timezone.localdate()
    if not t.rent_start or today < t.rent_start:
        return t.base_rent
    years = 0
    while add_months(t.rent_start, 12 * (years + 1)) <= today:
        years += 1
    rate = 1 + (t.escalation_pct or Decimal(0)) / 100
    return (t.base_rent * rate ** years).quantize(Decimal("1"))


def years_left(t, today=None):
    today = today or timezone.localdate()
    if t is None or not t.expiration or t.expiration <= today:
        return 0.0
    return round((t.expiration - today).days / 365.25, 1)


def terms_for(contract):
    try:
        return contract.lease_terms
    except LeaseTerms.DoesNotExist:
        return None


def rent_roll(campus=None, today=None):
    """Signed leases with their terms: rows grouped by campus, and totals (sf, annual rent, rent-weighted WALT)."""
    today = today or timezone.localdate()
    qs = Contract.objects.filter(kind=Contract.Kind.LEASE, status=Contract.Status.SIGNED).select_related(
        "campus", "site", "opportunity__company")
    if campus is not None:
        qs = qs.filter(campus=campus)
    rows = []
    for c in qs.order_by("campus__code", "title"):
        t = terms_for(c)
        rent = rent_now(t, today)
        sf = t.premises_sf if t else None
        notice = notice_deadline(t)
        rows.append({
            "contract": c, "terms": t, "campus": c.campus.code if c.campus_id else "No campus",
            "tenant": (c.opportunity.company.name if c.opportunity_id else "") or c.counterparty or c.title,
            "rent": rent, "psf": (rent / sf).quantize(Decimal("0.01")) if rent and sf else None,
            "years_left": years_left(t, today), "notice": notice,
            "notice_soon": bool(notice and today <= notice <= add_months(today, NOTICE_SOON_MONTHS)),
            "expiring": bool(t and t.expiration and t.expiration <= add_months(today, 24)),
        })
    weighted = [r for r in rows if r["rent"] and r["years_left"]]
    rent_total = sum((r["rent"] for r in rows if r["rent"]), Decimal(0))
    walt = (sum(float(r["rent"]) * r["years_left"] for r in weighted) / float(sum(r["rent"] for r in weighted))
            if weighted else None)
    groups = []
    for r in rows:
        if not groups or groups[-1][0] != r["campus"]:
            groups.append((r["campus"], []))
        groups[-1][1].append(r)
    return {"rows": rows, "groups": groups, "today": today,
            "totals": {"sf": sum(r["terms"].premises_sf or 0 for r in rows if r["terms"]), "rent": rent_total,
                       "walt": round(walt, 1) if walt is not None else None, "count": len(rows)}}


def sync_dates(contract, t, user=None):
    """Keep the lease's expiration, renewal notice and rent commencement on its critical dates. Dates this module
    added follow the terms; one someone kept by hand of the same kind is left alone. Past ones are added as done."""
    today = timezone.localdate()
    wanted = [(ContractDate.Kind.EXPIRATION, "Lease expiration", t.expiration if t else None),
              (ContractDate.Kind.NOTICE, "Renewal notice deadline", notice_deadline(t)),
              (ContractDate.Kind.PAYMENT, "Rent commencement", t.rent_start if t else None)]
    for kind, label, when in wanted:
        mine = contract.dates.filter(kind=kind, rule=AUTO).first()
        if when is None:
            if mine:
                mine.delete()
            continue
        if mine:
            if mine.date != when:
                mine.date, mine.done = when, when < today
                mine.save(update_fields=["date", "done"])
        elif not contract.dates.filter(kind=kind, confirmed=True).exists():
            ContractDate.objects.create(contract=contract, kind=kind, label=label, date=when, rule=AUTO,
                                        done=when < today, created_by=user)


def _parse(kind, raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    if kind == "date":
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    if kind == "text":
        return raw[:200]
    digits = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    return int(value) if kind == "int" else value


def read_terms(contract, user=None):
    """Have Claude read the lease (as amended) and fill the blank terms, keeping the quote behind each. Returns the
    labels of the terms filled in. Raises contracts.ai.Unavailable."""
    from ..contracts import ai

    docs = ai.fit(ai.default_docs(contract))
    if not docs:
        raise ai.Unavailable("Upload the lease first (a PDF or Word file with text).")
    content = ai.doc_blocks(docs, citations=False) + [{"type": "text", "text": ai.facts(contract, docs)}]
    message = ai._stream(TERMS_SYSTEM, content, 8000,
                         {"effort": "high", "format": {"type": "json_schema", "schema": TERMS_SCHEMA}})
    if message.stop_reason == "max_tokens":
        raise ai.Unavailable("Claude's reading of the lease was cut off. Try again.")
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        found = json.loads(text)["terms"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ai.Unavailable("Claude's reading of the lease couldn't be read. Try again.") from exc
    t = terms_for(contract) or LeaseTerms(contract=contract)
    kinds = dict(TERM_FIELDS)
    sources, filled = dict(t.sources or {}), []
    for item in found:
        field = item.get("field")
        value = _parse(kinds.get(field), item.get("value"))
        if field not in kinds or value is None or getattr(t, field) not in (None, ""):
            continue
        setattr(t, field, value)
        doc = docs[item["document"] - 1] if 1 <= item.get("document", 0) <= len(docs) else None
        start, end = ai.anchor(doc.text, None, None, item["quote"]) if doc is not None and doc.text else (None, None)
        sources[field] = {"doc": doc.pk if doc else None, "quote": item["quote"][:1500], "start": start, "end": end,
                          "note": item.get("note", "")[:300]}
        filled.append(LeaseTerms._meta.get_field(field).verbose_name)
    t.sources, t.updated_by = sources, user
    t.save()
    sync_dates(contract, t, user)
    return filled
