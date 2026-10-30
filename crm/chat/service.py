"""Chat in the hub, the logic behind the pages: who is in a space, the list of conversations with unread markers, sending,
and the new-message alerts (docs/CHAT.md). Everything is one person's own Chat through their own Google permission."""
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from crm import notify, prefs
from crm.mail.gmail_api import parallel_map

from . import connect
from .api import ChatClient, ChatError
from .render import decorate, message_view, parse_time, plain, stamp, when_label

OVERVIEW_TTL = 15  # seconds the list of conversations is reused: the dock, a box and the page all ask for it
MEMBERS_TTL = 3600  # who is in a space rarely changes
READ_TTL = 30  # seconds an unread conversation's read time is reused: you may read it in Google Chat meanwhile
READ_KEEP = 600  # a read one stays read until a new message changes it, so it isn't asked about every half minute
UNREAD_DAYS = 14  # older conversations never show as unread
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
            "email": email,
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


def load(user, client, space, *, after=None, before=None, limit=50, previous=None):
    """Messages of a space as the page shows them, oldest first: (views, raw messages, directory). `previous` is the
    message just above these on the page ({"uid", "time", "reply"}), so grouping carries on across a refresh."""
    directory = members_directory(client, space)
    raw = client.messages(space, after=after, before=before, limit=limit)
    me = me_of(user)
    return decorate([message_view(m, directory, me) for m in raw], previous), raw, directory


def post(user, client, space, text, thread=""):
    """Send as the person; returns the message as the page shows it."""
    created = client.send(space, text, thread or None)
    mark_read(user, client, space)  # what you just wrote is read
    view = message_view(created, members_directory(client, space), me_of(user), mine=True)
    if view["name"] == "Someone":
        view["name"] = user.get_full_name() or user.get_username()
    return view


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
            made += bool(notify.new_chat(user, view["name"], where, plain(view["text"]) or "(an attachment)", url, view["id"]))
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
