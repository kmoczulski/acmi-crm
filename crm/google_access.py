"""Google access: what the hub may use in one person's Google account, in one list (Settings -> Google access).

Google keeps ONE permission per person for the hub, and each feature asks for its piece of it on top of what was
already granted (include_granted_scopes). So nobody has to sign out and back in: every row below has its own button that
sends the person to Google for just that piece and brings them back to the Settings page.

The rows, in the order a person meets them:

  Sign-in     profile and email: how you log in. Always on, nothing to switch.
  Calendar    read-only: your meetings on the Meetings page and company timelines (given when you first connect).
  Scheduling  calendar.events: put a meeting you schedule in the hub on your own calendar.
  Gmail       gmail.modify: read, search, label, draft and send in the hub's Mail.
  Chat        Google Chat spaces and messages, in the hub's Chat.
  Drive       drive.readonly: only for the person whose Drive holds the Granola text export (ACMI_GRANOLA_DRIVE_USER).

Nothing here calls Google while the page draws, except one `tokeninfo` lookup for the Drive row of the one person who
needs it. `disconnect_all` is the only way to take a single permission back: Google drops them together.
"""
import logging

from django.conf import settings
from django.urls import reverse
from django.utils.timesince import timesince

from . import gcal
from .chat import connect as chat_connect
from .mail import connect as mail_connect
from .models import CalendarSync, ChatConnection
from .schedule import connect as schedule_connect

log = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_SESSION_KEY = "drive_connect_pending"


class DriveConnectError(Exception):
    """Something the person can act on, in plain words."""


def drive_needed(user):
    """Only the person whose Google sign-in reads the Granola folder needs Drive; for everyone else the row is hidden."""
    wanted = (getattr(settings, "GRANOLA_DRIVE_USER", "") or "").strip().lower()
    return bool(wanted and user and user.email and user.email.lower() == wanted)


def drive_connect_url(request):
    from allauth.socialaccount.adapter import get_adapter

    provider = get_adapter().get_provider(request, "google")
    return provider.get_login_url(
        request, process="connect", scope=DRIVE_SCOPE, auth_params="prompt=consent&include_granted_scopes=true",
        next=reverse("crm:settings_google_drive_done"),
    )


def drive_finish(user, session=None):
    """After Google sends the person back: check Drive really arrived and the Google account is theirs."""
    token = gcal.token_for(user)
    if token is None:
        raise DriveConnectError("Google didn't connect. Try Connect Drive again.")
    google_email = (token.account.extra_data or {}).get("email", "").lower()
    if google_email and user.email and google_email != user.email.lower():
        raise DriveConnectError(f"That was {google_email}. Connect Drive with your own account, {user.email}.")
    try:
        granted = gcal.granted_scopes(user, session)
    except gcal.CalendarError as exc:
        raise DriveConnectError(str(exc)) from exc
    if DRIVE_SCOPE not in granted:
        raise DriveConnectError("Google didn't give the hub access to Drive. Try again and leave the Drive box ticked on "
                                "Google's screen.")


def disconnect_all(user, session=None):
    """Take back everything the hub holds in this person's Google account: revoke at Google, delete the token, and
    switch Mail, Chat and scheduling off. Sign-in with Google keeps working (it asks again, for the basics only)."""
    removed = mail_connect.turn_off(user, session)  # revokes and deletes every Google token of theirs
    chat_connect.turn_off(user)
    schedule_connect.forget(user)
    return removed


def _action(label, url=None, *, post=None, primary=False, danger=False, confirm=""):
    """One button: a link (`url`) or a POST form (`post`), the way settings_google.html draws it."""
    return {"label": label, "url": url, "post": post, "primary": primary, "danger": danger, "confirm": confirm}


def calendar_url(request, here):
    """allauth's Google connect for the Calendar permission, coming back to the Settings page `here`."""
    from allauth.socialaccount.adapter import get_adapter

    provider = get_adapter().get_provider(request, "google")
    return provider.get_login_url(request, process="connect", auth_params="prompt=consent&include_granted_scopes=true",
                                  next=here)


