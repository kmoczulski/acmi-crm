"""Claude reads a contract's documents and answers with citations (Notessa's "every answer is sourced"), writes
abstracts from review templates, and finds the critical dates.

The documents go to Claude in the order they took effect, each labeled with what it is (original, First Amendment,
amended and restated…), as plain text with citations on. Claude's citations come back as character ranges in our
own extracted text, so a click opens the exact passage in the reader. A scanned PDF goes as the PDF itself: Claude
can read it but can't cite it. The documents carry a cache marker, so follow-up questions within five minutes reread
them at a tenth of the price. Needs ANTHROPIC_API_KEY, like crm/ai.py.
"""
import base64
import html
import json
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from ..ai import enabled
from ..models import ContractAnswer, ContractDate, ContractDocument
from . import timeline

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
EFFORT = "medium"  # the same for questions and abstracts, so they share the cached documents
MAX_CHARS = 1_500_000  # all documents in one request, about 400k tokens
MAX_PDF_BYTES = 25 * 1024 * 1024
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """You read real estate agreements for ACMI Properties, which assembles land and develops purpose-built \
manufacturing campuses for defense, aerospace and energetics companies, then leases or sells the facilities to them. \
The team asks you about one agreement at a time: a letter of intent, a purchase and sale agreement for land, an \
option, a lease, a development agreement with a county, a JV, loan, design or construction agreement.

You get the agreement file: every document in the order it took effect, each titled with its number and type. Read \
them as one transaction:
- Later documents control. An amendment changes the agreement only where it says so; an amended and restated \
agreement replaces everything before it (earlier documents are history). A side letter, notice or waiver changes only \
what it covers.
- When a term changed, give the current term first, then what it was and which document changed it.
- Follow defined terms and cross-references to where they lead, and cite those passages too when they matter.
- Cite the language behind every term, number and date you state. If the documents don't answer the question, say \
so plainly and don't fill the gap.
- For a deadline set relative to something else ("30 days after the Effective Date"), give the rule, and the calendar \
date only when the anchor date is in the documents or the facts given; note business days, weekends and holidays when \
the agreement addresses them.
- Flag what is ambiguous, inconsistent between documents, missing, or unusual for this kind of deal, and anything \
that needs a lawyer. You are not giving legal advice.
- Be brief and plain. Use the agreement's defined terms as written. Short paragraphs or "- " bullets; "## " headings \
only when the answer has several parts."""

DATES_SYSTEM = """You find the critical dates in a real estate agreement for ACMI Properties' contract tracker: every \
deadline, payment and notice date a party must meet or can act on. Typical ones: deposits and when they go hard \
(non-refundable), the feasibility or due diligence period, title and survey objection and cure dates, approval and \
entitlement deadlines, closing and outside closing dates, extension options with their notice dates and fees, rent \
commencement, expiration, renewal notice windows, reporting and covenant tests, and termination rights with a \
deadline.

The documents are in the order they took effect; later ones control and an amended and restated agreement replaces \
what came before. Give each date as it stands now, once. Compute a calendar date only from anchor dates that are in \
the documents or the facts given, and follow the agreement's own rules for business days and weekends. When a date \
can't be fixed yet (it hangs on an event that hasn't happened), leave "date" empty and explain it in "rule". Copy \
"quote" word for word from the document it comes from: the one or two sentences that set the date.

Also give each date as a rule so the team can rework the timeline from another effective date: a short "key" \
(use feasibility_end, deposit, deposit_2, deposit_hard, title_objection, approvals, closing, outside_closing, \
extension, extension_2, expiration, rent_start and the like), what it counts from ("anchor": "effective" for the \
Effective Date or another date's key; empty for a fixed calendar date), "offset" and "unit" (days, business, weeks, \
months; a date before its anchor has a negative offset). For an extension option, "extends" is the key of the date it \
pushes out and "extend_by" / "extend_unit" how far; put any fee in "amount" and the notice it needs in "rule"."""

