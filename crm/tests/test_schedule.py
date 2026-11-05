"""Scheduling in the hub (docs/SCHEDULING.md): the rules that turn busy times into a week of cells and suggested times,
the Google Calendar calls (free/busy and creating a meeting), the permission to write to a calendar, and the pages.
A fake Google Calendar stands in for Google."""
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import gcal
from crm.models import CalendarSync, Company, Meeting, Person
from crm.permissions import PARTNERS
from crm.schedule import connect, service, slots

from .test_gmail_api import ME, ON, FakeResponse
from .test_mail_actions import connected_user

CT = ZoneInfo("America/Chicago")
MON = date(2026, 11, 2)  # DST ended the day before, so Central is UTC-6
SIMON, WILL = "sshewmaker@acmigroup.com", "will@firehawkaerospace.com"
FULL_SCOPES = f"openid {gcal.SCOPE} {gcal.SCHEDULE_SCOPE}"


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CT)


def utc(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def google_error(status, message, reason="", state=""):
    return status, {"error": {"code": status, "message": message, "errors": [{"reason": reason}] if reason else [],
                              "status": state}}


# ---------------------------------------------------------------------------
# The rules (no Google, no database)
# ---------------------------------------------------------------------------


def people(*emails):
    return [{"email": e, "name": e.split("@")[0].title()} for e in emails]


def week(busy=None, who=("me@acmigroup.com", SIMON), minutes=30, now=None, hours=(8, 18), ndays=5):
    return slots.availability(people(*who), busy or {}, MON, ndays, hours, minutes, now or at(MON - timedelta(days=1), 8), CT)


def blocks(*spans, problem=""):
    return {"busy": list(spans), "problem": problem}


class SlotTests(SimpleTestCase):
    def test_each_person_gets_their_own_busy_blocks_joined_where_they_touch(self):
        a = week({SIMON: blocks((at(MON, 9), at(MON, 10)), (at(MON, 10), at(MON, 11)), (at(MON, 13), at(MON, 14)))})
        simon = a["people"][1]
        self.assertEqual(len(simon["busy"]), 2)  # 9-11 joined, 1-2 apart
        self.assertEqual(a["people"][0]["busy"], [])

    def test_a_busy_hour_blocks_its_two_half_hours_for_that_person_only(self):
        a = week({SIMON: blocks((at(MON, 9), at(MON, 10)))})
        monday = a["cells"][0]
        self.assertEqual([monday[s]["busy"] for s in (1, 2, 3, 4)], [[], [1], [1], []])  # 8:30, 9:00, 9:30, 10:00
        self.assertEqual(a["cells"][1][2]["busy"], [])  # Tuesday is clear

    def test_a_short_call_blocks_the_whole_half_hour(self):
        a = week({SIMON: blocks((at(MON, 9, 10), at(MON, 9, 25)))})
        self.assertEqual([a["cells"][0][s]["busy"] for s in (1, 2, 3)], [[], [1], []])

    def test_a_longer_meeting_is_blocked_by_anything_inside_it(self):
        a = week({SIMON: blocks((at(MON, 9), at(MON, 9, 30)))}, minutes=60)
        monday = a["cells"][0]
        self.assertEqual([monday[s]["busy"] for s in (0, 1, 2, 3)], [[], [1], [1], []])  # 8:00 ends before 9; 8:30 and 9:00 run into it

    def test_a_meeting_must_fit_in_the_day_and_not_start_in_the_past(self):
        a = week(minutes=60)
        self.assertTrue(a["cells"][0][18]["ok"])  # 5:00 pm to 6:00 pm
        self.assertFalse(a["cells"][0][19]["ok"])  # 5:30 pm would run past 6
        a = week(now=at(MON, 10, 15))
        self.assertEqual([a["cells"][0][s]["ok"] for s in (3, 4, 5)], [False, False, True])  # 9:30 and 10:00 have started
        self.assertFalse(any(c["ok"] for c in week(now=at(MON + timedelta(days=7), 8))["cells"][0]))

    def test_times_come_from_google_in_utc_and_land_in_the_hubs_time_zone(self):
        nine = datetime(2026, 11, 2, 15, 0, tzinfo=dt_timezone.utc)  # 9:00 am Central
        a = week({SIMON: blocks((nine, nine + timedelta(hours=1)))})
        self.assertEqual([a["cells"][0][s]["busy"] for s in (1, 2, 3, 4)], [[], [1], [1], []])

    def test_a_calendar_that_could_not_be_read_is_flagged_and_never_counted_busy(self):
        a = week({SIMON: blocks((at(MON, 9), at(MON, 10)), problem="Not shared with you.")})
        self.assertEqual(a["people"][1]["problem"], "Not shared with you.")
        self.assertEqual(a["people"][0]["problem"], "")
        self.assertTrue(all(not c["busy"] for day in a["cells"] for c in day))

    def test_suggestions_are_the_first_free_times_spread_through_each_day(self):
        a = week()
        self.assertEqual([s["time"] for s in a["suggestions"][:3]], ["08:00", "10:00", "12:00"])
        self.assertEqual(len(a["suggestions"]), slots.SUGGESTIONS)
        self.assertEqual(a["suggestions"][3]["day"], "2026-11-03")
        self.assertEqual(a["suggestions"][0]["label"], "Mon, Nov 2 · 8:00–8:30 AM")

    def test_suggestions_skip_whatever_anyone_is_busy_for(self):
        a = week({SIMON: blocks((at(MON, 8), at(MON, 10)))})
        self.assertEqual([s["time"] for s in a["suggestions"][:3]], ["10:00", "12:00", "14:00"])

    def test_a_suggestion_shows_the_meetings_own_length(self):
        label = week(minutes=45)["suggestions"][0]["label"]
        self.assertEqual(label, "Mon, Nov 2 · 8:00–8:45 AM")

    def test_the_week_can_show_the_weekend_and_marks_today_and_the_past(self):
        a = week(ndays=7, now=at(MON + timedelta(days=1), 12))
        self.assertEqual([d["short"] for d in a["days"]], ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        self.assertEqual([(d["today"], d["past"]) for d in a["days"][:3]], [(False, True), (True, False), (False, False)])
        self.assertEqual(a["days"][0], {"date": "2026-11-02", "label": "Mon, Nov 2", "short": "Mon", "num": "Nov 2",
                                        "today": False, "past": True})
        self.assertEqual(a["zone"], "CST")

    def test_the_day_shown_is_the_one_asked_for(self):
        a = week(hours=(9, 12))
        self.assertEqual([s["time"] for s in a["slots"]], ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"])
        self.assertEqual(a["slots"][0]["label"], "9:00 AM")
        self.assertEqual([s["hour"] for s in a["slots"]][:3], [True, False, True])

    def test_small_helpers(self):
        self.assertEqual(slots.span(at(MON, 14), at(MON, 14, 30)), "2:00–2:30 PM")
        self.assertEqual(slots.span(at(MON, 11, 30), at(MON, 12, 30)), "11:30 AM–12:30 PM")
        self.assertEqual((slots.clock(at(MON, 0, 5)), slots.clock(at(MON, 12)), slots.clock(at(MON, 13, 45))),
                         ("12:05 AM", "12:00 PM", "1:45 PM"))
        self.assertEqual([slots.cells_for(m) for m in (15, 30, 31, 60, 90)], [1, 1, 2, 2, 3])
        self.assertEqual(slots.monday(date(2026, 11, 8)), MON)  # a Sunday belongs to the week before it
        self.assertEqual((slots.initials("Simon Shewmaker"), slots.initials("", "will@x.com"), slots.initials("Cher")),
                         ("SS", "WI", "CH"))


# ---------------------------------------------------------------------------
# A fake Google Calendar
# ---------------------------------------------------------------------------


class FakeCalendar:
    """Stands in for `requests` against Google Calendar: free/busy answers, events.insert and events.get, tokeninfo."""

    def __init__(self, scope=FULL_SCOPES):
        self.busy, self.missing, self.events = {}, set(), {}
        self.freebusy_calls, self.inserts, self.calls = [], [], []
        self.fail = None  # (status, body) answered to every free/busy and insert call
        self.scope, self.meet = scope, True

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url))
        if url == gcal.TOKENINFO_URL:
            return FakeResponse({"scope": self.scope})
        if url.startswith(gcal.EVENTS_URL + "/"):
            event = self.events.get(url.rsplit("/", 1)[1])
            return FakeResponse(event) if event else FakeResponse(*google_error(404, "Not found")[::-1])
        return FakeResponse({}, 404)

    def post(self, url, params=None, json=None, headers=None, timeout=None, data=None):
        self.calls.append(("POST", url))
        if url == gcal.TOKEN_URL:
            return FakeResponse({"access_token": "fresh", "expires_in": 3600})
        if self.fail:
            return FakeResponse(self.fail[1], self.fail[0])
        if url == gcal.FREEBUSY_URL:
            self.freebusy_calls.append(json)
            calendars = {}
            for item in json["items"]:
                email = item["id"]
                if email in self.missing:
                    calendars[email] = {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}
                else:
                    calendars[email] = {"busy": [{"start": utc(s), "end": utc(e)} for s, e in self.busy.get(email, [])]}
            return FakeResponse({"kind": "calendar#freeBusy", "timeMin": json["timeMin"], "timeMax": json["timeMax"],
                                 "calendars": calendars})
        if url == gcal.EVENTS_URL:
            self.inserts.append({"params": params, "body": json})
            if json["id"] in self.events:
                return FakeResponse(*google_error(409, "The requested identifier already exists.", "duplicate")[::-1])
            event = {"id": json["id"], "status": "confirmed", "summary": json["summary"],
                     "htmlLink": f"https://www.google.com/calendar/event?eid={json['id']}",
                     "start": json["start"], "end": json["end"],
                     "attendees": [{"email": ME, "organizer": True, "self": True, "responseStatus": "accepted"}]
                     + [{"email": a["email"], "responseStatus": "needsAction"} for a in json["attendees"]]}
            if "conferenceData" in json and self.meet:
                event["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
            self.events[json["id"]] = event
            span = (datetime.fromisoformat(json["start"]["dateTime"]), datetime.fromisoformat(json["end"]["dateTime"]))
            for who in [ME] + [a["email"] for a in json["attendees"]]:
                self.busy.setdefault(who, []).append(span)
            return FakeResponse(event)
        return FakeResponse({}, 404)


@override_settings(**{**ON, "TIME_ZONE": "America/Chicago"})
class ScheduleTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.user.first_name, self.user.last_name = "Kellen", "Moczulski"
        self.user.save()
        self.simon = User.objects.create_user("simon", password="x", email=SIMON, first_name="Simon", last_name="Shewmaker")
        self.fake = FakeCalendar()
        patcher = mock.patch.object(gcal, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.user)
        self.start = timezone.now().astimezone(CT).date() + timedelta(days=21)
        self.monday = slots.monday(self.start)

    def allow(self):
        CalendarSync.objects.update_or_create(user=self.user, defaults={"can_schedule": True})

    def form(self, **extra):
        day = self.monday + timedelta(days=1)  # a Tuesday, three weeks out
        data = {"title": "Site tour", "start": f"{day.isoformat()}T14:00", "minutes": "30", "attendees": f"{WILL},{SIMON}",
                "location": "", "description": "", "meet": "1", "request": "a" * 32}
        data.update(extra)
        return data


# ---------------------------------------------------------------------------
# The Google calls
# ---------------------------------------------------------------------------


class CalendarCallTests(ScheduleTestCase):
    def ask(self, emails):
        start = at(self.monday, 0)
        return gcal.freebusy(self.user, emails, start, start + timedelta(days=7))

    def test_busy_times_come_back_per_calendar_and_unseen_calendars_are_named(self):
        self.fake.busy[SIMON] = [(at(self.monday, 9), at(self.monday, 10))]
        self.fake.missing.add("ghost@acmigroup.com")
        got = self.ask([ME, "SSHEWMAKER@acmigroup.com", "ghost@acmigroup.com", ME])
        self.assertEqual(list(got), [ME, SIMON, "ghost@acmigroup.com"])  # lower-cased, once each
        self.assertEqual(got[SIMON]["busy"], [(at(self.monday, 9), at(self.monday, 10))])
        self.assertEqual(got[SIMON]["problem"], "")
        self.assertEqual(got[ME], {"busy": [], "problem": ""})
        self.assertIn("isn't shared", got["ghost@acmigroup.com"]["problem"])
        self.assertEqual(got["ghost@acmigroup.com"]["busy"], [])
        sent = self.fake.freebusy_calls[0]
        self.assertEqual([i["id"] for i in sent["items"]], [ME, SIMON, "ghost@acmigroup.com"])
        self.assertTrue(sent["timeMin"].endswith("-06:00"))  # an aware time Google can read

    def test_more_than_fifty_calendars_are_asked_in_several_questions(self):
        got = self.ask([f"p{n}@acmigroup.com" for n in range(120)])
        self.assertEqual(len(got), 120)
        self.assertEqual([len(c["items"]) for c in self.fake.freebusy_calls], [50, 50, 20])

    def test_google_refusals_are_said_in_words(self):
        self.fake.fail = google_error(403, "Request had insufficient authentication scopes.", "insufficientPermissions")
        with self.assertRaises(gcal.ScopeMissing):
            self.ask([ME])
        self.fake.fail = google_error(403, "Rate Limit Exceeded", "rateLimitExceeded")
        with self.assertRaisesMessage(gcal.CalendarError, "busy right now"):
            self.ask([ME])
        self.fake.fail = google_error(429, "Too many requests")
        with self.assertRaisesMessage(gcal.CalendarError, "busy right now"):
            self.ask([ME])
        self.fake.fail = google_error(401, "Invalid Credentials")
        with self.assertRaisesMessage(gcal.CalendarError, "Reconnect Google Calendar"):
            self.ask([ME])
        self.fake.fail = google_error(500, "Backend Error")
        with self.assertRaises(requests.HTTPError):
            self.ask([ME])

    def test_without_a_google_connection_it_says_so(self):
        loner = User.objects.create_user("loner", password="x", email="l@acmigroup.com")
        with self.assertRaisesMessage(gcal.CalendarError, "Connect Google Calendar"):
            gcal.freebusy(loner, [ME], at(MON, 0), at(MON, 23))

    def make(self, **extra):
        args = dict(event_id="hub" + "b" * 32, title="Site tour", start=at(self.monday, 14), end=at(self.monday, 14, 30),
                    attendees=[WILL, SIMON], description="Walk the site", location="Hangar 2", meet=True)
        args.update(extra)
        return gcal.create_event(self.user, **args)

    def test_a_meeting_is_made_on_the_primary_calendar_with_invitations_and_a_meet_link(self):
        event, created = self.make()
        self.assertTrue(created)
        insert = self.fake.inserts[0]
        self.assertEqual(insert["params"], {"sendUpdates": "all", "conferenceDataVersion": "1"})
        body = insert["body"]
        self.assertEqual(body["id"], "hub" + "b" * 32)
        self.assertEqual((body["summary"], body["description"], body["location"]), ("Site tour", "Walk the site", "Hangar 2"))
        self.assertEqual(body["start"], {"dateTime": at(self.monday, 14).isoformat(), "timeZone": "America/Chicago"})
        self.assertEqual([a["email"] for a in body["attendees"]], [WILL, SIMON])
        self.assertEqual(body["conferenceData"]["createRequest"]["conferenceSolutionKey"], {"type": "hangoutsMeet"})
        self.assertEqual(event["hangoutLink"], "https://meet.google.com/abc-defg-hij")

    def test_no_meet_link_when_it_isnt_wanted(self):
        self.make(meet=False)
        self.assertNotIn("conferenceData", self.fake.inserts[0]["body"])

    def test_asking_twice_for_the_same_meeting_finds_the_first(self):
        first, made = self.make()
        again, made_again = self.make()
        self.assertEqual((made, made_again), (True, False))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.fake.events), 1)

    def test_a_meeting_deleted_in_google_cannot_be_made_again_under_the_same_request(self):
        self.make()
        self.fake.events["hub" + "b" * 32]["status"] = "cancelled"
        with self.assertRaisesMessage(gcal.CalendarError, "cancelled in Google Calendar"):
            self.make()

    def test_writing_without_the_permission_is_its_own_error(self):
        self.fake.fail = google_error(403, "Request had insufficient authentication scopes.", "insufficientPermissions",
                                      "PERMISSION_DENIED")
        with self.assertRaises(gcal.ScopeMissing):
            self.make()
        self.fake.fail = google_error(400, "Invalid attendee email.", "invalid")
        with self.assertRaisesMessage(gcal.CalendarError, "Invalid attendee email"):
            self.make()

    def test_granted_scopes_are_read_from_google(self):
        self.assertIn(gcal.SCHEDULE_SCOPE, gcal.granted_scopes(self.user))
        self.fake.scope = f"openid {gcal.SCOPE}"
        self.assertNotIn(gcal.SCHEDULE_SCOPE, gcal.granted_scopes(self.user))


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ServiceTests(ScheduleTestCase):
    def test_emails_are_checked_lowered_and_counted(self):
        self.assertEqual(service.clean_emails(" A@x.com, b@y.org;a@x.com\n<c@z.io> ", 5), ["a@x.com", "b@y.org", "c@z.io"])
        self.assertEqual(service.clean_emails([], 5), [])
        with self.assertRaisesMessage(service.ScheduleError, "isn't an email address"):
            service.clean_emails("fine@x.com, nope", 5)
        with self.assertRaisesMessage(service.ScheduleError, "too many"):
            service.clean_emails("a@x.com,b@x.com,c@x.com", 2)

    def test_the_team_is_acmi_people_with_the_signed_in_person_first(self):
        partner = User.objects.create_user("p", password="x", email="p@acmigroup.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        User.objects.create_user("outside", password="x", email="o@gmail.com")
        User.objects.create_user("noemail", password="x")
        User.objects.create_user("gone", password="x", email="gone@acmigroup.com", is_active=False)
        User.objects.create_user("abe", password="x", email="abe@acmigroup.com", first_name="Abe", last_name="Zed")
        self.assertEqual([(t["name"], t["me"]) for t in service.team(self.user)],
                         [("Kellen Moczulski", True), ("Abe Zed", False), ("Simon Shewmaker", False)])

    def test_searching_finds_teammates_crm_people_and_a_typed_address(self):
        company = Company.objects.create(name="Firehawk Aerospace")
        Person.objects.create(first_name="Will", last_name="Edwards", email=WILL, company=company, title="CEO")
        Person.objects.create(first_name="No", last_name="Email")
        found = {r["email"]: r for r in service.search_people(self.user, "wil")}
        self.assertEqual(found[WILL]["detail"], "Firehawk Aerospace")
        self.assertEqual(service.search_people(self.user, "sim")[0], {"email": SIMON, "name": "Simon Shewmaker", "detail": "Teammate"})
        self.assertEqual(service.search_people(self.user, "new@client.com"),
                         [{"email": "new@client.com", "name": "new@client.com", "detail": "Use this address"}])
        self.assertEqual(service.search_people(self.user, "s"), [])

    def test_a_week_has_the_signed_in_person_first_and_names_from_the_crm(self):
        company = Company.objects.create(name="Firehawk Aerospace")
        Person.objects.create(first_name="Will", last_name="Edwards", email=WILL, company=company)
        self.fake.busy[SIMON] = [(at(self.monday + timedelta(days=1), 9), at(self.monday + timedelta(days=1), 10))]
        week_ = service.availability(self.user, [SIMON, WILL, ME], self.start.isoformat())
        self.assertEqual([p["email"] for p in week_["people"]], [ME, SIMON, WILL])
        self.assertEqual([p["name"] for p in week_["people"]], ["Kellen Moczulski", "Simon Shewmaker", "Will Edwards"])
        self.assertEqual(week_["start"], self.monday.isoformat())
        self.assertEqual(week_["next"], (self.monday + timedelta(days=7)).isoformat())
        self.assertEqual(week_["cells"][1][2]["busy"], [1])  # Tuesday 9:00: Simon
        self.assertEqual(len(week_["days"]), 5)
        self.assertEqual(len(service.availability(self.user, [], self.start.isoformat(), ndays=7)["days"]), 7)

    def test_a_meeting_just_made_shows_as_busy_for_everyone_invited(self):
        self.allow()
        before = service.availability(self.user, [SIMON], self.start.isoformat())
        service.create_meeting(self.user, self.form())  # Tuesday 2:00 pm, with Will and Simon
        after = service.availability(self.user, [SIMON], self.start.isoformat())
        cell = (1, 12)  # Tuesday, 2:00 pm in a day that starts at 8
        self.assertEqual((before["cells"][cell[0]][cell[1]]["busy"], after["cells"][cell[0]][cell[1]]["busy"]), ([], [0, 1]))

    def test_a_week_is_asked_of_google_once_for_a_minute_and_again_after_a_meeting_is_made(self):
        service.availability(self.user, [SIMON], self.start.isoformat())
        service.availability(self.user, [SIMON], self.start.isoformat(), minutes=60)  # a different length: same busy times
        self.assertEqual(len(self.fake.freebusy_calls), 1)
        service.availability(self.user, [SIMON, WILL], self.start.isoformat())  # other people: a new question
        self.assertEqual(len(self.fake.freebusy_calls), 2)
        self.allow()
        service.create_meeting(self.user, self.form())
        service.availability(self.user, [SIMON], self.start.isoformat())
        self.assertEqual(len(self.fake.freebusy_calls), 3)

    def test_the_hours_lengths_and_weeks_asked_for_are_kept_sane(self):
        self.assertEqual(service.hours_from("9", "17"), (9, 17))
        self.assertEqual(service.hours_from("17", "9"), slots.DEFAULT_HOURS)
        self.assertEqual(service.hours_from("x", None), slots.DEFAULT_HOURS)
        self.assertEqual((service.minutes_from("45"), service.minutes_from("0"), service.minutes_from("9999"),
                          service.minutes_from("abc")), (45, 30, 30, 30))
        self.assertEqual(service.week_start("2026-11-04"), MON)
        self.assertEqual(service.week_start("garbage", now=at(MON + timedelta(days=3), 12)), MON)

    def test_the_form_is_checked_before_anything_goes_to_google(self):
        for change, words in [({"title": "   "}, "title"), ({"title": "x" * 201}, "too long"), ({"start": "soon"}, "day and a time"),
                              ({"minutes": "2"}, "5 minutes"), ({"attendees": ""}, "at least one"),
                              ({"attendees": ME}, "at least one"), ({"attendees": "nope"}, "isn't an email"),
                              ({"start": "2020-01-01T09:00"}, "passed"),
                              ({"start": f"{self.monday + timedelta(days=400)}T09:00"}, "year away"),
                              ({"description": "x" * 4001}, "too long")]:
            with self.subTest(change=change), self.assertRaisesMessage(service.ScheduleError, words):
                service.meeting_fields(self.user, self.form(**change))
        fields = service.meeting_fields(self.user, self.form(attendees=f" {WILL.upper()}, {ME}, {WILL}", title="  Site   tour "))
        self.assertEqual((fields["title"], fields["attendees"]), ("Site tour", [WILL]))  # not the person, once each
        self.assertEqual(fields["start"].tzinfo.key, "America/Chicago")
        self.assertEqual(fields["end"] - fields["start"], timedelta(minutes=30))
        self.assertTrue(fields["meet"])
        self.assertFalse(service.meeting_fields(self.user, self.form(meet=""))["meet"])

    def test_the_google_calendar_link_is_the_form_filled_in(self):
        link = service.google_link(service.meeting_fields(self.user, self.form(location="Hangar 2", description="Walk it")))
        self.assertTrue(link.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE&text=Site+tour"))
        day = self.monday + timedelta(days=1)
        starts = at(day, 14).astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ends = at(day, 14, 30).astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.assertIn(f"dates={starts}%2F{ends}", link)
        self.assertIn("add=will%40firehawkaerospace.com%2Csshewmaker%40acmigroup.com", link)
        self.assertIn("location=Hangar+2", link)
        self.assertIn("ctz=America%2FChicago", link)

    def test_a_meeting_is_made_invited_and_filed_on_the_hubs_calendar_and_the_companys_timeline(self):
        company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        Person.objects.create(first_name="Will", last_name="Edwards", email=WILL, company=company)
        self.allow()
        result = service.create_meeting(self.user, self.form(description="Walk the site"))
        self.assertTrue(result["ok"] and result["created"])
        self.assertEqual(result["invited"], [WILL, SIMON])
        self.assertEqual(result["meet"], "https://meet.google.com/abc-defg-hij")
        self.assertTrue(result["link"].startswith("https://www.google.com/calendar/event"))
        self.assertRegex(result["when"], r"^\w{3}, \w{3} \d{1,2} · 2:00–2:30 PM C[SD]T$")
        meeting = Meeting.objects.get(external_id="calendar:hub" + "a" * 32)
        self.assertEqual(result["meeting"], meeting.get_absolute_url())
        self.assertEqual((meeting.title, meeting.source), ("Site tour", "calendar"))
        self.assertEqual(list(meeting.companies.all()), [company])
        self.assertIn(self.user, meeting.team.all())
        self.assertIn(self.simon, meeting.team.all())
        self.assertEqual(self.fake.inserts[0]["body"]["id"], "hub" + "a" * 32)

    def test_pressing_send_twice_makes_one_meeting(self):
        self.allow()
        first = service.create_meeting(self.user, self.form())
        second = service.create_meeting(self.user, self.form())
        self.assertEqual((first["created"], second["created"]), (True, False))
        self.assertEqual(Meeting.objects.count(), 1)
        self.assertEqual(second["meeting"], first["meeting"])

    def test_the_request_id_must_be_the_one_the_page_made(self):
        for bad in ("", "short", "z" * 32, "A" * 33):
            with self.subTest(bad=bad), self.assertRaisesMessage(service.ScheduleError, "Reload the page"):
                service.create_meeting(self.user, self.form(request=bad))
        self.assertEqual(self.fake.inserts, [])

    def test_a_meeting_without_a_meet_link_still_files(self):
        self.fake.meet = False
        result = service.create_meeting(self.user, self.form(meet=""))
        self.assertEqual(result["meet"], "")
        self.assertEqual(Meeting.objects.count(), 1)

    def test_links_from_google_must_be_secure(self):
        self.assertEqual(service.meet_link({"conferenceData": {"entryPoints": [
            {"entryPointType": "phone", "uri": "tel:+1"}, {"entryPointType": "video", "uri": "https://meet.google.com/x"}]}}),
            "https://meet.google.com/x")
        self.assertEqual(service.meet_link({"hangoutLink": "javascript:alert(1)"}), "")
        self.assertEqual(service.meet_link({}), "")


# ---------------------------------------------------------------------------
# The permission to write to a calendar
# ---------------------------------------------------------------------------


class PermissionTests(ScheduleTestCase):
    def test_nobody_can_schedule_until_they_have_allowed_it(self):
        self.assertFalse(connect.can_schedule(self.user))
        self.allow()
        self.assertTrue(connect.can_schedule(self.user))
        self.user.socialaccount_set.all().delete()
        self.assertFalse(connect.can_schedule(self.user))  # no Google connection, no scheduling

    def test_allow_goes_to_google_for_the_calendar_events_permission_on_top_of_what_was_granted(self):
        r = self.client.post(reverse("crm:schedule_allow"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(gcal.SCHEDULE_SCOPE.replace(":", "%3A").replace("/", "%2F"), r["Location"])
        self.assertIn("include_granted_scopes", r["Location"])
        self.assertTrue(self.client.session[connect.SESSION_KEY])
        self.assertEqual(self.client.get(reverse("crm:schedule_allow")).status_code, 405)

    def test_allow_needs_a_google_connection_first(self):
        self.user.socialaccount_set.all().delete()
        r = self.client.post(reverse("crm:schedule_allow"), follow=True)
        self.assertContains(r, "Connect Google Calendar")
        self.assertNotIn(connect.SESSION_KEY, self.client.session)

    def test_finishing_checks_the_permission_really_arrived_and_the_account_is_theirs(self):
        connect.finish(self.user, FakeCalendar())
        self.assertTrue(connect.can_schedule(self.user))
        CalendarSync.objects.filter(user=self.user).update(can_schedule=False)
        with self.assertRaisesMessage(connect.ScheduleConnectError, "permission to put meetings"):
            connect.finish(self.user, FakeCalendar(scope=f"openid {gcal.SCOPE}"))
        self.assertFalse(connect.can_schedule(self.user))
        account = gcal.token_for(self.user).account
        account.extra_data = {"email": "someone@acmigroup.com"}
        account.save()
        with self.assertRaisesMessage(connect.ScheduleConnectError, "your own account"):
            connect.finish(self.user, FakeCalendar())

    def test_coming_back_from_google(self):
        s = self.client.session
        s[connect.SESSION_KEY] = True
        s.save()
        with mock.patch.object(connect, "finish", return_value=None) as finish:
            r = self.client.get(reverse("crm:schedule_allowed"), follow=True)
        self.assertContains(r, "Meetings you schedule here go on your Google Calendar")
        finish.assert_called_once()
        self.client.get(reverse("crm:schedule_allowed"))  # a second visit does nothing
        self.assertEqual(finish.call_count, 1)
        s = self.client.session
        s[connect.SESSION_KEY] = True
        s.save()
        with mock.patch.object(connect, "finish", side_effect=connect.ScheduleConnectError("Google said no.")):
            r = self.client.get(reverse("crm:schedule_allowed"), follow=True)
        self.assertContains(r, "Google said no.")


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------


class PageTests(ScheduleTestCase):
    def test_the_page_opens_with_the_team_and_what_it_needs(self):
        r = self.client.get(reverse("crm:schedule"))
        self.assertEqual(r.status_code, 200)
        config = r.context["config"]
        self.assertEqual(config["me"], ME)
        self.assertEqual([t["email"] for t in config["team"]], [ME, SIMON])
        self.assertEqual(config["urls"]["availability"], reverse("crm:schedule_availability"))
        self.assertFalse(config["canSchedule"])
        self.assertContains(r, 'id="sched-config"')
        self.assertContains(r, 'class="panel sched-allow" data-allow>')  # the banner shows, and there's no Send button yet
        self.assertContains(r, "data-send hidden")

    def test_once_allowed_the_page_offers_to_send_invitations(self):
        self.allow()
        r = self.client.get(reverse("crm:schedule"))
        self.assertTrue(r.context["config"]["canSchedule"])
        self.assertContains(r, 'class="panel sched-allow" data-allow hidden>')  # kept on the page, hidden, in case Google takes it back
        self.assertNotContains(r, "data-send hidden")

    def test_without_google_it_says_what_to_connect(self):
        self.user.socialaccount_set.all().delete()
        r = self.client.get(reverse("crm:schedule"))
        self.assertContains(r, "Connect in Google access")
        self.assertNotContains(r, 'id="sched-config"')
        with override_settings(GOOGLE_SIGNIN_ENABLED=False):
            self.assertContains(self.client.get(reverse("crm:schedule")), "isn't set up")

    def test_a_person_or_company_page_opens_it_with_people_and_a_title_filled_in(self):
        company = Company.objects.create(name="Firehawk Aerospace")
        will = Person.objects.create(first_name="Will", last_name="Edwards", email=WILL, company=company)
        Person.objects.create(first_name="Ann", last_name="Lee", email="ann@firehawkaerospace.com", company=company)
        config = self.client.get(reverse("crm:schedule"), {"person": will.pk}).context["config"]
        self.assertEqual(config["invited"], [{"email": WILL, "name": "Will Edwards"}])
        self.assertEqual(config["title"], "Meeting with Will Edwards")
        self.assertEqual(config["company"], "Firehawk Aerospace")
        self.assertEqual([c["email"] for c in config["contacts"]], ["ann@firehawkaerospace.com", WILL])
        config = self.client.get(reverse("crm:schedule"), {"company": company.pk}).context["config"]
        self.assertEqual((config["title"], config["invited"]), ("ACMI / Firehawk Aerospace", []))
        config = self.client.get(reverse("crm:schedule"), {"with": f"{SIMON}, junk", "title": "Lender call"}).context["config"]
        self.assertEqual(config["title"], "Lender call")
        self.assertEqual(config["invited"], [])  # one bad address and the list is ignored, never half-trusted
        config = self.client.get(reverse("crm:schedule"), {"with": f"{SIMON},{WILL}"}).context["config"]
        self.assertEqual([i["email"] for i in config["invited"]], [SIMON, WILL])
        self.assertEqual(self.client.get(reverse("crm:schedule"), {"person": "x", "company": "999999"}).status_code, 200)

    def test_availability_answers_with_the_week_the_page_draws(self):
        self.fake.busy[SIMON] = [(at(self.monday, 9), at(self.monday, 10))]
        r = self.client.get(reverse("crm:schedule_availability"),
                            {"emails": SIMON, "start": self.start.isoformat(), "minutes": "30", "days": "5", "from": "8", "to": "18"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual([p["email"] for p in d["people"]], [ME, SIMON])
        self.assertEqual(d["cells"][0][2]["busy"], [1])
        self.assertEqual(len(d["slots"]), 20)
        self.assertTrue(d["suggestions"])
        self.assertEqual(d["range"].split(",")[-1].strip(), str(self.monday + timedelta(days=4))[:4])

    def test_availability_trouble_comes_back_as_words(self):
        r = self.client.get(reverse("crm:schedule_availability"), {"emails": "not-an-address"})
        self.assertEqual((r.status_code, "isn't an email" in r.json()["error"]), (400, True))
        self.fake.fail = google_error(401, "Invalid Credentials")
        r = self.client.get(reverse("crm:schedule_availability"), {"emails": SIMON})
        self.assertEqual((r.status_code, "Reconnect" in r.json()["error"]), (400, True))
        self.fake.fail = google_error(500, "Backend Error")
        cache.clear()
        r = self.client.get(reverse("crm:schedule_availability"), {"emails": SIMON})
        self.assertEqual((r.status_code, "Couldn't reach Google Calendar" in r.json()["error"]), (502, True))

    def test_optional_guests_are_marked_optional_on_the_invite(self):
        self.allow()
        form = self.form()
        form["optional"] = SIMON
        self.assertEqual(self.client.post(reverse("crm:schedule_create"), form).status_code, 200)
        guests = {a["email"]: a for a in self.fake.inserts[0]["body"]["attendees"]}
        self.assertTrue(guests[SIMON].get("optional"))
        self.assertFalse(guests[WILL].get("optional"))

    def test_people_search(self):
        r = self.client.get(reverse("crm:schedule_people"), {"q": "sim"})
        self.assertEqual(r.json()["results"][0]["email"], SIMON)
        body = self.client.get(reverse("crm:schedule_people")).json()
        self.assertEqual(body["results"], [])
        self.assertEqual(body["team"][0]["email"], ME)  # the teammates to tick, the person first
        self.assertTrue(body["team"][0]["me"])
        self.assertIn(SIMON, [t["email"] for t in body["team"]])

    def test_scheduling_makes_the_meeting_and_answers_with_where_to_see_it(self):
        self.allow()
        r = self.client.post(reverse("crm:schedule_create"), self.form())
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["ok"] and d["created"])
        self.assertEqual(d["meet"], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(Meeting.objects.get().get_absolute_url(), d["meeting"])
        self.assertEqual(self.client.get(reverse("crm:schedule_create")).status_code, 405)

    def test_scheduling_before_allowing_it_asks_for_the_permission_and_changes_nothing(self):
        r = self.client.post(reverse("crm:schedule_create"), self.form())
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.json()["needs_permission"])
        self.assertEqual(self.fake.inserts, [])

    def test_if_google_says_the_permission_is_gone_the_page_asks_for_it_again(self):
        self.allow()
        self.fake.fail = google_error(403, "Request had insufficient authentication scopes.", "insufficientPermissions")
        r = self.client.post(reverse("crm:schedule_create"), self.form())
        self.assertEqual((r.status_code, r.json()["needs_permission"]), (403, True))
        self.assertFalse(connect.can_schedule(self.user))

    def test_scheduling_mistakes_and_google_trouble_come_back_as_words(self):
        self.allow()
        r = self.client.post(reverse("crm:schedule_create"), self.form(title=""))
        self.assertEqual((r.status_code, r.json()["error"]), (400, "Give the meeting a title."))
        self.fake.fail = google_error(400, "Invalid attendee email.", "invalid")
        r = self.client.post(reverse("crm:schedule_create"), self.form())
        self.assertEqual(r.status_code, 400)
        self.assertIn("Invalid attendee email", r.json()["error"])
        self.fake.fail = google_error(500, "Backend Error")
        r = self.client.post(reverse("crm:schedule_create"), self.form())
        self.assertEqual(r.status_code, 502)
        self.assertIn("won't invite anyone twice", r.json()["error"])
        self.assertEqual(Meeting.objects.count(), 0)

    def test_open_in_google_sends_the_person_to_calendars_own_new_event_screen(self):
        r = self.client.get(reverse("crm:schedule_open"), self.form(title="Site tour"))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("https://calendar.google.com/calendar/render?action=TEMPLATE"))
        self.assertIn("text=Site+tour", r["Location"])
        r = self.client.get(reverse("crm:schedule_open"), self.form(title=""), follow=True)
        self.assertContains(r, "Give the meeting a title.")
        self.assertEqual(self.fake.inserts, [])

    def test_signed_out_people_and_partners_get_nothing(self):
        urls = [reverse(f"crm:schedule{n}") for n in ("", "_availability", "_people", "_open")]
        self.client.logout()
        for url in urls:
            self.assertNotEqual(self.client.get(url).status_code, 200, url)
        partner = User.objects.create_user("p", password="x", email="p@bank.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(partner)
        for url in urls:
            self.assertNotEqual(self.client.get(url).status_code, 200, url)
        self.assertNotEqual(self.client.post(reverse("crm:schedule_create"), self.form()).status_code, 200)
        self.assertEqual(self.fake.inserts, [])

    def test_the_calendar_page_and_the_record_pages_link_to_it(self):
        company = Company.objects.create(name="Firehawk Aerospace")
        will = Person.objects.create(first_name="Will", last_name="Edwards", email=WILL, company=company)
        schedule = reverse("crm:schedule")
        self.assertContains(self.client.get(reverse("crm:meeting_list")), f'href="{schedule}"')
        self.assertContains(self.client.get(will.get_absolute_url()), f'href="{schedule}?person={will.pk}"')
        self.assertContains(self.client.get(company.get_absolute_url()), f'href="{schedule}?company={company.pk}"')
