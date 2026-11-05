"""Showing a thread in the reading pane safely (docs/EMAIL_CLIENT.md, section 5).

Messages are read from Gmail when opened and held in the cache for a few minutes only. Each body is cleaned with
the news review's allow-list sanitizer and served on its own URL, in a sandboxed frame that can't run scripts, with
a Content-Security-Policy that blocks remote images until the person clicks Show images (so senders' tracking pixels
don't report the open). Inline images (cid:) come through the hub; attachments always download as files.
"""
import base64
import html
import re
from datetime import datetime, timezone as dt_timezone
from email.utils import getaddresses, parseaddr

from django.core.cache import cache
from django.urls import reverse

from crm.news.drafts import sanitize_html

from . import textfix
from .parse import new_text

CACHE_SECONDS = 300
INLINE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_IMG_SRC = re.compile(r'(<img\b[^>]*?\ssrc=")([^"]*)(")', re.I)


def _key(mailbox, kind, ident):
    return f"mail-{kind}:{mailbox.pk}:{ident}"


def _headers(part):
    return {h["name"].lower(): h["value"] for h in part.get("headers", [])}


def _walk(part):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk(child)


def _decode(data, part):
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)) if data else b""
    ctype = _headers(part).get("content-type", "").lower()
    charset = ctype.split("charset=", 1)[1].split(";")[0].strip().strip('"\'') if "charset=" in ctype else "utf-8"
    try:
        return textfix.repair(raw.decode(charset or "utf-8"))
    except (LookupError, UnicodeDecodeError):
        return textfix.repair(raw.decode("cp1252", errors="replace"))


def get_thread(mailbox, client, gmail_id):
    """threads.get (full), cached a few minutes; each message is cached on its own for the body frame."""
    key = _key(mailbox, "thread", gmail_id)
    data = cache.get(key)
    if data is None:
        data = client.thread(gmail_id, full=True)
        cache.set(key, data, CACHE_SECONDS)
        for m in data.get("messages", []):
            cache.set(_key(mailbox, "msg", m["id"]), m, CACHE_SECONDS)
    return data


def get_message(mailbox, client, message_id):
    key = _key(mailbox, "msg", message_id)
    data = cache.get(key)
    if data is None:
        data = client.message(message_id)
        cache.set(key, data, CACHE_SECONDS)
    return data


def attachments(msg):
    """[{name, size, mime, part}] for the files on a message (not inline images without a name)."""
    out = []
    for part in _walk(msg.get("payload") or {}):
        body = part.get("body") or {}
        if part.get("filename") and body.get("attachmentId") and part.get("partId"):
            out.append({"name": part["filename"], "size": int(body.get("size") or 0),
                        "mime": part.get("mimeType", ""), "part": part.get("partId", "")})
    return out


def find_part(msg, part_id):
    for part in _walk(msg.get("payload") or {}):
        if part.get("partId") == part_id:
            return part
    return None


def summary(msg):
    """What the reading pane shows for one message, apart from its body."""
    h = _headers(msg.get("payload") or {})
    name, addr = parseaddr(h.get("from", ""))
    when = None
    if msg.get("internalDate"):
        when = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=dt_timezone.utc)
    return {
        "id": msg["id"],
        "from_name": name or addr, "from_addr": addr.lower(),
        "to": [n or a for n, a in getaddresses([h.get("to", "")]) if a],
        "cc": [n or a for n, a in getaddresses([h.get("cc", "")]) if a],
        # (name, address) of each, so a name can open who they are (crm/static/crm/who.js)
        "to_people": [(n or a, a.lower()) for n, a in getaddresses([h.get("to", "")]) if a],
        "cc_people": [(n or a, a.lower()) for n, a in getaddresses([h.get("cc", "")]) if a],
        "subject": h.get("subject", ""),
        "date": when,
        "snippet": html.unescape(msg.get("snippet", "")),
        "unread": "UNREAD" in (msg.get("labelIds") or []),
        "draft": "DRAFT" in (msg.get("labelIds") or []),
        "attachments": attachments(msg),
    }


def thread_view(mailbox, client, gmail_id):
    """(subject, messages newest first) for the reading pane. The newest message, and any unread one, open."""
    data = get_thread(mailbox, client, gmail_id)
    msgs = [summary(m) for m in data.get("messages", [])]
    subject = next((m["subject"] for m in msgs if m["subject"]), "(no subject)")
    msgs.reverse()
    for i, m in enumerate(msgs):
        m["open"] = i == 0 or m["unread"]
    return subject, msgs


