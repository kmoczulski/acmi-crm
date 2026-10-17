"""Claude on an approval's papers: a cited summary of a staff report, comment letter or resolution, and the
conditions of approval pulled out of a decision for the team to keep (each quote matched back to the text)."""
import json

from django.utils import timezone

from ..contracts import ai
from ..contracts.ai import Unavailable  # noqa: F401 - callers catch it from here
from ..models import ApprovalCondition

SUMMARY_SYSTEM = """You review land-use and permitting documents for ACMI Properties, which develops purpose-built \
manufacturing campuses for defense, aerospace and energetics companies. Summarize the document for the development \
team, who track every approval to the date the project needs it.

- A staff report: the recommendation, the issues raised, proposed conditions, and the hearing dates.
- A comment letter: each comment and what it takes to resolve it.
- A resolution, permit or approval letter: what was approved, every condition, deadlines, the expiration and the \
appeal period.
- Anything else: what it decides or asks for, and what the team must do next and by when.
Cite the document for every point. Lead with anything that could stop or delay the project. Brief and plain: "## " \
headings and "- " bullets. You are not giving legal advice."""

CONDITIONS_SYSTEM = """You pull the conditions of approval out of a land-use or permit decision (a resolution, an \
approval letter, a permit, or a staff report's recommended conditions) for ACMI Properties' entitlement tracker. \
List every condition the developer must meet, once each, in the document's order: what must be done ("text"), and \
when or before what ("timing", e.g. "before plat recordation", "before the building permit", "before occupancy", \
"ongoing"). Give "due" (YYYY-MM-DD) only when the document states a calendar date. Copy "quote" word for word: the \
sentence or sentences that set the condition. Also give the approval's expiration date and the end of the appeal \
period when the document states them (YYYY-MM-DD), else empty."""

CONDITIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "conditions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What must be done, in plain words."},
                    "timing": {"type": "string", "description": "When or before what."},
                    "due": {"type": "string", "description": "YYYY-MM-DD if the document states a date, else empty."},
                    "quote": {"type": "string", "description": "The sentence(s) that set it, copied exactly."},
                },
                "required": ["text", "timing", "due", "quote"],
                "additionalProperties": False,
            },
        },
        "expires_on": {"type": "string", "description": "YYYY-MM-DD, or empty."},
        "appeal_ends_on": {"type": "string", "description": "YYYY-MM-DD, or empty."},
    },
    "required": ["conditions", "expires_on", "appeal_ends_on"],
    "additionalProperties": False,
}


def _about(doc):
    a = doc.approval
    lines = [f"APPROVAL: {a.title} ({a.get_kind_display()})"
             + (f" before {a.jurisdiction}" if a.jurisdiction else "") + (f", case {a.case_number}" if a.case_number else "")]
    if a.project_id or a.site_id:
        lines.append(f"FOR: {a.home_label}")
    lines.append(f"DOCUMENT: {doc.title} ({doc.get_kind_display()})" + (f", dated {doc.dated:%b} {doc.dated.day}, "
                                                                        f"{doc.dated.year}" if doc.dated else ""))
    return "\n".join(lines)


def summarize(doc, user=None):
    """Keep Claude's cited summary on the document. Raises Unavailable."""
    doc.summary, doc.summary_model = ai.cited_summary(doc, SUMMARY_SYSTEM, _about(doc) + "\nSummarize it for the team.",
                                                      doc.title, doc.get_kind_display())
    doc.summary_at = timezone.now()
    doc.save(update_fields=["summary", "summary_model", "summary_at"])
    return doc


def _norm(s):
    return " ".join((s or "").lower().split())


def find_conditions(doc, user=None):
    """Conditions Claude finds in the document, kept as unconfirmed ApprovalConditions for the team to keep. Earlier
    suggestions from this document are replaced and conditions already on the approval aren't repeated. Fills the
    approval's expiration and appeal dates when it has none. Returns (new conditions, [what was filled in])."""
    block = ai.source_block(doc, doc.title)
    del block["citations"]  # structured output can't be combined with citations; quotes are matched instead
    message = ai._stream(CONDITIONS_SYSTEM, [block, {"type": "text", "text": _about(doc)}], 12000,
                         {"effort": "high", "format": {"type": "json_schema", "schema": CONDITIONS_SCHEMA}})
    if message.stop_reason == "max_tokens":
        raise Unavailable("The list of conditions was cut off. Try again.")
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        found = json.loads(text)
        items = found["conditions"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Unavailable("Claude's list of conditions couldn't be read. Try again.") from exc
    approval = doc.approval
    approval.conditions.filter(confirmed=False, source=doc).delete()
    have = {_norm(c.text) for c in approval.conditions.all()}
    made = []
    for item in items:
        if not item.get("text") or _norm(item["text"]) in have:
            continue
        start, end = ai.anchor(doc.text, None, None, item["quote"]) if doc.text else (None, None)
        made.append(ApprovalCondition.objects.create(
            approval=approval, text=item["text"], timing=item["timing"][:200], due_on=ai._date(item["due"]),
            quote=item["quote"], source=doc, source_start=start, source_end=end, confirmed=False, from_claude=True,
            created_by=user))
        have.add(_norm(item["text"]))
    filled = []
    for field, label in (("expires_on", "expiration"), ("appeal_ends_on", "end of the appeal period")):
        when = ai._date(found.get(field))
        if when and not getattr(approval, field):
            setattr(approval, field, when)
            filled.append(f"{label} {when:%b} {when.day}, {when.year}")
    if filled:
        approval.save(update_fields=["expires_on", "appeal_ends_on", "updated_at"])
    return made, filled
