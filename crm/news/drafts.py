"""Gmail drafts for the news review: list, match to scan entries, edit, trash.

Ported from Desktop/Prospecting/news_review/gmail_client.py. Uses the CRM
mailbox's app password. Drafts are moved to Trash, never permanently deleted.
"""
import email
import imaplib
import re
import time
from difflib import SequenceMatcher
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses

from django.conf import settings

STOPWORDS = {
    "inc", "inc.", "llc", "l.l.c", "ltd", "ltd.", "corp", "corp.", "corporation", "company", "co", "co.", "the",
    "and", "group", "holdings", "usa", "us", "america", "american", "technologies", "technology", "systems",
    "system", "solutions", "industries", "international", "labs", "laboratories", "acmi", "properties", "x", "re",
    "fw", "fwd",
}
# Sector words many prospects share: an overlap made only of these is not evidence of the same company.
INDUSTRY_WORDS = {
    "aerospace", "space", "defense", "defence", "energetics", "dynamics", "robotics", "munitions", "propulsion",
    "nuclear", "steel", "metals", "materials", "manufacturing", "industrial", "national", "global", "advanced",
    "applied", "federal", "research", "sciences", "science",
}
# Drafts are titled "ACMI x <Company>: ..." or "ACMI Properties <> <Company> - ...".
SUBJECT_TARGET = re.compile(r"(?:^|\]|\s)acmi(?:\s+properties)?\s*(?:x|<>|-|—|:)\s*(?P<name>[^:\-—<>]{2,70})", re.I)
# Placeholder drafts open with "No verified contact found for <Company>".
BODY_TARGET = re.compile(r"no verified contact found for\s+(?P<name>[^\n.;]{2,70})", re.I)
MATCH_THRESHOLD = 0.45
MAX_INLINE_BYTES = 512 * 1024
DRAFT_LIMIT = 400