RULE_PROPS = {
    "key": {"type": "string", "description": "Short snake_case name, e.g. feasibility_end."},
    "anchor": {"type": "string", "description": "'effective', another date's key, or empty for a fixed date."},
    "offset": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "Units after the anchor."},
    "unit": {"type": "string", "enum": ["days", "business", "weeks", "months", ""]},
    "extends": {"type": "string", "description": "Extension options: the key of the date it pushes out."},
    "extend_by": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "extend_unit": {"type": "string", "enum": ["days", "business", "weeks", "months", ""]},
}

DATES_SCHEMA = {
    "type": "object",
    "properties": {
        "dates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ContractDate.Kind.values},
                    "label": {"type": "string", "description": "Short name, e.g. 'Second deposit due'."},
                    "date": {"type": "string", "description": "YYYY-MM-DD, or empty when it can't be fixed yet."},
                    "rule": {"type": "string", "description": "How the agreement sets it, in its own terms."},
                    "amount": {"type": "string", "description": "Dollars tied to it (deposit, fee), digits only, or empty."},
                    "document": {"type": "integer", "description": "Number of the document it comes from."},
                    "quote": {"type": "string", "description": "The sentence(s) that set it, copied exactly."},
                    **RULE_PROPS,
                },
                "required": ["kind", "label", "date", "rule", "amount", "document", "quote", *RULE_PROPS],
                "additionalProperties": False,
            },
        },
    },
    "required": ["dates"],
    "additionalProperties": False,
}


class Unavailable(Exception):
    pass


# ---------------------------------------------------------------------------
# Which documents Claude reads
# ---------------------------------------------------------------------------


def readable(doc):
    if doc.text_status == ContractDocument.TextStatus.OK and doc.text:
        return True
    if doc.text_status != ContractDocument.TextStatus.SCAN or not doc.file or not doc.file.name.lower().endswith(".pdf"):
        return False
    try:
        return doc.file.storage.exists(doc.file.name)
    except OSError:
        return False


def chain(contract):
    """All documents in the order they took effect (undated ones last)."""
    return list(contract.documents.all())


def default_docs(contract):
    """What a question reads: the signed documents, or while it's still being negotiated, the latest draft."""
    docs = [d for d in chain(contract) if readable(d)]
    signed = [d for d in docs if not d.is_draft]
    if signed:
        return signed
    drafts = [d for d in docs if d.is_draft]
    return drafts[-1:]


def superseded(docs):
    """{doc pk: the restated document that replaced it}."""
    out, latest = {}, None
    for d in reversed(docs):
        if latest and not d.is_draft:
            out[d.pk] = latest
        if d.kind == ContractDocument.Kind.RESTATED and latest is None:
            latest = d
    return out


def _size(doc):
    return len(doc.text) if doc.text_status == ContractDocument.TextStatus.OK else doc.file.size // 4


def fit(docs):
    """Drop superseded documents, then exhibits, until the set fits in one request."""
    docs = list(docs)
    old = superseded(docs)
    for drop in (lambda d: d.pk in old, lambda d: d.kind == ContractDocument.Kind.EXHIBIT):
        if sum(_size(d) for d in docs) <= MAX_CHARS:
            break
        docs = [d for d in docs if not drop(d)]
    total = sum(_size(d) for d in docs)
    if total > MAX_CHARS:
        raise Unavailable(f"These documents are too long to read at once ({total:,} characters). "
                          "Pick fewer documents.")
    return docs


def _when(d):
    return f"{d:%b} {d.day}, {d.year}" if d else ""


def doc_context(doc, n, docs):
    parts = [doc.get_kind_display()]
    if doc.dated:
        parts.append(("sent " if doc.is_draft else "dated ") + _when(doc.dated))
    if doc.is_draft and doc.sent_by:
        parts.append(f"sent by {doc.get_sent_by_display().lower()}")
    by = superseded(docs).get(doc.pk)
    if by:
        parts.append(f"superseded by document {docs.index(by) + 1} (amended and restated); history only")
    return "; ".join(parts)


