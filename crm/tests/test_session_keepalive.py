"""Staying signed in on a phone (feedback 2026-11-02, item 7): the session lasts 60 days of use and moves out daily."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

User = get_user_model()


class SessionKeepAliveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x")
        self.client.force_login(self.user)

    def test_sessions_last_sixty_days(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 60 * 86400)
        self.client.get(reverse("crm:home"))
        self.assertGreater(self.client.session.get_expiry_age(), 59 * 86400)

    def test_a_signed_in_request_stamps_the_day_once(self):
        self.client.get(reverse("crm:home"))
        self.assertEqual(self.client.session["_touched"], timezone.localdate().isoformat())

    def test_the_stamp_is_renewed_on_a_new_day(self):
        session = self.client.session
        session["_touched"] = "2020-01-01"
        session.save()
        self.client.get(reverse("crm:home"))
        self.assertEqual(self.client.session["_touched"], timezone.localdate().isoformat())

    def test_signed_out_polling_is_sent_to_the_sign_in_page_not_json(self):
        self.client.logout()
        r = self.client.get(reverse("crm:notification_poll"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])
