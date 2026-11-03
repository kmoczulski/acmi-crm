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
from django.utils.dateparse import parse_datetime

from . import meetings
from .models import Activity, CalendarSync, Meeting

TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SCHEDULE_SCOPE = "https://www.googleapis.com/auth/calendar.events"  # put meetings on the person's own calendar
FREEBUSY_CALENDARS = 50  # Google's cap on calendars in one free/busy question
TIMEOUT = 30


class CalendarError(Exception):
    """Something the person has to fix (usually: reconnect Google)."""


class ScopeMissing(CalendarError):
    """Google says the hub may not write to this person's calendar yet (they haven't allowed scheduling)."""


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


def week(user, start, days=7, session=requests):
    """The person's own calendar from `start` (an aware datetime) for `days`, for picking meeting times while writing
    an email: [{start, end, title, all_day}], read live (read only) and never stored. Events they marked "free" or
    declined don't block time and are left out."""
    token = token_for(user)
    if token is None:
        raise CalendarError("Connect Google Calendar on the Calendar page to see your week here.")
    params = {"timeMin": start.isoformat(), "timeMax": (start + timedelta(days=days)).isoformat(),
              "singleEvents": "true", "orderBy": "startTime", "maxResults": "250"}
    r = session.get(EVENTS_URL, params=params, timeout=TIMEOUT,
                    headers={"Authorization": f"Bearer {access_token(token, session)}"})
    if r.status_code in (401, 403):
        raise CalendarError("Google Calendar access was not granted. Reconnect Google Calendar.")
    r.raise_for_status()
    out = []
    for e in r.json().get("items", []):
        declined = any(a.get("self") and a.get("responseStatus") == "declined" for a in e.get("attendees", []))
        if e.get("status") == "cancelled" or e.get("transparency") == "transparent" or declined:
            continue
        s, en = e.get("start", {}), e.get("end", {})
        out.append({"start": s.get("dateTime") or s.get("date"), "end": en.get("dateTime") or en.get("date"),
                    "title": e.get("summary", "") or "Busy", "all_day": "dateTime" not in s})
    return out


