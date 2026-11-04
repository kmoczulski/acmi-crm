"""Chat in the hub, the logic behind the pages: who is in a space, the list of conversations with unread markers, sending,
and the new-message alerts (docs/CHAT.md). Everything is one person's own Chat through their own Google permission."""
import hashlib
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.safestring import mark_safe

from crm import notify, prefs
from crm.auth import email_allowed
from crm.mail.gmail_api import parallel_map
from crm.permissions import team_users

from . import connect
from .api import BadRequest, ChatClient, ChatError, emoji_ok, message_name
from .render import decorate, message_view, parse_time, plain, plain_text, stamp, when_label

OVERVIEW_TTL = 15  # seconds the list of conversations is reused: the dock, a box and the page all ask for it
MEMBERS_TTL = 3600  # who is in a space rarely changes
READ_TTL = 30  # seconds an unread conversation's read time is reused: you may read it in Google Chat meanwhile
READ_KEEP = 600  # a read one stays read until a new message changes it, so it isn't asked about every half minute
UNREAD_DAYS = 14  # older conversations never show as unread
MINE_TTL = 60  # seconds the emoji a person gave on a message are reused
TAIL = 40  # how many of the newest messages the page re-reads to catch reactions, edits and deletions by others
MAX_FILES = 10  # on one message
MAX_TEXT = 4000
WORKERS = 6
EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
ICONS = {"SPACE": "#", "GROUP_CHAT": "👥", "DIRECT_MESSAGE": "👤"}

log = logging.getLogger(__name__)


def client_for(user, session=None):
    return ChatClient(user, session=session)


def me_of(user):
    conn = connect.connection(user)
    return ((conn.google_email if conn and conn.google_email else user.email) or "").lower()


def members_directory(client, space):
    """{users/ID: {"name", "email"}} for the people in a space; kept an hour."""
    key = f"chat:members:{client.owner}:{space}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    directory = {}
    for membership in client.members(space):
        person = membership.get("member") or {}
        if not person.get("name") or person.get("type") == "BOT":
            continue
        email = (person.get("email") or "").lower()
        directory[person["name"]] = {
            "name": person.get("displayName") or (email.split("@")[0].replace(".", " ").title() if email else "Someone"),
            "email": email, "avatar": person.get("avatarUrl") or "",
        }
    cache.set(key, directory, MEMBERS_TTL)
    return directory


def _title(kind, display, directory, me, my_name):
    """What to call a conversation: a space has its name; a direct message or group chat is named for the others."""
    if kind == "SPACE":
        return display or "Unnamed space"
    others = [p["name"] for p in directory.values() if not (p["email"] and p["email"] == me) and p["name"] != my_name]
    if kind == "DIRECT_MESSAGE":
        return others[0] if others else (display or "Direct message")
    shown = ", ".join(others[:3]) + (f" +{len(others) - 3}" if len(others) > 3 else "")
    return display or shown or "Group chat"


def _row(space):
    name = space.get("name", "")
    kind = space.get("spaceType") or ("DIRECT_MESSAGE" if space.get("type") == "DM" else "SPACE")
    last = space.get("lastActiveTime") or ""
    return {"name": name, "id": name.split("/", 1)[-1], "type": kind, "display": space.get("displayName", ""),
            "uri": space.get("spaceUri", ""), "icon": ICONS.get(kind, "#"), "last_raw": last,
            "last_active": parse_time(last), "unread": False, "muted": False, "title": ""}


def _last_read(client, row):
    """When the person last read a conversation (a datetime), None if Chat has no read time for it, False if unknown."""
    key = f"chat:read:{client.owner}:{row['name']}"
    cached = cache.get(key)
    if cached is not None and cached[0] == row["last_raw"]:
        return cached[1]
    try:
        state = client.read_state(row["name"])
    except ChatError:
        return False
    when = parse_time(state.get("lastReadTime"))
    covered = bool(when and row["last_active"] and when >= row["last_active"])
    cache.set(key, (row["last_raw"], when), READ_KEEP if covered else READ_TTL)
    return when


def invalidate(user):
    cache.delete(f"chat:overview:{user.pk}")


