"""Shared mailboxes (docs/EMAIL_CLIENT.md, section 6; step 7).

A shared address (deals@) is connected once by a Manager who can sign in as it: the hub sends them to Google as that
address, asking for gmail.modify, and keeps the permission encrypted (SharedMailboxToken). Managers then give named
teammates access: read, or read and send (which also allows archive, labels, Trash and snooze). It shows in the hub
as a second mailbox under your own, and every open, send and change in it is logged with who did it.

Not domain-wide delegation, which would let the hub read any mailbox in the company without asking. Partners never
get access: grants are only ever given to team_users(), and every check asks again.
"""
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from crm import tokens
from crm.auth import email_allowed
from crm.models import (
    MailboxAccess, MailboxAccessLog, Mailbox, MailCrmLabel, MailLabel, MailSnooze, MailThread, SharedMailboxToken,
)
from crm.permissions import is_partner, team_users

from .connect import GMAIL_SCOPE, REVOKE_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
TIMEOUT = 30
SESSION_KEY = "shared_mailbox_connect"
SHARED_BACKFILL_DAYS = 30


class SharedError(Exception):
    """Something the Manager can act on, in plain words."""


def role(user, mailbox):
    """"owner" (your own mailbox), "send" or "read" (a shared one you were given), or None."""
    if not (user and user.is_authenticated) or is_partner(user) or not user.is_active:
        return None
    if not mailbox.shared:
        return "owner" if mailbox.user_id == user.pk else None
    if not SharedMailboxToken.objects.filter(mailbox=mailbox).exists():
        return None
    grant = MailboxAccess.objects.filter(mailbox=mailbox, user=user, user__in=team_users()).first()
    return None if grant is None else ("send" if grant.can_send else "read")


def can_change(user, mailbox):
    return role(user, mailbox) in ("owner", "send")


def shared_for(user):
    """The shared mailboxes this person was given, in name order."""
    if not user or is_partner(user):
        return []
    return [m for m in Mailbox.objects.filter(shared=True, grants__user=user, shared_token__isnull=False)
            .order_by("name", "email").distinct() if role(user, m)]


def log(mailbox, user, action, gmail_id="", detail=""):
    """Note who did what in a shared mailbox. A person's own mailbox isn't logged."""
    if mailbox.shared:
        MailboxAccessLog.objects.create(mailbox=mailbox, user=user, action=action, gmail_id=gmail_id[:40],
                                        detail=detail[:300])


def recipients(mailbox):
    """Who hears about new mail in a mailbox: its owner, or everyone given a shared one."""
    if not mailbox.shared:
        return [mailbox.user] if mailbox.user_id else []
    return [g.user for g in MailboxAccess.objects.filter(mailbox=mailbox).select_related("user")
            if role(g.user, mailbox)]


# ---------------------------------------------------------------------------
# Connecting one: Google sign-in as that address
# ---------------------------------------------------------------------------


def _app():
    app = settings.SOCIALACCOUNT_PROVIDERS.get("google", {}).get("APP", {})
    if not app.get("client_id"):
        raise SharedError("Google sign-in isn't set up on this hub (GOOGLE_CLIENT_ID in .env).")
    return app


def start(request, email):
    """Where to send the Manager: Google's sign-in, as the shared address."""
    email = (email or "").strip().lower()
    if not email_allowed(email):
        raise SharedError("A shared mailbox has to be an ACMI address.")
    if team_users().filter(email__iexact=email).exists():
        raise SharedError(f"{email} is a teammate's own address. They turn Mail on for themselves.")
    if not tokens.configured():
        raise SharedError("The hub has no encryption key for Google tokens yet (ACMI_TOKEN_KEY in .env).")
    app = _app()
    state = secrets.token_urlsafe(24)
    request.session[SESSION_KEY] = {"state": state, "email": email}
    return AUTH_URL + "?" + urlencode({
        "client_id": app["client_id"], "redirect_uri": request.build_absolute_uri(reverse("crm:mail_shared_done")),
        "response_type": "code", "scope": f"openid email {GMAIL_SCOPE}", "access_type": "offline",
        "prompt": "consent select_account", "login_hint": email, "state": state, "hd": email.rsplit("@", 1)[-1],
    })


def finish(request, session=None):
    """Google sent the Manager back: check it's that address and Gmail was granted, then keep the permission."""
    session = session or requests
    pending = request.session.pop(SESSION_KEY, None)
    if not pending or not secrets.compare_digest(pending.get("state", ""), request.GET.get("state", "")):
        raise SharedError("That sign-in didn't start here. Try Connect again.")
    if request.GET.get("error"):
        raise SharedError("Google didn't connect the mailbox (the sign-in was cancelled).")
    app = _app()
    r = session.post(TOKEN_URL, data={
        "code": request.GET.get("code", ""), "client_id": app["client_id"], "client_secret": app.get("secret", ""),
        "redirect_uri": request.build_absolute_uri(reverse("crm:mail_shared_done")), "grant_type": "authorization_code",
    }, timeout=TIMEOUT)
    if r.status_code != 200:
        raise SharedError("Google didn't accept the sign-in. Try Connect again.")
    data = r.json()
    if GMAIL_SCOPE not in (data.get("scope") or "").split():
        raise SharedError("Google didn't give the hub access to that mailbox's Gmail. Leave the Gmail box ticked.")
    if not data.get("refresh_token"):
        raise SharedError("Google didn't give a lasting permission. Try Connect again.")
    who = session.get(USERINFO_URL, headers={"Authorization": f"Bearer {data['access_token']}"}, timeout=TIMEOUT)
    info = who.json() if who.status_code == 200 else {}
    email = (info.get("email") or "").lower()
    if email != pending["email"] or not info.get("email_verified"):
        raise SharedError(f"That was {email or 'another account'}, not {pending['email']}. Sign in as the shared address.")
    with transaction.atomic():
        mailbox = Mailbox.objects.filter(shared=True, email__iexact=email).first() or Mailbox(
            shared=True, email=email, name=email.split("@")[0].title(), backfill_days=SHARED_BACKFILL_DAYS)
        mailbox.backend, mailbox.enabled, mailbox.last_error = Mailbox.Backend.GMAIL_API, True, ""
        mailbox.save()
        SharedMailboxToken.objects.update_or_create(mailbox=mailbox, defaults={
            "token": data["access_token"], "token_secret": data["refresh_token"],
            "expires_at": timezone.now() + timedelta(seconds=int(data.get("expires_in", 3600))),
            "connected_by": request.user,
        })
    return mailbox


def disconnect(mailbox, session=None):
    """Take a shared mailbox out of the hub: revoke its permission at Google and drop what the hub kept of it (its
    index, labels, snoozes, grants). Timeline entries and filings stay with their records."""
    session = session or requests
    token = SharedMailboxToken.objects.filter(mailbox=mailbox).first()
    if token is not None:
        try:
            session.post(REVOKE_URL, data={"token": token.token_secret or token.token}, timeout=TIMEOUT)
        except requests.RequestException:
            pass
        token.delete()
    for model in (MailThread, MailLabel, MailSnooze, MailCrmLabel, MailboxAccess):
        model.objects.filter(mailbox=mailbox).delete()
    Mailbox.objects.filter(pk=mailbox.pk).update(enabled=False, history_id="", pending_ids=[])
