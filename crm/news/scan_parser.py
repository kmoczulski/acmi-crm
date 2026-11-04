"""Parse the Weekly Prospect News Scan (.pdf or .docx) into reviewable entries.

The report has changed shape once already (2026-08-21 vs 2026-08-28), so this
module supports both:

CURRENT format (2026-08-28 on), per board company:

    <Section heading>                      e.g. "Active Discussions"
    <Company>
    Headline: <headline>
    Date: <date>
    Source: <url>
    Synopsis: <body>
    Outreach action: <what happened - freeform>
    Framing: <External | Internal | N/A | ...>
    Trello card: <status note, no URL>          e.g. "Updated"

LEGACY format (2026-08-21), per board company:

    <Section heading>
    <Company> - <Headline>
    Date: <date>   |   Source: <url>
    <body>
    Outreach draft created in Gmail (addressed to <Contact>).
    Trello card: https://trello.com/c/<id>/<slug>

`parse_scan` tries the current format first and falls back to legacy if it
yields nothing, so an old file left in the watch folder still loads.

The current format also introduces a "New Prospects to Target" section - a
research list of off-board companies, grouped Strong Fit / Worth Watching /
weak-or-no-fit - parsed separately by `parse_new_prospects` since it has no
per-entry Trello card, contact, or draft to track.
"""

import re
from pathlib import Path

EM_DASH = "—"
EN_DASH = "–"

# Canonical section names, keyed by a normalized form (lowercased, trailing
# parenthetical stripped). Matched this loosely because the wording already
# drifted once between weeks ("Active Discussions (at least 1 meeting)" ->
# "Active Discussions") and pinning to one exact string is what silently
# broke parsing the first time.
CORE_SECTIONS = {
    "early engagement": "Early Engagement",
    "active discussions": "Active Discussions",
    "loi / pda negotiations": "LOI / PDA Negotiations",
    "lois executed": "LOIs Executed",
    "lease negotiations": "Lease Negotiations",
    "lease / pda signed": "Lease / PDA Signed",
    "cold": "Cold",
    "no further outreach": "No Further Outreach",
    "partners - business development": "Partners - Business Development",
    "outreach summary / admin": "Outreach Summary / Admin",
}


def _normalize_heading(line):
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", line.strip()).strip().lower()
    return CORE_SECTIONS.get(stripped)


