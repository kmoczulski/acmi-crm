"""Turn raw RFC 822 messages into ParsedEmail, and decide what each one means.

Classification follows Desktop/Prospecting/scan_responses.py (bounce senders and
subjects, out-of-office subjects, not-interested phrases), plus header checks
that only a full message gives us (X-Failed-Recipients, Auto-Submitted,
List-Unsubscribe).
"""
import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes, message_from_string, policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from django.utils import timezone

BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail-daemon")
BOUNCE_SUBJECTS = ("undeliverable", "delivery status", "mail delivery failed", "delivery has failed",
                   "returned mail", "failure notice")
OOO_SUBJECTS = ("out of office", "automatic reply", "ooo:", "away from", "autoreply", "auto-reply")
NOT_INTERESTED_PHRASES = ("unsubscribe", "remove me", "stop emailing", "opt out", "not interested")
NO_REPLY_LOCALPARTS = ("noreply", "no-reply", "donotreply", "do-not-reply", "notifications", "notification",
                       "calendar-notification", "news", "newsletter", "marketing", "updates")
BOUNCE_BODY_PATTERNS = (
    r"wasn'?t delivered to\s+<?([^\s<>]+@[^\s<>]+)",
    r"delivery to[:\s]+<?([^\s<>]+@[^\s<>]+)",
    r"could not be delivered to[:\s]+<?([^\s<>]+@[^\s<>]+)",
    r"rejected.*?<?([^\s<>]+@[^\s<>]+)",
    r"undeliverable.*?<?([^\s<>]+@[^\s<>]+)",
)
BODY_LIMIT = 4000


@dataclass
class ParsedEmail:
    uid: int
    message_id: str
    thread_id: str
    occurred_at: datetime
    from_addr: str
    from_name: str
    to: list = field(default_factory=list)  # [(name, addr)]
    cc: list = field(default_factory=list)
    subject: str = ""
    body: str = ""
    in_reply_to: str = ""
    references: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)  # lower-case name -> value, selected headers only
    attachments: list = field(default_factory=list)  # [(filename, approx bytes, mime type)]
    calendar: list = field(default_factory=list)  # the text of each calendar part (.ics), for crm/invites.py

    @property
    def is_reply(self):
        return bool(self.in_reply_to or self.references) or self.subject.lower().startswith(("re:", "re :"))

    @property
    def is_bulk(self):
        """Newsletters, notifications and other machine mail."""
        local = self.from_addr.split("@")[0]
        precedence = self.headers.get("precedence", "").lower()
        return bool(
            self.headers.get("list-unsubscribe") or self.headers.get("list-id")
            or precedence in ("bulk", "list", "junk")
            or any(local == p or local.startswith(p + "+") for p in NO_REPLY_LOCALPARTS)
        )


@dataclass
class Envelope:
    """One counterparty on one email, and what the email means for them."""

    address: str
    name: str
    kind: str  # an Activity.Kind value
    outcome: str = ""


KEPT_HEADERS = ("x-failed-recipients", "auto-submitted", "x-autoreply", "x-autorespond", "precedence",
                "list-unsubscribe", "list-id")


def _clean_addr(addr):
    return (addr or "").strip().strip("<>").lower()


def _text_body(msg):
    """Best-effort plain text: text/plain part, else text/html stripped of tags."""
    plain, html_part = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, ValueError, AssertionError):
            content = None
        if content is None or (isinstance(content, str) and "\ufffd" in content):
            # Mislabelled charset (often Outlook's Windows-1252 curly quotes): decode the bytes ourselves.
            payload = part.get_payload(decode=True) or b""
            try:
                content = payload.decode(part.get_content_charset() or "utf-8")
            except (LookupError, UnicodeDecodeError):
                content = payload.decode("cp1252", errors="replace")
        if not isinstance(content, str):
            # message/delivery-status and similar come back as objects; use their text form.
            content = str(content)
        if ctype == "text/plain" and plain is None:
            plain = content
        elif ctype == "text/html" and html_part is None:
            html_part = content
        elif ctype in ("message/delivery-status", "text/rfc822-headers") and plain is None:
            plain = content
    return finish_text(plain, html_part)


CALENDAR_TYPES = ("text/calendar", "application/ics")


def calendar_parts(msg):
    """The text of every calendar part (an invite's .ics, inline or attached, also inside a forwarded message)."""
    out = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        name = (part.get_filename() or "").lower()
        if part.get_content_type() not in CALENDAR_TYPES and not name.endswith(".ics"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if "BEGIN:VEVENT" in text and text not in out:
            out.append(text)
    return out


def finish_text(plain, html_part):
    """The plain part, else the HTML part without tags; whitespace tidied and cut to BODY_LIMIT."""
    text = plain
    if text is None and html_part:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_part)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()[:BODY_LIMIT]


