"""Browser notifications (Web Push) and each person's choice of which alerts reach them.

A browser that turns on notifications (Notification settings page, static/crm/push.js) registers the service worker
at /sw.js and saves a PushSubscription. notify.send then pushes to every one of that person's subscriptions, for the
kinds they left on, so alerts arrive with the hub's tab closed. Clicking one opens the record.

Push needs VAPID keys in the environment (`manage.py vapid_keys` makes a pair) and HTTPS, except on localhost.
Capital partners (the Partners group) never subscribe and never get a push.

UserPrefs.data["notify"] = {kind: {"hub": bool, "push": bool}}; a kind missing from it is on for both.
"""
import json
import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import Notification, PushSubscription
from .permissions import is_partner
from .prefs import get_prefs, set_pref

log = logging.getLogger(__name__)

# The kinds a person can switch, in the order the settings page shows them, with what each one covers.
KINDS = [
    (Notification.Kind.MAIL, "New email in your inbox (once the email client is on for you)."),
    (Notification.Kind.CHAT, "A new Google Chat message from someone else (once Chat is on for you), except in conversations you mute."),
    (Notification.Kind.MENTION, "Someone @mentions you in a comment."),
    (Notification.Kind.COMMENT, "A comment on a task, deal, company, site, card or contract you own or follow."),
    (Notification.Kind.ASSIGNED, "Someone gives you a task."),
    (Notification.Kind.DUE, "Your tasks and cards due today or overdue, and 15 minutes before a task's time."),
    (Notification.Kind.CARD, "Someone adds you to a board card."),
    (Notification.Kind.STAGE, "Someone moves one of your deals to another stage."),
    (Notification.Kind.REPLY, "A company you own or work on replies by email."),
    (Notification.Kind.DEADLINE, "Contract dates, diligence and closing items, entitlements and capital deadlines."),
    (Notification.Kind.DIRECTORY, "Claude filed new email contacts in the directory."),
]
CHANNELS = ("hub", "push")


def choices(user):
    """{kind: {"hub": bool, "push": bool}} for every switchable kind."""
    saved = get_prefs(user).get("notify") or {}
    out = {}
    for kind, _ in KINDS:
        row = saved.get(kind) if isinstance(saved.get(kind), dict) else {}
        out[kind] = {c: row.get(c) is not False for c in CHANNELS}
    return out


def wants(user, kind, channel):
    row = (get_prefs(user).get("notify") or {}).get(kind)
    return not (isinstance(row, dict) and row.get(channel) is False)


def save_choices(user, posted):
    """posted: the settings form (a checkbox named "<kind>:<channel>" for each switch left on)."""
    set_pref(user, "notify", {kind: {c: f"{kind}:{c}" in posted for c in CHANNELS} for kind, _ in KINDS})


def configured():
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def device_name(user_agent):
    ua = user_agent or ""
    browser = next((b for b, pat in (("Edge", "Edg/"), ("Opera", "OPR/"), ("Samsung Internet", "SamsungBrowser"),
                                     ("Chrome", "Chrome/"), ("Firefox", "Firefox/"), ("Safari", "Safari/"))
                    if pat in ua), "Browser")
    system = next((s for s, pat in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                    ("Windows", "Windows"), ("Mac", "Macintosh"), ("Linux", "Linux")) if pat in ua), "")
    return f"{browser} on {system}" if system else browser


# The browsers' own push services: Chrome (and Opera, Samsung), Firefox, Edge, Safari. The server POSTs to whatever
# endpoint is saved, so anything else is refused rather than letting it reach an address of someone's choosing.
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com", "push.apple.com",
              "web.push.apple.com")


def push_service_endpoint(endpoint):
    """True for an https:// URL on one of the browsers' push services (the host itself or a subdomain of it)."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password:
        return False
    return any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def subscribe(user, data, user_agent=""):
    """data: the browser's PushSubscription.toJSON(). Returns the saved row, or None if it's not usable."""
    endpoint = str(data.get("endpoint") or "")
    keys = data.get("keys") or {}
    if is_partner(user) or not push_service_endpoint(endpoint) or not keys.get("p256dh") or not keys.get("auth"):
        return None
    sub, _ = PushSubscription.objects.update_or_create(  # a browser someone else used before moves to this person
        endpoint=endpoint[:1000],
        defaults={"user": user, "p256dh": str(keys["p256dh"])[:200], "auth": str(keys["auth"])[:100],
                  "device": device_name(user_agent)[:120]},
    )
    return sub


def payload(note=None, *, title="", body="", url=""):
    """What the service worker shows. A saved notification opens through notification_open, which marks it read."""
    window = ""  # new email: the mail dock's little box for the thread, for a hub tab that's already open
    if note is not None:
        from . import notify

        title, body = note.get_kind_display(), note.text
        url = reverse("crm:notification_open", args=[note.pk]) if note.in_hub else (note.url or "/")
        tag = f"acmi-{note.pk}"
        window = notify.window_url(note)
    else:
        tag = "acmi-test"
    return {"title": title, "body": body, "url": url if url.startswith("/") else "/", "tag": tag, "window": window}


def deliver(user, data):
    """Push to every browser the person turned on. Returns how many accepted it. Gone subscriptions are removed."""
    if not configured() or is_partner(user) or not user.is_active:
        return 0
    from pywebpush import WebPushException, webpush

    sent = 0
    for sub in PushSubscription.objects.filter(user=user):
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=json.dumps(data),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                ttl=24 * 3600,
                timeout=5,
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):  # the browser unsubscribed or the person cleared the site's data
                sub.delete()
            else:
                log.warning("Push to %s failed: %s", sub, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - a push problem must never break the page that sent the alert
            log.warning("Push to %s failed: %s", sub, exc)
            continue
        sub.last_sent_at = timezone.now()
        sub.save(update_fields=["last_sent_at"])
        sent += 1
    return sent


def notification_saved(note):
    """From notify.send: push it once the change that caused it is committed."""
    if not configured() or not wants(note.user, note.kind, "push") or is_partner(note.user):
        return
    if not PushSubscription.objects.filter(user=note.user).exists():
        return
    data = payload(note)
    transaction.on_commit(lambda: deliver(note.user, data))
