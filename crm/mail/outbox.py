"""Undo send (Kellen, 2026-11-05: "I have to wait 10 seconds for it to send before I can do anything; there should be an
unsend button in the bottom left corner that pops up like a notification... so I can still do other tasks").

Send used to hold the message in the open page for ten seconds, so the person could do nothing else, and leaving the
page cancelled it. Now Send queues the finished message on the server (`PendingSend`) and the page is free at once; a
notification in the bottom left counts down with Undo (static/crm/outbox.js). The message goes when its time comes:

    1. the notification's own timer asks for it (`deliver`), on whichever page is open;
    2. any page's 30-second alerts poll sends what is due (`flush_due`), if that page was closed;
    3. the 15-minute mail sync sends anything still waiting, if every page was closed.

`deliver` claims a row atomically (queued -> sending), so those three can race and the message still goes once. A
failed send keeps what was typed and tells the person (a bell notification), so nothing is lost silently.
"""
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from crm import notify
from crm.models import Notification, PendingSend

from . import compose, index, shared
from .gmail_api import client_for as gmail_client_for
from .imap import MailboxError

log = logging.getLogger(__name__)

DELAY_SECONDS = 10  # the Undo window
STUCK_AFTER = timedelta(minutes=5)  # "sending" this long: the process died mid-send; don't retry, ask the person to check
KEEP_DAYS = 3  # finished rows are tidied away after this


def queue(mailbox, user, raw, *, thread_id="", draft_id="", subject="", to_text="", fields=None, back=""):
    """Hold a finished message for DELAY_SECONDS. Returns the PendingSend."""
    if not settings.MAIL_SEND_ENABLED:
        raise compose.ComposeError("Sending is switched off on this hub (ACMI_MAIL_SEND).")
    return PendingSend.objects.create(
        mailbox=mailbox, user=user, raw=raw, thread_id=thread_id or "", draft_id=draft_id or "", subject=subject[:500],
        to_text=to_text[:500], fields=fields or {}, back=back[:500],
        send_at=timezone.now() + timedelta(seconds=DELAY_SECONDS))


def as_item(p):
    """What the notification needs to draw (JSON)."""
    left = max((p.send_at - timezone.now()).total_seconds(), 0)
    return {"id": p.pk, "subject": p.subject or "(no subject)", "to": p.to_text, "seconds_left": round(left, 1),
            "status": p.status, "url": p.result_url, "error": p.error,
            "reopen": reverse("crm:inbox_compose") + f"?undo={p.pk}"}


def waiting(user):
    """The person's messages still in the Undo window or about to go."""
    return [as_item(p) for p in PendingSend.objects.filter(user=user, status=PendingSend.Status.QUEUED)
            .order_by("send_at")[:10]]


def undo(user, pk):
    """Cancel a queued message if it has not gone. Returns (ok, item). Atomic: it cannot be undone once claimed."""
    changed = PendingSend.objects.filter(pk=pk, user=user, status=PendingSend.Status.QUEUED,
                                         send_at__gt=timezone.now()).update(status=PendingSend.Status.CANCELLED, raw=b"")
    p = PendingSend.objects.filter(pk=pk, user=user).first()
    return bool(changed), (as_item(p) if p else None)


def _result_url(p, thread_id):
    if p.fields.get("mode") in ("reply", "all") and p.back.startswith("/") and not p.back.startswith("//") and "t=" in p.back:
        return p.back  # back to the thread, now with the reply in it
    return f"{reverse('crm:inbox')}?label=SENT&t={thread_id}"


def deliver(pk, user=None):
    """Send one due message. Returns the item as it stands (status sent / failed / queued when not yet due), or None
    when it is not this person's or another request already took it."""
    now = timezone.now()
    base = PendingSend.objects.filter(pk=pk)
    if user is not None:
        base = base.filter(user=user)
    claimed = base.filter(status=PendingSend.Status.QUEUED, send_at__lte=now).update(status=PendingSend.Status.SENDING)
    p = base.select_related("mailbox", "user").first()
    if p is None:
        return None
    if not claimed:
        return as_item(p)  # not due yet, or someone else is sending / has sent it
    try:
        client = gmail_client_for(p.mailbox)
        result = compose.send(p.mailbox, client, bytes(p.raw), thread_id=p.thread_id, draft_id=p.draft_id, user=p.user)
        thread = result.get("threadId", "")
        shared.log(p.mailbox, p.user, "send", thread, p.subject)
        try:
            index.refresh(p.mailbox, client, [thread])
        except Exception:  # the email went; the list catches up on its own
            log.warning("Index refresh after sending failed for %s", p.mailbox, exc_info=True)
        PendingSend.objects.filter(pk=p.pk).update(status=PendingSend.Status.SENT, raw=b"", error="",
                                                   result_url=_result_url(p, thread))
    except (compose.ComposeError, MailboxError, requests.RequestException) as exc:
        text = (str(exc) if isinstance(exc, (compose.ComposeError, MailboxError))
                else "Couldn't reach Gmail.") or "Gmail refused it."
        _failed(p, text)
    except Exception as exc:  # noqa: BLE001 - never lose a message silently
        log.exception("Sending %s failed", p.pk)
        _failed(p, "Something went wrong sending it.")
    return as_item(PendingSend.objects.get(pk=p.pk))


def _failed(p, text):
    PendingSend.objects.filter(pk=p.pk).update(status=PendingSend.Status.FAILED, error=text[:500])
    notify.send(p.user, Notification.Kind.MAIL,
                f"Couldn't send “{p.subject or '(no subject)'}”: {text} Open it to try again.",
                url=reverse("crm:inbox_compose") + f"?undo={p.pk}", key=f"send-failed:{p.pk}")


def flush_due(user=None, limit=20):
    """Send everything whose time has come (this person's, or everyone's from the sync). Cheap when nothing waits.
    Also gives up on a row stuck 'sending' (the process died), telling the person to check Sent rather than risk a
    second copy."""
    now = timezone.now()
    stuck = PendingSend.objects.filter(status=PendingSend.Status.SENDING, updated_at__lt=now - STUCK_AFTER)
    if user is not None:
        stuck = stuck.filter(user=user)
    for p in list(stuck.select_related("user")[:limit]):
        _failed(p, "It may or may not have gone: check your Sent folder before sending again.")
    due = PendingSend.objects.filter(status=PendingSend.Status.QUEUED, send_at__lte=now)
    if user is not None:
        due = due.filter(user=user)
    n = 0
    for pk in list(due.order_by("send_at").values_list("pk", flat=True)[:limit]):
        if deliver(pk):
            n += 1
    return n


def tidy():
    """Drop finished rows after a few days."""
    PendingSend.objects.filter(status__in=[PendingSend.Status.SENT, PendingSend.Status.CANCELLED],
                               updated_at__lt=timezone.now() - timedelta(days=KEEP_DAYS)).delete()
