"""Gmail push: Gmail tells the hub the moment a mailbox changes, so new mail reaches the timeline in seconds.

How it fits together: a watch on each Gmail-API mailbox makes Gmail publish {emailAddress, historyId} to a Pub/Sub
topic (settings.GMAIL_PUSH_TOPIC); the topic's push subscription POSTs it, signed as a service account, to
/internal/gmail-push/ (crm/mail/views.py); that runs the usual one-mailbox sync, which reads from the mailbox's own
history marker, so a lost, late or repeated notification costs nothing. The 15-minute sync stays as the safety net and
renews each watch before its 7 days run out. With no topic set nothing here does anything.
"""
import base64
import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from crm.models import Mailbox

from .imap import MailboxError

log = logging.getLogger(__name__)

RENEW_WITHIN = timedelta(days=2)  # renew a watch once it has less than this left


def enabled():
    return bool(settings.GMAIL_PUSH_TOPIC)


def _expiry(result):
    """Gmail's expiration is milliseconds since the epoch, as a string."""
    ms = int(result.get("expiration") or 0)
    return datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc) if ms else None


def start_watch(mailbox, client=None):
    """(Re)start Gmail's push watch on one mailbox. Returns when it runs out, or None if push is off."""
    if not enabled() or mailbox.backend != Mailbox.Backend.GMAIL_API:
        return None
    from .gmail_api import client_for

    client = client or client_for(mailbox)
    mailbox.watch_expires_at = _expiry(client.watch(settings.GMAIL_PUSH_TOPIC))
    mailbox.save(update_fields=["watch_expires_at"])
    return mailbox.watch_expires_at


def renew_watches():
    """Start a watch on every synced Gmail-API mailbox that has none or is about to lose it. One person's Gmail
    problem must not stop the others. Returns {mailbox: expiry or error text}."""
    if not enabled():
        return {}
    from crm.permissions import team_users

    due = timezone.now() + RENEW_WITHIN
    out = {}
    boxes = (Mailbox.objects.filter(enabled=True, backend=Mailbox.Backend.GMAIL_API)
             .filter(Q(user__in=team_users()) | Q(shared=True, shared_token__isnull=False))
             .filter(Q(watch_expires_at__isnull=True) | Q(watch_expires_at__lt=due)).distinct())
    for mb in boxes:
        try:
            out[str(mb)] = start_watch(mb)
        except MailboxError as exc:
            out[str(mb)] = f"not started: {exc}"
            log.warning("Gmail push watch failed for %s: %s", mb, exc)
        except Exception as exc:  # noqa: BLE001 - never fails the sync that called it
            out[str(mb)] = f"not started: {type(exc).__name__}: {exc}"
            log.exception("Gmail push watch crashed for %s", mb)
    return out


def verified(request):
    """True when the request carries a Google-signed token for the push subscription's service account."""
    header = request.headers.get("Authorization", "")
    if not (settings.GMAIL_PUSH_SA and settings.GMAIL_PUSH_AUDIENCE and header.startswith("Bearer ")):
        return False
    try:
        claims = _verify_token(header[7:], settings.GMAIL_PUSH_AUDIENCE)
    except ValueError:
        return False
    return bool(claims.get("email_verified")) and claims.get("email", "").lower() == settings.GMAIL_PUSH_SA.lower()


def _verify_token(token, audience):
    """Google's check of a signed ID token: signature, expiry and audience. ValueError if any fails."""
    from google.auth.transport import requests as google_requests  # in the cloud image; loaded only when push is used
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)


def notified_address(body):
    """The mailbox address inside a Pub/Sub push body, or "" if it isn't one."""
    try:
        message = json.loads(body)["message"]
        return json.loads(base64.b64decode(message["data"]))["emailAddress"].lower()
    except (ValueError, KeyError, TypeError):
        return ""


def sync_for(address):
    """Run the one-mailbox sync for a notified address. Returns the sync's stats, or None if nothing was run."""
    from . import sync

    mailbox = (Mailbox.objects.filter(enabled=True, backend=Mailbox.Backend.GMAIL_API, email__iexact=address)
               .first())
    if mailbox is None:
        return None
    try:
        return sync.sync_mailbox(mailbox)
    except sync.SyncBusy:
        return None  # a sync is already reading from the same marker; it will pick this change up
