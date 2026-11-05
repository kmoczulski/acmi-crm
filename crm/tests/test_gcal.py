from datetime import timedelta

from allauth.socialaccount.models import SocialAccount, SocialToken
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import gcal
from crm.models import Activity, CalendarSync, Company, Meeting

User = get_user_model()
GOOGLE = {"google": {"APP": {"client_id": "cid", "secret": "sec"}}}


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeGoogle:
    """Stands in for `requests`: one token endpoint, one events endpoint with two pages."""

    def __init__(self, pages, token_status=200, events_status=200):
        self.pages, self.token_status, self.events_status = pages, token_status, events_status
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(("post", url))
        if self.token_status != 200:
            return FakeResponse({"error": "invalid_grant"}, self.token_status)
        return FakeResponse({"access_token": "new-access", "expires_in": 3600})

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", params.get("pageToken"), headers["Authorization"]))
        if self.events_status != 200:
            return FakeResponse({}, self.events_status)
        return FakeResponse(self.pages[params.get("pageToken") or "first"])


def event(eid, title, start, attendees, status="confirmed"):
    return {"id": eid, "status": status, "summary": title, "htmlLink": f"https://calendar/{eid}",
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
            "attendees": [{"email": a} for a in attendees]}


# GOOGLE_SIGNIN_ENABLED comes from GOOGLE_CLIENT_ID in .env; set it here so the tests don't depend on a local .env.
@override_settings(SOCIALACCOUNT_PROVIDERS=GOOGLE, GOOGLE_SIGNIN_ENABLED=True, CALENDAR_DAYS_BACK=90,
                   CALENDAR_DAYS_AHEAD=60)
class CalendarSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", email="kmoczulski@acmigroup.com",
                                             first_name="Kellen", last_name="Moczulski")
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1")
        self.token = SocialToken.objects.create(account=account, token="old", token_secret="refresh",
                                                expires_at=timezone.now() - timedelta(minutes=5))
        self.company = Company.objects.create(name="ARA", domains=["ara.com"])
        soon = timezone.now() + timedelta(days=2)
        self.pages = {
            "first": {"items": [
                event("e1", "ARA design review", soon, ["kmoczulski@acmigroup.com", "kherrle@ara.com"]),
                event("gym", "Gym", soon, []),
            ], "nextPageToken": "p2"},
            "p2": {"items": [
                {"id": "allday", "status": "confirmed", "summary": "Holiday", "start": {"date": "2026-11-26"}},
                event("e2", "Old meeting", soon, ["kmoczulski@acmigroup.com", "x@ara.com"], status="cancelled"),
            ]},
        }

    def test_sync_refreshes_token_pages_and_imports(self):
        Meeting.objects.create(external_id="calendar:e2", source="calendar", title="Old meeting", start=timezone.now())
        fake = FakeGoogle(self.pages)
        stats = gcal.sync_user(self.user, session=fake)
        self.token.refresh_from_db()
        self.assertEqual(self.token.token, "new-access")
        self.assertEqual(self.token.token_secret, "refresh")
        self.assertEqual([c[1] for c in fake.calls if c[0] == "get"], [None, "p2"])
        self.assertTrue(Meeting.objects.filter(external_id="calendar:e1").exists())
        self.assertFalse(Meeting.objects.filter(title="Gym").exists())
        self.assertFalse(Meeting.objects.filter(external_id="calendar:e2").exists())  # cancelled -> removed
        self.assertEqual(stats["removed"], 1)
        self.assertTrue(Activity.objects.filter(kind=Activity.Kind.MEETING, company=self.company).exists())
        self.company.refresh_from_db()
        self.assertIsNotNone(self.company.next_interaction_at)
        state = CalendarSync.objects.get(user=self.user)
        self.assertEqual(state.events_seen, 4)
        self.assertEqual(state.last_error, "")

    def test_a_declined_invite_leaves_the_dashboard_calendar(self):
        """The Dashboard "This week" shows meetings the person is on. Declining in Google takes it off."""
        other = User.objects.create_user("simon", password="x", email="sshewmaker@acmigroup.com",
                                         first_name="Simon", last_name="Shewmaker")
        soon = timezone.now() + timedelta(days=1)
        both = ["kmoczulski@acmigroup.com", "sshewmaker@acmigroup.com", "kherrle@ara.com"]
        gcal.sync_user(self.user, session=FakeGoogle({"first": {"items": [event("d1", "ARA sync", soon, both)]}}))
        meeting = Meeting.objects.get(external_id="calendar:d1")
        self.assertEqual(set(meeting.team.all()), {self.user, other})
        declined = {**event("d1", "ARA sync", soon, both)}
        declined["attendees"] = [{**a, "responseStatus": "declined"} if a["email"].startswith("kmoczulski") else a
                                 for a in declined["attendees"]]
        gcal.sync_user(self.user, session=FakeGoogle({"first": {"items": [declined]}}))
        self.assertEqual(set(meeting.team.all()), {other})
        self.client.force_login(self.user)
        mine = self.client.get(reverse("crm:home"))
        self.assertNotContains(mine, "ARA sync")

    def test_valid_token_is_not_refreshed(self):
        self.token.expires_at = timezone.now() + timedelta(minutes=30)
        self.token.save()
        fake = FakeGoogle(self.pages)
        gcal.sync_user(self.user, session=fake)
        self.assertFalse(any(c[0] == "post" for c in fake.calls))
        self.assertIn("Bearer old", fake.calls[0][2])

    def test_revoked_access_is_reported(self):
        with self.assertRaises(gcal.CalendarError):
            gcal.sync_user(self.user, session=FakeGoogle(self.pages, token_status=400))
        self.assertIn("Reconnect", CalendarSync.objects.get(user=self.user).last_error)
        self.token.expires_at = timezone.now() + timedelta(minutes=30)
        self.token.save()
        with self.assertRaises(gcal.CalendarError):
            gcal.sync_user(self.user, session=FakeGoogle(self.pages, events_status=403))

    def test_not_connected(self):
        other = User.objects.create_user("simon", password="x")
        with self.assertRaises(gcal.CalendarError):
            gcal.sync_user(other)
        self.assertFalse(gcal.connected(other))
        self.assertTrue(gcal.connected(self.user))

    def test_meetings_page_shows_connect_state(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("crm:meeting_list"))
        self.assertContains(r, "Your Google Calendar is connected")
        other = User.objects.create_user("simon", password="x")
        self.client.force_login(other)
        self.assertContains(self.client.get(reverse("crm:meeting_list")), "Connect in Google access")

    # The Times panel beside an email: one week, read live

    def test_a_week_for_offering_times(self):
        start = timezone.now()
        fake = FakeGoogle({"first": {"items": [
            event("e1", "ARA design review", start + timedelta(hours=2), ["kmoczulski@acmigroup.com"]),
            {**event("free", "Focus (free)", start, []), "transparency": "transparent"},
            {**event("no", "Declined", start, []), "attendees": [{"email": "k@x", "self": True,
                                                                    "responseStatus": "declined"}]},
            event("gone", "Cancelled", start, [], status="cancelled"),
            {"id": "ooo", "status": "confirmed", "summary": "Out of office", "start": {"date": "2026-11-26"},
             "end": {"date": "2026-11-27"}},
        ]}})
        found = gcal.week(self.user, start, session=fake)
        self.assertEqual([(e["title"], e["all_day"]) for e in found],
                         [("ARA design review", False), ("Out of office", True)])
        self.assertEqual(found[1]["start"], "2026-11-26")
        _, _, auth = fake.calls[-1]
        self.assertEqual(auth, "Bearer new-access")

    def test_the_times_panel_answers_with_the_week_or_why_not(self):
        from unittest import mock

        self.client.force_login(self.user)
        with mock.patch.object(gcal, "week", return_value=[{"start": "2026-10-28T10:00:00-04:00",
                                                            "end": "2026-10-28T11:00:00-04:00",
                                                            "title": "Tour", "all_day": False}]) as week:
            r = self.client.get(reverse("crm:inbox_calendar"), {"start": "2026-10-26"})
        self.assertEqual(r.json()["events"][0]["title"], "Tour")
        self.assertEqual(r.json()["start"], "2026-10-26")
        self.assertEqual(timezone.localtime(week.call_args[0][1]).date().isoformat(), "2026-10-26")
        self.token.account.delete()
        r = self.client.get(reverse("crm:inbox_calendar"), {"start": "nonsense"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Connect Google Calendar", r.json()["error"])
