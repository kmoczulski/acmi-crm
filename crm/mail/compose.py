"""Writing email in the hub (docs/EMAIL_CLIENT.md, step 5): new messages, reply, reply all and forward, with
attachments, your signature, drafts saved to Gmail as you type, and an Undo-send countdown in the page.

Every email is sent by one person's click on Send, from their own Gmail through the Gmail API, so it lands in their
Sent folder and in the same Gmail thread. It is logged on the CRM timeline straight away through the same
process_messages the sync uses (same Message-ID key), so the sync later sees it as already logged. ACMI_MAIL_SEND=false
blocks sending entirely; it's always off while the tests run.
"""
import html
import logging
import mimetypes
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid

from django.conf import settings

from crm import prefs
from crm.news.drafts import html_to_text, sanitize_html, text_to_html

from . import render
from .gmail_api import decimal_thread_id, parse_api_message, uid_of

log = logging.getLogger(__name__)

MAX_BYTES = 25 * 1024 * 1024  # Gmail's limit for what you attach
MAX_RECIPIENTS = 100
_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_RE = re.compile(r"^\s*re\s*:", re.I)
_FWD = re.compile(r"^\s*(fwd?|fw)\s*:", re.I)


class ComposeError(Exception):
    """Something the person can fix before sending, in plain words."""


def _headers(msg):
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}


def parse_recipients(text, field="To"):
    """'Will <will@x.com>, pat@y.com' -> ['Will <will@x.com>', 'pat@y.com']; ComposeError for a bad address."""
    out = []
    for name, addr in getaddresses([text or ""]):
        if not addr and not name:
            continue
        if not _ADDRESS.match(addr or ""):
            raise ComposeError(f"“{(name or addr).strip()}” in {field} isn't an email address.")
        out.append(formataddr((name, addr)) if name else addr)
    return out


SIGNATURE_CACHE = 3600


def signature_mode(user):
    """"gmail" (the signature set in Gmail; the default), "custom" (one written on Mail in the hub) or "none"."""
    p = prefs.get_prefs(user)
    return p.get("mail_signature_mode") or ("custom" if p.get("mail_signature") else "gmail")


def gmail_signature(mailbox, client):
    """The signature the person set in Gmail (Settings › Signature), cleaned; "" if none. Read with the Gmail
    permission they already gave, cached an hour."""
    from django.core.cache import cache

    from crm.news.drafts import sanitize_html

    key = f"mail-gmail-signature:{mailbox.pk}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        found = client.get("/settings/sendAs").get("sendAs", [])
    except Exception:  # no signature is better than no email
        log.warning("Couldn't read the Gmail signature for %s", mailbox, exc_info=True)
        return ""
    own = next((a for a in found if a.get("sendAsEmail", "").lower() == mailbox.email.lower()), None) \
        or next((a for a in found if a.get("isPrimary")), {})
    value = sanitize_html(own.get("signature", "") or "")
    cache.set(key, value, SIGNATURE_CACHE)
    return value


def signature_parts(user, mailbox=None, client=None):
    """(plain, html) of the signature that goes under an email."""
    from crm.news.drafts import html_to_text

    if mailbox is not None and mailbox.shared:  # a shared mailbox signs with its own Gmail signature
        rich = gmail_signature(mailbox, client) if client is not None else ""
        return (html_to_text(rich), rich) if rich else (mailbox.label, html.escape(mailbox.label))
    mode = signature_mode(user)
    if mode == "none":
        return "", ""
    if mode == "custom":
        text = prefs.get_prefs(user).get("mail_signature", "")
        return text, html.escape(text).replace("\n", "<br>")
    rich = gmail_signature(mailbox, client) if mailbox is not None and client is not None else ""
    if rich:
        return html_to_text(rich), rich
    name = user.get_full_name() or user.username
    return name, html.escape(name)


def signature(user, mailbox=None, client=None):
    """The signature as text, for showing on a page."""
    return signature_parts(user, mailbox, client)[0]


def _without(addresses, own):
    return [(n, a) for n, a in addresses if a and a.lower() != own]


