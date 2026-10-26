"""Mail in the hub: each person's on / off switch and the Gmail permission behind it (docs/EMAIL_CLIENT.md, step 1).

Turning Mail on sends the person to Google once, through the same allauth connect flow as "Connect Google Calendar",
asking for one more permission, gmail.modify (read, label, draft, send; never permanent delete), added to their
existing grant (include_granted_scopes). The token stays in allauth's SocialToken, encrypted (crm/tokens.py).

Turning it off revokes the permission at Google and deletes the token. Google keeps one permission per person for the
hub, so this disconnects the calendar too; the page says so and offers Connect Google Calendar again.

Never available to capital partners (the Partners group), and never without ACMI_TOKEN_KEY.
"""
import logging

import requests
from requests import RequestException
from allauth.socialaccount.models import SocialToken
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from crm import gcal, tokens
from crm.models import MailConnection, Mailbox
from crm.permissions import is_partner

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
TIMEOUT = 30
SESSION_KEY = "mail_connect_pending"

log = logging.getLogger(__name__)


class MailConnectError(Exception):
    """Something the person can act on, in plain words."""


def unavailable_reason(user):
    """Why this person can't turn Mail on, or "" when they can."""
    if not (user and user.is_authenticated) or is_partner(user) or not user.is_active:
        return "Mail isn't available for this account."
    if not settings.MAIL_CLIENT_ENABLED:
        return "Mail in the hub is switched off for everyone (ACMI_MAIL_CLIENT)."
    if not settings.GOOGLE_SIGNIN_ENABLED:
        return "Google sign-in isn't set up on this hub yet (GOOGLE_CLIENT_ID in .env)."
    if not tokens.configured():
        return "The hub has no encryption key for Google tokens yet (ACMI_TOKEN_KEY in .env), so Mail stays off."
    if not user.email:
        return "Your hub account has no email address. Ask an admin to add your ACMI address first."
    from crm.auth import email_allowed

    if not email_allowed(user.email):
        return "Mail works with ACMI Google accounts only."
    return ""


def connection(user):
    return MailConnection.objects.filter(user=user).first()


def is_on(user):
    """Mail in the hub is on for this person: their switch, the site's switch, the key, and never a partner."""
    if unavailable_reason(user):
        return False
    conn = connection(user)
    return bool(conn and conn.enabled and gcal.connected(user))


def connect_url(request):
    """allauth's Google connect, asking for Gmail on top of what the person already granted. prompt=consent makes
    Google return a refresh token for the combined grant."""
    from allauth.socialaccount.adapter import get_adapter

    provider = get_adapter().get_provider(request, "google")
    return provider.get_login_url(
        request, process="connect", scope=GMAIL_SCOPE, auth_params="prompt=consent&include_granted_scopes=true",
        next=reverse("crm:mail_connected"),
    )


def granted_scopes(user, session=None):
    session = session or requests
    token = gcal.token_for(user)
    if token is None:
        return set()
    try:
        access = gcal.access_token(token, session)
    except gcal.CalendarError as exc:
        raise MailConnectError("Google didn't accept the hub's sign-in. Try Turn on Mail again.") from exc
    r = session.get(TOKENINFO_URL, params={"access_token": access}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise MailConnectError("Google wouldn't say which permissions the hub has. Try Turn on Mail again.")
    return set((r.json().get("scope") or "").split())


def finish(user, session=None):
    """After Google sends the person back: check the permission really includes Gmail and the Google account is
    theirs, then switch Mail on. Returns the MailConnection; raises MailConnectError otherwise."""
    session = session or requests
    reason = unavailable_reason(user)
    if reason:
        raise MailConnectError(reason)
    token = gcal.token_for(user)
    if token is None:
        raise MailConnectError("Google didn't connect. Try Turn on Mail again.")
    google_email = (token.account.extra_data or {}).get("email", "").lower()
    if google_email and google_email != user.email.lower():
        raise MailConnectError(f"That was {google_email}. Turn on Mail with your own account, {user.email}.")
    if GMAIL_SCOPE not in granted_scopes(user, session):
        raise MailConnectError("Google didn't give the hub access to Gmail. Try again and leave the Gmail box "
                               "ticked on Google's screen.")
    conn, _ = MailConnection.objects.get_or_create(user=user)
    conn.enabled, conn.google_email, conn.turned_on_at, conn.last_error = True, google_email or user.email, \
        timezone.now(), ""
    conn.save()
    return conn


def revoke(token, session=None):
    """Ask Google to drop the hub's permission. Best effort: the token is deleted here whatever Google says."""
    session = session or requests
    value = token.token_secret or token.token
    if not value:
        return False
    try:
        r = session.post(REVOKE_URL, data={"token": value}, timeout=TIMEOUT)
        return r.status_code == 200
    except RequestException:
        log.warning("Google revoke failed for %s; token deleted locally", token.account.user)
        return False


def turn_off(user, session=None):
    """Switch Mail off: revoke at Google, delete the token, and put the timeline sync back on IMAP. True when a
    Google token was removed (the calendar is then disconnected too)."""
    session = session or requests
    conn, _ = MailConnection.objects.get_or_create(user=user)
    conn.enabled, conn.turned_off_at = False, timezone.now()
    conn.save()
    Mailbox.objects.filter(user=user, backend=Mailbox.Backend.GMAIL_API).update(
        backend=Mailbox.Backend.IMAP, history_id="")
    removed = False
    for token in SocialToken.objects.filter(account__user=user, account__provider="google").select_related("account"):
        revoke(token, session)
        token.delete()
        removed = True
    return removed