def body_html(mailbox, msg, show_images=False):
    """(safe HTML for the frame, how many remote images were held back)."""
    payload = msg.get("payload") or {}
    plain = rich = None
    cids = {}
    for part in _walk(payload):
        mime, body = part.get("mimeType", ""), part.get("body") or {}
        cid = _headers(part).get("content-id", "").strip("<> ")
        if cid and part.get("partId") and mime in INLINE_IMAGE_TYPES:
            cids[cid] = part["partId"]
        if part.get("filename") or not body.get("data"):
            continue
        if mime == "text/html" and rich is None:
            rich = _decode(body["data"], part)
        elif mime == "text/plain" and plain is None:
            plain = _decode(body["data"], part)
    if rich is None:
        text = plain or msg.get("snippet", "")
        cut = len(new_text(text).rstrip())
        if cut < len(text.rstrip()) and len(text[:cut].strip()) >= MIN_NEW_TEXT:
            return (f'<div class="plain">{html.escape(text[:cut].rstrip())}</div>'
                    f'{QUOTE_OPEN}<div class="plain">{html.escape(text[cut:].strip())}</div></details>'), 0
        return f'<div class="plain">{html.escape(text)}</div>', 0
    clean = fold_quotes(sanitize_html(rich))
    blocked = 0

    def fix(m):
        nonlocal blocked
        src = html.unescape(m.group(2))
        if src.lower().startswith("cid:"):
            part = cids.get(src[4:])
            if part:
                url = reverse("crm:inbox_part", args=[msg["id"], part]) + "?inline=1"
                return f"{m.group(1)}{html.escape(url)}{m.group(3)}"
            return f'{m.group(1)}{m.group(3)}'
        if src.lower().startswith(("http:", "https:", "//")) and not show_images:
            blocked += 1
            return f'{m.group(1)}{m.group(3)} data-blocked="1"'
        return m.group(0)

    return _IMG_SRC.sub(fix, clean), blocked


# Where the quoted history starts in a reply: Gmail, Apple Mail, Outlook, Thunderbird.
_QUOTE_START = re.compile(
    r'<div[^>]*\sclass="[^"]*\b(?:gmail_quote|OutlookMessageHeader|moz-cite-prefix)|<blockquote[^>]*\stype="cite"'
    r'|<hr[^>]*>\s*(?:<div[^>]*>\s*)*(?:<p[^>]*>\s*)?(?:<font[^>]*>\s*)?(?:<span[^>]*>\s*)?<b>\s*From:'
    r'|<b>\s*From:\s*</b>|(?:<(?:p|div|span)[^>]*>\s*)*-{3,}\s*Original Message\s*-{3,}'
    r'|<div[^>]*>\s*On [^<]{4,200} wrote:', re.I)
QUOTE_OPEN = '<details class="quoted"><summary>Show quoted text</summary>'
MIN_NEW_TEXT = 2  # characters: a reply that is only the quote (a bare forward) stays open


def fold_quotes(clean):
    """Only the new words: everything from where the quoted history starts goes behind Show quoted text."""
    m = _QUOTE_START.search(clean)
    if not m:
        return clean
    head = re.sub(r"<[^>]+>|&nbsp;|\s", "", clean[:m.start()])
    if len(head) < MIN_NEW_TEXT:
        return clean
    return clean[:m.start()] + QUOTE_OPEN + clean[m.start():] + "</details>"


FRAME_CSS = """body{margin:0;padding:4px 2px;font:14px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1d2330;
word-wrap:break-word;overflow-wrap:anywhere}img{max-width:100%;height:auto}img[data-blocked]{display:inline-block;
min-width:24px;min-height:16px;background:#eef0f3}table{max-width:100%}.plain{white-space:pre-wrap}
blockquote{margin:0 0 0 8px;padding-left:8px;border-left:2px solid #e3e6eb;color:#555}a{color:#1f3a5f}
details.quoted{margin-top:8px}details.quoted>summary{cursor:pointer;color:#6b7280;font-size:12px;list-style:none;
display:inline-block;padding:1px 8px;border:1px solid #e3e6eb;border-radius:9px}details.quoted[open]>summary{margin-bottom:6px}
mark{background:#fde68a;color:inherit}mark.current{background:#f59e0b}"""


def frame_document(body):
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><base target=\"_blank\">"
            f"<style>{FRAME_CSS}</style></head><body>{body}</body></html>")


def frame_policy(show_images):
    images = "img-src 'self' data: https: http:" if show_images else "img-src 'self' data:"
    return (f"default-src 'none'; {images}; style-src 'unsafe-inline'; font-src 'none'; form-action 'none'; "
            "base-uri 'none'; frame-ancestors 'self'; sandbox allow-same-origin allow-popups "
            "allow-popups-to-escape-sandbox")


def thread_texts(mailbox, client, gmail_id):
    """({message id: "sender body" lowercased} for Find in this thread, [Message-IDs] for team notes), each message
    parsed once and kept as long as the thread is."""
    from .gmail_api import parse_api_message

    key = _key(mailbox, "texts", gmail_id)
    found = cache.get(key)
    if found is None:
        texts, ids = {}, []
        for raw in get_thread(mailbox, client, gmail_id).get("messages", []):
            email = parse_api_message(raw)
            texts[raw["id"]] = f"{email.from_name} {email.body}".lower() if email else ""
            if email and email.message_id and not email.message_id.startswith("<uid-"):
                ids.append(email.message_id)
        found = (texts, ids)
        cache.set(key, found, CACHE_SECONDS)
    return found


def forget(mailbox, gmail_id):
    """Drop a thread from the cache after it changed (read, labels, Trash), so the pane shows it as it is now."""
    data = cache.get(_key(mailbox, "thread", gmail_id))
    cache.delete(_key(mailbox, "thread", gmail_id))
    cache.delete(_key(mailbox, "texts", gmail_id))
    for m in (data or {}).get("messages", []):
        cache.delete(_key(mailbox, "msg", m["id"]))
