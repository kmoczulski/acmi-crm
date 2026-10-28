"""Changing mail from the hub (docs/EMAIL_CLIENT.md, step 4): archive, read / unread, star, labels, move, Trash and
snooze, on one thread or several, each with an Undo.

Everything is a Gmail label change on the person's own mailbox, so it shows in Gmail on the phone a moment later.
Trash is Gmail's Trash (Gmail empties it after 30 days); the hub never deletes anything permanently. Snooze is the
hub's own, since Gmail's isn't open to apps: the thread leaves the inbox under the "ACMI/Snoozed" label and the sync
brings it back, unread, when the time comes.
"""
import logging
from datetime import datetime, time, timedelta

from django.utils import timezone

from crm.models import MailLabel, MailSnooze

from . import index, render
from .gmail_api import NotFound, parallel_map

log = logging.getLogger(__name__)

SNOOZE_LABEL = "ACMI/Snoozed"
PARALLEL = 3

# action -> (labels to add, labels to remove); "{label}" is the label the person picked.
LABEL_CHANGES = {
    "archive": ((), ("INBOX",)),
    "inbox": (("INBOX",), ()),
    "read": ((), ("UNREAD",)),
    "unread": (("UNREAD",), ()),
    "star": (("STARRED",), ()),
    "unstar": ((), ("STARRED",)),
    "label": (("{label}",), ()),
    "unlabel": ((), ("{label}",)),
    "move": (("{label}",), ("INBOX",)),
    "unmove": (("INBOX",), ("{label}",)),
}
UNDO = {"archive": "inbox", "inbox": "archive", "read": "unread", "unread": "read", "star": "unstar",
        "unstar": "star", "label": "unlabel", "unlabel": "label", "move": "unmove", "unmove": "move",
        "trash": "untrash", "untrash": "trash", "snooze": "unsnooze"}
DONE = {"archive": "Archived", "inbox": "Moved to the inbox", "read": "Marked read", "unread": "Marked unread",
        "star": "Starred", "unstar": "Star removed", "label": "Label added", "unlabel": "Label removed",
        "move": "Moved", "unmove": "Moved back", "trash": "Moved to Trash", "untrash": "Restored from Trash",
        "snooze": "Snoozed", "unsnooze": "Back in the inbox"}
ACTIONS = set(DONE)
LEAVES_THE_LIST = {"archive", "move", "trash", "snooze"}  # the reading pane closes, as in Gmail


class ActionError(Exception):
    pass


def snooze_label(mailbox, client):
    """The Gmail label id for snoozed threads, made the first time someone snoozes."""
    row = MailLabel.objects.filter(mailbox=mailbox, name=SNOOZE_LABEL).first()
    if row:
        return row.gmail_id
    found = next((lb for lb in client.labels() if lb["name"] == SNOOZE_LABEL), None) or client.create_label(SNOOZE_LABEL)
    MailLabel.objects.update_or_create(mailbox=mailbox, gmail_id=found["id"], defaults={"name": SNOOZE_LABEL})
    return found["id"]