def doc_blocks(docs, citations=True):
    blocks = []
    for n, doc in enumerate(docs, start=1):
        block = {"type": "document", "title": f"{n}. {doc.title}", "context": doc_context(doc, n, docs)}
        if doc.text_status == ContractDocument.TextStatus.OK:
            block["source"] = {"type": "text", "media_type": "text/plain", "data": doc.text}
        else:
            if doc.file.size > MAX_PDF_BYTES:
                raise Unavailable(f"{doc.title} is a scan over 25 MB; Claude can't read it in one go.")
            with doc.file.open("rb") as fh:
                data = base64.standard_b64encode(fh.read()).decode()
            block["source"] = {"type": "base64", "media_type": "application/pdf", "data": data}
        if citations:
            block["citations"] = {"enabled": True}
        blocks.append(block)
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}  # everything up to here is reused by the next question
    return blocks


def facts(contract, docs, today=None):
    today = today or timezone.localdate()
    c = contract
    lines = ["FACTS FROM THE CRM (not from the documents)",
             f"- Agreement: {c.title} ({c.get_kind_display()}), status {c.get_status_display().lower()}."]
    if c.counterparty or c.our_entity:
        lines.append(f"- Parties on record: {c.our_entity or 'ACMI'} and {c.counterparty or 'the other side'}.")
    if c.effective_date:
        lines.append(f"- Effective date on record: {_when(c.effective_date)}.")
    if c.site_id:
        lines.append(f"- Land: {c.site.name}" + (f", {c.site.campus.code} campus." if c.site.campus_id else "."))
    lines.append(f"- Today is {today:%A}, {_when(today)}.")
    lines.append("- Documents, in the order they took effect: " + "; ".join(
        f"{n}. {d.title} ({doc_context(d, n, docs)})" for n, d in enumerate(docs, start=1)) + ".")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Calling Claude
# ---------------------------------------------------------------------------


