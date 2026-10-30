"""How a Chat message looks in the hub: who said it, when, and the little formatting Chat uses (docs/CHAT.md).

Text from Chat is always escaped first; only markup this module builds is marked safe, and links are http(s) only.
"""
import html
import re
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone
from django.utils.safestring import mark_safe

GROUP_GAP = timedelta(minutes=5)  # messages from the same person this close together read as one group

_LINK = re.compile(r"&lt;(https?://[^\s|&]+?)(?:\|(.+?))?&gt;")  # <url> or <url|label> after escaping
_BARE = re.compile(r"(?<![\w\"'>=/\x00])(https?://[^\s<\x00]+)")
_MENTION = re.compile(r"&lt;users/([\w.-]+)&gt;")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE = re.compile(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])")
_HOLE = re.compile(r"\x00(\d+)\x00")


def parse_time(value):
    """An aware UTC datetime from Chat's RFC 3339 stamp (which can carry nanoseconds), or None."""
    if not value:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", str(value)).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(dt_timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


def stamp(moment):
    """The RFC 3339 form Chat takes in a filter or a read time."""
    return moment.astimezone(dt_timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def when_label(moment):
    """A short "when" for a list row: the time today, "Yesterday", the weekday this week, else the date."""
    if not moment:
        return ""
    local, now = timezone.localtime(moment), timezone.localtime()
    days = (now.date() - local.date()).days
    if days <= 0:
        return local.strftime("%I:%M %p").lstrip("0").lower()
    if days == 1:
        return "Yesterday"
    if days < 7:
        return local.strftime("%a")
    return f"{local:%b} {local.day}"


def format_text(text, names=None):
    """Chat's text as safe HTML: links, @mentions, *bold*, _italic_, ~strike~ and `code`, line breaks kept."""
    names = names or {}
    esc = html.escape(text or "", quote=False)
    holes = []

    def keep(markup):
        holes.append(markup)
        return f"\x00{len(holes) - 1}\x00"

    def anchor(url, label):
        url = html.unescape(url)
        return keep(f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>')

    esc = _CODE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), esc)
    esc = _MENTION.sub(lambda m: keep('<span class="chat-mention">@' + html.escape(
        "all" if m.group(1) == "all" else names.get(f"users/{m.group(1)}") or "someone", quote=False) + "</span>"), esc)
    esc = _LINK.sub(lambda m: anchor(m.group(1), m.group(2) or html.escape(html.unescape(m.group(1)), quote=False)), esc)

    def bare(m):
        url, tail = m.group(1), ""
        while url and url[-1] in ".,;:!?)":
            url, tail = url[:-1], url[-1] + tail
        return anchor(url, html.escape(html.unescape(url), quote=False)) + tail

    esc = _BARE.sub(bare, esc)
    for pattern, tag in ((_BOLD, "strong"), (_ITALIC, "em"), (_STRIKE, "s")):
        esc = pattern.sub(lambda m, tag=tag: f"<{tag}>{m.group(1)}</{tag}>", esc)
    esc = esc.replace("\n", "<br>")
    for _ in range(3):  # a link inside bold or code left its own hole inside another one
        esc = _HOLE.sub(lambda m: holes[int(m.group(1))], esc)
    return mark_safe(esc)


def plain(text, limit=100):
    """One short line of a message, for an alert."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _attachments(message):
    out = []
    for a in message.get("attachment") or []:
        name = a.get("contentName") or "Attachment"
        drive = (a.get("driveDataRef") or {}).get("driveFileId")
        url = f"https://drive.google.com/file/d/{drive}/view" if drive else a.get("downloadUri") or ""
        out.append({"name": name, "url": url if url.startswith("https://") else ""})
    return out


def message_view(message, directory, me_email="", mine=None):
    """One Chat message as the page shows it. directory: {users/ID: {"name", "email"}} for the space's members."""
    sender = message.get("sender") or {}
    uid = sender.get("name", "")
    known = directory.get(uid, {})
    name = sender.get("displayName") or known.get("name") or ("A Chat app" if sender.get("type") == "BOT" else "Someone")
    email = (sender.get("email") or known.get("email") or "").lower()
    if mine is None:
        mine = bool(email and me_email and email == me_email.lower())
    text = message.get("formattedText") or message.get("text") or ""
    body = format_text(text, {u: p.get("name", "") for u, p in directory.items()})
    if not text and message.get("cardsV2"):
        body = mark_safe("<em>A card. Open it in Google Chat.</em>")
    return {
        "id": message.get("name", ""), "uid": uid, "name": name, "email": email, "mine": mine,
        "initial": name[:1].upper(), "hue": sum(map(ord, name)) % 360,  # the little round avatar
        "time": parse_time(message.get("createTime")), "iso": message.get("createTime", ""), "html": body,
        "text": message.get("text") or "", "thread": (message.get("thread") or {}).get("name", ""),
        "reply": bool(message.get("threadReply")), "attachments": _attachments(message),
        "deleted": bool(message.get("deleteTime")),
    }


def decorate(views, previous=None):
    """Mark where a new group of messages starts (a different person, or a pause) and where the day changes."""
    prev = previous
    for v in views:
        t = timezone.localtime(v["time"]) if v["time"] else None
        pt = timezone.localtime(prev["time"]) if prev and prev["time"] else None
        v["new_day"] = bool(t and (pt is None or t.date() != pt.date()))
        v["head"] = bool(
            prev is None or v["new_day"] or v["uid"] != prev["uid"] or v["reply"] != prev["reply"]
            or not (v["time"] and prev["time"]) or v["time"] - prev["time"] > GROUP_GAP
        )
        prev = v
    return views