def _read_pdf(path):
    import pypdf

    reader = pypdf.PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _read_docx(path):
    import docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def read_scan_text(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in (".docx", ".doc"):
        return _read_docx(path)
    raise ValueError(f"Unsupported scan format: {suffix}")


def _join_url(text, start):
    """Pull a URL out of `text` at `start`, rejoining lines the PDF wrapped.

    A wrapped URL continues on the next line only when that line has no spaces
    in it; body copy always does, so that is a reliable stopping point.

    Returns (url, end) where `end` is the offset in `text` just past the URL.
    The caller needs that offset rather than searching for the joined URL --
    the joined form no longer matches the wrapped original in `text`.
    """
    lines = text[start:].split("\n")
    if not lines:
        return "", start
    url = lines[0].strip()
    end = start + len(lines[0])
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or re.search(r"\s", stripped):
            break
        url += stripped
        end += 1 + len(line)  # the newline we just crossed, plus the line
    return url, end


def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def find_latest_scan(folder):
    """Newest .pdf/.docx in `folder`, ignoring files we generated ourselves."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    candidates = [
        p
        for p in folder.iterdir()
        if p.suffix.lower() in (".pdf", ".docx")
        and not p.name.startswith("~$")
        and "REVIEWED" not in p.stem.upper()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ------------------------------------------------------------- current format

TRELLO_NOTE = re.compile(r"Trello card:\s*([^\n]*)", re.I)
HEADLINE_LABEL = re.compile(r"Headline:\s*", re.I)
DATE_LABEL = re.compile(r"\nDate:\s*", re.I)
SOURCE_LABEL = re.compile(r"\nSource:\s*", re.I)
SYNOPSIS_LABEL = re.compile(r"\nSynopsis:\s*", re.I)
OUTREACH_LABEL = re.compile(r"\nOutreach action:\s*", re.I)
FRAMING_LABEL = re.compile(r"\nFraming:\s*", re.I)

HAS_DRAFT_CURRENT = re.compile(r"^outreach draft created in gmail", re.I)
NEEDS_CONTACT_CURRENT = re.compile(r"needs contact lookup", re.I)

# Contact names show up in freeform prose this format ("fresh cold intro to
# Les Lake", "threaded into existing relationship with Jack \"2fish\"
# Fischer"), not a clean "(addressed to X)" tag like last week. Best-effort
# only: the real recipient still comes from matching the Gmail draft itself,
# same as before, so a miss here just means the scan's own text has no name.
CONTACT_PATTERNS_CURRENT = [
    re.compile(r"\)\s+to ([^(.;]+)", re.I),
    re.compile(r"(?:cold intro|fresh angle|follow-up) to ([^(.;]+)", re.I),
    re.compile(r"relationship with ([^().;]+)", re.I),  # "(external, threaded ...) to Bill Denzel (COO) flagging ..."
]


def _extract_contact_current(outreach_action):
    if NEEDS_CONTACT_CURRENT.search(outreach_action):
        return None, True
    for pattern in CONTACT_PATTERNS_CURRENT:
        match = pattern.search(outreach_action)
        if match:
            return _clean(match.group(1)), False
    return None, False


def _strip_section_current(header):
    """Split a header block at its last section heading, current-format.

    Same idea as the legacy stripper below: a header can carry more than the
    company line (the doc intro on the very first entry), and that always
    sits before the section heading that introduces the next company.
    """
    lines = header.split("\n")
    last_i, last_section = None, None
    for i, line in enumerate(lines):
        found = _normalize_heading(line)
        if found:
            last_i, last_section = i, found
    if last_i is None:
        return None, header
    return last_section, "\n".join(lines[last_i + 1:])


def _parse_current(text):
    """Board-company entries in the 2026-08-28-on format. See module docstring."""
    boundaries = list(TRELLO_NOTE.finditer(text))
    entries = []
    section = None
    cursor = 0

    for match in boundaries:
        segment = text[cursor:match.start()]
        trello_note = _clean(match.group(1))
        cursor = match.end()

        headline_match = HEADLINE_LABEL.search(segment)
        if not headline_match:
            # Not a company entry (intro prose, page furniture); keep going.
            continue
        header, body = segment[:headline_match.start()], segment[headline_match.end():]

        found_section, header_rest = _strip_section_current(header)
        if found_section:
            section = found_section
        company = _clean(header_rest)

        date_match = DATE_LABEL.search(body)
        source_match = SOURCE_LABEL.search(body)
        synopsis_match = SYNOPSIS_LABEL.search(body)
        outreach_match = OUTREACH_LABEL.search(body)
        framing_match = FRAMING_LABEL.search(body)
        if not (company and date_match and source_match and synopsis_match
                and outreach_match and framing_match):
            continue

        headline = _clean(body[:date_match.start()])
        date_str = _clean(body[date_match.end():source_match.start()])
        source_url, _ = _join_url(body, source_match.end())
        synopsis = _clean(body[synopsis_match.end():outreach_match.start()])
        outreach_action = _clean(body[outreach_match.end():framing_match.start()])
        framing = _clean(body[framing_match.end():])

        contact, needs_contact = _extract_contact_current(outreach_action)
        has_draft = bool(HAS_DRAFT_CURRENT.match(outreach_action))

        entries.append(
            {
                "index": len(entries),
                "section": section or "",
                "company": company,
                "headline": headline,
                "date": date_str,
                "source_url": source_url,
                "summary": synopsis,
                "outreach_action": outreach_action,
                "framing": framing,
                "trello_note": trello_note,
                # This format states no per-card URL, only a status note
                # ("Updated" / "NOT updated - <reason>") captured above.
                "trello_url": "",
                "contact": contact,
                "needs_contact": needs_contact,
                "has_draft": has_draft,
                "format": "current",
            }
        )
    return entries


# ------------------------------------------------------------ numbered format
# 2026-10-28 on: "Part A" lists the week's board companies as "1. Company (Section)" with the same labelled lines
# as the current format, and no section headings. A Trello note can run over several lines.

NUMBERED_HEADING = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*\(([^()\n]+)\)\s*$", re.M)
NUMBERED_LABELS = ("Headline", "Date", "Source", "Synopsis", "Outreach action", "Framing", "Trello card")
NUMBERED_LABEL_LINE = re.compile(r"^(%s):[ \t]*" % "|".join(NUMBERED_LABELS), re.M | re.I)


def _section_from_parenthetical(text):
    """'Cold / New Leads' -> Cold; 'LOI / PDA Negotiations' and 'EARLY ENGAGEMENT' as they are."""
    whole = _normalize_heading(text)
    if whole:
        return whole
    return _normalize_heading(text.split("/")[0]) or _clean(text)


def _parse_numbered(text):
    """Board-company entries in the 2026-10-28-on format. See above."""
    start = re.search(r"Part A\b", text)
    end = NEW_PROSPECTS_HEADING.search(text, start.end() if start else 0)
    region = text[start.end() if start else 0:end.start() if end else len(text)]
    headings = list(NUMBERED_HEADING.finditer(region))
    entries = []
    for i, heading in enumerate(headings):
        segment = region[heading.end():headings[i + 1].start() if i + 1 < len(headings) else len(region)]
        labels = list(NUMBERED_LABEL_LINE.finditer(segment))
        fields = {}
        for j, label in enumerate(labels):
            stop = labels[j + 1].start() if j + 1 < len(labels) else len(segment)
            fields.setdefault(label.group(1).lower(), segment[label.end():stop])
        if not all(k in fields for k in ("headline", "synopsis", "outreach action")):
            continue
        outreach_action = _clean(fields["outreach action"])
        contact, needs_contact = _extract_contact_current(outreach_action)
        entries.append({
            "index": len(entries),
            "section": _section_from_parenthetical(heading.group(3)),
            "company": _clean(heading.group(2)),
            "headline": _clean(fields["headline"]),
            "date": _clean(fields.get("date", "")),
            "source_url": _join_url(fields.get("source", "").strip(), 0)[0],
            "summary": _clean(fields["synopsis"]),
            "outreach_action": outreach_action,
            "framing": _clean(fields.get("framing", "")),
            "trello_note": _clean(fields.get("trello card", "")),
            "trello_url": "",
            "contact": contact,
            "needs_contact": needs_contact,
            "has_draft": bool(HAS_DRAFT_CURRENT.match(outreach_action)),
            "format": "numbered",
        })
    return entries


# -------------------------------------------------------------- legacy format

LEGACY_TRELLO_LINE = re.compile(r"Trello card:\s*", re.I)
LEGACY_DATE_LINE = re.compile(r"Date:\s*(.*?)\s*\|\s*Source:\s*", re.S)
LEGACY_DRAFT_LINE = re.compile(r"Outreach draft[^\n]*(?:\n(?!Trello card:)[^\n]*)*", re.I)
LEGACY_ADDRESSED_TO = re.compile(r"addressed to\s+([^)]+?)\s*\)", re.I | re.S)
LEGACY_NEEDS_CONTACT = re.compile(r"needs contact lookup", re.I)


def _split_company_headline_legacy(header):
    """Header is '<Company> - <Headline>'. Take the first dash as the split."""
    header = _clean(header)
    for dash in (EM_DASH, EN_DASH):
        if dash in header:
            company, _, headline = header.partition(dash)
            return _clean(company), _clean(headline)
    return header, ""


def _strip_leading_section_legacy(header):
    """Split a header block at its last section heading, legacy-format."""
    lines = header.split("\n")
    last_i, last_section = None, None
    for i, line in enumerate(lines):
        found = _normalize_heading(line)
        if found:
            last_i, last_section = i, found
    if last_i is None:
        return None, header
    return last_section, "\n".join(lines[last_i + 1:])


def _parse_legacy(text):
    """Board-company entries in the 2026-08-21 format. See module docstring."""
    boundaries = list(LEGACY_TRELLO_LINE.finditer(text))
    entries = []
    section = None
    cursor = 0

    for match in boundaries:
        segment = text[cursor:match.start()]
        trello_url, trello_end = _join_url(text, match.end())

        date_match = LEGACY_DATE_LINE.search(segment)
        if not date_match:
            cursor = trello_end
            continue

        header = segment[:date_match.start()]
        found_section, header = _strip_leading_section_legacy(header)
        if found_section:
            section = found_section

        date_str = _clean(date_match.group(1))
        source_url, source_end = _join_url(segment, date_match.end())
        after_source = segment[source_end:]

        draft_match = LEGACY_DRAFT_LINE.search(after_source)
        draft_note = _clean(draft_match.group(0)) if draft_match else ""
        body = after_source[:draft_match.start()] if draft_match else after_source

        needs_contact = bool(LEGACY_NEEDS_CONTACT.search(draft_note))
        contact = None
        if not needs_contact:
            addressed = LEGACY_ADDRESSED_TO.search(draft_note)
            if addressed:
                contact = _clean(addressed.group(1))

        company, headline = _split_company_headline_legacy(header)
        if company:
            entries.append(
                {
                    "index": len(entries),
                    "section": section or "",
                    "company": company,
                    "headline": headline,
                    "date": date_str,
                    "source_url": source_url,
                    "summary": _clean(body),
                    "outreach_action": draft_note,
                    "framing": "",
                    "trello_note": "",
                    "contact": contact,
                    "needs_contact": needs_contact,
                    "has_draft": bool(draft_note),
                    "trello_url": trello_url,
                    "format": "legacy",
                }
            )

        cursor = trello_end
    return entries


# --------------------------------------------------------- new prospects list

NEW_PROSPECTS_HEADING = re.compile(r"New Prospects to Target", re.I)
CHECKED_HEADING = re.compile(r"Companies Checked", re.I)
STRONG_FIT_HEADING = re.compile(r"\nStrong Fit \(\d+\)\n")
WORTH_WATCHING_HEADING = re.compile(r"\nWorth Watching \(\d+\)\n")
WEAK_HEADING = re.compile(r"Also surfaced,[^\n]*\n|\nWeak or No U\.S\.-Site Fit \(\d+\)\n", re.I)

PROSPECT_ENTRY_NUM = re.compile(r"\n(\d+)\.\s+")
HQ_LABEL = re.compile(r"\nHQ:\s*")
WHAT_LABEL = re.compile(r"\nWhat they do:\s*", re.I)
SIGNAL_LABEL = re.compile(r"\n(?:This week's )?signal:\s*", re.I)
WHY_LABEL = re.compile(r"\n(?:Why it fits|Fit note):\s*", re.I)
PROSPECT_SOURCE_LABEL = re.compile(r"\nSource:\s*")
# The signal's trailing "(YYYY-MM-DD)" sometimes has its date split across a
# PDF page wrap ("(2026-08-\n20)"), which _clean() turns into "(2026-08- 20)"
# -- so whitespace is tolerated around every component, not just outside them.
TRAILING_DATE = re.compile(r"\(\s*(\d{4})\s*-\s*(\d{2})\s*-\s*(\d{2})\s*\)\s*$")

WEAK_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9 ,&/().'\"-]{1,45}):\s*(.+)$")


def _parse_prospect_block(block, tier):
    """Strong Fit / Worth Watching entries: numbered, HQ/What/Signal/Why/Source."""
    padded = "\n" + block
    numbered = list(PROSPECT_ENTRY_NUM.finditer(padded))
    out = []
    for i, num_match in enumerate(numbered):
        end = numbered[i + 1].start() if i + 1 < len(numbered) else len(padded)
        seg = padded[num_match.end():end]

        hq_match = HQ_LABEL.search(seg)
        what_match = WHAT_LABEL.search(seg)
        signal_match = SIGNAL_LABEL.search(seg)
        why_match = WHY_LABEL.search(seg)
        source_match = PROSPECT_SOURCE_LABEL.search(seg)
        if not (hq_match and what_match and signal_match and why_match and source_match):
            continue

        head = seg[:hq_match.start()]
        named = re.search(r"Company Name:\s*(.*)", head, re.I | re.S)
        company = _clean(named.group(1) if named else head)
        if not company:
            continue

        # Each field runs to the start of whichever label comes next; the order differs between layouts
        # (Source follows Fit Note in the older one, precedes it from 2026-10-28).
        marks = sorted((m.start(), m.end(), name) for name, m in (
            ("hq", hq_match), ("what", what_match), ("signal", signal_match), ("why", why_match),
            ("source", source_match)))
        field = {name: seg[end:marks[k + 1][0] if k + 1 < len(marks) else len(seg)]
                 for k, (_, end, name) in enumerate(marks)}

        signal_raw = field["signal"].strip()
        date_match = TRAILING_DATE.search(signal_raw)
        signal_date = "-".join(date_match.groups()) if date_match else ""
        signal = _clean(TRAILING_DATE.sub("", signal_raw))

        out.append(
            {
                "tier": tier,
                "company": company,
                "hq": _clean(field["hq"]),
                "what_they_do": _clean(field["what"]),
                "signal": signal,
                "signal_date": signal_date,
                "why_fits": _clean(field["why"]),
                "source_name": _clean(field["source"]),
            }
        )
    return out


def _parse_weak_prospects(block):
    """The one-line 'Name: reason' dismissals - reference only, no review needed."""
    entries = []
    current = None
    for line in block.split("\n"):
        if not line.strip():
            continue
        match = WEAK_LINE.match(line)
        if line.lstrip().startswith("●"):  # "● Name — HQ. Reason" in the 2026-10-28 layout
            if current:
                entries.append(current)
            name, _, reason = line.lstrip("● \t").partition(EM_DASH)
            current = {"company": name.strip(), "reason": reason.strip()}
        elif match:
            if current:
                entries.append(current)
            current = {"company": match.group(1).strip(), "reason": match.group(2).strip()}
        elif current:
            current["reason"] += " " + line.strip()
    if current:
        entries.append(current)
    return entries


def parse_new_prospects(text):
    """The 'New Prospects to Target' section: off-board companies to screen.

    Returns {"intro": str, "strong_fit": [...], "worth_watching": [...],
    "weak": [...]}. Each strong_fit/worth_watching entry carries a flat
    `index` unique across both lists together, for decision-tracking.
    Absent from legacy-format files - returns empty lists, not an error.
    """
    start_match = NEW_PROSPECTS_HEADING.search(text)
    empty = {"intro": "", "strong_fit": [], "worth_watching": [], "weak": []}
    if not start_match:
        return empty

    end_match = CHECKED_HEADING.search(text, start_match.end())
    section_text = text[start_match.end():end_match.start() if end_match else len(text)]

    strong_heading = STRONG_FIT_HEADING.search(section_text)
    watch_heading = WORTH_WATCHING_HEADING.search(section_text)
    weak_heading = WEAK_HEADING.search(section_text)
    if not (strong_heading and watch_heading):
        return empty

    intro = _clean(section_text[:strong_heading.start()])
    strong_block = section_text[strong_heading.end():watch_heading.start()]
    watch_end = weak_heading.start() if weak_heading else len(section_text)
    watch_block = section_text[watch_heading.end():watch_end]
    weak_block = section_text[weak_heading.end():] if weak_heading else ""

    combined = _parse_prospect_block(strong_block, "strong") + _parse_prospect_block(
        watch_block, "watching"
    )
    for i, item in enumerate(combined):
        item["index"] = i

    return {
        "intro": intro,
        "strong_fit": [c for c in combined if c["tier"] == "strong"],
        "worth_watching": [c for c in combined if c["tier"] == "watching"],
        "weak": _parse_weak_prospects(weak_block),
    }


# ------------------------------------------------------------------ top-level

_DATE_RE = re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2}")


def _scan_date(head):
    """The scan's date: the first date written out in the title or the next line ("November 4, 2026"). A second line
    that is really a sentence ("Prepared for ...") is not a date and is ignored."""
    for line in head[:2]:
        m = _DATE_RE.search(line)
        if m:
            return m.group(0)
    return ""


def parse_scan(path):
    """Return (metadata, entries, prospects) parsed from the scan at `path`.

    `entries` are board-company items (current or legacy format, whichever
    the file actually matches). `prospects` is the new off-board research
    list - empty if the file predates it.
    """
    path = Path(path)
    text = read_scan_text(path)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    entries = _parse_numbered(text)
    fmt = "current"
    if not entries:
        entries = _parse_current(text)
    if not entries:
        entries = _parse_legacy(text)
        fmt = "legacy"

    prospects = parse_new_prospects(text)

    head = text.strip().split("\n")
    metadata = {
        "title": _clean(head[0]) if head else "Weekly Prospect News Scan",
        "scan_date": _scan_date(head),
        "stats_line": _clean(head[2]) if fmt == "current" and len(head) > 2 else "",
        "source_file": str(path),
        "entry_count": len(entries),
        "format": fmt,
        "new_prospects_count": len(prospects["strong_fit"]) + len(prospects["worth_watching"]),
    }
    return metadata, entries, prospects


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else find_latest_scan(
        Path(__file__).resolve().parent.parent / "Weekly News Search"
    )
    meta, rows, prospects = parse_scan(target)
    print(json.dumps(meta, indent=2))
    for row in rows:
        print(
            f"[{row['section']}] {row['company']}"
            f" | contact={row['contact'] or ('NEEDS CONTACT' if row['needs_contact'] else '?')}"
            f" | draft={row['has_draft']}"
        )
    print()
    print(f"New prospects: {len(prospects['strong_fit'])} strong, "
          f"{len(prospects['worth_watching'])} watching, {len(prospects['weak'])} weak")
    for p in prospects["strong_fit"] + prospects["worth_watching"]:
        print(f"  [{p['tier']}] {p['company']} ({p['hq'][:30]})")
