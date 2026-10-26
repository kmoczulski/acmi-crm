"""The dashboard greeting follows the team's clock (TIME_ZONE), not the server's."""
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.test import SimpleTestCase

from crm.templatetags.crm_tags import greeting


class GreetingTests(SimpleTestCase):
    def at(self, utc_hour, utc_minute=0):
        moment = datetime(2026, 10, 26, utc_hour, utc_minute, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=moment):
            return greeting()

    def test_morning_afternoon_evening_in_central_time(self):
        self.assertEqual(self.at(14), "Good morning")  # 9 AM CDT
        self.assertEqual(self.at(18), "Good afternoon")  # 1 PM
        self.assertEqual(self.at(22), "Good evening")  # 5 PM
        self.assertEqual(self.at(3, 56), "Good evening")  # 10:56 PM the night before
