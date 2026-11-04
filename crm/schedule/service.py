"""Scheduling logic (docs/SCHEDULING.md): who can be picked, a week of availability, and the meeting itself.

Everything goes through the signed-in person's own Google permission (crm/gcal.py), so the hub can only show what
that person could see in Google Calendar, and a meeting is always created on their own calendar.
"""
import hashlib
import re
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from crm import gcal, meetings
from crm.auth import email_allowed
from crm.models import Meeting, Person
from crm.permissions import team_users

from . import slots

CACHE_TTL = 60  # seconds a week of busy times is reused, so dragging between weeks and lengths asks Google once
MAX_PEOPLE = 20  # calendars in one availability question
MAX_INVITED = 30  # people on one meeting
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
GOOGLE_TEMPLATE = "https://calendar.google.com/calendar/render"


class ScheduleError(Exception):
    """Something wrong with what was asked, said to the person as it is."""


def zone():
    return ZoneInfo(settings.TIME_ZONE)


def name_of(user):
    return user.get_full_name().strip() or user.username


def team(user):
    """The teammates the person can pick: [{email, name, me}], the person first, then by name."""
    rows = [{"email": u.email.lower(), "name": name_of(u), "me": u.pk == user.pk}
            for u in team_users().exclude(email="") if email_allowed(u.email)]
    rows.sort(key=lambda r: (not r["me"], r["name"].lower()))
    return rows


def clean_emails(values, limit):
    """A list (or a comma / space separated string) of addresses, lower-cased, without repeats; ScheduleError for one
    that isn't an address."""
    if isinstance(values, str):
        values = re.split(r"[,;\s]+", values)
    out = []
    for value in values or []:
        value = (value or "").strip().strip("<>").lower()
        if not value:
            continue
        try:
            validate_email(value)
        except ValidationError:
            raise ScheduleError(f"“{value[:60]}” isn't an email address.") from None
        if value not in out:
            out.append(value)
    if len(out) > limit:
        raise ScheduleError(f"That's too many people (at most {limit}).")
    return out


def names_for(emails):
    """{email: name} for the addresses the hub knows: teammates first, then people in the CRM."""
    emails = list(emails)
    names = {u.email.lower(): name_of(u)
             for u in team_users().annotate(lowered=Lower("email")).filter(lowered__in=emails)}
    for person in Person.objects.filter(email__in=[e for e in emails if e not in names]):
        names[person.email] = person.full_name or person.email
    return names


def search_people(user, text):
    """For the "Add someone" box: teammates and CRM people matching the text, and the text itself when it is an address."""
    text = (text or "").strip()
    if len(text) < 2:
        return []
    low, found, seen = text.lower(), [], set()
    for t in team(user):
        if low in t["name"].lower() or low in t["email"]:
            found.append({"email": t["email"], "name": t["name"], "detail": "Teammate"})
            seen.add(t["email"])
    people = (Person.objects.filter(Q(full_name__icontains=text) | Q(email__icontains=text))
              .exclude(email__isnull=True).exclude(email="").select_related("company").order_by("full_name")[:10])
    for p in people:
        if p.email not in seen:
            found.append({"email": p.email, "name": p.full_name or p.email,
                          "detail": p.company.name if p.company_id else p.title})
            seen.add(p.email)
    try:
        validate_email(low)
        if low not in seen:
            found.append({"email": low, "name": low, "detail": "Use this address"})
    except ValidationError:
        pass
    return found[:10]


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def _version(user):
    return cache.get(f"schedule-v:{user.pk}", 0)


def forget_busy(user):
    """A meeting was just made: the next look at anyone's week asks Google again."""
    cache.set(f"schedule-v:{user.pk}", _version(user) + 1, None)


def _busy(user, emails, first, session=None):
    start = datetime.combine(first, time(), tzinfo=zone())
    key = "schedule:%s:%s:%s:%s" % (user.pk, _version(user), first, hashlib.md5(",".join(sorted(emails)).encode()).hexdigest())
    data = cache.get(key)
    if data is None:
        data = gcal.freebusy(user, emails, start, start + timedelta(days=7), session)
        cache.set(key, data, CACHE_TTL)
    return data


def week_start(text, now=None):
    """The Monday of the week holding the date in `text` (YYYY-MM-DD), or of this week."""
    try:
        day = date.fromisoformat(text or "")
    except ValueError:
        day = (now or timezone.now()).astimezone(zone()).date()
    return slots.monday(day)


def hours_from(first, last):
    try:
        first, last = int(first), int(last)
    except (TypeError, ValueError):
        return slots.DEFAULT_HOURS
    if not (0 <= first <= 22 and first < last <= 24):
        return slots.DEFAULT_HOURS
    return first, last


def minutes_from(value, default=slots.DEFAULT_MINUTES):
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return default
    return minutes if 5 <= minutes <= 480 else default


def availability(user, emails, start=None, ndays=5, hours=slots.DEFAULT_HOURS, minutes=slots.DEFAULT_MINUTES,
                 session=None, now=None):
    """A week of who is busy when, for the grid (crm/schedule/slots.py has the rules). The person is always in it."""
    me = (user.email or "").lower()
    if not me:
        raise ScheduleError("Your hub account has no email address. Ask an admin to add your ACMI address first.")
    now = now or timezone.now()
    wanted = [me] + [e for e in clean_emails(emails, MAX_PEOPLE) if e != me]
    first = week_start(start, now)
    names = names_for(wanted)
    people = [{"email": e, "name": names.get(e) or e} for e in wanted]
    result = slots.availability(people, _busy(user, wanted, first, session), first, 7 if ndays == 7 else 5, hours,
                                minutes, now, zone())
    last = first + timedelta(days=len(result["days"]) - 1)
    result.update({"start": first.isoformat(), "previous": (first - timedelta(days=7)).isoformat(),
                   "next": (first + timedelta(days=7)).isoformat(),
                   "range": f"{first:%b} {first.day} – {last:%b} {last.day}, {last.year}"})
    return result


