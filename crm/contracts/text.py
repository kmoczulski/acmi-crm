"""Text out of contract documents, and the structure Notessa-style reading needs: pages, defined terms, numbered
sections, cross-references, and which amendment changes which section.

All of it is pattern matching over the extracted text, so it costs nothing and works without Claude. It follows how
US agreements are written ("Effective Date" means…; (the "Buyer"); Section 5.2(a)). Unusual drafting only means
fewer links in the reader, never wrong text.
"""
import html
import io
import logging
import re
import zipfile
from bisect import bisect_right
from xml.etree import ElementTree

log = logging.getLogger(__name__)

MAX_CHARS = 2_000_000  # per document; a 600-page agreement is about 1.5M characters
PAGE_GAP = "\n\n"
SECTION_CHARS = 6000  # most of a section shown in the side panel
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract(data, name):
    """(text, page_starts, status) for a file's bytes; status is a ContractDocument.TextStatus value."""
    return extract_full(data, name)[:3]


def extract_full(data, name):
    """(text, page_starts, status, tracked): extract() plus a Word file's tracked changes, as
    [{"type": "ins" | "del", "start", "end", "author", "date", "text"}] against the text (a deletion sits at a point and
    keeps the words it took out). The text is the document with every change accepted."""
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    changes = []
    try:
        if suffix == "pdf":
            pages = _pdf_pages(data)
        elif suffix == "docx":
            pages = _docx_pages(data, changes)
        elif suffix in ("txt", "md"):
            pages = [data.decode("utf-8", errors="replace")]
        else:
            return "", [], "unsupported", []
    except Exception as exc:  # damaged, password-protected, or not really that file type
        log.warning("Could not read %s: %s", name, exc)
        return "", [], "error", []
    text, starts = join_pages([clean(p) for p in pages])
    text, starts, tracked = _take_marks(text, starts, changes)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        starts = [s for s in starts if s < MAX_CHARS]
        tracked = [c for c in tracked if c["end"] <= MAX_CHARS]
    # A scan has pages but (almost) no text layer: Claude can still read the PDF itself, the reader can't.
    if suffix == "pdf" and len(re.sub(r"\s", "", text)) < 40 * max(1, len(starts)):
        return text, starts, "scan", []
    return text, starts, "ok" if text.strip() else "scan", tracked


def _pdf_pages(data):
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")  # signed PDFs are often locked with an empty password just to stop edits
    return [page.extract_text() or "" for page in reader.pages]


# Tracked changes ride through cleaning as private-use marks: open + id ... close for an insertion, del + id for the
# point where a deletion was; _take_marks() turns them into offsets.
INS_OPEN, INS_CLOSE, DEL_AT, ID_BASE = "\ue000", "\ue001", "\ue002", 0xE100


def _docx_pages(data, changes=None):
    """Paragraph text from a .docx, split where Word put page breaks, with tracked changes accepted (insertions kept,
    deletions left out) and noted in `changes`. Auto-numbered headings lose their numbers (Word draws them), so
    cross-references in Word files link less often than in PDFs."""
    changes = [] if changes is None else changes
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    pages, lines = [], []
    parts = []

    def change(el, kind):
        changes.append({"type": kind, "author": el.get(W + "author") or "", "date": (el.get(W + "date") or "")[:10],
                        "text": ""})
        return chr(ID_BASE + len(changes) - 1)

    def walk(el):
        nonlocal parts, lines
        for child in el:
            tag = child.tag
            if tag in (W + "ins", W + "moveTo"):
                parts.append(INS_OPEN + change(child, "ins"))
                walk(child)
                parts.append(INS_CLOSE)
            elif tag in (W + "del", W + "moveFrom"):
                mark = change(child, "del")
                changes[-1]["text"] = "".join(t.text or "" for t in child.iter() if t.tag in (W + "delText", W + "t"))
                parts.append(DEL_AT + mark)
            elif tag == W + "t":
                parts.append(child.text or "")
            elif tag == W + "tab":
                parts.append("\t")
            elif tag in (W + "br", W + "cr", W + "lastRenderedPageBreak"):
                if tag == W + "lastRenderedPageBreak" or child.get(W + "type") == "page":
                    if parts or lines:
                        lines.append("".join(parts))
                        pages.append("\n".join(lines))
                        parts, lines = [], []
                else:
                    parts.append("\n")
            elif tag in (W + "pPr", W + "rPr", W + "delText"):
                continue
            else:
                walk(child)

    for p in root.iter(W + "p"):
        walk(p)
        lines.append("".join(parts))
        parts = []
    pages.append("\n".join(lines))
    return pages