def _stream(system, content, max_tokens, output_config):
    if not enabled():
        raise Unavailable("Add ANTHROPIC_API_KEY to the .env file to turn on Claude.")
    import anthropic

    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config=output_config,
            # If a safety classifier declines, the API retries on a fallback model in the same call.
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise Unavailable("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.") from exc
    except anthropic.RateLimitError as exc:
        raise Unavailable("Anthropic is rate-limiting requests. Try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        log.warning("Contract request failed: %s", exc)
        raise Unavailable(f"Anthropic API error ({exc.status_code}). Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise Unavailable("Could not reach Anthropic. Check the internet connection.") from exc
    if message.stop_reason == "refusal":
        raise Unavailable("Claude declined to answer this one.")
    return message


def _norm(s):
    return " ".join((s or "").split()).lower()


def anchor(text, start, end, quote):
    """The cited range, checked against our text; re-found by its words if the indexes drifted."""
    if start is not None and end is not None and 0 <= start < end <= len(text):
        if not quote or _norm(text[start:end]) == _norm(quote):
            return start, end
    words = (quote or "").split()
    if not words:
        return start, end
    probe = r"\s+".join(re.escape(w) for w in words[:12])
    m = re.search(probe, text, re.I)
    if not m:
        return start, end
    return m.start(), min(len(text), m.start() + max(len(quote), m.end() - m.start()))


def blocks(message, docs):
    """[{"text": ..., "cites": [...]}] from Claude's reply: text blocks only (not thinking or fallback markers)."""
    out = []
    for block in message.content:
        if getattr(block, "type", "") != "text":
            continue
        cites = []
        for c in getattr(block, "citations", None) or []:
            i = getattr(c, "document_index", None)
            if i is None or not 0 <= i < len(docs):
                continue
            doc = docs[i]
            quote = (getattr(c, "cited_text", "") or "")[:2000]
            cite = {"doc": doc.pk, "title": doc.title, "quote": quote}
            if c.type == "char_location":
                start, end = anchor(doc.text, c.start_char_index, c.end_char_index, quote)
                cite.update(start=start, end=end, page=doc.page_at(start or 0))
            elif c.type == "page_location":
                cite["page"] = c.start_page_number
            cites.append(cite)
        if out and not cites and not out[-1]["cites"]:
            out[-1]["text"] += block.text
        else:
            out.append({"text": block.text, "cites": cites})
    return out


def source_block(obj, title, context=""):
    """A cited document block for one report (a diligence report, a staff report, a resolution): its text, or a scan
    as the PDF. `obj` has .text, .text_status and .file. Raises Unavailable when there's nothing to read."""
    if obj.text_status == "ok" and obj.text:
        if len(obj.text) > MAX_CHARS:
            raise Unavailable("That document is too long to read at once.")
        source = {"type": "text", "media_type": "text/plain", "data": obj.text}
    elif obj.text_status == "scan" and obj.file and obj.file.name.lower().endswith(".pdf"):
        if obj.file.size > MAX_PDF_BYTES:
            raise Unavailable("That scan is over 25 MB; Claude can't read it in one go.")
        with obj.file.open("rb") as fh:
            source = {"type": "base64", "media_type": "application/pdf",
                      "data": base64.standard_b64encode(fh.read()).decode()}
    else:
        raise Unavailable("Upload the document (PDF or Word) first.")
    block = {"type": "document", "source": source, "title": title, "citations": {"enabled": True}}
    if context:
        block["context"] = context
    return block


def cited_summary(obj, system, prompt, title, context=""):
    """(blocks, model): Claude's summary of one document, each part citing it (see blocks()). `obj` also needs .pk,
    .title and .page_at() for the citations."""
    message = _stream(system, [source_block(obj, title, context), {"type": "text", "text": prompt}], 12000,
                      {"effort": EFFORT})
    if message.stop_reason == "max_tokens":
        raise Unavailable("The summary ran too long and was cut off. Try again.")
    return blocks(message, [obj]), message.model or MODEL


def _save(contract, question, message, docs, user, template=None):
    usage = message.usage
    return ContractAnswer.objects.create(
        contract=contract, template=template, question=question, blocks=blocks(message, docs),
        documents=[{"id": d.pk, "title": d.title} for d in docs], model=message.model or MODEL,
        input_tokens=(usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0),
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0, asked_by=user,
    )


def ask(contract, question, user=None, docs=None):
    """Answer a question about the contract, citing the documents. Returns the saved ContractAnswer."""
    docs = fit(docs if docs is not None else default_docs(contract))
    if not docs:
        raise Unavailable("Upload the agreement first (a PDF or Word file with text).")
    content = doc_blocks(docs) + [{"type": "text", "text": f"{facts(contract, docs)}\n\nQUESTION\n{question}"}]
    message = _stream(SYSTEM, content, 10000, {"effort": EFFORT})
    if message.stop_reason == "max_tokens":
        raise Unavailable("The answer ran too long and was cut off. Ask a narrower question.")
    return _save(contract, question, message, docs, user)


def template_prompt(template):
    lines = [f"Write an abstract of this agreement: {template.name}.",
             "Cover each point below in order, each under its own \"## <label>\" heading. Under each, give the "
             "current terms briefly, with citations, and note which document changed them. If the documents don't "
             "address a point, write \"Not addressed.\" End with \"## Flags\": anything ambiguous, inconsistent, "
             "missing or unusual for this kind of deal.", "", "POINTS"]
    lines += [f"{n}. {p['label']}" + (f": {p['ask']}" if p.get("ask") else "")
              for n, p in enumerate(template.points, start=1)]
    if template.instructions.strip():
        lines += ["", "HOW TO WRITE IT", template.instructions.strip()]
    return "\n".join(lines)


def run_template(contract, template, user=None, docs=None):
    """An abstract of the contract from a review template, every point cited. Returns the saved ContractAnswer."""
    docs = fit(docs if docs is not None else default_docs(contract))
    if not docs:
        raise Unavailable("Upload the agreement first (a PDF or Word file with text).")
    prompt = f"{facts(contract, docs)}\n\n{template_prompt(template)}"
    message = _stream(SYSTEM, doc_blocks(docs) + [{"type": "text", "text": prompt}], 32000, {"effort": EFFORT})
    if message.stop_reason == "max_tokens":
        raise Unavailable("The abstract ran too long and was cut off. Trim the template's points and try again.")
    return _save(contract, f"Abstract: {template.name}", message, docs, user, template=template)


def find_dates(contract, user=None):
    """Ask Claude for the contract's critical dates and keep them as unconfirmed ContractDates for the team to keep or
    discard. Earlier unconfirmed suggestions are replaced; dates already on the contract aren't suggested twice."""
    docs = fit(default_docs(contract))
    if not docs:
        raise Unavailable("Upload the agreement first (a PDF or Word file with text).")
    content = doc_blocks(docs, citations=False) + [{"type": "text", "text": facts(contract, docs)}]
    message = _stream(DATES_SYSTEM, content, 16000,
                      {"effort": "high", "format": {"type": "json_schema", "schema": DATES_SCHEMA}})
    if message.stop_reason == "max_tokens":
        raise Unavailable("The list of dates was cut off. Try again.")
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        found = json.loads(text)["dates"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Unavailable("Claude's list of dates couldn't be read. Try again.") from exc
    contract.dates.filter(confirmed=False).delete()
    have = {(d.kind, d.date) for d in contract.dates.all()} | {(d.label.lower(), d.date) for d in contract.dates.all()}
    made = []
    for item in found:
        when = _date(item["date"])
        if (item["kind"], when) in have or (item["label"].lower(), when) in have:
            continue
        doc = docs[item["document"] - 1] if 1 <= item["document"] <= len(docs) else None
        start = end = None
        if doc is not None and doc.text:
            start, end = anchor(doc.text, None, None, item["quote"])
        d = ContractDate(
            contract=contract, kind=item["kind"], label=item["label"][:200], date=when, rule=item["rule"][:300],
            amount=_amount(item["amount"]), quote=item["quote"], source=doc, source_start=start, source_end=end,
            confirmed=False, from_claude=True, created_by=user, critical=item["kind"] != ContractDate.Kind.PAYMENT,
            **_rule_parts(item),
        )
        timeline.fill_rule(d, made + list(contract.dates.all()))
        d.save()
        made.append(d)
        have.add((item["kind"], when))
    return made


def _slug(raw):
    return re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower()).strip("_")[:40]


def _rule_parts(item):
    """The key / anchor / offset / extension fields Claude gave for a date, cleaned for a ContractDate."""
    unit = lambda u: u if u in timeline.UNITS else ""  # noqa: E731
    out = {"key": _slug(item.get("key")), "anchor": _slug(item.get("anchor")), "offset": item.get("offset"),
           "unit": unit(item.get("unit")), "extends": _slug(item.get("extends")), "extend_by": item.get("extend_by"),
           "extend_unit": unit(item.get("extend_unit"))}
    if out["offset"] is None or not out["anchor"]:
        out.update(anchor="", offset=None, unit="")
    if not out["extends"] or out["extend_by"] is None:
        out.update(extends="", extend_by=None, extend_unit="")
    return out


# ---------------------------------------------------------------------------
# One version's timeline, and margin notes on a redline
# ---------------------------------------------------------------------------

TIMELINE_SYSTEM = DATES_SYSTEM + """

This time you read one version of the agreement (a negotiation draft, or the signed agreement as amended by a given \
document) so the team can compare versions' timelines. Give every date as a rule from the Effective Date or another \
date, even when the version fills in a calendar date, and keep "date" only for dates fixed on the calendar. When keys \
from other versions are listed, use the same key for the same date so they line up."""


def read_timeline(doc, user=None):
    """Have Claude read one version's dates as rules and keep them on the document (ContractDocument.timeline).
    A draft is read alone; a signed document with the agreement it amends up to it."""
    contract = doc.contract
    docs = chain(contract)
    signed = [d for d in docs if not d.is_draft and readable(d)]
    by_pk = {d.pk: d for d in signed}
    if doc.is_draft or doc.pk not in by_pk:
        scope = [doc]
    else:
        scope = signed[:[d.pk for d in signed].index(doc.pk) + 1]
    if not readable(doc):
        raise Unavailable("This version has no text to read.")
    scope = fit(scope)
    keys = sorted({t.get("key") for d in docs if d.pk != doc.pk for t in (d.timeline or []) if t.get("key")}
                  | set(contract.dates.exclude(key="").values_list("key", flat=True)))
    prompt = facts(contract, scope) + f"\n\nREAD THE TIMELINE OF: document {len(scope)}, {doc.title}."
    if keys:
        prompt += "\nKEYS USED IN OTHER VERSIONS: " + ", ".join(keys)
    message = _stream(TIMELINE_SYSTEM, doc_blocks(scope, citations=False) + [{"type": "text", "text": prompt}], 16000,
                      {"effort": "high", "format": {"type": "json_schema", "schema": DATES_SCHEMA}})
    if message.stop_reason == "max_tokens":
        raise Unavailable("The timeline was cut off. Try again.")
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        found = json.loads(text)["dates"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Unavailable("Claude's timeline couldn't be read. Try again.") from exc
    out, taken = [], set()
    for item in found:
        src = scope[item["document"] - 1] if 1 <= item["document"] <= len(scope) else doc
        start, end = anchor(src.text, None, None, item["quote"]) if src.text else (None, None)
        parts = _rule_parts(item)
        key = timeline.unique_key(parts["key"] or timeline.KIND_KEYS.get(item["kind"], "date"), taken)
        taken.add(key)
        when = _date(item["date"])
        amount = _amount(item["amount"])
        out.append({**parts, "key": key, "kind": item["kind"] if item["kind"] in ContractDate.Kind.values else "other",
                    "label": item["label"][:200], "date": when.isoformat() if when else None, "rule": item["rule"][:300],
                    "amount": str(amount) if amount is not None else None, "quote": item["quote"][:2000],
                    "doc": src.pk, "start": start, "end": end})
    doc.timeline, doc.timeline_at = out, timezone.now()
    doc.save(update_fields=["timeline", "timeline_at"])
    return out


NOTES_SYSTEM = """You review a redline between two versions of a real estate agreement for ACMI Properties, which \
assembles land and develops manufacturing campuses for defense, aerospace and energetics companies. For each numbered \
change, write the margin comment a careful deal lawyer would leave for the team: what the change does in plain words \
(not a restatement of the words struck and added), who it favors, and what it means for ACMI (money, dates, risk, \
obligations). Flag a change that looks unintended, inconsistent with the rest of the agreement, or unfavorable to \
ACMI. Typo and formatting fixes get a few words. One to three sentences each. You are not giving legal advice."""

NOTES_SCHEMA = {
    "type": "object",
    "properties": {"notes": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "comment": {"type": "string"},
                       "flag": {"type": "boolean", "description": "True when the team should look at it closely."}},
        "required": ["n", "comment", "flag"], "additionalProperties": False}}},
    "required": ["notes"],
    "additionalProperties": False,
}
MAX_NOTES = 150


