"""Dropping files onto a contract: each file becomes a document, and Claude reads it to fill in what a person would
otherwise type (what kind of document it is, its title and date, whether it's a draft and whose, who redlined it, a
one-line summary), plus the contract's own details when they're still blank (a contract started by dropping its
agreement on the contracts list gets its title, kind, parties, price, acres and effective date this way).

Without ANTHROPIC_API_KEY, or if Claude can't read it, the file name is the guide (guess()): "Amendment", "draft",
"redline", "executed" and a date in the name. Claude only fills blanks on the contract; anything a person already
entered stays as it is.
"""
import json
import re
from datetime import date

from ..ai import enabled
from ..models import Contract, ContractDocument
from . import ai

READ_CHARS = 60_000  # the first ~15 pages say what a document is

SYSTEM = """You file real estate and construction documents for ACMI Properties, which assembles land and develops \
manufacturing campuses for defense, aerospace and energetics companies. ACMI's people sign as ACMI Properties or an \
ACMI project entity (e.g. ACMI Properties MEIH LLC); emails end in acmigroup.com. Someone just dropped a file onto a \
contract in ACMI's tracker. Read it and say what it is, so nobody has to type it in.

- kind: an executed or final agreement is "original"; a later signed change is "amendment"; "restated" for an amended \
and restated agreement; side letters, assignments, notices / extension or waiver letters, exhibits and estoppels have \
their own kinds. Anything unsigned that is still being negotiated (a draft, a redline, a markup, comments) is \
"draft". Signature blocks that are blank, "DRAFT" watermarks or headers, tracked changes and bracketed blanks mean a \
draft.
- title: how the team would name it, e.g. "PSA draft 3 (seller comments)", "First Amendment to PSA", "MBI B101 \
redline (Cozen)". Short.
- dated: the effective date of a signed document, or the date on a draft; YYYY-MM-DD, or empty.
- sent_by (drafts only): "us" if ACMI or ACMI's counsel produced this round, "them" if the other side did, else empty.
- author: who made this round's changes, as a firm or person with their role, e.g. "Venable (seller's counsel)"; \
empty if you can't tell.
- summary: one sentence on what it does or what this round changes.
- contract: the agreement's own facts as this document states them; leave a field empty when it doesn't say. Price \
and acres as digits only."""

SCHEMA = {
    "type": "object",
    "properties": {
        "document": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ContractDocument.Kind.values},
                "title": {"type": "string"},
                "dated": {"type": "string"},
                "sent_by": {"type": "string", "enum": ["us", "them", ""]},
                "author": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["kind", "title", "dated", "sent_by", "author", "summary"],
            "additionalProperties": False,
        },
        "contract": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "e.g. Wamsley PSA, MBI B101 (ARA facility)"},
                "kind": {"type": "string", "enum": Contract.Kind.values},
                "counterparty": {"type": "string"},
                "our_entity": {"type": "string"},
                "price": {"type": "string"},
                "acres": {"type": "string"},
                "effective_date": {"type": "string"},
                "status": {"type": "string", "enum": ["drafting", "negotiating", "signed"]},
            },
            "required": ["title", "kind", "counterparty", "our_entity", "price", "acres", "effective_date", "status"],
            "additionalProperties": False,
        },
    },
    "required": ["document", "contract"],
    "additionalProperties": False,
}

DATE_IN_NAME = (
    (re.compile(r"(20\d\d)[.\-_ ](\d{1,2})[.\-_ ](\d{1,2})"), ("y", "m", "d")),
    (re.compile(r"(\d{1,2})[.\-_](\d{1,2})[.\-_](20\d\d|\d\d)(?!\d)"), ("m", "d", "y")),
)
NAME_KINDS = (
    (r"amended (and|&) restated|a ?& ?r\b", ContractDocument.Kind.RESTATED),
    (r"\bdraft|redline|red-line|markup|comments?\b|\brl\b|\bv\d+\b|clean|compare", ContractDocument.Kind.DRAFT),
    (r"amendment", ContractDocument.Kind.AMENDMENT),
    (r"side letter", ContractDocument.Kind.SIDE_LETTER),
    (r"assignment", ContractDocument.Kind.ASSIGNMENT),
    (r"notice|extension|waiver", ContractDocument.Kind.NOTICE),
    (r"exhibit|schedule", ContractDocument.Kind.EXHIBIT),
    (r"estoppel|snda", ContractDocument.Kind.ESTOPPEL),
)