def overview(user, client=None, force=False):
    """The person's conversations, newest first, each named, with unread and muted marks:
    {"rows": [...], "unread": how many unmuted ones are unread, "me": their address, "at": when it was read}."""
    key = f"chat:overview:{user.pk}"
    if not force:
        cached = cache.get(key)
        if cached is not None:
            return cached
    client = client or client_for(user)
    conn = connect.connection(user)
    me, my_name = me_of(user), user.get_full_name()
    rows = [_row(s) for s in client.spaces()]
    client.prime()  # one token refresh here, not in every worker thread

    def directory_of(row):
        try:
            return members_directory(client, row["name"])
        except ChatError:
            return {}

    unnamed = [r for r in rows if r["type"] != "SPACE"]
    for row, directory in zip(unnamed, parallel_map(directory_of, unnamed, WORKERS)):
        row["title"] = _title(row["type"], row["display"], directory, me, my_name)
    for row in rows:
        row["title"] = row["title"] or _title(row["type"], row["display"], {}, me, my_name)
    now = timezone.now()
    if conn is None or conn.can_read_state:
        recent = [r for r in rows if r["last_active"] and now - r["last_active"] <= timedelta(days=UNREAD_DAYS)]
        for row, when in zip(recent, parallel_map(lambda r: _last_read(client, r), recent, WORKERS)):
            if when is False:
                continue  # Chat wouldn't say: no marker rather than a wrong one
            row["unread"] = row["last_active"] > when if when else now - row["last_active"] <= timedelta(days=1)
    muted = set(prefs.get_prefs(user).get("chat_muted") or [])
    for row in rows:
        row["muted"] = row["id"] in muted
        row["when"] = when_label(row["last_active"])
    rows.sort(key=lambda r: r["last_active"] or EPOCH, reverse=True)
    result = {"rows": rows, "unread": sum(1 for r in rows if r["unread"] and not r["muted"]), "me": me, "at": now}
    cache.set(key, result, OVERVIEW_TTL)
    return result


def find(user, client, space):
    """The overview row for a space, or one read straight from Chat when the list doesn't have it yet."""
    for row in overview(user, client)["rows"]:
        if row["name"] == space:
            return row
    row = _row(client.get_space(space))
    row["title"] = _title(row["type"], row["display"], members_directory(client, space), me_of(user), user.get_full_name())
    return row


def my_id(directory, me):
    """The person's own users/ID in a space, from the member list ("" when Chat doesn't give addresses)."""
    return next((uid for uid, p in directory.items() if me and p["email"] == me), "")


def _mine_key(client, message):
    return f"chat:mine:{client.owner}:{message}"


def mine_reactions(user, client, raw, directory):
    """{message name: {emoji}} the person gave, for the messages that have reactions (one question each, kept a minute)."""
    me = my_id(directory, me_of(user))
    targets = [m["name"] for m in raw if m.get("emojiReactionSummaries") and m.get("name") and not m.get("deleteTime")]
    if not me or not targets:
        return {}
    out, missing = {}, []
    for name in targets:
        cached = cache.get(_mine_key(client, name))
        if cached is None:
            missing.append(name)
        else:
            out[name] = cached
    if missing:
        client.prime()

        def ask(name):
            try:
                found = {r["emoji"]["unicode"] for r in client.reactions(name, user=me) if (r.get("emoji") or {}).get("unicode")}
            except ChatError:
                return None  # not knowing is better than a wrong highlight
            return found

        for name, found in zip(missing, parallel_map(ask, missing, WORKERS)):
            if found is not None:
                out[name] = found
                cache.set(_mine_key(client, name), found, MINE_TTL)
    return out


def load(user, client, space, *, after=None, before=None, limit=50, previous=None, deleted=True):
    """Messages of a space as the page shows them, oldest first: (views, raw messages, directory). `previous` is the
    message just above these on the page ({"uid", "time", "reply"}), so grouping carries on across a refresh. Deleted
    ones come as "message deleted" lines, as in Chat."""
    directory = members_directory(client, space)
    raw = client.messages(space, after=after, before=before, limit=limit, deleted=deleted)
    me = me_of(user)
    mine = mine_reactions(user, client, raw, directory)
    return decorate([message_view(m, directory, me, mine_reactions=mine.get(m.get("name"), ())) for m in raw], previous), raw, directory


def _quote_meta(client, space, quote, directory):
    """What Chat wants to quote a message, from the message as it is now ({name, lastUpdateTime}), and the quote in words
    for when Chat won't take its own kind: (meta, words)."""
    name = message_name(quote)
    if not name or not name.startswith(space + "/messages/"):
        raise BadRequest("That message isn't in this conversation.")
    current = client.get_message(name)
    if current.get("deleteTime"):
        raise BadRequest("That message was deleted, so it can't be quoted.")
    sender = current.get("sender") or {}
    who = sender.get("displayName") or directory.get(sender.get("name", ""), {}).get("name") or "Someone"
    snippet = plain(plain_text(current.get("text") or "", {u: p["name"] for u, p in directory.items()}), 140) or "(attachment)"
    meta = {"name": name, "lastUpdateTime": current.get("lastUpdateTime") or current.get("createTime") or ""}
    return meta, f"↪ {who}: “{snippet}”\n\n"