def _take_marks(text, starts, changes):
    """Strip the tracked-change marks out of the text: (text, page_starts, tracked) with offsets in the clean text."""
    if not changes or not any(c in text for c in (INS_OPEN, DEL_AT)):
        return text.replace(INS_CLOSE, ""), starts, []
    out, tracked, open_ = [], [], []
    removed = []  # positions in the marked text that were dropped, to move the page starts back
    pos, i = 0, 0
    while i < len(text):
        ch = text[i]
        if ch in (INS_OPEN, DEL_AT) and i + 1 < len(text):
            info = changes[ord(text[i + 1]) - ID_BASE] if 0 <= ord(text[i + 1]) - ID_BASE < len(changes) else None
            removed += [i, i + 1]
            if info is not None:
                if ch == INS_OPEN:
                    open_.append({**info, "start": pos})
                elif info["text"]:
                    tracked.append({**info, "start": pos, "end": pos})
            i += 2
            continue
        if ch == INS_CLOSE:
            removed.append(i)
            if open_:
                c = open_.pop()
                if pos > c["start"]:
                    tracked.append({**c, "end": pos})
            i += 1
            continue
        out.append(ch)
        pos += 1
        i += 1
    starts = [s - bisect_right(removed, s - 1) for s in starts]
    tracked.sort(key=lambda c: (c["start"], c["type"] != "del"))
    return "".join(out), starts, tracked


