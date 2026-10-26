"""Long threads (docs/EMAIL_CLIENT.md, section 1, "Long threads"; step 3b).

Kellen: "threads that have over 80 messages… really hard to find attachments or the most recent message". So every
thread gets one attachments panel (newest first, versions of the same document grouped, logos and calendar files and
files re-attached unchanged hidden), a one-line-per-message outline, and a link to the other parts of a
conversation Gmail split after 100 messages. The same grouping drives the Files view across the whole mailbox.
"""
import re
from datetime import datetime, timezone as dt_timezone
from email.utils import parseaddr

from . import render

# What makes two file names the same document: dates, version numbers and the words people add between rounds.
_VERSION_NOISE = re.compile(
    r"\b\d{1,4}[.\-/]\d{1,2}(?:[.\-/]\d{1,4})?\b|\b(?:19|20)\d{6}\b|\b\d{6}\b"  # dates first: "redline 10.21"
    r"|\b(?:v|ver|version|rev|r)\s*\.?\s*\d+\b"
    r"|\b(?:redline[ds]?|redlined|blackline|clean|final|draft|revised|markup|marked|comments?|executed|signed|copy|"
    r"updated|acmi|tracked|changes|compare|comparison|round|turn)\b(?:\s*#?\d{1,3}\b(?![.\-/]\d))?"
    r"|\(\d+\)",
    re.I,
)
FAMILIES = {"doc": "word", "docx": "word", "rtf": "word", "xls": "excel", "xlsx": "excel", "xlsm": "excel",
            "csv": "excel", "pdf": "pdf", "png": "image", "jpg": "image", "jpeg": "image", "gif": "image",
            "webp": "image", "heic": "image", "ppt": "slides", "pptx": "slides", "dwg": "drawing", "dxf": "drawing"}
TYPE_NAMES = {"pdf": "PDF", "word": "Word", "excel": "Excel", "image": "Images", "slides": "Slides",
              "drawing": "Drawings", "other": "Other"}
_SIGNATURE_IMAGE = re.compile(r"^(image|outlook|logo|signature|sig|banner)[\w\-]*\.(png|jpe?g|gif|bmp)$", re.I)
SMALL_IMAGE = 25_000  # bytes: a logo or a signature picture, not a photo of the site


def family(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return FAMILIES.get(ext, "other")


def doc_key(name):
    """The document a file is a version of: "PSA - ACMI redline 10.21.docx" and "PSA - ACMI redline 10.24.docx" are
    both ("psa", "word")."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    key = re.sub(r"[^a-z0-9]+", " ", _VERSION_NOISE.sub(" ", stem.lower().replace("_", " "))).strip()
    return key or stem.lower(), family(name)


def is_noise(f):
    """Logos, signature pictures and calendar files."""
    name, mime, size = f["name"], f.get("mime", ""), f.get("size", 0)
    if mime == "text/calendar" or name.lower().endswith(".ics"):
        return True
    return mime.startswith("image/") and (size < SMALL_IMAGE or bool(_SIGNATURE_IMAGE.match(name)))


def files_of(messages):
    """Every attachment on these message summaries (render.summary), newest first, with who sent it and when."""
    out = []
    for m in messages:
        for a in m["attachments"]:
            out.append({**a, "message": m["id"], "from_name": m["from_name"], "from_addr": m["from_addr"],
                        "date": m["date"], "thread": m.get("thread", "")})
    epoch = datetime.min.replace(tzinfo=dt_timezone.utc)
    return sorted(out, key=lambda f: f["date"] or epoch, reverse=True)


def group(files):
    """Versions grouped: [{"latest": file, "older": [files], "type": family}] newest first, plus how many files were
    hidden (noise, or the same file re-attached unchanged on a reply)."""
    groups, order, hidden = {}, [], 0
    seen = set()
    for f in files:  # newest first
        if is_noise(f):
            hidden += 1
            continue
        same = (f["name"].lower(), f.get("size", 0))
        if same in seen:  # re-attached unchanged
            hidden += 1
            continue
        seen.add(same)
        key = doc_key(f["name"])
        if key not in groups:
            groups[key] = {"latest": f, "older": [], "type": key[1]}
            order.append(key)
        else:
            groups[key]["older"].append(f)
    return [groups[k] for k in order], hidden


def outline(messages):
    """The thread oldest first, one line each, numbered as people count them ("message 12")."""
    rows = sorted(messages, key=lambda m: m["date"] or datetime.min.replace(tzinfo=dt_timezone.utc))
    out, n = [], 0
    for m in rows:
        if not m["draft"]:
            n += 1  # drafts aren't counted, so the numbers match Claude's summary
        out.append({"n": None if m["draft"] else n, "id": m["id"], "from_name": m["from_name"],
                    "from_addr": m["from_addr"], "date": m["date"], "words": m["snippet"][:90],
                    "files": len([a for a in m["attachments"] if not is_noise(a)]), "unread": m["unread"], "draft": m["draft"]})
    return out


def people(messages):
    seen, out = set(), []
    for m in sorted(messages, key=lambda m: m["from_name"].lower()):
        if m["from_addr"] not in seen:
            seen.add(m["from_addr"])
            out.append((m["from_addr"], m["from_name"]))
    return out


_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg)\s*(\[\d+\])?\s*:\s*)+", re.I)
SPLIT_AT = 50  # only look for other parts of conversations this long (Gmail splits at about 100)


def core_subject(subject):
    return " ".join(_PREFIX.sub("", subject or "").split())


def other_parts(mailbox, client, gmail_id, subject, messages):
    """Threads with the same subject and some of the same people: the parts of a conversation Gmail split."""
    from . import index

    core = core_subject(subject)
    if len(messages) < SPLIT_AT or len(core) < 4:
        return []
    people_here = {m["from_addr"] for m in messages}
    found, _, _ = index.page(mailbox, client, query=f'subject:"{core.replace(chr(34), "")}"')
    return [t for t in found if t.gmail_id != gmail_id and core_subject(t.subject).lower() == core.lower()
            and people_here & {s[1] for s in t.senders}][:5]


def summaries_for(mailbox, client, gmail_id):
    data = render.get_thread(mailbox, client, gmail_id)
    msgs = [render.summary(m) for m in data.get("messages", [])]
    for m in msgs:
        m["thread"] = gmail_id
    return msgs


def sender_name(header):
    name, addr = parseaddr(header)
    return name or addr