def guess(name):
    """{"kind", "title", "dated"} from a file name alone."""
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    words = stem.replace("_", " ").strip()
    kind = next((k for pattern, k in NAME_KINDS if re.search(pattern, words, re.I)), ContractDocument.Kind.ORIGINAL)
    dated = None
    for rx, order in DATE_IN_NAME:
        m = rx.search(stem)
        if m:
            parts = dict(zip(order, (int(g) for g in m.groups())))
            year = parts["y"] + 2000 if parts["y"] < 100 else parts["y"]
            try:
                dated = date(year, parts["m"], parts["d"])
                break
            except ValueError:
                continue
    return {"kind": kind, "title": words[:300] or "Document", "dated": dated}


def _context(contract):
    c = contract
    lines = [f"CONTRACT IN THE TRACKER: {c.title or '(new, not named yet)'} ({c.get_kind_display()}, "
             f"{c.get_status_display().lower()})"]
    for label, value in (("Counterparty", c.counterparty), ("Our entity", c.our_entity), ("Counsel", c.counsel)):
        if value:
            lines.append(f"{label}: {value}")
    docs = [d for d in ai.chain(c) if d.pk]
    if docs:
        lines.append("DOCUMENTS ALREADY FILED: " + "; ".join(
            f"{d.title} ({d.get_kind_display()}{', ' + d.dated.isoformat() if d.dated else ''}"
            f"{', by ' + d.made_by if d.is_draft and d.made_by else ''})" for d in docs))
    return "\n".join(lines)


def read(doc, contract, user=None, new_contract=False):
    """Have Claude say what `doc` is and fill it in (and the contract's blank details). Returns the labels filled.
    Raises ai.Unavailable."""
    if not enabled():
        raise ai.Unavailable("Add ANTHROPIC_API_KEY to the .env file to turn on Claude.")
    if doc.text_status == ContractDocument.TextStatus.OK and doc.text:
        source = {"type": "text", "media_type": "text/plain", "data": doc.text[:READ_CHARS]}
        block = {"type": "document", "source": source, "title": doc.file.name.rsplit("/", 1)[-1] if doc.file else doc.title}
    else:
        block = ai.source_block(doc, doc.title)
        block.pop("citations", None)
    prompt = _context(contract) + "\n\nFile name: " + (doc.file.name.rsplit("/", 1)[-1] if doc.file else doc.title)
    message = ai._stream(SYSTEM, [block, {"type": "text", "text": prompt}], 4000,
                         {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
    text = next((b.text for b in message.content if getattr(b, "type", "") == "text"), "")
    try:
        found = json.loads(text)
    except ValueError as exc:
        raise ai.Unavailable("Claude's reading of the file couldn't be used.") from exc
    return apply(doc, contract, found, new_contract)


def apply(doc, contract, found, new_contract=False):
    """Put Claude's reading on the document (all of it: nobody typed anything) and on the contract (blank fields only,
    plus title / kind / status for a contract that was just started from this file)."""
    filled = []
    d = found.get("document") or {}
    if d.get("kind") in ContractDocument.Kind.values:
        doc.kind = d["kind"]
    if d.get("title", "").strip():
        doc.title = d["title"].strip()[:300]
    when = ai._date(d.get("dated"))
    if when:
        doc.dated = when
    doc.sent_by = d.get("sent_by", "") if doc.kind == ContractDocument.Kind.DRAFT else ""
    doc.author = (d.get("author") or "").strip()[:200] if doc.kind == ContractDocument.Kind.DRAFT else doc.author
    if d.get("summary", "").strip() and not doc.summary:
        doc.summary = d["summary"].strip()[:1000]
    doc.save()
    filled.append("the document's type, title and date")

    c = found.get("contract") or {}
    changed = []
    if new_contract:
        if c.get("title", "").strip():
            contract.title = c["title"].strip()[:200]
            changed.append("title")
        if c.get("kind") in Contract.Kind.values:
            contract.kind = c["kind"]
            changed.append("kind")
        if c.get("status") in Contract.Status.values:
            contract.status = c["status"]
            changed.append("status")
    for field, raw in (("counterparty", c.get("counterparty")), ("our_entity", c.get("our_entity"))):
        if raw and raw.strip() and not getattr(contract, field):
            setattr(contract, field, raw.strip()[:200])
            changed.append(Contract._meta.get_field(field).verbose_name)
    for field, value in (("price", ai._amount(c.get("price"))), ("acres", ai._amount(c.get("acres"))),
                         ("effective_date", ai._date(c.get("effective_date")))):
        if value is not None and getattr(contract, field) is None:
            if field == "effective_date" and doc.is_draft:
                continue  # a draft's date isn't the agreement's effective date
            setattr(contract, field, value)
            changed.append(Contract._meta.get_field(field).verbose_name.split(" (")[0])
    if changed:
        contract.save()
        filled.append("the contract's " + ", ".join(changed))
    return filled