def reply_fields(mailbox, msg, mode):
    """The form for replying to (mode reply / all) or forwarding (forward) one Gmail message."""
    h = _headers(msg)
    own = mailbox.email.lower()
    sender = getaddresses([h.get("reply-to") or h.get("from", "")])
    original_to, original_cc = getaddresses([h.get("to", "")]), getaddresses([h.get("cc", "")])
    from_me = any(a.lower() == own for _, a in getaddresses([h.get("from", "")]))
    subject = h.get("subject", "")
    fmt = lambda pairs: ", ".join(formataddr((n, a)) if n else a for n, a in pairs)  # noqa: E731
    fields = {"subject": subject, "to": "", "cc": "", "thread_id": "", "in_reply_to": "", "references": "",
              "reply_to_id": msg["id"], "mode": mode}
    if mode == "forward":
        fields["subject"] = subject if _FWD.match(subject) else f"Fwd: {subject}"
        fields["forward_files"] = render.attachments(msg)
    else:
        target = _without(original_to, own) if from_me else _without(sender, own)
        cc = []
        if mode == "all":
            seen = {a.lower() for _, a in target}
            for n, a in _without(original_to + original_cc, own):
                if a.lower() not in seen:
                    seen.add(a.lower())
                    cc.append((n, a))
        fields.update({
            "subject": subject if _RE.match(subject) else f"Re: {subject}",
            "to": fmt(target), "cc": fmt(cc), "thread_id": msg.get("threadId", ""),
            "in_reply_to": h.get("message-id", ""),
            "references": " ".join(x for x in [h.get("references", ""), h.get("message-id", "")] if x).strip(),
        })
    return fields


def quoted(mailbox, msg, mode):
    """(plain, html) of the original under a reply or forward, the way Gmail quotes it."""
    h = _headers(msg)
    email = parse_api_message(msg)
    when = f"{email.occurred_at:%a, %b %d, %Y at %I:%M %p}" if email else h.get("date", "")
    who = h.get("from", "")
    body_html, _ = render.body_html(mailbox, msg, show_images=False)
    body_html = re.sub(r"<img\b[^>]*>", "", body_html)  # pictures don't travel: the hub's links wouldn't work outside
    text = email.body if email else ""
    if mode == "forward":
        head = (f"---------- Forwarded message ---------\nFrom: {who}\nDate: {when}\nSubject: {h.get('subject', '')}\n"
                f"To: {h.get('to', '')}\n")
        plain = f"\n\n{head}\n{text}"
        rich = (f'<br><div class="gmail_quote">{html.escape(head).replace(chr(10), "<br>")}<br>{body_html}</div>')
    else:
        intro = f"On {when}, {who} wrote:"
        plain = f"\n\n{intro}\n" + "\n".join(f"> {line}" for line in text.splitlines())
        rich = (f'<br><div class="gmail_quote"><div>{html.escape(intro)}</div><blockquote style="margin:0 0 0 .8ex;'
                f'border-left:1px solid #ccc;padding-left:1ex">{body_html}</blockquote></div>')
    return plain, rich


