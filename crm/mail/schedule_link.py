"""The "Schedule" button on an email thread (Kellen, 2026-11-05): when someone settles on a time for a meeting, offer to open the
scheduler with everyone on the thread plugged in and the time ready to confirm.

`suggest()` reads the newest few messages other people wrote (their new words, not the quoted thread under them) with
`meeting_hint.find`, collects everyone on the whole chain (From, To, Cc, minus the person's own address and automated
senders), adds any teammate the message names ("include Simon"), and builds the scheduler link (crm/schedule/views.py
`_prefill`: ?with= ?title= ?when= ?start=). Nothing is sent and nothing is booked: the scheduler shows the time for the
person to confirm. An agreed time that has already passed is not offered."""
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from crm.permissions import team_users

from . import meeting_hint, render as mail_render
from .gmail_api import parse_api_message

LOOK_BACK = 3  # the newest messages from other people that are read for a time
AUTOMATED = re.compile(r"(^|[._-])(no-?reply|donotreply|notifications?|mailer-daemon|postmaster|bounce[s]?|calendar-notification|"
                       r"invitations?)([._-]|@)", re.I)
RE_REPLY = re.compile(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)+", re.I)
MAX_PEOPLE = 20
CACHE_SECONDS = 300


def _people(msgs, own):
    """[(name, address)] everyone on the chain, in the order they first appear, without the person's own address."""
    seen, out = {own.lower()}, []
    for m in reversed(msgs):  # oldest first
        pool = [(m.get("from_name", ""), m.get("from_addr", ""))] + list(m.get("to_people", [])) + list(m.get("cc_people", []))
        for name, addr in pool:
            addr = (addr or "").strip().lower()
            if not addr or "@" not in addr or addr in seen or AUTOMATED.search(addr):
                continue
            seen.add(addr)
            out.append((name if name and name.lower() != addr else "", addr))
    return out[:MAX_PEOPLE]


def _teammates_named(names, me):
    """Teammates the message asks to include ("include Simon", "add Simon Shewmaker"), as [(name, email)]. `names` are the
    strings meeting_hint.find returns: a first name, or a first and last."""
    wanted = []
    for n in names or []:
        first, _, last = n.partition(" ")
        wanted.append((first.lower(), last.strip().lower()))
    if not wanted:
        return []
    out = []
    for u in team_users().exclude(pk=me.pk).exclude(email=""):
        first, last = (u.first_name or "").lower(), (u.last_name or "").lower()
        if any(f == first and (not l or l == last) for f, l in wanted):
            out.append((u.get_full_name() or u.username, u.email.lower()))
    return out


def _week_start(day):
    return day - timedelta(days=day.weekday())


def suggest(mailbox, client, gmail_id, msgs, user, now=None):
    """The Schedule button for this thread, or None: {"kind", "label", "url", "people": [names], "note"}."""
    key = f"mail:schedhint:{mailbox.pk}:{gmail_id}:{user.pk}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    now = now or timezone.now()
    own = mailbox.email.lower()
    best, said_by = None, None
    raws = list(mail_render.get_thread(mailbox, client, gmail_id).get("messages", []))
    looked = 0
    for raw in reversed(raws):  # newest first
        email = parse_api_message(raw)
        if email is None or (email.from_addr or "").lower() == own or "DRAFT" in (raw.get("labelIds") or []):
            continue
        looked += 1
        if looked > LOOK_BACK:
            break
        sent = timezone.localtime(email.occurred_at) if email.occurred_at and timezone.is_aware(email.occurred_at) \
            else (email.occurred_at or timezone.localtime(now))
        from .parse import new_text

        text = new_text(email.body or "")
        hint = meeting_hint.find(text, sent)
        if hint is None:
            continue
        if hint["kind"] == "agreed" and timezone.make_aware(datetime.combine(hint["date"], hint["time"])) < now:
            continue  # the agreed time has gone by
        best, said_by = (hint, text), email
        break
    if best is None:
        cache.set(key, False, CACHE_SECONDS)
        return None
    hint, text = best
    people = _people(msgs, own)
    for name, addr in _teammates_named(hint.get("names", []), user):
        if addr not in {a for _, a in people}:
            people.append((name, addr))
    params = {"with": ",".join(a for _, a in people), "title": RE_REPLY.sub("", msgs[0].get("subject", "")).strip()[:200]}
    if hint.get("date"):
        params["start"] = _week_start(hint["date"]).isoformat()
    if hint["kind"] == "agreed":
        params["when"] = f"{hint['date'].isoformat()}T{hint['time']:%H:%M}"
    params["from"] = "email"
    out = {
        "kind": hint["kind"], "label": hint.get("label", ""), "people": [n or a.split("@")[0] for n, a in people],
        "by": said_by.from_name or said_by.from_addr, "url": reverse("crm:schedule") + "?" + urlencode(params),
    }
    cache.set(key, out, CACHE_SECONDS)
    return out
