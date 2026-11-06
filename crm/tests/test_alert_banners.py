"""Browser banners (Kellen: "notifcation banners arent working in browser"). The live alerts script (static/crm/alerts.js) asks
notification_poll for what is newer than the browser's baseline, the id of the newest alert it has already handled. A baseline of
0 is a real baseline: a browser that started with no alerts must still be handed the first one. No baseline at all means "tell me
where things stand, replay nothing"."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Notification

User = get_user_model()


class PollBaselineTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x")
        self.simon = User.objects.create_user("simon", password="x")
        self.first = self.alert(self.kellen, "Call Ben")
        self.read = self.alert(self.kellen, "Old news", read_at=timezone.now())
        self.second = self.alert(self.kellen, "Send LOI")
        self.theirs = self.alert(self.simon, "Simon's own")
        self.client.force_login(self.kellen)
        self.poll = reverse("crm:notification_poll")

    def alert(self, user, text, **extra):
        return Notification.objects.create(user=user, kind=Notification.Kind.ASSIGNED, text=text, url="/tasks/", **extra)

    def get(self, **params):
        return self.client.get(self.poll, params).json()

    def test_a_baseline_of_zero_is_handed_the_unread_alerts_oldest_first(self):
        d = self.get(after="0")
        self.assertEqual([i["id"] for i in d["items"]], [self.first.pk, self.second.pk])  # not the read one, not Simon's
        self.assertEqual(d["unread"], 2)
        self.assertEqual(d["latest"], self.second.pk)
        self.assertTrue(all(i["dismiss"] and i["url"] for i in d["items"]))

    def test_no_baseline_replays_nothing_but_says_where_things_stand(self):
        d = self.client.get(self.poll).json()
        self.assertEqual(d["items"], [])
        self.assertEqual((d["unread"], d["latest"]), (2, self.second.pk))  # the script takes `latest` as its baseline

    def test_an_empty_or_unreadable_baseline_counts_as_none(self):
        for value in ("", "abc", "1.5", "0x10"):
            self.assertEqual(self.get(after=value)["items"], [], value)

    def test_a_baseline_hands_out_only_what_is_newer(self):
        self.assertEqual([i["id"] for i in self.get(after=str(self.first.pk))["items"]], [self.second.pk])
        self.assertEqual(self.get(after=str(self.second.pk))["items"], [])

    def test_a_first_alert_reaches_a_browser_that_started_with_none(self):
        # The stuck case: no alerts at the first visit, so the script stored a baseline of 0 (`latest` was 0).
        Notification.objects.filter(user=self.kellen).delete()
        self.assertEqual(self.get()["latest"], 0)
        fresh = self.alert(self.kellen, "Arrived later")
        d = self.get(after="0")
        self.assertEqual([i["id"] for i in d["items"]], [fresh.pk])
        self.assertEqual(d["unread"], 1)

    def test_at_most_ten_come_at_once_and_the_rest_follow_on_the_next_poll(self):
        for n in range(12):
            self.alert(self.kellen, f"More {n}")
        ids = [i["id"] for i in self.get(after="0")["items"]]
        self.assertEqual(len(ids), 10)
        later = [i["id"] for i in self.get(after=str(ids[-1]))["items"]]
        self.assertEqual(len(later), 14 - 10)  # two before, twelve now: fourteen unread in all

    def test_every_page_tells_the_script_where_to_turn_banners_on(self):
        page = self.client.get(reverse("crm:home"))
        self.assertContains(page, f'data-settings="{reverse("crm:notification_settings")}"')
