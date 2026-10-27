"""Meeting invites that arrive by email: a calendar part (.ics, RFC 5545) or Google's "Invitation: …" email, often
forwarded by a teammate. The mail sync (crm/mail/sync.py) hands each such email here and the meeting goes on the
calendar as source "invite", so a lender call someone forwarded shows up even when it isn't on anyone's connected
calendar.

The same meeting already on the calendar (a calendar event or a Granola note within 15 minutes with a similar title
or a shared outside attendee) just gains the invite's attendees. A later calendar event or Granola note takes over an
invite row the same way (crm/meetings.py). A cancellation removes an invite row that nothing else has claimed.
Invites with nobody from outside ACMI are left to the calendar sync.
"""
import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction

from . import meetings, needs_filing
from .models import Meeting

# Outlook writes Windows zone names in TZID.
WINDOWS_ZONES = {
    "eastern standard time": "America/New_York", "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver", "pacific standard time": "America/Los_Angeles",
    "us mountain standard time": "America/Phoenix", "utc": "UTC", "gmt standard time": "Europe/London",
}
# The zone Google prints after an "Invitation:" subject.
ABBREVIATIONS = {"EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7, "UTC": 0,
                 "GMT": 0}
INVITATION = re.compile(
    r"^(?:(?:fwd?|fw|re)\s*:\s*)*(?:updated\s+)?invitation(?:\s+from\s+google\s+calendar)?\s*:\s*(?P<title>.+?)\s+@\s+"
    r"(?P<when>(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+[a-z]{3,9}\.?\s+\d{1,2},\s+\d{4}\s+.+?)"
    r"(?:\s*\((?P<tz>[A-Z]{2,5})\))?(?:\s*\([^()]*@[^()]*\))?\s*$", re.I)
WHEN = re.compile(r"(?P<date>[a-z]{3,9}\.?\s+\d{1,2},\s+\d{4})\s+(?P<t1>\d{1,2}(?::\d{2})?\s*[ap]m)"
                  r"(?:\s*[-–]\s*(?P<t2>\d{1,2}(?::\d{2})?\s*[ap]m))?", re.I)
EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def is_invitation(subject):
    return bool(INVITATION.match(subject or ""))


# ---------------------------------------------------------------------------
# iCalendar (RFC 5545)
# ---------------------------------------------------------------------------


def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text or "")


def _unescape(value):
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _split_params(line):
    """NAME;P1=a;P2="b:c":value -> (NAME, {P1: a, P2: b:c}, value)."""
    head, value, quoted = [], "", False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return "", {}, ""
    parts, buf, quoted = [], "", False
    for ch in head:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _zone(tzid):
    if not tzid:
        return None
    try:
        return ZoneInfo(WINDOWS_ZONES.get(tzid.lower(), tzid))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