def annotate_changes(contract, old, new, items, user=None):
    """Claude's margin note on each change in a redline (compare.redline's items), kept as comments on the newer
    document, pinned to the change. Earlier notes from Claude on that document that nobody replied to are replaced.
    Returns the comments made."""
    from django.db.models import Count

    from ..models import Comment

    items = items[:MAX_NOTES]
    if not items:
        return []
    docs = fit([d for d in (old, new) if d is not None])
    lines = [facts(contract, docs), "", (f"REDLINE: document 1 ({old.title}) to document 2 ({new.title}). Changes:"
                                         if old is not None and len(docs) == 2 else
                                         f"TRACKED CHANGES in {new.title} (the last document). Changes:")]
    for c in items:
        ctx = new.text[max(0, c["start"] - 160):c["start"]].split("\n")[-1][-160:]
        line = f'[{c["n"]}] near "...{ctx}"'
        if c["deleted"]:
            line += f' REMOVED: "{c["deleted"][:600]}"'
        if c["inserted"]:
            line += f' ADDED: "{c["inserted"][:600]}"'
        lines.append(line)
    message = _stream(NOTES_SYSTEM, doc_blocks(docs, citations=False) + [{"type": "text", "text": "\n".join(lines)}],
                      16000, {"effort": EFFORT, "format": {"type": "json_schema", "schema": NOTES_SCHEMA}})
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        notes = {n["n"]: n for n in json.loads(text)["notes"]}
    except (ValueError, KeyError, TypeError) as exc:
        raise Unavailable("Claude's notes couldn't be read. Try again.") from exc
    (Comment.objects.filter(document=new, by_claude=True, parent__isnull=True).annotate(r=Count("replies"))
     .filter(r=0).delete())
    made = []
    for c in items:
        note = notes.get(c["n"])
        if not note or not note["comment"].strip():
            continue
        body = ("Flag: " if note["flag"] else "") + note["comment"].strip()
        made.append(Comment.objects.create(
            contract=contract, document=new, start=c["start"], end=c["end"], by_claude=True,
            quote=(c["inserted"] or c["deleted"])[:500], body=f"Change {c['n']}. {body}"[:4000]))
    return made


