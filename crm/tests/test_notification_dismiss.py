"""Dismiss one alert (Kellen: "I want to be able to click off a single notification instead of having to click into it if I
have already handled it"): the × on a row of the Notifications page and on a live pop-up marks just that one read, without
opening it, and the bell's count follows (views.notification_dismiss, static/crm/notifications.js, alerts.js)."""
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Notification

User = get_user_model()
FETCH = {"HTTP_X_REQUESTED_WITH": "fetch"}  # what the page scripts send; the plain form post does not


class DismissTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x")
        self.simon = User.objects.create_user("simon", password="x")
        self.ben = self.alert(self.kellen, "Call Ben")
        self.loi = self.alert(self.kellen, "Send LOI")
        self.lease = self.alert(self.kellen, "Review lease")
        self.theirs = self.alert(self.simon, "Simon's follow-up")
        self.client.force_login(self.kellen)

    def alert(self, user, text, **extra):
        return Notification.objects.create(user=user, kind=Notification.Kind.ASSIGNED, text=text, url="/tasks/", **extra)

    def url(self, note):
        return reverse("crm:notification_dismiss", args=[note.pk])

    def unread(self, user=None):
        return Notification.objects.filter(user=user or self.kellen, read_at__isnull=True).count()

    def test_a_dismiss_marks_that_one_read_and_leaves_the_others(self):
        r = self.client.post(self.url(self.loi), **FETCH)
        self.assertEqual(r.status_code, 200)
        for note in (self.ben, self.loi, self.lease, self.theirs):
            note.refresh_from_db()
        self.assertIsNotNone(self.loi.read_at)
        self.assertIsNone(self.ben.read_at)
        self.assertIsNone(self.lease.read_at)
        self.assertIsNone(self.theirs.read_at)  # nor does it touch another person's alerts

    def test_dismissing_twice_keeps_the_first_read_time(self):
        self.client.post(self.url(self.loi), **FETCH)
        self.loi.refresh_from_db()
        first = self.loi.read_at
        self.assertEqual(self.client.post(self.url(self.loi), **FETCH).json()["unread"], 2)
        self.loi.refresh_from_db()
        self.assertEqual(self.loi.read_at, first)

    def test_another_persons_alert_is_a_404_and_stays_unread(self):
        for extra in (FETCH, {}):  # a script's post and a plain form post
            self.assertEqual(self.client.post(self.url(self.theirs), **extra).status_code, 404)
        self.assertEqual(self.client.post(reverse("crm:notification_dismiss", args=[999999]), **FETCH).status_code, 404)
        self.theirs.refresh_from_db()
        self.assertIsNone(self.theirs.read_at)
        self.assertEqual(self.unread(), 3)

    def test_get_is_refused(self):
        r = self.client.get(self.url(self.loi))
        self.assertEqual(r.status_code, 405)
        self.loi.refresh_from_db()
        self.assertIsNone(self.loi.read_at)  # a link or a prefetch can never read an alert for you

    def test_signed_out_it_changes_nothing(self):
        self.client.logout()
        r = self.client.post(self.url(self.loi), **FETCH)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])
        self.loi.refresh_from_db()
        self.assertIsNone(self.loi.read_at)

    def test_the_bell_count_drops_in_the_answer_the_poll_and_the_next_page(self):
        poll = reverse("crm:notification_poll")
        self.assertEqual(self.client.get(poll).json()["unread"], 3)
        answer = self.client.post(self.url(self.ben), **FETCH).json()
        self.assertEqual(answer, {"ok": True, "id": self.ben.pk, "unread": 2})  # the page sets the bell from this
        self.assertEqual(self.client.get(poll).json()["unread"], 2)
        page = self.client.get(reverse("crm:notification_list"))
        self.assertEqual(page.context["unread_notifications"], 2)
        self.assertContains(page, '<span class="count">2</span>')
        self.assertEqual(self.unread(self.simon), 1)  # Simon's bell is his own

    def test_the_last_one_takes_the_bell_to_zero(self):
        for note in (self.ben, self.loi):
            self.client.post(self.url(note), **FETCH)
        self.assertEqual(self.client.post(self.url(self.lease), **FETCH).json()["unread"], 0)
        self.assertNotContains(self.client.get(reverse("crm:notification_list")), '<span class="count">')

    def test_a_form_post_without_script_goes_back_to_the_page_it_came_from(self):
        back = reverse("crm:notification_list") + "?show=all&page=2"
        r = self.client.post(self.url(self.loi), {"next": back})
        self.assertRedirects(r, back, fetch_redirect_response=False)
        self.loi.refresh_from_db()
        self.assertIsNotNone(self.loi.read_at)
        r = self.client.post(self.url(self.ben))  # no next: the list
        self.assertRedirects(r, reverse("crm:notification_list"), fetch_redirect_response=False)
        for elsewhere in ("//evil.example/x", "https://evil.example/x"):  # never off the site
            r = self.client.post(self.url(self.lease), {"next": elsewhere})
            self.assertRedirects(r, reverse("crm:notification_list"), fetch_redirect_response=False)

    def test_each_live_pop_up_carries_its_own_dismiss_address(self):
        poll = reverse("crm:notification_poll")
        items = self.client.get(poll, {"after": self.ben.pk}).json()["items"]
        self.assertEqual([i["id"] for i in items], [self.loi.pk, self.lease.pk])
        for item in items:
            self.assertEqual(item["dismiss"], self.url(Notification.objects.get(pk=item["id"])))
        self.assertEqual(self.client.post(items[0]["dismiss"], **FETCH).json()["unread"], 2)
        again = self.client.get(poll, {"after": self.ben.pk}).json()["items"]
        self.assertEqual([i["id"] for i in again], [self.lease.pk])  # a read alert is not offered again


class PageTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x")
        self.simon = User.objects.create_user("simon", password="x")
        self.ben = self.alert(self.kellen, "Call Ben")
        self.loi = self.alert(self.kellen, "Send LOI")
        self.old = self.alert(self.kellen, "Old news", read_at=timezone.now() - timedelta(days=1))
        self.theirs = self.alert(self.simon, "Simon's follow-up")
        self.client.force_login(self.kellen)

    def alert(self, user, text, **extra):
        return Notification.objects.create(user=user, kind=Notification.Kind.ASSIGNED, text=text, url="/tasks/", **extra)

    def url(self, note):
        return reverse("crm:notification_dismiss", args=[note.pk])

    def test_each_unread_row_has_a_labelled_button_and_a_read_row_has_none(self):
        page = self.client.get(reverse("crm:notification_list"), {"show": "all"})
        for note in (self.ben, self.loi):
            self.assertContains(page, f'<form class="notif-dismiss" method="post" action="{self.url(note)}"')
        self.assertContains(page, 'aria-label="Dismiss: Call Ben"')  # the name a screen reader reads
        self.assertContains(page, 'title="Dismiss: mark as read"')
        self.assertContains(page, "Old news")
        self.assertNotContains(page, self.url(self.old))  # already read: nothing to dismiss
        self.assertNotContains(page, self.url(self.theirs))
        self.assertContains(page, 'name="next" value="/notifications/?show=all"')  # a plain post comes back to the same list
        self.assertContains(page, "crm/notifications.js")

    def test_the_link_stays_a_link_the_button_sits_beside_it(self):
        page = self.client.get(reverse("crm:notification_list")).content.decode()
        for row in re.findall(r'<div class="notif-row".*?</div>', page, flags=re.S):
            link = re.search(r"<a [^>]*>.*?</a>", row, flags=re.S).group(0)
            self.assertNotIn("<button", link)  # a button can't live inside a link
            self.assertIn(reverse("crm:notification_open", args=[int(re.search(r"/notifications/(\d+)/", link).group(1))]), link)

    def test_a_dismissed_alert_leaves_the_unread_list_but_stays_under_all(self):
        self.client.post(self.url(self.loi), **FETCH)
        unread = self.client.get(reverse("crm:notification_list")).content.decode()
        self.assertIn("Call Ben", unread)
        self.assertNotIn("Send LOI", unread)
        everything = self.client.get(reverse("crm:notification_list"), {"show": "all"}).content.decode()
        self.assertIn("Send LOI", everything)
        self.assertNotIn(self.url(self.loi), everything)  # read now, so no button
        self.assertIn(self.url(self.ben), everything)

    def test_the_caught_up_note_shows_only_when_the_list_is_empty(self):
        page = self.client.get(reverse("crm:notification_list")).content.decode()
        self.assertIn('data-notif-empty tabindex="-1" hidden>', page)  # there are rows: the script un-hides it after the last
        for note in (self.ben, self.loi):
            self.client.post(self.url(note), **FETCH)
        page = self.client.get(reverse("crm:notification_list")).content.decode()
        self.assertIn('data-notif-empty tabindex="-1">You\'re all caught up.', page)

    def test_the_new_controls_use_theme_colours_and_nothing_see_through(self):
        """Kellen's design rules: theme variables, no hex colours, solid backgrounds (docs: Apple-style design)."""
        css = re.sub(r"/\*.*?\*/", "", (settings.BASE_DIR / "crm" / "static" / "crm" / "app.css").read_text(encoding="utf-8"), flags=re.S)
        mine = [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                if re.search(r"\.(notif-x|notif-dismiss|notif-row|notif-empty|alert-toast-x|alert-toast-link|notify-nudge)\b", sel)]
        self.assertGreaterEqual(len(mine), 8)
        for selector, body in mine:
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", body), f"{selector}: a hex colour, use a theme variable")
            self.assertNotIn("rgba(", body, f"{selector}: see-through")
            self.assertNotIn("backdrop-filter", body, f"{selector}: glass")
