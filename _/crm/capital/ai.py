"""Claude reads a lender's or investor's term sheet into its key terms, keeping the sentence behind each one so the
page can show where every number came from."""
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from ..contracts import ai
from ..contracts.ai import Unavailable  # noqa: F401 - callers catch it from here
from . import logic

TERMS_SYSTEM = """You read a term sheet, quote, LOI or commitment letter from a lender or equity investor for ACMI \
Properties, which develops purpose-built manufacturing campuses for defense, aerospace and energetics companies. \
Give each business term the document states, once, with the sentence or table row that sets it copied word for \
word. Skip terms it doesn't state.

Values: dollars and percents as plain numbers (digits and a decimal point; 65 for 65%); spread_bps in basis points \
(SOFR + 3.25% is 325); term_months as a whole number of months; dates as YYYY-MM-DD, only when the document fixes \
the date (for "expires 30 days after the date hereof", work it out from the document's date and say so in "note"). \
rate is the pricing as quoted, in a few words. amount is the loan amount or equity commitment (the maximum, when a \
range is given; say so in "note"). Text terms (recourse, guaranty, covenants, prepayment, conditions...) in a short \
phrase or a few; covenants and conditions may list several, separated by semicolons. expires_on is when the offer or \
commitment lapses if not accepted; close_by is the outside date to close the loan or investment."""

FIELDS = [f for f, _, _ in logic.TERMS]

TERMS_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": FIELDS},
                    "value": {"type": "string"},
                    "quote": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["field", "value", "quote", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["terms"],
    "additionalProperties": False,
}


def parse(field, raw):
    """A term's value from Claude's string, typed for its field; None when it can't be read."""
    raw = (raw or "").strip()
    if not raw:
        return None
    kind = logic.TERM_KINDS[field]
    model_field = logic.TermSheet._meta.get_field(field)
    if kind == "date":
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    if kind in ("text", "long"):
        return raw[:model_field.max_length] if getattr(model_field, "max_length", None) else raw
    digits = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    if kind == "months":
        return int(value) if 0 < value < 1000 else None
    return value


def _context(ts):
    s = ts.source
    parts = [f"{s.get_kind_display()} for {s.project.name}", f"from {s.provider}", f"version {ts.version}"]
    if ts.dated:
        parts.append(f"dated {ts.dated:%B} {ts.dated.day}, {ts.dated.year}")
    return "; ".join(parts)


def read_terms(ts, user=None):
    """Fill the term sheet's blank terms from its file, keeping the quote behind each (with its place in our text and
    its page). Returns the labels of the terms filled in. Raises Unavailable."""
    block = ai.source_block(ts, ts.title or str(ts), _context(ts))
    del block["citations"]  # structured output can't be combined with citations; quotes are matched back below
    today = timezone.localdate()
    message = ai._stream(TERMS_SYSTEM, [block, {"type": "text", "text": f"Today is {today:%B} {today.day}, {today.year}."}],
                         8000, {"effort": "high", "format": {"type": "json_schema", "schema": TERMS_SCHEMA}})
    if message.stop_reason == "max_tokens":
        raise Unavailable("Claude's reading of the term sheet was cut off. Try again.")
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        found = json.loads(text)["terms"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Unavailable("Claude's reading of the term sheet couldn't be read. Try again.") from exc
    sources, filled = dict(ts.sources or {}), []
    for item in found:
        field = item.get("field")
        if field not in logic.TERM_KINDS:
            continue
        value = parse(field, item.get("value"))
        if value is None or getattr(ts, field) not in (None, ""):
            continue
        setattr(ts, field, value)
        quote = (item.get("quote") or "")[:1500]
        start, end = ai.anchor(ts.text, None, None, quote) if ts.text else (None, None)
        sources[field] = {"quote": quote, "start": start, "end": end,
                          "page": ts.page_at(start) if start is not None else None,
                          "note": (item.get("note") or "")[:300]}
        filled.append(logic.term_label(field))
    ts.sources, ts.read_at = sources, timezone.now()
    ts.save()
    return filled