def rows(request, here):
    """The Google access rows for the signed-in person. `here` is the path of the Settings page, so a button comes back
    to it. Each row: key, label, purpose, state ("on", "off", "attention", "locked"), state_text, detail, actions."""
    user = request.user
    token = gcal.token_for(user)
    ready = settings.GOOGLE_SIGNIN_ENABLED
    google_email = (token.account.extra_data or {}).get("email", "") if token else ""
    sync = CalendarSync.objects.filter(user=user).first()
    out = []

    out.append({
        "key": "signin", "label": "Sign-in", "state": "on" if ready else "off",
        "state_text": "On" if ready else "Not set up",
        "purpose": "Lets you sign in to the hub with your ACMI Google account. Shares your name and email address only.",
        "detail": f"Signed in as {google_email or user.email}." if (google_email or user.email) else "",
        "actions": [],
    })

    cal_actions, cal_detail, cal_state = [], "", "off"
    if not ready:
        cal_state = "locked"
        cal_detail = "Google isn't set up on this hub yet (GOOGLE_CLIENT_ID)."
    elif token is not None:
        cal_state = "attention" if (sync and sync.last_error) else "on"
        if sync and sync.last_synced_at:
            cal_detail = f"Last synced {timesince(sync.last_synced_at)} ago."
        if sync and sync.last_error:
            cal_detail = sync.last_error
        cal_actions = [_action("Sync now", post=reverse("crm:calendar_sync_now")),
                       _action("Reconnect", calendar_url(request, here), primary=cal_state == "attention")]
    else:
        cal_actions = [_action("Connect Calendar", calendar_url(request, here), primary=True)]
    out.append({
        "key": "calendar", "label": "Calendar", "state": cal_state,
        "state_text": {"on": "Connected", "attention": "Needs attention", "off": "Not connected", "locked": "Unavailable"}[cal_state],
        "purpose": "Reads your calendar (never changes it) so your meetings appear on the Meetings page and on company "
                   "timelines, and so scheduling can see when people are free.",
        "detail": cal_detail, "actions": cal_actions,
    })

    can_schedule = schedule_connect.can_schedule(user)
    if not ready or token is None:
        sched = {"state": "locked", "state_text": "Needs Calendar", "detail": "Connect Calendar first.", "actions": []}
    elif can_schedule:
        sched = {"state": "on", "state_text": "Allowed", "detail": "", "actions": [
            _action("Reconnect", post=reverse("crm:schedule_allow"))]}
    else:
        sched = {"state": "off", "state_text": "Not allowed", "detail":
                 "Until you allow it, \"Open in Google Calendar\" fills a meeting in for you to save yourself.",
                 "actions": [_action("Allow scheduling", post=reverse("crm:schedule_allow"), primary=True)]}
    out.append({"key": "scheduling", "label": "Scheduling",
                "purpose": "Puts meetings you schedule in the hub on your own Google Calendar, and has Google email the invitations.",
                **sched})

    mail_reason = mail_connect.unavailable_reason(user)
    mail_conn = mail_connect.connection(user)
    if mail_connect.is_on(user):
        mail = {"state": "on", "state_text": "On",
                "detail": (mail_conn.google_email if mail_conn else "") or user.email,
                "actions": [_action("Reconnect", post=reverse("crm:mail_turn_on")),
                            _action("Turn off", post=reverse("crm:mail_turn_off"), danger=True,
                                    confirm="Turn off Mail? This removes the hub's whole Google permission, so your "
                                            "Calendar disconnects too. You can connect it again here.")]}
    elif mail_reason:
        mail = {"state": "locked", "state_text": "Unavailable", "detail": mail_reason, "actions": []}
    else:
        mail = {"state": "attention" if (mail_conn and mail_conn.last_error) else "off", "state_text": "Off",
                "detail": f"Last try: {mail_conn.last_error}" if mail_conn and mail_conn.last_error else "",
                "actions": [_action("Turn on Mail", post=reverse("crm:mail_turn_on"), primary=True)]}
    out.append({"key": "gmail", "label": "Gmail",
                "purpose": "Read, search, label, draft and send your email in the hub's Mail, next to the companies and "
                           "deals it's about. Cannot delete anything permanently, and nobody else sees your mailbox.",
                **mail})

    chat_reason = chat_connect.unavailable_reason(user)
    chat_conn = chat_connect.connection(user)
    if chat_connect.is_on(user):
        chat = {"state": "on", "state_text": "On", "detail": "",
                "actions": [_action("Reconnect", post=reverse("crm:chat_turn_on")),
                            _action("Turn off", post=reverse("crm:chat_turn_off"))]}
    elif chat_reason:
        chat = {"state": "locked", "state_text": "Unavailable", "detail": chat_reason, "actions": []}
    else:
        chat = {"state": "attention" if (chat_conn and chat_conn.last_error) else "off", "state_text": "Off",
                "detail": f"Last try: {chat_conn.last_error}" if chat_conn and chat_conn.last_error else "",
                "actions": [_action("Turn on Chat", post=reverse("crm:chat_turn_on"), primary=True)]}
    out.append({"key": "chat", "label": "Google Chat",
                "purpose": "Shows your Google Chat conversations in the hub and alerts you to new messages.", **chat})

    if drive_needed(user):
        if not ready or token is None:
            drive = {"state": "locked", "state_text": "Needs Calendar", "detail": "Connect Calendar first.", "actions": []}
        else:
            try:
                has_drive = DRIVE_SCOPE in gcal.granted_scopes(user)
                known = True
            except Exception:  # Google unreachable or the token was refused: say so rather than guess
                has_drive, known = False, False
            if known and has_drive:
                drive = {"state": "on", "state_text": "Connected", "detail": "",
                         "actions": [_action("Reconnect", post=reverse("crm:settings_google_drive"))]}
            else:
                drive = {"state": "off" if known else "attention", "state_text": "Not connected" if known else "Unknown",
                         "detail": "" if known else "Google didn't answer. Try Connect Drive.",
                         "actions": [_action("Connect Drive", post=reverse("crm:settings_google_drive"), primary=True)]}
        out.append({"key": "drive", "label": "Google Drive",
                    "purpose": "Reads the folder where the daily Granola task saves your meeting notes (read-only), so the "
                               "hub can import them. Only you need this.", **drive})
    return out