def build(mailbox, user, to, cc=(), bcc=(), subject="", body="", quote=("", ""), in_reply_to="", references="",
          files=(), sig=None, body_html=""):
    """The email as bytes and its Message-ID. files: [(name, bytes, mime type)]; sig: (plain, html), by default the
    person's chosen signature without Gmail's (which needs a Gmail client). body_html: the body as formatted in the
    editor (bold, lists, links); when it's there it is what the HTML part shows, and the plain part is made from it."""
    if not to and not cc and not bcc:
        raise ComposeError("Add at least one recipient.")
    if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
        raise ComposeError(f"That's more than {MAX_RECIPIENTS} recipients. Use the send queue for a mailing.")
    if sum(len(data) for _, data, _ in files) > MAX_BYTES:
        raise ComposeError("Attachments add up to more than 25 MB, Gmail's limit. Share a Drive link instead.")
    sig_plain, sig_html = sig if sig is not None else signature_parts(user)
    msg = EmailMessage()
    try:
        msg["From"] = formataddr(((mailbox.name if mailbox.shared else user.get_full_name()) or "", mailbox.email))
        if to:
            msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = " ".join((subject or "").split())[:500] or "(no subject)"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=mailbox.email.rsplit("@", 1)[-1])
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to
    except ValueError as exc:  # a line break in a name, an address or a threading header (headers can't hold one)
        raise ComposeError("A recipient's name or a header has a line break in it. Take it out and try again.") from exc
    plain_quote, html_quote = quote
    rich_body = formatted(body_html)
    if rich_body:
        body = plain_text(rich_body)
    plain = (body or "").rstrip() + (f"\n\n{sig_plain}" if sig_plain else "") + plain_quote
    rich = (rich_body or text_to_html(body or "")) + (f'<br><div class="gmail_signature">{sig_html}</div>' if sig_html else "") \
        + html_quote
    msg.set_content(plain)
    msg.add_alternative(f'<div dir="ltr">{rich}</div>', subtype="html")
    for name, data, mime in files:
        maintype, _, subtype = (mime or mimetypes.guess_type(name)[0] or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
    return msg.as_bytes(), msg["Message-ID"]


def formatted(body_html):
    """The editor's HTML, cleaned (only formatting tags and safe links), or "" when it has no text in it."""
    clean = sanitize_html(body_html or "")
    return clean if html_to_text(clean) else ""


def plain_text(body_html):
    """The plain-text version of the editor's HTML, numbered lists numbered."""
    def number(m):
        items = re.split(r"<\s*li\b[^>]*>", m.group(1), flags=re.I)[1:]
        items = [re.sub(r"<\s*/\s*li\s*>", "", item, flags=re.I) for item in items]
        return "".join(f"<div>{i}. {item}</div>" for i, item in enumerate(items, 1))

    return html_to_text(re.sub(r"<\s*ol\b[^>]*>(.*?)<\s*/\s*ol\s*>", number, body_html or "", flags=re.I | re.S))


def forwarded_files(mailbox, client, original, parts):
    """The original's attachments the person kept ticked on a forward, read from Gmail."""
    out = []
    for part_id in parts:
        part = render.find_part(original, part_id)
        if part and (part.get("body") or {}).get("attachmentId"):
            data = client.attachment(original["id"], part["body"]["attachmentId"])
            out.append((part.get("filename") or "attachment", data, part.get("mimeType", "")))
    return out


def send(mailbox, client, raw, thread_id="", draft_id="", user=None):
    """Send it, drop the draft, and log it on the timeline the way the sync would. Returns Gmail's answer."""
    if not settings.MAIL_SEND_ENABLED:
        raise ComposeError("Sending is switched off on this hub (ACMI_MAIL_SEND).")
    result = client.send(raw, thread_id=thread_id)
    result["message_id"] = client.sent_message_id(result)
    if draft_id:
        try:
            client.delete_draft(draft_id)
        except Exception:  # the email went; a leftover draft is only clutter
            log.warning("Couldn't drop draft %s for %s", draft_id, mailbox, exc_info=True)
    if mailbox.enabled:  # the person's mail is logged on timelines (as the sync would)
        try:
            log_sent(mailbox, raw, result, user)
        except Exception:  # the sync logs it on its next run anyway
            log.exception("Couldn't log the sent email for %s", mailbox)
    render.forget(mailbox, result.get("threadId", ""))
    return result


def log_sent(mailbox, raw, result, user=None):
    from .parse import parse_message
    from .sync import process_messages

    email = parse_message(raw, uid=uid_of(result.get("id", "0")), thread_id=decimal_thread_id(result.get("threadId", "")),
                          labels=["\\Sent"])
    if result.get("message_id"):  # what Gmail really sent it with
        email.message_id = result["message_id"][:500]
    recorder = process_messages(mailbox, [email])
    recorder.finish()
    if user is not None:  # from a shared mailbox, the timeline still says who wrote it
        from crm.models import Activity

        Activity.objects.filter(message_id=email.message_id, kind=Activity.Kind.EMAIL_SENT, actor__isnull=True) \
            .update(actor=user)
    return recorder.stats


def save_draft(mailbox, client, user, fields, draft_id=""):
    """Save what's typed so far as a Gmail draft (no attachments: those go on at Send). Returns the draft id."""
    if fields["to"] or fields["cc"] or fields["bcc"]:
        raw, _ = build(mailbox, user, fields["to"], fields["cc"], fields["bcc"], fields["subject"], fields["body"],
                       quote=fields.get("quote", ("", "")), in_reply_to=fields.get("in_reply_to", ""),
                       references=fields.get("references", ""), sig=signature_parts(user, mailbox, client),
                       body_html=fields.get("body_html", ""))
    else:
        raw = _draft_without_recipients(mailbox, user, fields)
    answer = client.save_draft(raw, draft_id=draft_id, thread_id=fields.get("thread_id", ""))
    return answer.get("id", draft_id)


def _draft_without_recipients(mailbox, user, fields):
    msg = EmailMessage()
    msg["From"] = formataddr((user.get_full_name() or "", mailbox.email))
    msg["Subject"] = " ".join((fields["subject"] or "").split())[:500]
    rich = formatted(fields.get("body_html", ""))
    msg.set_content(plain_text(rich) if rich else (fields["body"] or ""))
    if rich:
        msg.add_alternative(f'<div dir="ltr">{rich}</div>', subtype="html")
    return msg.as_bytes()