def post(user, client, space, text, thread="", quote="", attachments=()):
    """Send as the person; returns the message as the page shows it. quote: the name of a message in this space to
    quote; attachments: upload tokens (see upload). A quote Chat won't take (it may not for a person's own account) goes
    as words in the text instead, so the reply still says what it answers."""
    directory = members_directory(client, space)
    tokens = list(attachments)[:MAX_FILES]
    meta = words = None
    if quote:
        meta, words = _quote_meta(client, space, quote, directory)
    try:
        created = client.send(space, text, thread or None, quote=meta, attachments=tokens)
    except ChatError as exc:
        if not meta or exc.status not in (400, 403):
            raise
        created, meta = client.send(space, words + text, thread or None, attachments=tokens), None
    if meta and not created.get("quotedMessageMetadata"):
        # Chat took the message and left the quote off: say it in words on the message itself
        try:
            created = client.update_message(created["name"], words + text)
        except ChatError:
            log.warning("Couldn't add the quote in words to %s", created.get("name"), exc_info=True)
    if (tokens or quote) and created.get("name"):
        try:
            created = client.get_message(created["name"])  # the full message: its files and quote as Chat holds them
        except ChatError:
            pass
    mark_read(user, client, space)  # what you just wrote is read
    view = message_view(created, directory, me_of(user), mine=True)
    if view["name"] == "Someone":
        view["name"] = user.get_full_name() or user.get_username()
    return view


def _message_in(space, message):
    name = message_name(message)
    if not name or not name.startswith(space + "/messages/"):
        raise BadRequest("That message isn't in this conversation.")
    return name


def _own(client, user, space, message):
    """(name, the message as Chat holds it, the member list) for a message that is the person's own; a clear refusal when it
    isn't (Chat's own would read as a lost permission)."""
    name = _message_in(space, message)
    directory = members_directory(client, space)
    current = client.get_message(name)
    sender = current.get("sender") or {}
    uid, me = sender.get("name", ""), me_of(user)
    email = (sender.get("email") or directory.get(uid, {}).get("email") or "").lower()
    if not ((email and email == me) or (uid and uid == my_id(directory, me))):
        raise BadRequest("You can only change your own messages.")
    if current.get("deleteTime"):
        raise BadRequest("That message was already deleted.")
    return name, current, directory


def edit(user, client, space, message, text):
    """Change one of the person's own messages; returns it as the page shows it."""
    name, current, directory = _own(client, user, space, message)
    updated = client.update_message(name, text)
    current = {**current, **{k: v for k, v in updated.items() if k not in ("formattedText", "emojiReactionSummaries")}}
    current["formattedText"] = text  # Chat's own markup, as sent: what a second edit starts from
    mine = mine_reactions(user, client, [current], directory).get(name, ())
    return message_view(current, directory, me_of(user), mine=True, mine_reactions=mine)


def remove(user, client, space, message, force=False):
    name, _, _ = _own(client, user, space, message)
    client.delete_message(name, force=force)
    cache.delete(_mine_key(client, name))
    return True


def _reaction_chips(user, client, space, name):
    """The message's emoji as chips (what Chat holds now) with the ones the person gave marked."""
    directory = members_directory(client, space)
    current = client.get_message(name)
    mine = mine_reactions(user, client, [current], directory).get(name, ())
    return message_view(current, directory, me_of(user), mine_reactions=mine)["reactions"]


def toggle_reaction(user, client, space, message, emoji):
    """Put the emoji on the message, or take the person's own away if it is already there. Returns {on, reactions}."""
    name = _message_in(space, message)
    if not emoji_ok(emoji):
        raise BadRequest("That isn't an emoji.")
    directory = members_directory(client, space)
    me = my_id(directory, me_of(user))
    ours = [r for r in client.reactions(name, emoji=emoji, user=me or None)] if me else []
    ours = [r for r in ours if not me or (r.get("user") or {}).get("name") == me]
    if ours:
        client.unreact(ours[0]["name"])
        on = False
    else:
        try:
            made = client.react(name, emoji)
            on = True
            me = me or (made.get("user") or {}).get("name", "")
        except ChatError as exc:
            if exc.status not in (400, 409) or "exist" not in str(exc).lower():
                raise
            same = [r for r in client.reactions(name, emoji=emoji) if (r.get("user") or {}).get("name") == me] if me else []
            if not same:
                raise ChatError("That reaction is already there.") from exc
            client.unreact(same[0]["name"])
            on = False
    cache.delete(_mine_key(client, name))
    return {"on": on, "reactions": _reaction_chips(user, client, space, name)}