_SCRIPT_BLOCK = re.compile(r"<\s*(script|style|iframe|object|embed|link|meta)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
_SELF_CLOSING_BAD = re.compile(r"<\s*(link|meta|script|iframe)\b[^>]*/?>", re.I)
_EVENT_ATTR = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_HREF = re.compile(r"(href|src)\s*=\s*(\"|')\s*javascript:[^\"']*\2", re.I)
_BODY_INNER = re.compile(r"<\s*body[^>]*>(.*?)<\s*/\s*body\s*>", re.I | re.S)


class DraftError(RuntimeError):
    pass


def sanitize_html(html):
    """Make a mail body safe to show in an editable region."""
    if not html:
        return ""
    inner = _BODY_INNER.search(html)
    if inner:
        html = inner.group(1)
    for rx in (_SCRIPT_BLOCK, _SELF_CLOSING_BAD, _EVENT_ATTR, _JS_HREF):
        html = rx.sub("", html)
    return html.strip()


def html_to_text(html):
    if not html:
        return ""
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", html, flags=re.I)
    text = re.sub(r"<\s*/\s*(p|div|li|tr|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                         ("&#39;", "'"), ("&rsquo;", "’"), ("&mdash;", "—"), ("&ndash;", "–")):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def text_to_html(text):
    if not text:
        return ""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", escaped) if b.strip()]
    return "".join(f"<p>{b.replace(chr(10), '<br>')}</p>" for b in blocks)


def _decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _bodies(msg):
    text_body, html_body = "", ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or "attachment" in str(part.get("Content-Disposition") or "").lower():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            decoded = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain" and not text_body:
            text_body = decoded
        elif ctype == "text/html" and not html_body:
            html_body = decoded
    return text_body, html_body


# ------------------------------------------------------------------ matching

def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOPWORDS and len(w) > 2}


def _name_similarity(company, candidate):
    left, right = _tokens(company), _tokens(candidate)
    overlap = left & right
    if not left or not right or not overlap:
        return 0.0
    coverage = len(overlap) / len(left)
    ratio = SequenceMatcher(None, " ".join(sorted(left)), " ".join(sorted(right))).ratio()
    score = 0.65 * coverage + 0.35 * ratio
    if not (overlap - INDUSTRY_WORDS):
        score *= 0.4
    return min(score, 1.0)


def match_score(company, subject, body=""):
    """How strongly a draft identifies `company` (0-1). Compares the subject's company slot, not the whole subject."""
    if not _tokens(company):
        return 0.0
    candidates = []
    target = SUBJECT_TARGET.search(subject or "")
    if target:
        candidates.append(_name_similarity(company, target.group("name").strip()))
    body_match = BODY_TARGET.search(body or "")
    if body_match:
        candidates.append(0.95 * _name_similarity(company, body_match.group("name")))
    if not target:
        candidates.append(0.7 * _name_similarity(company, subject))
    return round(max(candidates) if candidates else 0.0, 3)


def match_drafts(items, drafts, threshold=MATCH_THRESHOLD):
    """{item.pk: (draft, score)}: strongest pairs first; each draft and item used once."""
    scored = []
    for item in items:
        for d in drafts:
            score = match_score(item.company_name, d["subject"], d["snippet"])
            if score >= threshold:
                scored.append((score, item.pk, d["uid"]))
    scored.sort(key=lambda r: (-r[0], r[1]))
    by_uid = {d["uid"]: d for d in drafts}
    used_uids, out = set(), {}
    for score, pk, uid in scored:
        if uid in used_uids or pk in out:
            continue
        out[pk] = (by_uid[uid], score)
        used_uids.add(uid)
    return out


def recipients(to_text, cc_text="", exclude=()):
    """[{name, email}] from To/Cc, minus your own and internal addresses."""
    exclude = {e.lower() for e in exclude}
    internal = {d.lower() for d in settings.INTERNAL_EMAIL_DOMAINS}
    people, seen = [], set()
    for raw_name, address in getaddresses([", ".join(filter(None, [to_text, cc_text]))]):
        address = (address or "").strip().lower()
        if (not address or "@" not in address or address in exclude or address in seen
                or address.rsplit("@", 1)[1] in internal):
            continue
        seen.add(address)
        people.append({"name": (raw_name or "").strip().strip('"'), "email": address})
    return people


# ------------------------------------------------------------------ IMAP

class Drafts:
    """Context manager over the mailbox's Drafts folder."""

    def __init__(self, mailbox):
        self.mailbox = mailbox
        self.conn = None

    def __enter__(self):
        password = self.mailbox.get_password()
        if not password:
            raise DraftError(f"No Gmail app password: add {self.mailbox.password_env_var} to .env.")
        try:
            self.conn = imaplib.IMAP4_SSL(settings.IMAP_HOST)
            self.conn.login(self.mailbox.email, password)
        except imaplib.IMAP4.error as exc:
            raise DraftError(f"Gmail sign-in failed: {exc}") from exc
        self.folders = self._special_folders()
        return self

    def __exit__(self, *exc):
        for step in (self.conn.close, self.conn.logout):
            try:
                step()
            except Exception:
                pass

    def _special_folders(self):
        """Drafts and Trash by their special-use flags (folder names are localized)."""
        found = {"drafts": "[Gmail]/Drafts", "trash": "[Gmail]/Trash"}
        typ, data = self.conn.list()
        for line in data or []:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            name = re.search(r'"([^"]*)"\s*$', text.strip())
            if not name:
                continue
            if "\\Drafts" in text:
                found["drafts"] = name.group(1)
            elif "\\Trash" in text:
                found["trash"] = name.group(1)
        return found

    def _select(self, readonly):
        typ, data = self.conn.select(f'"{self.folders["drafts"]}"', readonly=readonly)
        if typ != "OK":
            raise DraftError(f"Could not open the Drafts folder: {data}")

    def list(self, limit=DRAFT_LIMIT):
        self._select(readonly=True)
        typ, data = self.conn.uid("SEARCH", None, "ALL")
        uids = (data[0] or b"").split()[-limit:] if typ == "OK" and data else []
        sizes = {}
        if uids:
            typ, resp = self.conn.uid("FETCH", b",".join(uids), "(RFC822.SIZE)")
            for item in resp or []:
                text = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
                m = re.search(r"UID (\d+).*?RFC822\.SIZE (\d+)", text)
                if m:
                    sizes[m.group(1)] = int(m.group(2))
        drafts = []
        for uid in uids:
            uid_text = uid.decode()
            too_large = sizes.get(uid_text, 0) > MAX_INLINE_BYTES
            typ, fetched = self.conn.uid("FETCH", uid, "(BODY.PEEK[HEADER])" if too_large else "(BODY.PEEK[])")
            raw = next((p[1] for p in fetched or [] if isinstance(p, tuple) and len(p) == 2), b"")
            if typ != "OK" or not raw:
                continue
            msg = email.message_from_bytes(raw)
            text_body, html_body = ("", "") if too_large else _bodies(msg)
            editable = sanitize_html(html_body) or text_to_html(text_body)
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_body or text_body)).strip()[:400]
            drafts.append({
                "uid": uid_text, "subject": _decode(msg.get("Subject")), "to": _decode(msg.get("To")),
                "cc": _decode(msg.get("Cc")), "html": editable, "snippet": snippet, "too_large": too_large,
            })
        return drafts

    def update(self, uid, to, subject, html_body, cc=""):
        """Save an edited draft: append the new version first, then move the original to Trash. Returns new uid."""
        self._select(readonly=False)
        typ, fetched = self.conn.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
        original = next((email.message_from_bytes(p[1]) for p in fetched or [] if isinstance(p, tuple)), None)
        message = EmailMessage()
        message["From"] = self.mailbox.email
        if to:
            message["To"] = to
        if cc:
            message["Cc"] = cc
        message["Subject"] = subject or ""
        if original is not None:
            for header in ("In-Reply-To", "References"):
                if original.get(header):
                    message[header] = original.get(header)
        message.set_content(html_to_text(html_body))
        message.add_alternative(html_body, subtype="html")
        typ, resp = self.conn.append(f'"{self.folders["drafts"]}"', "(\\Draft)",
                                     imaplib.Time2Internaldate(time.time()), message.as_bytes())
        if typ != "OK":
            raise DraftError(f"Could not save the revised draft: {resp}")
        new_uid = None
        for item in resp or []:
            m = re.search(r"APPENDUID \d+ (\d+)", item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item))
            if m:
                new_uid = m.group(1)
        self._trash([uid])
        return new_uid

    def trash(self, uids):
        self._select(readonly=False)
        return self._trash(uids)

    def _trash(self, uids):
        results = {}
        for uid in uids:
            typ, _ = self.conn.uid("COPY", uid, f'"{self.folders["trash"]}"')
            if typ != "OK":
                results[uid] = "copy to Trash failed"
                continue
            self.conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            results[uid] = "trashed"
        self.conn.expunge()
        return results
