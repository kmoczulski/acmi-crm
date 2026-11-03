"""Chat in the hub: each person's on / off switch and the Google Chat permissions behind it (docs/CHAT.md).

Turning Chat on sends the person to Google once, through the same allauth connect flow as Mail, asking for the Chat
permissions on top of what they already granted (include_granted_scopes). The token stays in allauth's SocialToken,
encrypted (crm/tokens.py), next to Gmail and Calendar.

Turning Chat off only stops the hub using it. Google keeps one permission per person for the hub, so taking back just
Chat's part isn't possible; Turn off Mail removes the whole grant, as it always has.

Never available to capital partners (the Partners group), and never without ACMI_TOKEN_KEY.
"""
import logging

import requests
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from crm import gcal, tokens
from crm.models import ChatConnection
from crm.permissions import is_partner

SCOPE_SPACES = "https://www.googleapis.com/auth/chat.spaces.readonly"
SCOPE_MEMBERS = "https://www.googleapis.com/auth/chat.memberships.readonly"
SCOPE_MESSAGES = "https://www.googleapis.com/auth/chat.messages"  # read and write; Google's "restricted" kind, like Gmail's
SCOPE_READ_STATE = "https://www.googleapis.com/auth/chat.users.readstate"  # what's read in Chat (unread markers)
SCOPE_CREATE = "https://www.googleapis.com/auth/chat.spaces.create"  # start a new direct message, group chat or space
# chat.messages already covers writing, reactions, editing, deleting, files and searching messages.
NEEDED = (SCOPE_SPACES, SCOPE_MEMBERS, SCOPE_MESSAGES)
CHAT_SCOPES = [*NEEDED, SCOPE_READ_STATE, SCOPE_CREATE]
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
TIMEOUT = 30
SESSION_KEY = "chat_connect_pending"

log = logging.getLogger(__name__)


class ChatConnectError(Exception):
    """Something the person can act on, in plain words."""


def unavailable_reason(user):
    """Why this person can't turn Chat on, or "" when they can."""
    if not (user and user.is_authenticated) or is_partner(user) or not user.is_active:
        return "Chat isn't available for this account."
    if not settings.CHAT_ENABLED:
        return "Chat in the hub is switched off for everyone (ACMI_CHAT)."
    if not settings.GOOGLE_SIGNIN_ENABLED:
        return "Google sign-in isn't set up on this hub yet (GOOGLE_CLIENT_ID in .env)."
    if not tokens.configured():
        return "The hub has no encryption key for Google tokens yet (ACMI_TOKEN_KEY in .env), so Chat stays off."
    if not user.email:
        return "Your hub account has no email address. Ask an admin to add your ACMI address first."
    from crm.auth import email_allowed

    if not email_allowed(user.email):
        return "Chat works with ACMI Google accounts only."
    return ""


def connection(user):
    return ChatConnection.objects.filter(user=user).first()


def status(user):
    """(available, on): whether the top bar offers Chat to this person, and whether it is on for them."""
    if unavailable_reason(user):
        return False, False
    conn = connection(user)
    return True, bool(conn and conn.enabled and gcal.connected(user))


def is_on(user):
    return status(user)[1]


def connect_url(request):
    """allauth's Google connect, asking for the Chat permissions on top of what the person already granted.
    prompt=consent makes Google return a refresh token for the combined grant."""
    from allauth.socialaccount.adapter import get_adapter

    provider = get_adapter().get_provider(request, "google")
    return provider.get_login_url(
        request, process="connect", scope=",".join(CHAT_SCOPES),  # allauth splits extra scopes on commas
        auth_params="prompt=consent&include_granted_scopes=true", next=reverse("crm:chat_connected"),
    )


def granted_scopes(user, session=None):
    session = session or requests
    token = gcal.token_for(user)
    if token is None:
        return set()
    try:
        access = gcal.access_token(token, session)
    except gcal.CalendarError as exc:
        raise ChatConnectError("Google didn't accept the hub's sign-in. Try Turn on Chat again.") from exc
    r = session.get(TOKENINFO_URL, params={"access_token": access}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise ChatConnectError("Google wouldn't say which permissions the hub has. Try Turn on Chat again.")
    return set((r.json().get("scope") or "").split())


def finish(user, session=None):
    """After Google sends the person back: check the permissions really include Chat and the Google account is theirs,
    then switch Chat on. Returns the ChatConnection; raises ChatConnectError otherwise."""
    session = session or requests
    reason = unavailable_reason(user)
    if reason:
        raise ChatConnectError(reason)
    token = gcal.token_for(user)
    if token is None:
        raise ChatConnectError("Google didn't connect. Try Turn on Chat again.")
    google_email = (token.account.extra_data or {}).get("email", "").lower()
    if google_email and google_email != user.email.lower():
        raise ChatConnectError(f"That was {google_email}. Turn on Chat with your own account, {user.email}.")
    granted = granted_scopes(user, session)
    if any(scope not in granted for scope in NEEDED):
        raise ChatConnectError("Google didn't give the hub all the Chat permissions. Try again and leave every box "
                               "ticked on Google's screen.")
    now = timezone.now()
    conn, _ = ChatConnection.objects.get_or_create(user=user)
    conn.enabled, conn.google_email, conn.turned_on_at, conn.last_error = True, google_email or user.email, now, ""
    conn.can_read_state = SCOPE_READ_STATE in granted
    conn.can_create = SCOPE_CREATE in granted
    conn.alerted_through = now  # alerts start from now: turning Chat on never replays old messages
    conn.save()
    return conn


def turn_off(user):
    conn, _ = ChatConnection.objects.get_or_create(user=user)
    conn.enabled, conn.turned_off_at = False, timezone.now()
    conn.save()
    return conn