def _date(raw):
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _amount(raw):
    raw = re.sub(r"[^\d.]", "", raw or "")
    try:
        return Decimal(raw) if raw else None
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------------
# Showing an answer
# ---------------------------------------------------------------------------

_MARK = "{}"
_MARK_RE = re.compile("(\\d+)")


def _inline(s):
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def _markdown(text):
    """Claude's light markdown (## headings, - and 1. lists, **bold**) as HTML. The text is escaped first."""
    out, para, lst = [], [], None

    def flush():
        nonlocal para, lst
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para = []
        if lst:
            out.append(f"<{lst[0]}>" + "".join(f"<li>{_inline(i)}</li>" for i in lst[1]) + f"</{lst[0]}>")
            lst = None

    for line in html.escape(text, quote=False).split("\n"):
        s = line.strip()
        heading = re.match(r"#{1,4}\s+(.*)", s)
        bullet = re.match(r"[-*•]\s+(.*)", s)
        number = re.match(r"\d{1,2}[.)]\s+(.*)", s)
        if not s:
            flush()
        elif heading:
            flush()
            out.append(f"<h4>{_inline(heading.group(1))}</h4>")
        elif bullet or number:
            kind = "ul" if bullet else "ol"
            if para or (lst and lst[0] != kind):
                flush()
            lst = lst or (kind, [])
            lst[1].append((bullet or number).group(1))
        elif lst and _MARK_RE.fullmatch(s):
            lst[1][-1] += s  # a citation chip on its own line belongs to the bullet above
        else:
            if lst:
                flush()
            para.append(s)
    flush()
    return "".join(out)


def render(answer, cite_href):
    """(html, sources) for an answer: Claude's text with a numbered chip after each cited part, and the list of
    sources the chips point to. `cite_href(cite)` gives the reader link for a citation."""
    parts, sources, index = [], [], {}
    for b in answer.blocks or []:
        parts.append(b.get("text", ""))
        nums = []
        for c in b.get("cites", []):
            key = (c.get("doc"), c.get("start"), c.get("end"), c.get("page"))
            if key not in index:
                sources.append({**c, "n": len(sources) + 1, "href": cite_href(c)})
                index[key] = len(sources)
            nums.append(index[key])
        parts += [_MARK.format(n) for n in dict.fromkeys(nums)]
    body = _markdown("".join(parts))
    by_n = {s["n"]: s for s in sources}

    def chip(m):
        s = by_n[int(m.group(1))]
        where = f"{s['title']}" + (f", p. {s['page']}" if s.get("page") else "")
        return (f'<a class="cite" href="{html.escape(s["href"])}" data-n="{s["n"]}" '
                f'data-where="{html.escape(where)}" data-quote="{html.escape(s.get("quote", ""))}" '
                f'title="{html.escape(where)}">{s["n"]}</a>')

    return _MARK_RE.sub(chip, body), sources
