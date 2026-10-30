"""How a Chat message looks in the hub: who said it, when, and the little formatting Chat uses (docs/CHAT.md).

Text from Chat is always escaped first; only markup this module builds is marked safe, and links are http(s) only.
"""
import html
import re
from datetime import datetime, timedelta, timezone as dt_timezone

from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.safestring import mark_safe

from .api import media_name

GROUP_GAP = timedelta(minutes=5)  # messages from the same person this close together read as one group
EDIT_GAP = timedelta(seconds=1)  # a message updated later than this after it was made has been edited
# Shown in the page itself. Anything else (a web page, an SVG that could carry script) is only ever a download.
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif"}

_LINK = re.compile(r"&lt;(https?://[^\s|&]+?)(?:\|(.+?))?&gt;")  # <url> or <url|label> after escaping
_BARE = re.compile(r"(?<![\w\"'>=/\x00])(https?://[^\s<\x00]+)")
_MENTION = re.compile(r"&lt;users/([\w.-]+)&gt;")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE = re.compile(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])")
_HOLE = re.compile(r"\x00(\d+)\x00")
_RAW_LINK = re.compile(r"<(https?://[^\s|>]+)(?:\|([^>]*))?>")
_RAW_MENTION = re.compile(r"<users/([\w.-]+)>")


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


def plain_text(text, names=None):
    """A message's words without Chat's markup (links as their words, mentions as @name, no *_~` marks): for the line a
    quote shows and for an alert."""
    names = names or {}
    out = _RAW_MENTION.sub(lambda m: "@" + ("all" if m.group(1) == "all" else names.get(f"users/{m.group(1)}") or "someone"), str(text or ""))
    out = _RAW_LINK.sub(lambda m: m.group(2) or m.group(1), out)
    for pattern in (_CODE, _BOLD, _ITALIC, _STRIKE):
        out = pattern.sub(lambda m: m.group(1), out)
    return out


def plain(text, limit=100):
    """One short line of a message, for an alert."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _icon(kind, name):
    low = name.lower()
    if kind.startswith("video/") or low.endswith((".mp4", ".mov", ".webm")):
        return "🎞"
    if kind.startswith("audio/") or low.endswith((".mp3", ".wav", ".m4a")):
        return "🎵"
    if kind == "application/pdf" or low.endswith(".pdf"):
        return "📕"
    if low.endswith((".xls", ".xlsx", ".csv")) or "spreadsheet" in kind:
        return "📊"
    if low.endswith((".zip", ".gz", ".7z", ".rar")) or "zip" in kind:
        return "🗜"
    return "📄"


def _attachments(message):
    """Files on a message: {name, type, kind (image, file, drive), icon, url (to open or download), src (to show inline)}.
    A file uploaded to Chat comes through the hub (views.attachment), which asks Chat for it as the signed-in person."""
    out = []
    for a in message.get("attachment") or []:
        name = a.get("contentName") or "Attachment"
        kind = (a.get("contentType") or "").lower()
        ref = (a.get("attachmentDataRef") or {}).get("resourceName") or ""
        drive = (a.get("driveDataRef") or {}).get("driveFileId") or ""
        item = {"name": name, "type": kind, "kind": "file", "url": "", "src": "", "icon": _icon(kind, name)}
        if drive:
            item.update(kind="drive", url=f"https://drive.google.com/file/d/{drive}/view")
        elif media_name(ref):
            query = urlencode({"r": ref, "n": name})
            item["url"] = f"{reverse('crm:chat_attachment')}?{query}&d=1"
            if kind in IMAGE_TYPES:
                item.update(kind="image", src=f"{reverse('crm:chat_attachment')}?{query}")
        else:
            url = a.get("downloadUri") or ""
            item["url"] = url if url.startswith("https://") else ""
        out.append(item)
    return out


def _reactions(message, mine=()):
    """The emoji on a message as chips: {key, label, image, count, mine, custom}. mine: the keys the person gave."""
    out = []
    for summary in message.get("emojiReactionSummaries") or []:
        emoji = summary.get("emoji") or {}
        unicode, custom = emoji.get("unicode") or "", emoji.get("customEmoji") or {}
        count = int(summary.get("reactionCount") or 0)
        if not count or not (unicode or custom):
            continue
        key = unicode or custom.get("uid") or custom.get("name") or ""
        image = custom.get("temporaryImageUri") or ""
        out.append({"key": key, "label": unicode or ":" + (custom.get("emojiName") or "emoji").strip(":") + ":",
                    "image": image if image.startswith("https://") else "", "count": count, "mine": key in mine,
                    "custom": bool(custom)})
    return out


def _quote(message, directory):
    """The message this one quotes: {id, name, text, forwarded, from_space}, or None."""
    names = {u: p.get("name", "") for u, p in directory.items()}
    q = message.get("quotedMessageMetadata") or {}
    if not q.get("name"):
        return None
    snap = q.get("quotedMessageSnapshot") or {}
    sender = snap.get("sender") or ""
    name = directory.get(sender, {}).get("name") if sender.startswith("users/") else sender
    forwarded = q.get("quoteType") == "FORWARD"
    text = (snap.get("formattedText") if forwarded else "") or snap.get("text") or ""
    return {"id": q["name"], "name": name or "Someone", "text": plain(plain_text(text, names), 220), "forwarded": forwarded,
            "from_space": (q.get("forwardedMetadata") or {}).get("spaceDisplayName", "")}


def _edited(message):
    updated, created = parse_time(message.get("lastUpdateTime")), parse_time(message.get("createTime"))
    return bool(updated and created and updated - created > EDIT_GAP and not message.get("deleteTime"))


def message_view(message, directory, me_email="", mine=None, mine_reactions=()):
    """One Chat message as the page shows it. directory: {users/ID: {"name", "email"}} for the space's members.
    mine_reactions: the reaction keys (emoji) the signed-in person has put on it."""
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
    deleted = bool(message.get("deleteTime"))
    attachments = _attachments(message)
    avatar = sender.get("avatarUrl") or known.get("avatar") or ""
    return {
        "id": message.get("name", ""), "uid": uid, "name": name, "email": email, "mine": mine,
        "initial": name[:1].upper(), "hue": sum(map(ord, name)) % 360,  # the little round avatar
        "avatar": avatar if avatar.startswith("https://") else "",
        "time": parse_time(message.get("createTime")), "iso": message.get("createTime", ""), "html": body,
        "text": message.get("text") or "", "thread": (message.get("thread") or {}).get("name", ""),
        "reply": bool(message.get("threadReply")), "attachments": attachments, "deleted": deleted,
        "reactions": [] if deleted else _reactions(message, mine_reactions),
        "quote": None if deleted else _quote(message, directory), "edited": _edited(message),
        "raw": text,  # what the edit box starts with: Chat's own markup, mentions as <users/ID>
        "can_change": bool(mine and not deleted and message.get("name")),
        "snippet": plain(plain_text(message.get("text") or "", {u: p.get("name", "") for u, p in directory.items()}), 160)
        or (f"📎 {attachments[0]['name']}" if attachments else ""),
        "quote_time": message.get("lastUpdateTime") or message.get("createTime") or "",  # what Chat checks a quote against
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
