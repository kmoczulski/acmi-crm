"""Meetings page: Day / Week / Month and the People checkboxes (feedback 2026-11-02, item 2)."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Meeting

User = get_user_model()


class MeetingViewTests(TestCase):
    def setUp(self):
        self.me = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.simon = User.objects.create_user("simon", password="x", first_name="Simon")
        self.client.force_login(self.me)
        now = timezone.now().replace(hour=15, minute=0, second=0, microsecond=0)
        self.mine = Meeting.objects.create(title="Kellen call", start=now, external_id="t1")
        self.mine.team.add(self.me)
        self.his = Meeting.objects.create(title="Simon site visit", start=now + timedelta(hours=1), external_id="t2")
        self.his.team.add(self.simon)
        self.next_week = Meeting.objects.create(title="Next week sync", start=now + timedelta(days=8), external_id="t3")
        self.next_week.team.add(self.me)
        self.url = reverse("crm:meeting_list")
        self.today = timezone.localdate()

    def test_month_is_the_default_and_shows_only_mine(self):
        r = self.client.get(self.url)
        self.assertEqual(r.context["view"], "month")
        self.assertContains(r, "Kellen call")
        self.assertNotContains(r, "Simon site visit")

    def test_day_view_shows_that_day_only(self):
        r = self.client.get(self.url, {"view": "day", "when": self.today.isoformat(), "who": "all"})
        self.assertEqual([len(w) for w in r.context["weeks"]], [1])
        self.assertContains(r, "Kellen call")
        self.assertContains(r, "Simon site visit")
        self.assertNotContains(r, "Next week sync")
        nxt = self.client.get(self.url, {"view": "day", "when": (self.today + timedelta(days=8)).isoformat()})
        self.assertContains(nxt, "Next week sync")
        self.assertContains(self.client.get(self.url, {"view": "day", "when": "2031-01-01"}), "Nothing scheduled")

    def test_week_view_is_seven_days_sunday_first_with_prev_and_next(self):
        r = self.client.get(self.url, {"view": "week", "when": self.today.isoformat()})
        days = [d["date"] for d in r.context["weeks"][0]]
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0].weekday(), 6)  # Sunday
        self.assertIn(self.today, days)
        self.assertEqual(r.context["next_when"] - r.context["prev_when"], timedelta(days=14))
        self.assertNotContains(r, "Next week sync") if self.today + timedelta(days=8) > days[-1] else None

    def test_people_checkboxes_pick_whose_meetings_show(self):
        r = self.client.get(self.url, {"view": "day", "when": self.today.isoformat(), "people": [self.simon.pk]})
        self.assertEqual(r.context["who"], "people")
        self.assertContains(r, "Simon site visit")
        self.assertNotContains(r, "Kellen call")
        both = self.client.get(self.url, {"view": "day", "when": self.today.isoformat(),
                                          "people": [self.simon.pk, self.me.pk]})
        self.assertContains(both, "Simon site visit")
        self.assertContains(both, "Kellen call")
        self.assertContains(both, 'name="people"')
        self.assertContains(both, "People (2)")

    def test_junk_in_the_address_is_ignored(self):
        r = self.client.get(self.url, {"view": "decade", "when": "not-a-date", "people": ["x", "9999"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["view"], "month")
        self.assertEqual(r.context["who"], "me")