def reactors(user, client, space, message):
    """Who gave which emoji on a message: [{"label", "names": [...]}], the person themself as "You"."""
    name = _message_in(space, message)
    directory = members_directory(client, space)
    me = my_id(directory, me_of(user))
    grouped = {}
    for r in client.reactions(name):
        emoji = r.get("emoji") or {}
        label = emoji.get("unicode") or ":" + ((emoji.get("customEmoji") or {}).get("emojiName") or "emoji").strip(":") + ":"
        who = r.get("user") or {}
        uid = who.get("name", "")
        person = "You" if me and uid == me else (directory.get(uid, {}).get("name") or who.get("displayName") or "Someone")
        grouped.setdefault(label, []).append(person)
    return [{"label": label, "names": sorted(names, key=lambda n: (n != "You", n.lower()))} for label, names in grouped.items()]


def signature(views):
    """A short fingerprint of what the page shows for these messages: when it changes, the page redraws them."""
    parts = [[v["id"], v["raw"], v["edited"], v["deleted"], [(r["key"], r["count"], r["mine"]) for r in v["reactions"]],
              [a["name"] for a in v["attachments"]], (v["quote"] or {}).get("id")] for v in views]
    return hashlib.md5(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def refresh_tail(user, client, space, known=""):
    """Re-read the newest messages for what others changed since (reactions, edits, deletions). {"changed": False, "sig"}
    when nothing differs from `known`, else the views to draw in their place."""
    views, raw, directory = load(user, client, space, limit=TAIL)
    sig = signature(views)
    return {"changed": sig != known, "sig": sig, "views": views, "raw": raw, "directory": directory}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def upload(user, client, space, filename, content_type, data):
    """Put a file in a space ready for a message; returns {"token", "name"}."""
    ref = client.upload(space, filename, content_type, data)
    token = ref.get("attachmentUploadToken") or ""
    if not token:
        raise ChatError("Google Chat didn't take that file.")
    return {"token": token, "name": filename}


# ---------------------------------------------------------------------------
# Starting conversations, finding messages
# ---------------------------------------------------------------------------


def teammates(user):
    """Teammates to start a chat with: [{"email", "name"}], not the person themself."""
    me = (user.email or "").lower()
    rows = [{"email": u.email.lower(), "name": u.get_full_name().strip() or u.get_username()}
            for u in team_users().exclude(email="") if email_allowed(u.email) and u.email.lower() != me]
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def clean_people(values, me=""):
    """Addresses (a list or comma / space separated text) checked, lower-cased, once each, without the person themself."""
    if isinstance(values, str):
        values = re.split(r"[,;\s]+", values)
    out = []
    for value in values or []:
        value = (value or "").strip().strip("<>").lower()
        if not value or value == me or value in out:
            continue
        try:
            validate_email(value)
        except ValidationError:
            raise BadRequest(f"“{value[:60]}” isn't an email address.") from None
        out.append(value)
    if len(out) > 49:
        raise BadRequest("A conversation can start with up to 49 other people.")
    return out


def start_chat(user, client, people, name="", allowed=False):
    """Open a conversation with these people, making it if it isn't there: one person is a direct message (found, or made),
    several are a group chat, and a name makes it a space. Returns the overview row. `allowed`: the person has given the
    hub the permission to create conversations; finding one that already exists never needs it."""
    emails = clean_people(people, me_of(user))
    name = " ".join((name or "").split())[:128]
    if not emails and not name:
        raise BadRequest("Pick at least one person.")
    if name:
        kind = "SPACE"
    elif len(emails) == 1:
        kind = "DIRECT_MESSAGE"
    else:
        kind = "GROUP_CHAT"
    space = client.find_dm(emails[0]) if kind == "DIRECT_MESSAGE" else None
    if space is None:
        if not allowed:
            raise NeedsPermission("Allow starting chats first.")
        space = client.set_up(kind, emails, name)
    invalidate(user)
    return find(user, client, space["name"])


class NeedsPermission(BadRequest):
    """The hub isn't allowed to start conversations for this person yet."""


_WORD = re.compile(r'"[^"]*"|\S+')


def search_query(text):
    """Chat's search words from what was typed: each word kept plain, anything that could be read as a filter dropped."""
    words = []
    for token in _WORD.findall(text or ""):
        quoted = token.startswith('"') and token.endswith('"') and len(token) > 1
        clean = re.sub(r'["\\():<>=]', " ", token).strip()
        if clean:
            words.append(f'"{clean}"' if quoted or " " in clean else clean)
    return " ".join(words)[:900]


def _highlight(text, words):
    """The start of a message as safe HTML with the searched words marked."""
    shown = html.escape(plain(text, 220), quote=False)
    for word in sorted({w.strip('"') for w in words if len(w.strip('"')) > 1}, key=len, reverse=True):
        shown = re.sub(f"({re.escape(html.escape(word, quote=False))})", r"<mark>\1</mark>", shown, flags=re.I)
    return mark_safe(shown)


def search(user, client, text, page_token=None):
    """Messages that match some words, newest first: {"results": [...], "next": token}. Each result says where it is."""
    query = search_query(text)
    if len(query) < 2:
        raise BadRequest("Type a word or two to search for.")
    answer = client.search(query, limit=25, page_token=page_token)
    rows = {r["name"]: r for r in overview(user, client)["rows"]}
    me = me_of(user)
    words = [w for w in re.split(r"\s+", query) if w]
    results = []
    for item in answer.get("results") or []:
        message = item.get("message") or {}
        name = message.get("name", "")
        if not message_name(name):
            continue
        space = name.split("/messages/")[0]
        row = rows.get(space) or {}
        sender = message.get("sender") or {}
        who = sender.get("displayName") or "Someone"
        mine = bool(me and (sender.get("email") or "").lower() == me)
        results.append({
            "space": space.split("/", 1)[1], "title": row.get("title") or "A conversation", "icon": row.get("icon") or "#",
            "who": "You" if mine else who, "time": parse_time(message.get("createTime")), "iso": message.get("createTime", ""),
            "html": _highlight(plain_text(message.get("text") or ""), words) or mark_safe("<em>(an attachment)</em>"), "id": name,
        })
    return {"results": results, "next": answer.get("nextPageToken", "")}


def mark_read(user, client, space):
    try:
        client.mark_read(space, stamp(timezone.now()))
    except ChatError:
        return False
    cache.delete(f"chat:read:{client.owner}:{space}")
    invalidate(user)
    return True


def toggle_mute(user, space_id):
    """Alerts for a conversation off / on. Returns True when it is now muted."""
    muted = list(prefs.get_prefs(user).get("chat_muted") or [])
    if space_id in muted:
        muted.remove(space_id)
    else:
        muted.append(space_id)
    prefs.set_pref(user, "chat_muted", muted)
    invalidate(user)
    return space_id in muted


def alert(user, client=None):
    """One notification for each new message from someone else in a conversation that isn't muted, since the last time
    this ran (never from before Chat was turned on). Returns how many were made."""
    conn = connect.connection(user)
    if not (conn and conn.enabled):
        return 0
    client = client or client_for(user)
    ov = overview(user, client)
    through = conn.alerted_through or conn.turned_on_at or timezone.now()
    newest, made = through, 0
    for row in ov["rows"]:
        if row["muted"] or not row["unread"] or not row["last_active"] or row["last_active"] <= through:
            continue
        directory = members_directory(client, row["name"])
        for m in client.messages(row["name"], after=stamp(through), limit=10):
            view = message_view(m, directory, ov["me"])
            if view["mine"] or view["deleted"] or not view["time"] or view["time"] <= through:
                continue
            where = row["title"] if row["type"] != "DIRECT_MESSAGE" else ""
            url = f"{reverse('crm:chat')}?{urlencode({'s': row['id']})}"
            made += bool(notify.new_chat(user, view["name"], where, plain(plain_text(view["text"])) or "(an attachment)", url, view["id"]))
        newest = max(newest, row["last_active"])
    if newest > through:
        conn.alerted_through = newest
        conn.save(update_fields=["alerted_through"])
    return made


def sweep(user):
    """For the scheduled sweep (notify.sweep_everyone): alerts for someone whose Chat is on, whatever goes wrong
    at Google; a person's Chat trouble never stops anybody else's reminders."""
    try:
        if connect.is_on(user):
            alert(user)
    except Exception:  # noqa: BLE001
        log.warning("Chat alerts failed for %s", user, exc_info=True)
