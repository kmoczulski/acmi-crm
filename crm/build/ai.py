"""Claude on a project's GC proposals: level the bids side by side (every number cited to the proposal it comes from),
answer questions about them, and read one proposal's price, fee, general conditions and schedule into its record."""
import json
from decimal import Decimal, InvalidOperation

from ..contracts import ai
from ..contracts.ai import Unavailable  # noqa: F401 - callers catch it from here
from ..models import ProjectAnswer

ABOUT = """You help ACMI Properties choose general contractors. ACMI develops purpose-built manufacturing campuses for \
defense, aerospace and energetics companies and builds the facilities its tenants occupy. You get the GC proposals \
for one project, one document per bidder."""

LEVEL_SYSTEM = ABOUT + """ Level them so the team can compare the bids on the same scope:

- Side by side: base price, fee, general conditions, insurance and bonds, contingency, allowances, alternates, unit \
prices, schedule and key milestones, key staff.
- Each proposal's exclusions, clarifications and assumptions, and what one includes that another leaves out.
- Scope gaps, risky qualifications, low allowances, and anything likely to come back as a change order.
- End with the questions to ask each bidder.
Cite the proposals for every number and claim; write "not stated" where a proposal is silent. Brief and plain: "## " \
headings and "- " bullets. You are not giving legal advice."""

ASK_SYSTEM = ABOUT + """ Answer the team's question from the proposals, citing them for every number and claim. If \
they don't say, say so. Brief and plain; "- " bullets when it helps."""

TERMS_SYSTEM = """You read one general contractor's proposal for ACMI Properties' bid tracker. Give the proposed total \
price (lump sum or GMP), the fee as a percent, general conditions in dollars, and the construction duration in \
months, as plain numbers (digits and a decimal point only), or empty when the proposal doesn't state them. List the \
exclusions and qualifications in a few words each."""

TERMS_SCHEMA = {
    "type": "object",
    "properties": {
        "amount": {"type": "string"},
        "fee_pct": {"type": "string"},
        "general_conditions": {"type": "string"},
        "duration_months": {"type": "string"},
        "exclusions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["amount", "fee_pct", "general_conditions", "duration_months", "exclusions"],
    "additionalProperties": False,
}


def proposals(project):
    """The bids with a proposal Claude can read, cheapest first."""
    return [b for b in project.bids.all()
            if (b.text_status == "ok" and b.text) or (b.text_status == "scan" and b.file)]


def _context(bid):
    parts = [bid.get_status_display()]
    if bid.amount:
        parts.append(f"price on record ${bid.amount:,.0f}")
    return "; ".join(parts)


def _blocks(bids):
    blocks = [ai.source_block(b, b.title, _context(b)) for b in bids]
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}  # a follow-up question rereads them at a tenth the price
    return blocks


def _save(project, question, message, bids, user):
    usage = message.usage
    return ProjectAnswer.objects.create(
        project=project, question=question, blocks=ai.blocks(message, bids),
        documents=[{"id": b.pk, "title": b.title} for b in bids], model=message.model or ai.MODEL,
        input_tokens=(usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0),
        output_tokens=usage.output_tokens or 0, asked_by=user)


def _prompt(project, text):
    head = f"PROJECT: {project.name}" + (f" ({project.campus.code} campus)" if project.campus_id else "")
    return f"{head}\n\n{text}"


def level(project, user=None):
    bids = proposals(project)
    if not bids:
        raise Unavailable("Upload at least one proposal (PDF or Word) first.")
    message = ai._stream(LEVEL_SYSTEM, _blocks(bids) + [{"type": "text", "text": _prompt(
        project, f"Level these {len(bids)} proposal{'s' if len(bids) != 1 else ''}.")}], 24000, {"effort": ai.EFFORT})
    if message.stop_reason == "max_tokens":
        raise Unavailable("The leveling ran too long and was cut off. Try again.")
    return _save(project, "Bid leveling", message, bids, user)


def ask(project, question, user=None):
    bids = proposals(project)
    if not bids:
        raise Unavailable("Upload at least one proposal (PDF or Word) first.")
    message = ai._stream(ASK_SYSTEM, _blocks(bids) + [{"type": "text", "text": _prompt(project, f"QUESTION\n{question}")}],
                         10000, {"effort": ai.EFFORT})
    if message.stop_reason == "max_tokens":
        raise Unavailable("The answer ran too long and was cut off. Ask a narrower question.")
    return _save(project, question, message, bids, user)


def _number(raw):
    raw = "".join(ch for ch in (raw or "") if ch.isdigit() or ch == ".")
    try:
        return Decimal(raw) if raw else None
    except InvalidOperation:
        return None


def read_terms(bid, user=None):
    """Fill the bid's blank price, fee, general conditions and duration from its proposal, and note the exclusions in
    its concerns when those are empty. Returns the names of the fields filled in."""
    block = ai.source_block(bid, bid.title)
    del block["citations"]  # structured output can't be combined with citations
    message = ai._stream(TERMS_SYSTEM, [block, {"type": "text", "text": f"BIDDER: {bid.firm}"}], 4000,
                         {"effort": ai.EFFORT, "format": {"type": "json_schema", "schema": TERMS_SCHEMA}})
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        found = json.loads(text)
    except ValueError as exc:
        raise Unavailable("Claude's reading of the proposal couldn't be read. Try again.") from exc
    filled = []
    for field in ("amount", "fee_pct", "general_conditions", "duration_months"):
        value = _number(found.get(field))
        if value is not None and getattr(bid, field) is None:
            setattr(bid, field, value)
            filled.append(bid._meta.get_field(field).verbose_name)
    if found.get("exclusions") and not bid.concerns.strip():
        bid.concerns = "Exclusions and qualifications (from the proposal): " + "; ".join(found["exclusions"])
        filled.append("concerns")
    bid.updated_by = user
    bid.save()
    return filled