# ---------------------------------------------------------------------------
# The meeting
# ---------------------------------------------------------------------------


def meeting_fields(user, data, now=None):
    """What a meeting form says, checked: title, start and end (aware), attendees (without the person), place, notes."""
    now = now or timezone.now()
    title = " ".join((data.get("title") or "").split())
    if not title:
        raise ScheduleError("Give the meeting a title.")
    if len(title) > 200:
        raise ScheduleError("That title is too long (200 letters at most).")
    try:
        begin = datetime.fromisoformat((data.get("start") or "").strip())
    except ValueError:
        raise ScheduleError("Pick a day and a time.") from None
    if begin.tzinfo is None:
        begin = begin.replace(tzinfo=zone())
    minutes = minutes_from(data.get("minutes"), default=0)
    if not minutes:
        raise ScheduleError("A meeting runs from 5 minutes to 8 hours.")
    if begin < now - timedelta(minutes=10):
        raise ScheduleError("That time has already passed.")
    if begin > now + timedelta(days=366):
        raise ScheduleError("That's more than a year away.")
    me = (user.email or "").lower()
    attendees = [e for e in clean_emails(data.get("attendees"), MAX_INVITED) if e != me]
    if not attendees:
        raise ScheduleError("Add at least one person to meet with.")
    description = (data.get("description") or "").strip()
    location = " ".join((data.get("location") or "").split())
    video = data.get("video")
    video = video.strip().lower() if isinstance(video, str) else None
    if video not in ("meet", "teams", ""):
        # No video choice came with the form (the older form and the API): the plain meet flag decides. An empty
        # `video` is the form's "No video".
        video = "meet" if str(data.get("meet", "")).lower() in ("1", "true", "on", "yes") else ""
    if len(description) > 4000 or len(location) > 300:
        raise ScheduleError("The notes or the place are too long.")
    return {"title": title, "start": begin, "end": begin + timedelta(minutes=minutes), "minutes": minutes,
            "attendees": attendees, "description": description, "location": location,
            "meet": video == "meet", "video": video}


def google_link(fields):
    """Google Calendar's own "new event" screen filled in: what the person uses before they have allowed scheduling
    (they press Save there, so nothing needs the hub to write to their calendar)."""
    utc = lambda dt: dt.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    return GOOGLE_TEMPLATE + "?" + urlencode({
        "action": "TEMPLATE", "text": fields["title"], "dates": f"{utc(fields['start'])}/{utc(fields['end'])}",
        "details": fields["description"], "location": fields["location"], "add": ",".join(fields["attendees"]),
        "ctz": settings.TIME_ZONE,
    })


def _safe_link(url):
    return url if isinstance(url, str) and url.startswith("https://") else ""


def meet_link(event):
    link = event.get("hangoutLink")
    if not link:
        for entry in (event.get("conferenceData") or {}).get("entryPoints") or []:
            if entry.get("entryPointType") == "video" and entry.get("uri"):
                link = entry["uri"]
                break
    return _safe_link(link)


def import_created(user, event):
    """Put the new meeting on the hub's Calendar and the companies' timelines right away, the way the sync would."""
    row = gcal.to_meeting(event)
    if row is None:
        return None
    me = (user.email or "").lower()
    if me and not any(a["email"].lower() == me for a in row["attendees"]):
        row["attendees"].append({"email": user.email, "name": name_of(user)})  # a meeting needs two people to be filed
    meetings.import_meetings([row])
    return Meeting.objects.filter(external_id=f"calendar:{event['id']}").first()


def create_meeting(user, data, session=None, now=None):
    """Make the meeting on the person's own Google Calendar, invite everyone (Google sends the invitations), and file it
    in the hub. A repeat of the same request id finds the meeting made the first time instead of inviting again."""
    fields = meeting_fields(user, data, now)
    request_id = (data.get("request") or "").strip().lower()
    if not REQUEST_ID.match(request_id):
        raise ScheduleError("Reload the page and try again.")
    event, created = gcal.create_event(
        user, event_id="hub" + request_id, title=fields["title"], start=fields["start"], end=fields["end"],
        attendees=fields["attendees"], description=fields["description"], location=fields["location"],
        meet=fields["meet"], video=fields.get("video", ""), session=session)
    meeting = import_created(user, event)
    forget_busy(user)
    # What Google holds, not what was just typed: a repeated request finds the first meeting as it was made.
    begin = parse_datetime((event.get("start") or {}).get("dateTime", "")) or fields["start"]
    finish = parse_datetime((event.get("end") or {}).get("dateTime", "")) or fields["end"]
    local, local_end = begin.astimezone(zone()), finish.astimezone(zone())
    invited = [a["email"] for a in event.get("attendees") or [] if a.get("email") and not a.get("self")]
    return {"ok": True, "created": created, "title": event.get("summary") or fields["title"],
            "when": f"{slots.day_label(local.date())} · {slots.span(local, local_end)} {local.tzname()}",
            "link": _safe_link(event.get("htmlLink")), "meet": meet_link(event),
            "meeting": meeting.get_absolute_url() if meeting else "", "invited": invited or fields["attendees"]}