def _when(value, params):
    """A DTSTART / DTEND value -> aware datetime, or a date for an all-day event."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()
    utc = value.endswith("Z")
    dt = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt.replace(tzinfo=_zone(params.get("TZID")) or ZoneInfo(settings.TIME_ZONE))


def _person(value, params):
    email = re.sub(r"^mailto:", "", value.strip(), flags=re.I).lower()
    return {"name": _unescape(params.get("CN", "")).strip(), "email": email} if "@" in email else None


def parse_ics(text):
    """The events in an iCalendar text: [{uid, title, start, end, organizer, attendees, method, status}]."""
    events, ev, method, depth = [], None, "", 0
    for line in _unfold(text).splitlines():
        name, params, value = _split_params(line)
        if name == "METHOD" and ev is None:
            method = value.strip().upper()
        elif name == "BEGIN" and value.strip().upper() == "VEVENT":
            ev, depth = {"attendees": [], "organizer": None, "title": "", "uid": "", "start": None, "end": None,
                         "status": "", "method": method}, 0
        elif ev is None:
            continue
        elif name == "BEGIN":  # an alarm inside the event
            depth += 1
        elif name == "END" and depth:
            depth -= 1
        elif name == "END" and value.strip().upper() == "VEVENT":
            events.append(ev)
            ev = None
        elif depth:
            continue
        elif name == "UID":
            ev["uid"] = value.strip()
        elif name == "SUMMARY":
            ev["title"] = _unescape(value).strip()
        elif name in ("DTSTART", "DTEND"):
            try:
                ev["start" if name == "DTSTART" else "end"] = _when(value, params)
            except ValueError:
                pass
        elif name == "STATUS":
            ev["status"] = value.strip().upper()
        elif name in ("ATTENDEE", "ORGANIZER"):
            p = _person(value, params)
            if p and name == "ORGANIZER":
                ev["organizer"] = p
            elif p:
                ev["attendees"].append(p)
    return events


# ---------------------------------------------------------------------------
# Google's "Invitation:" email, when there's no calendar part
# ---------------------------------------------------------------------------


def _clock(text):
    t = text.lower().replace(" ", "")
    hour, minute = (int(x) for x in (t[:-2].split(":") + ["0"])[:2])
    if t.endswith("pm") and hour != 12:
        hour += 12
    if t.endswith("am") and hour == 12:
        hour = 0
    return hour, minute


def parse_invitation(email):
    """One event from an "Invitation: Title @ Thu Oct 29, 2026 2pm - 3pm (EDT) (who@acmigroup.com)" email, with
    every address in the headers and body as an attendee, or None."""
    m = INVITATION.match(email.subject or "")
    if not m:
        return None
    w = WHEN.search(m.group("when"))
    if not w:
        return None
    try:
        day = datetime.strptime(re.sub(r"\.", "", w.group("date"))[:3] + " " + w.group("date").split(None, 1)[1],
                                "%b %d, %Y").date()
    except ValueError:
        return None
    tz = m.group("tz")
    zone = (dt_timezone(timedelta(hours=ABBREVIATIONS[tz.upper()])) if tz and tz.upper() in ABBREVIATIONS
            else ZoneInfo(settings.TIME_ZONE))
    start = datetime(day.year, day.month, day.day, *_clock(w.group("t1")), tzinfo=zone)
    end = None
    if w.group("t2"):
        end = datetime(day.year, day.month, day.day, *_clock(w.group("t2")), tzinfo=zone)
        if end <= start:
            end += timedelta(days=1)
    people, names = [], {a: n for n, a in email.to + email.cc}
    names.setdefault(email.from_addr, email.from_name)
    for addr in [email.from_addr, *names, *EMAIL.findall(email.body or "")]:
        addr = addr.lower().strip(".")
        if addr and addr not in {p["email"] for p in people} and not addr.startswith(("calendar-notification",
                                                                                         "noreply", "no-reply")):
            people.append({"name": names.get(addr, ""), "email": addr})
    title = m.group("title").strip()
    return {"uid": f"{start:%Y%m%dT%H%M}-{meetings._norm(title)[:80]}", "title": title, "start": start, "end": end,
            "organizer": None, "attendees": people, "method": "REQUEST", "status": ""}


# ---------------------------------------------------------------------------
# Onto the calendar
# ---------------------------------------------------------------------------


def events_in(email):
    """The events an email carries: its calendar parts, else its "Invitation:" subject."""
    events = []
    for text in getattr(email, "calendar", None) or []:
        events += parse_ics(text)
    if not events:
        ev = parse_invitation(email)
        if ev:
            events.append(ev)
    return events


def from_email(email, mailbox=None):
    """Put the invites in an email on the calendar. Returns (meetings added, meetings merged into)."""
    added = merged = 0
    for ev in events_in(email):
        result = save(ev, mailbox.user if mailbox else None)
        added += result == "added"
        merged += result == "merged"
    return added, merged


def _people(ev):
    out, seen = [], set()
    for p in ([ev["organizer"]] if ev.get("organizer") else []) + ev["attendees"]:
        if p["email"] not in seen:
            seen.add(p["email"])
            out.append(p)
    return out


@transaction.atomic
def save(ev, owner=None):
    """One parsed event -> the calendar: "added", "merged", "updated", "cancelled" or "" (nothing to do)."""
    start, uid = ev.get("start"), (ev.get("uid") or "").strip()
    if not uid or not isinstance(start, datetime):
        return ""  # all-day and undated events aren't meetings
    ext = f"invite:{uid}"[:255]
    people = _people(ev)
    if ev.get("method") == "CANCEL" or ev.get("status") == "CANCELLED":
        return "cancelled" if Meeting.objects.filter(external_id=ext, source="invite").delete()[0] else ""
    if not meetings._outside(people):
        return ""
    end = ev.get("end") if isinstance(ev.get("end"), datetime) else None
    title = (ev.get("title") or "Meeting")[:500]
    event_id = uid.split("@", 1)[0]  # Google's UID is <event id>@google.com
    meeting = (Meeting.objects.filter(external_id=ext).first()
               or Meeting.objects.filter(external_id=f"calendar:{event_id}").first()
               or Meeting.objects.filter(calendar_id=event_id).first())
    if meeting is None and ev.get("method") in ("REPLY", "COUNTER"):
        return ""  # someone's answer to an invite: only ever adds to a meeting already there
    if meeting is None:
        window = timedelta(minutes=15)
        near = Meeting.objects.filter(start__gte=start - window, start__lte=start + window)
        meeting = next((m for m in near if meetings.same_invite(m, title, people)), None)
    if meeting is not None and meeting.source != "invite":
        before = len(meeting.attendees or [])
        meeting.attendees = meetings._merge_attendees(people, meeting.attendees, keep=True)
        if len(meeting.attendees) != before:
            meeting.save(update_fields=["attendees", "updated_at"])
        result = "merged"
    else:
        result = "updated" if meeting else "added"
        meeting = meeting or Meeting(source="invite", external_id=ext)
        meeting.title, meeting.start, meeting.end = title, start, end
        meeting.attendees = meetings._merge_attendees(people, meeting.attendees, keep=True)
        meeting.save()
    team = meetings._team_lookup()
    users = {team[p["email"]] for p in people if p["email"] in team}
    if owner is not None:
        users.add(owner)
    meeting.team.add(*users)
    needs_filing.link(meeting)
    return result