def parse_message(raw, uid=0, thread_id="", labels=None):
    """raw: the message's bytes (IMAP), or its text (the Gmail API's already-decoded headers)."""
    if isinstance(raw, str):
        msg = message_from_string(raw, policy=policy.default)
    else:
        msg = message_from_bytes(raw, policy=policy.default)
    from_name, from_addr = parseaddr(str(msg.get("From", "")))
    try:
        occurred_at = parsedate_to_datetime(str(msg.get("Date", "")))
        if timezone.is_naive(occurred_at):
            occurred_at = timezone.make_aware(occurred_at)
    except (TypeError, ValueError, IndexError):
        occurred_at = timezone.now()
    message_id = str(msg.get("Message-ID", "")).strip() or f"<uid-{uid}-{thread_id}@local>"
    return ParsedEmail(
        uid=uid,
        message_id=message_id[:500],
        thread_id=str(thread_id),
        occurred_at=occurred_at,
        from_addr=_clean_addr(from_addr),
        from_name=from_name,
        to=[(n, _clean_addr(a)) for n, a in getaddresses([str(v) for v in msg.get_all("To", [])]) if a],
        cc=[(n, _clean_addr(a)) for n, a in getaddresses([str(v) for v in msg.get_all("Cc", [])]) if a],
        subject=" ".join(str(msg.get("Subject", "")).split())[:500],
        body=_text_body(msg),
        calendar=calendar_parts(msg),
        in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
        references=str(msg.get("References", "")).split(),
        labels=list(labels or []),
        headers={h: str(msg.get(h, "")) for h in KEPT_HEADERS if msg.get(h)},
    )


QUOTE_MARKERS = (
    r"\n\s*On .{0,200}wrote:",
    r"\n\s*-{2,}\s*Original Message\s*-{2,}",
    r"\n\s*From:\s.*\n\s*(Sent|Date):",
    r"\n\s*>",
)


def new_text(body):
    """The part of a reply above the quoted thread."""
    cut = len(body)
    for marker in QUOTE_MARKERS:
        m = re.search(marker, body, flags=re.IGNORECASE)
        if m:
            cut = min(cut, m.start())
    return body[:cut]


def _bounced_address(email, own_addresses):
    failed = email.headers.get("x-failed-recipients", "")
    if failed:
        return _clean_addr(failed.split(",")[0])
    body = email.body.lower()
    for pattern in BOUNCE_BODY_PATTERNS:
        m = re.search(pattern, body)
        if m:
            candidate = m.group(1).strip().strip("<>.,;:()[]'\"")
            if "@" in candidate and not any(b in candidate for b in BOUNCE_SENDERS) and candidate not in own_addresses:
                return candidate
    return ""


def classify(email, own_addresses, is_our_thread):
    """What this email means, per external counterparty.

    own_addresses: the mailbox's own address(es), lower-case.
    is_our_thread(email): True when ACMI sent something earlier in this conversation.
    Returns a list of Envelope. Internal/protected filtering happens in sync.
    """
    from crm.models import Activity

    subject = email.subject.lower()
    sender_local = email.from_addr.split("@")[0]

    if email.from_addr in own_addresses:
        recipients = {}
        for name, addr in email.to + email.cc:
            if addr not in own_addresses:
                recipients.setdefault(addr, name)
        return [Envelope(addr, name, Activity.Kind.EMAIL_SENT) for addr, name in recipients.items()]

    if any(b in sender_local for b in BOUNCE_SENDERS) or any(b in subject for b in BOUNCE_SUBJECTS):
        addr = _bounced_address(email, own_addresses)
        return [Envelope(addr, "", Activity.Kind.BOUNCE, "Undeliverable")] if addr else []

    auto = email.headers.get("auto-submitted", "no").lower() not in ("", "no")
    if auto or email.headers.get("x-autoreply") or email.headers.get("x-autorespond") or any(
        o in subject for o in OOO_SUBJECTS
    ):
        return [Envelope(email.from_addr, email.from_name, Activity.Kind.AUTO_REPLY, "Out of Office Reply")]

    if email.is_reply and is_our_thread(email):
        body = new_text(email.body).lower()
        outcome = "Not Interested" if any(p in body for p in NOT_INTERESTED_PHRASES) else "Responded"
        return [Envelope(email.from_addr, email.from_name, Activity.Kind.REPLY, outcome)]

    return [Envelope(email.from_addr, email.from_name, Activity.Kind.EMAIL_RECEIVED)]