def granted_scopes(user, session=None):
    """The permissions Google says the person's token holds right now (a set of scope addresses)."""
    session = session or requests
    token = token_for(user)
    if token is None:
        return set()
    r = session.get(TOKENINFO_URL, params={"access_token": access_token(token, session)}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise CalendarError("Google wouldn't say which permissions the hub has. Try again.")
    return set((r.json().get("scope") or "").split())


def _problem(r):
    """(message, reasons) Google gave for a failed call."""
    try:
        error = (r.json() or {}).get("error") or {}
    except ValueError:
        error = {}
    if not isinstance(error, dict):
        return "", ""
    reasons = " ".join(str(e.get("reason", "")) for e in error.get("errors") or [] if isinstance(e, dict))
    return str(error.get("message", "")), f"{reasons} {error.get('status', '')}"


def _check(r, what):
    """Turn Google's refusals into words the person can act on; anything else that failed raises for the caller."""
    if r.status_code < 400:
        return
    message, reasons = _problem(r)
    low = f"{message} {reasons}".lower()
    if r.status_code == 429 or (r.status_code == 403 and ("ratelimit" in low or "quota" in low)):
        raise CalendarError("Google Calendar is busy right now. Try again in a minute.")
    if r.status_code == 403 and ("insufficient" in low or "scope" in low):
        raise ScopeMissing(f"The hub isn't allowed to {what} yet.")
    if r.status_code in (401, 403):
        raise CalendarError("Google Calendar access was not granted. Reconnect Google Calendar.")
    if r.status_code in (400, 404):
        raise CalendarError(f"Google wouldn't {what}: {message or r.status_code}.")
    r.raise_for_status()


def freebusy(user, emails, start, end, session=None):
    """When these calendars are busy between two times (aware datetimes), asked of Google with the signed-in person's
    own permission and never stored: {email: {"busy": [(start, end), ...], "problem": ""}}. A calendar Google won't show
    this person (not shared with them, not a Google calendar) comes back with no busy times and a "problem" in words.
    Events marked free and declined ones don't count as busy; Google decides that."""
    session = session or requests
    token = token_for(user)
    if token is None:
        raise CalendarError("Connect Google Calendar on the Calendar page to see people's availability.")
    wanted = list(dict.fromkeys(e.strip().lower() for e in emails if e and e.strip()))
    out = {}
    for i in range(0, len(wanted), FREEBUSY_CALENDARS):
        chunk = wanted[i:i + FREEBUSY_CALENDARS]
        r = session.post(FREEBUSY_URL, json={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                                             "items": [{"id": e} for e in chunk]},
                         headers={"Authorization": f"Bearer {access_token(token, session)}"}, timeout=TIMEOUT)
        _check(r, "read calendars")
        calendars = {str(k).lower(): v for k, v in (r.json().get("calendars") or {}).items()}
        for email in chunk:
            cal = calendars.get(email) or {}
            problem = ""
            if cal.get("errors") or email not in calendars:
                reasons = " ".join(str(e.get("reason", "")) for e in cal.get("errors") or [])
                problem = ("Their calendar isn't shared with you, or isn't a Google calendar."
                           if "notFound" in reasons or email not in calendars else "Google couldn't read their calendar.")
            busy = []
            for b in cal.get("busy") or []:
                s, e = parse_datetime(b.get("start", "")), parse_datetime(b.get("end", ""))
                if s and e and e > s:
                    busy.append((s, e))
            out[email] = {"busy": busy, "problem": problem}
    return out


def create_event(user, *, event_id, title, start, end, attendees, description="", location="", meet=True,
                 video="", session=None):
    """Put a meeting on the person's own calendar and invite the attendees (Google emails the invitations). `event_id`
    is chosen by the caller (letters a-v and digits), so asking twice for the same meeting finds the first instead of
    inviting everyone again. Returns (event, created): created is False when Google already had that id.

    video: "meet" adds a Google Meet link, "teams" asks the Teams add-on on this person's Google Calendar to make
    the meeting (the same add-on the Calendar app uses), "" is in person. `meet=True` still means Meet."""
    session = session or requests
    token = token_for(user)
    if token is None:
        raise CalendarError("Connect Google Calendar on the Calendar page first.")
    kind = video or ("meet" if meet else "")
    body = {
        "id": event_id, "summary": title, "description": description, "location": location,
        "start": {"dateTime": start.isoformat(), "timeZone": settings.TIME_ZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": settings.TIME_ZONE},
        "attendees": [{"email": a} for a in attendees],
    }
    if kind == "meet":
        body["conferenceData"] = {"createRequest": {"requestId": event_id,
                                                    "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    elif kind == "teams":
        body["conferenceData"] = {"createRequest": {"requestId": event_id,
                                                    "conferenceSolutionKey": {"type": "addOn"}}}
    headers = {"Authorization": f"Bearer {access_token(token, session)}"}
    r = session.post(EVENTS_URL, params={"sendUpdates": "all", "conferenceDataVersion": "1"}, json=body,
                     headers=headers, timeout=TIMEOUT)
    if r.status_code == 409:  # that meeting was already created (a double click, a retry)
        existing = session.get(f"{EVENTS_URL}/{event_id}", headers=headers, timeout=TIMEOUT)
        _check(existing, "find that meeting")
        found = existing.json()
        if found.get("status") == "cancelled":  # deleted in Google since: the id stays taken, so it can't be made again
            raise CalendarError("That meeting was cancelled in Google Calendar. Reload the page to schedule it again.")
        return found, False
    _check(r, "put a meeting on your calendar")
    event = r.json()
    if kind == "teams":
        event = _wait_for_conference(event_id, headers, session) or event
    return event, True


def _wait_for_conference(event_id, headers, session, tries=4):
    """The Teams add-on fills the link in after the event is made. A few short waits, then give up."""
    import time
    for _ in range(tries):
        time.sleep(1.5)
        r = session.get(f"{EVENTS_URL}/{event_id}", headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        event = r.json()
        status = ((event.get("conferenceData") or {}).get("createRequest") or {}).get("status") or {}
        if status.get("statusCode") == "success" or (event.get("conferenceData") or {}).get("entryPoints"):
            return event
        if status.get("statusCode") == "failure":
            return event
    return None


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
    from .permissions import team_users

    users = team_users().filter(socialaccount__provider="google").exclude(calendar_sync__enabled=False).distinct()
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
