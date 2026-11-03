# Scheduling: see when people are free and put a meeting on the calendar (built 2026-10-29, branch `schedule`)

Kellen (2026-10-29): "what about being able to schedule a meeting with someone else and seeing their availability. I know I can
do that within Google Calendar right now." The hub now does the same: **Calendar > ＋ Schedule a meeting**, or **📅 Schedule**
on a person or company page. Pick people, see one week of when they are free, click a time, and Google Calendar sends the
invitations (with a Meet link if you want one). The meeting also lands on the hub's Calendar and on the company's timeline
straight away, as if the 15-minute calendar sync had already found it.

## What it does

- **Who**: every teammate as a tick box, your own calendar always included, plus anyone else: type a name or email in
  "Add someone" (teammates and people in the CRM are suggested; any address works). From a person page the person is already
  ticked and the title is "Meeting with <name>"; from a company page the title is "ACMI / <company>" and the company's
  contacts are offered as "+ Name" buttons.
- **When**: a week of half-hour cells (Monday to Friday, or the weekend too), shaded **green** when everyone you can see is
  free for the meeting's whole length, **amber** with a number when some are busy (hover for who), **grey** when it is past or
  won't fit in the day. Length (15 minutes to 2 hours), the hours the day runs (8 am to 6 pm by default) and the week are
  yours to change. **Times everyone can do** lists the first good times, spread through each day.
- **Outside people** (a tenant, a lender): their free/busy shows only if they shared their calendar with you; otherwise the
  person is marked "can't see their calendar" and the times are for the people you can see. The meeting is still sent to them.
- **The meeting form** opens when you click a cell (or a suggested time): title, day, start, length, place, a Google Meet
  link (on by default), notes, who is invited, and a note saying who is busy then. **Send invitations** makes the event on
  *your* Google Calendar and Google emails everyone. **Open in Google Calendar** instead fills Google's own new-event screen
  with the same details, which needs no permission from the hub.
- Press Send twice, or again after a network hiccup, and one meeting is made: the page's request id is also the event's id,
  so Google answers "already exists" and the hub shows the first one.

## The one new permission

Seeing when people are busy needs nothing new: the Calendar permission everyone gave when they connected covers Google's
free/busy question. Making the meeting is a write (`https://www.googleapis.com/auth/calendar.events`), so each person
allows it once with **Allow scheduling** (the same connect flow as Mail and Chat; Google's screen lists the new permission
and keeps the others). Until then the page shows the "Allow scheduling" bar, hides Send, and **Open in Google Calendar**
still works. If Google later says the permission is gone (the person removed the hub's access), the bar comes back.
Nothing here can touch anyone else's calendar: every call uses the signed-in person's own permission, so the hub sees
what they could see in Google Calendar and writes only to their own.

## How it's built

- `crm/gcal.py`: `freebusy(user, emails, start, end)` (Google's free/busy question, 50 calendars at a time, a calendar Google
  won't show comes back with a plain-words problem), `create_event(...)` (primary calendar, `sendUpdates=all`, Meet through
  `conferenceData`, a caller-chosen event id so a repeat finds the first), `granted_scopes`, and `ScopeMissing` for "the
  permission isn't there". Google's refusals are said in words (`_check`).
- `crm/schedule/slots.py`: the rules, with no Google and no database: busy times to half-hour cells for each person, "could a
  meeting of this length start here", suggestions. Every rule has a test.
- `crm/schedule/service.py`: teammates and people search, a week of availability (busy times cached 60 seconds per person-set and
  week, so changing the length or weeks asks Google once; a new meeting clears it), checking the form, Google's own
  new-event link, making the meeting and filing it with `meetings.import_meetings` (the organizer is added if Google leaves
  them out, since a meeting needs two people to be filed).
- `crm/schedule/connect.py`: the write permission (`CalendarSync.can_schedule`, migration `0075`).
- `crm/schedule/views.py` and `templates/crm/schedule.html`, `static/crm/schedule.js`, styles at the end of `app.css`: the page
  and its JSON calls (`/schedule/`, `availability/`, `people/`, `create/`, `open/`, `allow/`, `allowed/`). Team only.
- Nothing from Google or the CRM is written to the page as HTML; the script builds the page with text nodes.
- 55 tests in `crm/tests/test_schedule.py` with a fake Google Calendar (`FakeCalendar`).

## What Google needs (Mel): `docs/FOR_MEL_SCHEDULING.md`

Almost nothing: the new permission listed on the consent screen, and the Workspace calendar sharing setting that lets
teammates see each other's free/busy. **The first real "Allow scheduling" and the first real invitation are what prove it**:
nothing here has run against real Google.

## Checked, and not checked

Checked in the in-app browser on a scratch database with a fake Google Calendar (never the real one): the week grid with busy
times from several people, suggested times, a person whose calendar can't be seen, adding people by search and from a company,
picking a time, the form, sending (result with Meet and Calendar links), the meeting appearing on the hub Calendar and the
company's Meetings tab, the permission bar and "Open in Google Calendar", the permission being taken away while the page was
open (found and fixed there: the bar didn't come back), a deep link to a week, dark mode and a 375 px phone.

**Not checked:** anything against real Google (the consent screen, free/busy for real teammates and outside people, the real
Meet link, the invitation emails), Safari, Firefox, a real phone.

## Not built yet

Recurring meetings; booking a room; seeing event titles (Google's free/busy answer has none, by design); each person's own
working hours and time zone (the grid uses the hub's time zone and one day window); editing or cancelling a meeting from the
hub (do it in Google Calendar); scheduling from a box in the mail dock; a booking link for outside people to pick a time;
RSVP status of the people invited.