def clean(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").replace("­", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def join_pages(pages):
    """One text for the document plus the offset where each page starts."""
    out, starts, pos = [], [], 0
    for i, page in enumerate(pages):
        if i:
            out.append(PAGE_GAP)
            pos += len(PAGE_GAP)
        starts.append(pos)
        out.append(page)
        pos += len(page)
    return "".join(out), starts


def page_of(starts, index):
    return max(1, bisect_right(starts or [0], index))


# ---------------------------------------------------------------------------
# Defined terms
# ---------------------------------------------------------------------------

_TERM = r"[A-Z][\w&/'’.,-]*(?: [\w&/'’.,-]+){0,7}?"
DEF_MEANS = re.compile(
    r"[\"“]\s*(" + _TERM + r")\s*[\"”]\s*(?:shall\s+)?(?:means?|mean|shall have the meanings?|has the meanings?|"
    r"have the meanings?|is defined|are defined|refers? to|shall refer to|includes?)\b"
)
PAREN = re.compile(r"\(([^()]{1,200})\)")
QUOTED = re.compile(r"[\"“]\s*(" + _TERM + r")\s*[\"”]")
PAREN_WORDS = {
    "the", "a", "an", "each", "collectively", "individually", "together", "hereinafter", "herein", "referred",
    "to", "as", "and", "or", "respectively", "being", "called", "defined", "sometimes", "jointly", "severally",
    "its", "this", "such", "with", "any", "all", "of", "them", "also", "known", "each,", "",
}


def _key(term):
    return " ".join(term.split()).rstrip(".,")


def _sentence_start(text, i, limit=500):
    floor = max(0, i - limit)
    best = floor
    for mark in (". ", ".\n", "\n\n", "; ", ":\n"):
        j = text.rfind(mark, floor, i)
        if j >= 0:
            best = max(best, j + len(mark))
    return best


def _passage_end(text, i, limit=1500):
    """End of the paragraph starting near `i`, cut at a sentence end if it runs long."""
    end = text.find("\n\n", i)
    end = len(text) if end < 0 else end
    nxt = re.compile(r"\n\s*[\"“][A-Z]").search(text, i + 1, end)  # the next definition in a list of them
    if nxt:
        end = nxt.start()
    if end - i > limit:
        cut = text.rfind(". ", i, i + limit)
        end = cut + 1 if cut > i else i + limit
    return end


def definitions(text):
    """[(term, start, end)]: each defined term with the passage that defines it, in text order."""
    found = []
    for m in DEF_MEANS.finditer(text):
        found.append((_key(m.group(1)), m.start(), _passage_end(text, m.start())))
    for m in PAREN.finditer(text):
        inner = m.group(1)
        terms = [q.group(1) for q in QUOTED.finditer(inner)]
        if not terms:
            continue
        rest = QUOTED.sub(" ", inner).replace(",", " ").lower().split()
        if any(w not in PAREN_WORDS for w in rest):  # (see "Exhibit A") or a quotation, not a definition
            continue
        start = _sentence_start(text, m.start())
        found += [(_key(t), start, m.end()) for t in terms]
    seen, out = set(), []
    for term, start, end in sorted(found, key=lambda f: f[1]):
        if 2 < len(term) <= 60 and term not in seen:
            seen.add(term)
            out.append((term, start, end))
    return out


# ---------------------------------------------------------------------------
# Sections and cross-references
# ---------------------------------------------------------------------------

_NUM = r"\d{1,3}(?:\.\d{1,3}){0,4}(?![\d])"
SECTION_HEAD = re.compile(
    r"(?m)^[ \t]*(?:"
    r"(?:Section|SECTION|Sec\.|SEC\.|Article|ARTICLE|Paragraph|PARAGRAPH|§)[ \t]*(?P<n1>" + _NUM + r")\.?"
    r"|(?P<n2>\d{1,3}(?:\.\d{1,3}){1,4})(?![\d])\.?"
    r"|(?P<n3>\d{1,3})[.)]"
    r")[ \t]+(?=[A-Z(\[\"“])"
)
XREF = re.compile(
    r"\b(?:Sections?|SECTIONS?|Paragraphs?|PARAGRAPHS?|Articles?|ARTICLES?)[ \t]+(" + _NUM + r")((?:\([a-zA-Z0-9]{1,5}\))*)"
    r"|§§?[ \t]*(" + _NUM + r")((?:\([a-zA-Z0-9]{1,5}\))*)"
)
XREF_MORE = re.compile(r"[ \t]*(?:,|and|or|through)[ \t]*(?:and[ \t]+)?(" + _NUM + r")((?:\([a-zA-Z0-9]{1,5}\))*)")
OF_BASE = re.compile(r"[ \t,]*of[ \t]+(?:the|this|that)[ \t]+(?:Original[ \t]+|Existing[ \t]+|Purchase[ \t]+|Lease[ \t]+)?"
                     r"(?:Agreement|Lease|Contract|PSA|Option)", re.I)
AMEND_VERBS = re.compile(r"\b(?:amend\w*|delet\w*|replac\w*|restat\w*|modif\w*|insert\w*|add|added|adding|"
                         r"supplement\w*|revis\w*|extend\w*|substitut\w*)\b", re.I)


def headings(text):
    return [(m.start(), m.group("n1") or m.group("n2") or m.group("n3")) for m in SECTION_HEAD.finditer(text)]


def sections(text):
    """{number: (start, end)} for each numbered heading. A section runs to the next heading that isn't one of its own
    subsections. A number seen twice (table of contents, then the body) keeps the one with the most text."""
    heads = headings(text)
    out = {}
    for i, (start, num) in enumerate(heads):
        j = i + 1
        while j < len(heads) and heads[j][1].startswith(num + "."):
            j += 1
        end = heads[j][0] if j < len(heads) else len(text)
        if num not in out or end - start > out[num][1] - out[num][0]:
            out[num] = (start, end)
    return out


def xrefs(text):
    """[(start, end, number, refers_to_base)]: each "Section 5.2(a)" with the section number it points at. The
    flag is set when the text says "…of the Agreement", i.e. the original rather than this document."""
    head_starts = {start for start, _ in headings(text)}
    out = []
    for m in XREF.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if line_start in head_starts and not text[line_start:m.start()].strip():
            continue  # the heading itself
        num = m.group(1) or m.group(3)
        hits = [(m.start(), m.end(), num)]
        pos = m.end()
        while True:  # "Sections 5.2, 5.3 and 5.4"; a number of another depth ("Section 5.2, 10 days") isn't one
            more = XREF_MORE.match(text, pos)
            if not more or more.group(1).count(".") != num.count("."):
                break
            hits.append((more.start(1), more.end(), more.group(1)))
            pos = more.end()
        base = bool(OF_BASE.match(text, pos))
        out += [(s, e, n, base) for s, e, n in hits]
    return out


SELF_REF = re.compile(r"\bthis (?:Amendment|Letter|Notice|Side Letter|Waiver)\b", re.I)


def amended_sections(text):
    """{number: (start, end)} for the sections of the base agreement that an amendment changes: sentences that cite
    a section and use an amending verb ("Section 5.2 of the Agreement is hereby deleted and replaced…"). The range is
    the amending paragraph."""
    out = {}
    for start, end, num, of_base in xrefs(text):
        s = _sentence_start(text, start, limit=300)
        stop = text.find(". ", end)
        sentence = text[s:min(stop if stop >= 0 else len(text), end + 300)]
        if not AMEND_VERBS.search(sentence) or (SELF_REF.search(sentence) and not of_base):
            continue
        para_end = text.find("\n\n", end)
        out.setdefault(num, (s, min(para_end if para_end >= 0 else len(text), s + 1500)))
    return out


# ---------------------------------------------------------------------------
# Reader HTML
# ---------------------------------------------------------------------------


def _term_pattern(terms):
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(r"(?<![\w])(" + alts + r")(?![\w])")


def reader_html(text, page_starts, *, terms=(), links=None, anchors=None, badges=None, highlight=None, tracked=None,
                marks=None):
    """The document as HTML, page by page: defined terms are buttons, cross-references are links, section headings
    carry anchors (and "amended by" badges), and the `highlight` range (start, end) is marked with id="hl".

    links: {start: (end, href, {data attribute: value})} for the cross-references that resolve.
    anchors: {position: element id} for empty anchors (section headings, definitions).
    badges: {position: [(label, href)]} shown at that position (before a section heading).
    tracked: Word tracked changes (ContractDocument.tracked): insertions underlined, deletions struck through in place.
    marks: [(start, end, id)] comment ranges, highlighted; they may overlap anything.

    Everything added that isn't the document's own text carries data-x, so the page can count characters back to
    offsets in `text` (for pinning a comment to a selection).
    """
    links, anchors, badges = links or {}, anchors or {}, badges or {}
    spans = []  # (start, end, open tag, close tag), never overlapping
    ins = [(c["start"], c["end"], c) for c in tracked or [] if c.get("type") == "ins" and c["end"] > c["start"]]
    for start, end, c in ins:
        who = html.escape(f"Added by {c.get('author') or 'unknown'}" + (f", {c['date']}" if c.get("date") else ""))
        spans.append((start, end, f'<ins class="tc-ins {c.get("cls", "")}" data-tc="{c.get("n", "")}" '
                                  f'data-author="{html.escape(c.get("author", ""))}" title="{who}">', "</ins>"))
    for start, (end, href, attrs) in sorted(links.items()):
        if any(s < end and start < e for s, e, _ in ins):
            continue  # inside a tracked insertion
        data = "".join(f' data-{k}="{html.escape(str(v))}"' for k, v in attrs.items())
        spans.append((start, end, f'<a class="cr-xref" href="{html.escape(href)}"{data}>', "</a>"))
    pattern = _term_pattern(terms)
    if pattern:
        taken = sorted((s, e) for s, e, *_ in spans)
        starts = [s for s, _ in taken]
        for m in pattern.finditer(text):
            i = bisect_right(starts, m.start()) - 1
            if (i >= 0 and taken[i][1] > m.start()) or (i + 1 < len(taken) and taken[i + 1][0] < m.end()):
                continue  # inside a cross-reference
            spans.append((m.start(), m.end(),
                          f'<button type="button" class="cr-term" data-term="{html.escape(m.group(1))}">', "</button>"))
    spans.sort()

    bounds = list(page_starts or [0]) + [len(text)]
    pages = []
    for n in range(len(bounds) - 1):
        ps, pe = bounds[n], bounds[n + 1]
        if n + 2 < len(bounds):
            pe = max(ps, pe - len(PAGE_GAP))  # the blank lines between pages belong to neither
        pages.append((ps, pe))
    if highlight:
        hs, he = highlight
        for s, e, *_ in spans:  # never split a link or a term: widen the mark over it
            if s < hs < e:
                hs = s
            if s < he < e:
                he = e
        for ps, pe in pages:  # a mark starting in the gap between pages starts on the next page
            if hs < ps:
                hs = min(ps, he)
                break
            if hs <= pe:
                break
        highlight = (hs, he) if he > hs else None

    points = {}
    for pos, ident in anchors.items():
        points.setdefault(pos, []).append(f'<span class="cr-anchor" id="{html.escape(ident)}"></span>')
    for pos, items in badges.items():
        points.setdefault(pos, []).append(
            "".join(f'<a class="cr-badge" data-x href="{html.escape(h)}">{html.escape(label)}</a>' for label, h in items))
    for c in tracked or []:
        if c.get("type") == "del" and c.get("text"):
            who = html.escape(f"Deleted by {c.get('author') or 'unknown'}" + (f", {c['date']}" if c.get("date") else ""))
            points.setdefault(c["start"], []).append(
                f'<del class="tc-del {c.get("cls", "")}" data-x data-tc="{c.get("n", "")}" '
                f'data-author="{html.escape(c.get("author", ""))}" title="{who}">'
                f'{html.escape(c["text"], quote=False)}</del>')
    marks = [(s, e, i) for s, e, i in marks or [] if e > s]

    out, span_i = [], 0
    for n, (ps, pe) in enumerate(pages, start=1):
        out.append(f'<section class="cr-page" id="page-{n}"><div class="cr-text" data-ps="{ps}">')
        page_spans = []
        while span_i < len(spans) and spans[span_i][0] < pe:
            if spans[span_i][1] <= pe:
                page_spans.append(spans[span_i])
            span_i += 1
        cuts = {ps, pe} | {p for p in points if ps <= p < pe}
        for s, e, *_ in page_spans:
            cuts.update((s, e))
        if highlight:
            cuts.update(p for p in highlight if ps <= p <= pe)
        cuts.update(p for s, e, _ in marks for p in (s, e) if ps < p < pe)
        opens = {s: (e, o, c) for s, e, o, c in page_spans}
        closing = {}
        marking = bool(highlight and highlight[0] < ps < highlight[1])
        if marking:
            out.append('<mark class="cr-hl">')
        cut_list = sorted(cuts)
        for k, pos in enumerate(cut_list[:-1]):
            if highlight and pos == highlight[0]:
                out.append('<mark class="cr-hl" id="hl">')
                marking = True
            out += points.get(pos, [])
            if pos in opens:
                end, open_tag, close_tag = opens[pos]
                out.append(open_tag)
                closing[end] = close_tag
            nxt = cut_list[k + 1]
            piece = html.escape(text[pos:nxt], quote=False)
            on = [str(i) for s, e, i in marks if s <= pos and nxt <= e]
            out.append(f'<mark class="cm-mark" data-c="{" ".join(on)}">{piece}</mark>' if on and piece else piece)
            if nxt in closing:
                out.append(closing.pop(nxt))
            if marking and highlight and nxt == highlight[1]:
                out.append("</mark>")
                marking = False
        if marking:
            out.append("</mark>")
        out.append(f'</div><div class="cr-page-no">Page {n} of {len(pages)}</div></section>')
    return "".join(out)
