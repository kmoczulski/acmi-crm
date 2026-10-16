"""Google Calendar -> Meetings calendar, using each person's Google sign-in token.

People connect once (Meetings -> "Connect Google Calendar", or simply signing in with Google);
allauth stores the access + refresh token. `sync_calendar` then reads each connected calendar
for CALENDAR_DAYS_BACK..CALENDAR_DAYS_AHEAD around today and hands the events to
crm.meetings.import_meetings, the same path the Granola import uses, so calendar events merge with
Granola notes and land on company timelines. Cancelled events are removed.
"""
from datetime import timedelta

import requests
from allauth.socialaccount.models import SocialToken
from django.conf import settings
from django.utils import timezone

from . import meetings
from .models import Activity, CalendarSync, Meeting

TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
TIMEOUT = 30


class CalendarError(Exception):
    """Something the person has to fix (usually: reconnect Google)."""


def token_for(user):
    return SocialToken.objects.filter(account__user=user, account__provider="google").select_related("account").first()


def connected(user):
    return token_for(user) is not None


def _google_app():
    app = settings.SOCIALACCOUNT_PROVIDERS.get("google", {}).get("APP", {})
    if not app.get("client_id"):
        raise CalendarError("Google sign-in is not configured (GOOGLE_CLIENT_ID missing from .env).")
    return app


def access_token(token, session=requests):
    """A valid access token, refreshing it with the stored refresh token when it has expired."""
    if token.expires_at and token.expires_at > timezone.now() + timedelta(seconds=60):
        return token.token
    if not token.token_secret:
        raise CalendarError("No refresh token stored. Reconnect Google Calendar.")
    app = _google_app()
    r = session.post(TOKEN_URL, data={
        "client_id": app["client_id"], "client_secret": app.get("secret", ""),
        "refresh_token": token.token_secret, "grant_type": "refresh_token",
    }, timeout=TIMEOUT)
    if r.status_code != 200:
        raise CalendarError(f"Google refused the refresh ({r.json().get('error', r.status_code)}). Reconnect Google Calendar.")
    data = r.json()
    token.token = data["access_token"]
    token.expires_at = timezone.now() + timedelta(seconds=int(data.get("expires_in", 3600)))
    token.save(update_fields=["token", "expires_at"])
    return token.token


def fetch_events(token, session=requests):
    """Every event in the sync window on the person's primary calendar (recurring ones expanded)."""
    now = timezone.now()
    params = {
        "timeMin": (now - timedelta(days=settings.CALENDAR_DAYS_BACK)).isoformat(),
        "timeMax": (now + timedelta(days=settings.CALENDAR_DAYS_AHEAD)).isoformat(),
        "singleEvents": "true", "showDeleted": "true", "maxResults": "250", "orderBy": "startTime",
    }
    events = []
    while True:
        r = session.get(EVENTS_URL, params=params, timeout=TIMEOUT,
                        headers={"Authorization": f"Bearer {access_token(token, session)}"})
        if r.status_code in (401, 403):
            raise CalendarError("Google Calendar access was not granted. Reconnect Google Calendar.")
        r.raise_for_status()
        data = r.json()
        events += data.get("items", [])
        if not data.get("nextPageToken"):
            return events
        params["pageToken"] = data["nextPageToken"]


def to_meeting(event):
    """Google event -> the dict crm.meetings.import_meetings reads. None for all-day events."""
    start = event.get("start", {}).get("dateTime")
    if not start:
        return None  # all-day: holidays, OOO, travel
    attendees = [
        {"email": a["email"], "name": a.get("displayName", "")}
        for a in event.get("attendees", [])
        if a.get("email") and a.get("responseStatus") != "declined" and not a.get("resource")
    ]
    return {
        "source": "calendar", "id": event["id"], "title": event.get("summary", "") or "(no title)",
        "start": start, "end": event.get("end", {}).get("dateTime"), "url": event.get("htmlLink", ""),
        "attendees": attendees,
    }


def remove_cancelled(event_ids):
    """Drop meetings (and their timeline rows) for events that were cancelled."""
    n = 0
    for eid in event_ids:
        n += Meeting.objects.filter(external_id=f"calendar:{eid}").delete()[0]
        Meeting.objects.filter(calendar_id=eid).update(calendar_id="")
        Activity.objects.filter(source=Activity.Source.CALENDAR, source_id__startswith=f"{eid}:").delete()
    return n


def sync_user(user, session=requests):
    state, _ = CalendarSync.objects.get_or_create(user=user)
    token = token_for(user)
    if token is None:
        state.last_error = "Not connected"
        state.save(update_fields=["last_error"])
        raise CalendarError("Not connected to Google. Use “Connect Google Calendar” on the Meetings page.")
    try:
        events = fetch_events(token, session)
    except (CalendarError, requests.RequestException) as exc:
        state.last_error = str(exc)[:1000]
        state.save(update_fields=["last_error"])
        raise
    cancelled = [e["id"] for e in events if e.get("status") == "cancelled"]
    rows = [m for m in (to_meeting(e) for e in events if e.get("status") != "cancelled") if m]
    stats = meetings.import_meetings(rows)
    stats["removed"] = remove_cancelled(cancelled)
    state.last_synced_at = timezone.now()
    state.last_error = ""
    state.events_seen = len(events)
    state.save()
    return stats


def sync_all(stdout=None):
    from django.contrib.auth import get_user_model

    users = get_user_model().objects.filter(
        is_active=True, socialaccount__provider="google",
    ).exclude(calendar_sync__enabled=False).distinct()
    results = {}
    for user in users:
        try:
            results[user.username] = sync_user(user)
        except (CalendarError, requests.RequestException) as exc:
            results[user.username] = {"error": str(exc)}
        if stdout:
            r = results[user.username]
            stdout.write(f"{user.username}: " + (r["error"] if "error" in r else
                         f"{r['calendar']} meetings ({r['meetings']} with companies), {r['removed']} cancelled removed"))
    return results
