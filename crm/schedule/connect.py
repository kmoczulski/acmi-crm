"""The one extra Google permission scheduling needs: putting a meeting on the person's own calendar (docs/SCHEDULING.md).

Seeing when people are busy needs nothing new: the Calendar permission everyone gave when they connected already covers
free/busy. Creating the meeting and sending the invitations is a write, so each person allows it once, through the same
allauth connect flow as Mail and Chat (include_granted_scopes keeps what they already granted). Until they do, the page
offers "Open in Google Calendar", which needs no permission at all.
"""
from django.urls import reverse

from crm import gcal
from crm.models import CalendarSync

SCOPE = gcal.SCHEDULE_SCOPE
SESSION_KEY = "schedule_connect_pending"


class ScheduleConnectError(Exception):
    """Something the person can act on, in plain words."""


def can_schedule(user):
    return bool(CalendarSync.objects.filter(user=user, can_schedule=True).exists() and gcal.connected(user))


def connect_url(request):
    """allauth's Google connect asking for the calendar.events permission on top of what the person granted.
    prompt=consent makes Google return a refresh token for the combined grant."""
    from allauth.socialaccount.adapter import get_adapter

    provider = get_adapter().get_provider(request, "google")
    return provider.get_login_url(
        request, process="connect", scope=SCOPE,
        auth_params="prompt=consent&include_granted_scopes=true", next=reverse("crm:schedule_allowed"),
    )


def finish(user, session=None):
    """After Google sends the person back: check the permission really arrived and the Google account is theirs."""
    token = gcal.token_for(user)
    if token is None:
        raise ScheduleConnectError("Google didn't connect. Try Allow scheduling again.")
    google_email = (token.account.extra_data or {}).get("email", "").lower()
    if google_email and user.email and google_email != user.email.lower():
        raise ScheduleConnectError(f"That was {google_email}. Allow scheduling with your own account, {user.email}.")
    try:
        granted = gcal.granted_scopes(user, session)
    except gcal.CalendarError as exc:
        raise ScheduleConnectError(str(exc)) from exc
    if SCOPE not in granted:
        raise ScheduleConnectError("Google didn't give the hub permission to put meetings on your calendar. Try again "
                                   "and leave the box ticked on Google's screen.")
    state, _ = CalendarSync.objects.get_or_create(user=user)
    state.can_schedule = True
    state.save(update_fields=["can_schedule"])
    return state


def forget(user):
    """Google said the permission isn't there (the person removed the hub's access): ask again next time."""
    CalendarSync.objects.filter(user=user).update(can_schedule=False)
