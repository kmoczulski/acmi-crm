"""📅 Schedule on an email where someone settles on a time: the button, its people and time, and the scheduler opening at it
(crm/mail/schedule_link.py, crm/schedule/views.py `_pick`)."""
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import gmail_api
from crm.models import Mailbox

from .test_compose import SEND, WILL
from .test_gmail_api import ME, FakeGmail, api_message
from .test_mail_actions import connected_user
from .test_schedule import ScheduleTestCase

User = get_user_model()
SIMON = "sshewmaker@acmigroup.com"
EXAMPLE = ("Sorry. Lets shoot for 12 noon tomorrow.\n\nKellen- can you please send out a meeting planner, and include Simon.")


@override_settings(**SEND)
class ButtonTests(TestCase):
    """The reading pane."""

    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.simon = User.objects.create_user("simon", password="x", email=SIMON, first_name="Simon", last_name="Shewmaker")
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API, backfill_done=True)
        self.now = timezone.now()
        self.client.force_login(self.user)
        self.addCleanup(mock.patch.stopall)

    def thread(self, *messages):
        fake = FakeGmail(list(messages))
        mock.patch.object(gmail_api, "requests", fake).start()
        return fake

    def reader(self, thread="a1"):
        return self.client.get(reverse("crm:inbox"), {"label": "INBOX", "t": thread})

    def test_the_example_email_offers_schedule_with_the_thread_and_simon_and_the_time(self):
        self.thread(
            api_message("0a01", "a1", f"Kellen <{ME}>", WILL, "Site visit with Mark", body="Can we meet this week?",
                        when=self.now - timedelta(hours=5), labels=("SENT",)),
            api_message("0b01", "a1", WILL, f"Kellen <{ME}>", "Re: Site visit with Mark", body=EXAMPLE,
                        when=self.now - timedelta(hours=1), extra_headers=[{"name": "Cc", "value": "Pat <pat@shoreunitedbank.com>"}]))
        page = self.reader()
        s = page.context["reader"]["schedule"]
        self.assertEqual(s["kind"], "agreed")
        tomorrow = timezone.localtime(self.now - timedelta(hours=1)).date() + timedelta(days=1)
        q = parse_qs(urlsplit(s["url"]).query)
        self.assertEqual(urlsplit(s["url"]).path, reverse("crm:schedule"))
        self.assertEqual(q["when"], [f"{tomorrow.isoformat()}T12:00"])
        self.assertEqual(q["title"], ["Site visit with Mark"])  # "Re: " dropped
        self.assertEqual(q["from"], ["email"])
        people = q["with"][0].split(",")
        self.assertEqual(people, ["will@firehawkaerospace.com", "pat@shoreunitedbank.com", SIMON])  # not me; Simon by name
        self.assertContains(page, "Schedule…")
        self.assertContains(page, "settled on")

    def test_an_ordinary_email_shows_no_button(self):
        self.thread(api_message("0a01", "a1", WILL, f"Kellen <{ME}>", "PDA", body="Thanks, I will review the PDA."))
        self.assertIsNone(self.reader().context["reader"]["schedule"])
        self.assertNotContains(self.reader(), "mail-schedule")

    def test_a_time_that_has_already_passed_is_not_offered(self):
        self.thread(api_message("0a01", "a1", WILL, f"Kellen <{ME}>", "Tour", body="See you tomorrow at 2pm.",
                                when=self.now - timedelta(days=5)))
        self.assertIsNone(self.reader().context["reader"]["schedule"])

    def test_an_offer_of_days_opens_the_scheduler_without_a_picked_time(self):
        body = ("We're available Monday, November 30, or Tuesday, December 1, if you'll be in town. On either day, we're "
                "free before 11:00 AM or between 1:30 and 3:00 PM. Would either time work for you?")
        self.thread(api_message("0a01", "a1", WILL, f"Kellen <{ME}>", "Dates", body=body))
        s = self.reader().context["reader"]["schedule"]
        self.assertEqual(s["kind"], "offer")
        q = parse_qs(urlsplit(s["url"]).query)
        self.assertNotIn("when", q)  # nothing pre-picked: the person chooses
        from datetime import date as _date
        self.assertEqual(_date.fromisoformat(q["start"][0]).weekday(), 0)  # opens on the Monday of the first offered day
        self.assertIn("offered", self.reader().content.decode())

    def test_a_request_for_an_invite_with_no_time_still_offers_the_scheduler(self):
        self.thread(api_message("0a01", "a1", WILL, f"Kellen <{ME}>", "Call", body="Can you send out a calendar invite for us?"))
        s = self.reader().context["reader"]["schedule"]
        self.assertEqual(s["kind"], "request")
        self.assertNotIn("when", parse_qs(urlsplit(s["url"]).query))

    def test_my_own_message_never_triggers_it(self):
        self.thread(api_message("0a01", "a1", f"Kellen <{ME}>", WILL, "Call", body="Tuesday at 2pm works.", labels=("SENT",)))
        self.assertIsNone(self.reader().context["reader"]["schedule"])


class SchedulerOpensAtTheTimeTests(ScheduleTestCase):
    def test_when_picks_the_day_and_time_and_the_week(self):
        day = self.monday + timedelta(days=3)  # a Thursday
        r = self.client.get(reverse("crm:schedule"), {"with": f"{WILL_ADDR},{SIMON}", "title": "Site visit",
                                                      "when": f"{day.isoformat()}T12:00", "from": "email"})
        config = r.context["config"]
        self.assertEqual(config["pick"], {"date": day.isoformat(), "time": "12:00"})
        self.assertEqual(config["start"], self.monday.isoformat())  # the week holding that day, even with no ?start=
        self.assertTrue(config["fromEmail"])
        self.assertEqual(config["title"], "Site visit")
        self.assertEqual({p["email"] for p in config["invited"]}, {WILL_ADDR, SIMON})

    def test_junk_in_when_is_ignored(self):
        for bad in ("tomorrow", "2026-13-40T12:00", "2026-11-06T25:00", "2026-11-06", "2026-11-06T12:00:00Z"):
            self.assertIsNone(self.client.get(reverse("crm:schedule"), {"when": bad}).context["config"]["pick"], bad)
        self.assertFalse(self.client.get(reverse("crm:schedule")).context["config"]["fromEmail"])


WILL_ADDR = "will@firehawkaerospace.com"
