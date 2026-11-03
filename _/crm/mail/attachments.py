"""Attachment names and sizes from an IMAP BODYSTRUCTURE, without downloading files.

Lets the CRM show which documents (brochures, PDAs, LOIs...) went to whom.
"""
import re
from email.header import decode_header, make_header

# One leaf body part: ("type" "subtype" (params) id description encoding size ...
_PART = re.compile(
    rb'\("([A-Za-z]+)" "([A-Za-z0-9.+-]+)" (\((?:[^()"]|"(?:[^"\\]|\\.)*")*\)|NIL) '
    rb'(?:NIL|"(?:[^"\\]|\\.)*") (?:NIL|"(?:[^"\\]|\\.)*") (?:NIL|"[^"]*") (\d+)'
)
_NAME = re.compile(rb'"(?:NAME|FILENAME|name|filename|Name|Filename)\*?" "((?:[^"\\]|\\.)*)"')
_DISPOSITION = re.compile(rb'\("(?:attachment|ATTACHMENT|inline|INLINE)" \("(?:FILENAME|filename)\*?" "((?:[^"\\]|\\.)*)"')
_INLINE_IMAGE = re.compile(r"^(image\d+|outlook-[\w-]+|~wrd\d+|logo\w*|signature\w*)\.(png|jpe?g|gif|bmp)$", re.I)

# First match wins. Keys are what the Documents panel shows.
CATEGORIES = [
    ("PDA", r"\bpda\b|pre[- ]?development"),
    ("LOI", r"\bloi\b|letter of intent"),
    ("NDA", r"\bnda\b|non[- ]?disclosure|confidentiality"),
    ("Contract", r"contract|agreement|amendment|\bexhibit\b|\bex\. ?[a-z]\b|\baia\b|\ba[12]\d\d\b"),
    ("Lease / term sheet", r"\blease\b|term ?sheet|\bbts\b|build[- ]to[- ]suit"),
    ("Proposal", r"proposal|\brfp\b|\brfq\b|quote|pricing"),
    ("Site / plans", r"site ?plan|master ?plan|layout|survey|plat\b|rendering|floor ?plan|\bcad\b|\.dwg$"),
    ("Marketing", r"brochure|flyer|overview|one[- ]?pager|deck|presentation|scaling|campus|nsih|meih|\bacmi\b"),
    ("Financials", r"pro ?forma|budget|model|\.xlsx?$|financial"),
]


def _decode(raw):
    text = raw.decode("utf-8", errors="replace").replace('\\"', '"')
    if "=?" in text:
        try:
            text = str(make_header(decode_header(text)))
        except Exception:
            pass
    return text.strip()


def categorize(filename):
    name = filename.lower()
    for label, pattern in CATEGORIES:
        if re.search(pattern, name):
            return label
    return "Other"


def parse_bodystructure(data):
    """[(filename, approx_bytes, mime_type)] for real attachments in a BODYSTRUCTURE response."""
    found = {}
    for m in _PART.finditer(data):
        ctype = f"{m.group(1).decode().lower()}/{m.group(2).decode().lower()}"
        params, size = m.group(3), int(m.group(4))
        names = _NAME.findall(params)
        if not names:
            # The filename may sit in the disposition that follows this part.
            tail = data[m.end(): m.end() + 400]
            names = _DISPOSITION.findall(tail)[:1]
        if not names:
            continue
        filename = _decode(names[0])
        approx = size * 3 // 4  # base64 on the wire
        if _INLINE_IMAGE.match(filename) or (ctype.startswith("image/") and approx < 40_000):
            continue  # signature logos and pasted screenshots
        if ctype in ("text/calendar", "application/ics") or filename.lower().endswith(".ics"):
            continue
        found.setdefault(filename, (filename, approx, ctype))
    return list(found.values())