def snooze_choices(now=None):
    """[(key, label, when)] for the Snooze menu, in the person's own time zone."""
    now = timezone.localtime(now)
    at = lambda day, hour: timezone.make_aware(datetime.combine(day, time(hour)))  # noqa: E731
    today = now.date()
    out = []
    later = (now + timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    if later.date() == today and later.hour <= 20:
        out.append(("later", f"Later today ({later:%I %p})".replace("(0", "("), later))
    out.append(("tomorrow", "Tomorrow morning (8 AM)", at(today + timedelta(days=1), 8)))
    if now.weekday() < 5:
        out.append(("weekend", "This weekend (Sat 8 AM)", at(today + timedelta(days=5 - now.weekday()), 8)))
    out.append(("week", "Next week (Mon 8 AM)", at(today + timedelta(days=7 - now.weekday()), 8)))
    return out


def snooze_until(choice, custom=""):
    """The time a Snooze menu choice (or a picked date and time) means; None if it isn't a time in the future."""
    for key, _, when in snooze_choices():
        if key == choice:
            return when
    if custom:
        try:
            when = timezone.make_aware(datetime.fromisoformat(custom))
        except ValueError:
            return None
        return when if when > timezone.now() else None
    return None


def managed(name):
    """The hub's own labels (ACMI/...): the CRM labels and Snoozed, which the hub would only put back."""
    return name == "ACMI" or name.startswith("ACMI/")


def _own_label(mailbox, label_id):
    row = MailLabel.objects.filter(mailbox=mailbox, gmail_id=label_id, system=False).first()
    if row is None:
        raise ActionError("Pick one of your labels.")
    if managed(row.name):
        raise ActionError("ACMI labels are kept by the hub. Switch them off on Mail in the hub instead.")
    return row


def _family(mailbox, row):
    """The label and the ones nested under it (Gmail nests by "/" in the name)."""
    return [row] + list(MailLabel.objects.filter(mailbox=mailbox, system=False, name__startswith=row.name + "/"))


def rename_label(mailbox, client, label_id, new_name):
    """Rename a label in Gmail; the labels nested under it move with it. Returns the new name."""
    row = _own_label(mailbox, label_id)
    new_name = "/".join(part.strip() for part in (new_name or "").strip().strip("/").split("/") if part.strip())[:225]
    if not new_name:
        raise ActionError("Give the label a name.")
    if managed(new_name):
        raise ActionError("Names starting ACMI/ are kept for the hub's own labels.")
    if new_name == row.name:
        return new_name
    if MailLabel.objects.filter(mailbox=mailbox, name__iexact=new_name).exclude(pk=row.pk).exists():
        raise ActionError(f"There's already a label called {new_name}.")
    for lb in _family(mailbox, row):
        name = new_name + lb.name[len(row.name):]
        client.update_label(lb.gmail_id, name)
        MailLabel.objects.filter(pk=lb.pk).update(name=name)
    return new_name


def delete_label(mailbox, client, label_id):
    """Delete a label and the ones nested under it. Gmail takes them off their emails; no email is deleted.
    Returns how many labels went."""
    row = _own_label(mailbox, label_id)
    family = _family(mailbox, row)
    for lb in sorted(family, key=lambda x: -len(x.name)):  # the deepest first
        try:
            client.delete_label(lb.gmail_id)
        except NotFound:
            pass  # already gone in Gmail
        lb.delete()
    return len(family)


def _changes(action, label):
    add, remove = LABEL_CHANGES[action]
    fill = lambda ids: [label if i == "{label}" else i for i in ids]  # noqa: E731
    return fill(add), fill(remove)


def apply(mailbox, client, action, thread_ids, label="", until=None):
    """Do `action` to these threads in Gmail. Returns (what to say, the undo as a dict for the next request, or
    None). Raises ActionError for something the person picked that can't be done."""
    thread_ids = list(dict.fromkeys(t for t in thread_ids if t))[:100]
    if action not in ACTIONS or not thread_ids:
        raise ActionError("Pick a conversation first.")
    if "{label}" in str(LABEL_CHANGES.get(action, "")):
        if not MailLabel.objects.filter(mailbox=mailbox, gmail_id=label, system=False).exists():
            raise ActionError("Pick one of your labels.")
    snooze_id = ""
    if action == "snooze":
        if until is None:
            raise ActionError("Pick when it should come back.")
        snooze_id = snooze_label(mailbox, client)
    elif action == "unsnooze":
        snooze_id = snooze_label(mailbox, client)

    def run(tid):
        if action == "trash":
            client.trash_thread(tid)
        elif action == "untrash":
            client.untrash_thread(tid)
        elif action == "snooze":
            client.modify_thread(tid, add=[snooze_id], remove=["INBOX"])
        elif action == "unsnooze":
            client.modify_thread(tid, add=["INBOX"], remove=[snooze_id])
        else:
            client.modify_thread(tid, *_changes(action, label))
        return tid

    client.prime()
    done = parallel_map(run, thread_ids, PARALLEL)
    if action == "snooze":
        for tid in done:
            MailSnooze.objects.update_or_create(mailbox=mailbox, gmail_id=tid, defaults={"until": until})
    elif action == "unsnooze":
        MailSnooze.objects.filter(mailbox=mailbox, gmail_id__in=done).delete()
    for tid in done:
        render.forget(mailbox, tid)
    try:
        index.refresh(mailbox, client, done)
    except Exception:  # the change is made; a stale row fixes itself on the next list
        log.warning("Index refresh after %s failed for %s", action, mailbox, exc_info=True)
    n = len(done)
    text = DONE[action] + (f" {n} conversations" if n > 1 else "")
    if action == "snooze":
        text += f" until {timezone.localtime(until):%a %b %d, %I:%M %p}".replace(" 0", " ")
    undo = {"action": UNDO[action], "threads": done, "label": label} if action in UNDO else None
    return text + ".", undo


def wake_due(mailbox, client, now=None):
    """Put snoozed threads whose time has come back in the inbox, unread (the sync calls this)."""
    due = list(MailSnooze.objects.filter(mailbox=mailbox, until__lte=now or timezone.now()))
    if not due:
        return 0
    label = snooze_label(mailbox, client)
    for s in due:
        try:
            client.modify_thread(s.gmail_id, add=["INBOX", "UNREAD"], remove=[label])
        except Exception:
            log.warning("Couldn't bring snoozed thread %s back for %s", s.gmail_id, mailbox, exc_info=True)
            continue
        render.forget(mailbox, s.gmail_id)
        s.delete()
    index.refresh(mailbox, client, [s.gmail_id for s in due])
    return len(due)
