"""Settings: one area with Profile, Google access, Mail, Notifications and the manager sections, old addresses
redirecting, and each Google permission connected from the Google access page (crm/settings_nav.py,
crm/google_access.py, crm/settings_views.py)."""
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from crm import google_access, settings_nav
from crm.models import CalendarSync, ChatConnection, MailConnection
from crm.permissions import MANAGERS, PARTNERS
from crm.tests.test_mail_connect import ON, FakeGoogle, google_token

User = get_user_model()
GOOGLE_PAGE = "/settings/google/"


def member(name="ann", email="ann@acmigroup.com", manager=False):
    user = User.objects.create_user(name, password="x", email=email)
    if manager:
        user.groups.add(Group.objects.get_or_create(name=MANAGERS)[0])
    return user


class SectionTests(TestCase):
    def setUp(self):
        self.user = member()
        self.client.force_login(self.user)

    def test_every_section_opens_and_shows_the_tab_strip(self):
        for url in ("/settings/", "/settings/profile/", GOOGLE_PAGE, "/settings/mail/", "/settings/notifications/"):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, url)
        page = self.client.get("/settings/profile/")
        for label in ("Profile", "Google access", "Mail", "Notifications"):
            self.assertContains(page, f">{label}</a>")

    def test_manager_sections_are_for_managers_only(self):
        page = self.client.get("/settings/")
        self.assertNotContains(page, "Dropdown lists")
        self.assertNotEqual(self.client.get("/settings/outreach/").status_code, 200)
        self.assertNotEqual(self.client.get("/settings/dropdowns/").status_code, 200)
        self.client.force_login(member("boss", "boss@acmigroup.com", manager=True))
        page = self.client.get("/settings/")
        for label in ("Dropdown lists", "Outreach", "Contract templates", "Data rooms"):
            self.assertContains(page, label)
        self.assertEqual(self.client.get("/settings/dropdowns/").status_code, 200)
        self.assertEqual(self.client.get("/settings/outreach/").status_code, 200)

    def test_the_top_bar_menu_lists_the_sections_for_everyone(self):
        page = self.client.get("/settings/profile/")
        self.assertContains(page, reverse("crm:settings_google"))
        self.assertContains(page, "Profile and settings")
        self.assertNotContains(page, "Customize my dashboard")

    def test_a_capital_partner_has_no_settings(self):
        partner = member("pat", "pat@fund.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(partner)
        for url in ("/settings/", "/settings/profile/", GOOGLE_PAGE):
            self.assertNotEqual(self.client.get(url).status_code, 200, url)

    def test_old_addresses_redirect_to_the_new_ones(self):
        for old, new in (("/notifications/settings/", "/settings/notifications/"), ("/mail-setup/", "/settings/mail/"),
                         ("/outreach/settings/", "/settings/outreach/")):
            r = self.client.get(old)
            self.assertEqual(r.status_code, 302, old)
            self.assertEqual(r["Location"], new)

    def test_settings_pages_never_send_a_person_off_site(self):
        rf = RequestFactory()
        request = rf.post("/x/", {"next": "https://evil.example/settings/"})
        self.assertEqual(settings_nav.clean_next(request, "https://evil.example/settings/"), "")
        self.assertEqual(settings_nav.clean_next(request, "//evil.example/settings/x"), "")
        self.assertEqual(settings_nav.clean_next(request, "/inbox/"), "")
        self.assertEqual(settings_nav.clean_next(request, "/settings/google/"), "/settings/google/")


@override_settings(**ON)
class GoogleAccessTests(TestCase):
    def setUp(self):
        self.user = member("kellen", "kmoczulski@acmigroup.com")
        self.client.force_login(self.user)

    def rows(self):
        return {r["key"]: r for r in self.client.get(GOOGLE_PAGE).context["rows"]}

    def test_a_person_with_no_google_token_is_offered_calendar_and_nothing_else_is_ready(self):
        rows = self.rows()
        self.assertEqual(list(rows), ["signin", "calendar", "scheduling", "gmail", "chat"])
        self.assertEqual(rows["calendar"]["state"], "off")
        self.assertEqual([a["label"] for a in rows["calendar"]["actions"]], ["Connect Calendar"])
        self.assertEqual(rows["scheduling"]["state"], "locked")
        self.assertEqual(rows["gmail"]["actions"][0]["label"], "Turn on Mail")

    def test_connected_rows_say_what_is_on_and_offer_reconnect(self):
        google_token(self.user)
        CalendarSync.objects.create(user=self.user, can_schedule=True)
        MailConnection.objects.create(user=self.user, enabled=True, google_email=self.user.email)
        rows = self.rows()
        self.assertEqual(rows["calendar"]["state"], "on")
        self.assertEqual(rows["scheduling"]["state"], "on")
        self.assertEqual(rows["gmail"]["state"], "on")
        self.assertEqual([a["label"] for a in rows["gmail"]["actions"]], ["Reconnect", "Turn off"])
        self.assertContains(self.client.get(GOOGLE_PAGE), "Remove all access")

    def test_each_permission_button_carries_the_page_it_came_from(self):
        google_token(self.user)
        page = self.client.get(GOOGLE_PAGE)
        self.assertContains(page, 'name="next" value="/settings/google/"')

    def test_turning_on_mail_from_this_page_comes_back_to_it(self):
        google_token(self.user)
        r = self.client.post(reverse("crm:mail_turn_on"), {"next": GOOGLE_PAGE})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/google/login/", r["Location"])
        google = FakeGoogle(["https://www.googleapis.com/auth/gmail.modify"])
        with mock.patch("crm.mail.connect.requests", google):
            back = self.client.get(reverse("crm:mail_connected"))
        self.assertRedirects(back, GOOGLE_PAGE, fetch_redirect_response=False)
        self.assertTrue(MailConnection.objects.get(user=self.user).enabled)

    def test_mail_turned_on_from_the_mail_page_still_lands_there(self):
        google_token(self.user)
        self.client.post(reverse("crm:mail_turn_on"))
        google = FakeGoogle([])
        with mock.patch("crm.mail.connect.requests", google):
            back = self.client.get(reverse("crm:mail_connected"))
        self.assertRedirects(back, "/settings/mail/", fetch_redirect_response=False)

    def test_chat_and_scheduling_come_back_here_too(self):
        google_token(self.user)
        self.client.post(reverse("crm:chat_turn_on"), {"next": GOOGLE_PAGE})
        back = self.client.get(reverse("crm:chat_connected"))
        self.assertRedirects(back, GOOGLE_PAGE, fetch_redirect_response=False)
        self.client.post(reverse("crm:schedule_allow"), {"next": GOOGLE_PAGE})
        back = self.client.get(reverse("crm:schedule_allowed"))
        self.assertRedirects(back, GOOGLE_PAGE, fetch_redirect_response=False)

    def test_remove_all_access_revokes_and_switches_everything_off(self):
        token = google_token(self.user)
        CalendarSync.objects.create(user=self.user, can_schedule=True)
        MailConnection.objects.create(user=self.user, enabled=True)
        ChatConnection.objects.create(user=self.user, enabled=True)
        google = FakeGoogle()
        with mock.patch("crm.mail.connect.requests", google):
            r = self.client.post(reverse("crm:settings_google_disconnect"))
        self.assertRedirects(r, GOOGLE_PAGE, fetch_redirect_response=False)
        self.assertTrue(any(c[0] == "post" and "revoke" in c[1] for c in google.calls))
        self.assertFalse(type(token).objects.filter(pk=token.pk).exists())
        self.assertFalse(MailConnection.objects.get(user=self.user).enabled)
        self.assertFalse(ChatConnection.objects.get(user=self.user).enabled)
        self.assertFalse(CalendarSync.objects.get(user=self.user).can_schedule)

    def test_disconnect_is_post_only(self):
        self.assertEqual(self.client.get(reverse("crm:settings_google_disconnect")).status_code, 405)

    def test_sign_in_no_longer_asks_for_drive(self):
        from django.conf import settings

        scopes = settings.SOCIALACCOUNT_PROVIDERS["google"]["SCOPE"] if "google" in settings.SOCIALACCOUNT_PROVIDERS else []
        self.assertNotIn(google_access.DRIVE_SCOPE, scopes)


@override_settings(**ON, GRANOLA_DRIVE_USER="kmoczulski@acmigroup.com")
class DriveRowTests(TestCase):
    def setUp(self):
        self.user = member("kellen", "kmoczulski@acmigroup.com")
        self.other = member("ann")
        self.client.force_login(self.user)

    def test_only_the_granola_person_sees_a_drive_row(self):
        google_token(self.user)
        with mock.patch("crm.gcal.granted_scopes", return_value=set()):
            keys = [r["key"] for r in self.client.get(GOOGLE_PAGE).context["rows"]]
        self.assertIn("drive", keys)
        self.client.force_login(self.other)
        keys = [r["key"] for r in self.client.get(GOOGLE_PAGE).context["rows"]]
        self.assertNotIn("drive", keys)
        self.assertEqual(self.client.post(reverse("crm:settings_google_drive")).status_code, 302)  # refused, back to the page

    def test_the_row_reads_what_google_says_is_granted(self):
        google_token(self.user)
        with mock.patch("crm.gcal.granted_scopes", return_value={google_access.DRIVE_SCOPE}):
            row = {r["key"]: r for r in self.client.get(GOOGLE_PAGE).context["rows"]}["drive"]
        self.assertEqual(row["state"], "on")
        with mock.patch("crm.gcal.granted_scopes", return_value=set()):
            row = {r["key"]: r for r in self.client.get(GOOGLE_PAGE).context["rows"]}["drive"]
        self.assertEqual((row["state"], row["actions"][0]["label"]), ("off", "Connect Drive"))
        with mock.patch("crm.gcal.granted_scopes", side_effect=RuntimeError("down")):
            row = {r["key"]: r for r in self.client.get(GOOGLE_PAGE).context["rows"]}["drive"]
        self.assertEqual(row["state"], "attention")

    def test_connect_drive_asks_google_for_only_drive_and_keeps_the_rest(self):
        google_token(self.user)
        r = self.client.post(reverse("crm:settings_google_drive"), {"next": GOOGLE_PAGE})
        self.assertEqual(r.status_code, 302)
        query = parse_qs(urlparse(r["Location"]).query)
        self.assertIn(google_access.DRIVE_SCOPE, query["scope"][0])
        self.assertIn("include_granted_scopes=true", query["auth_params"][0])

    def test_back_from_google_with_drive_granted_says_so_and_returns_to_the_page(self):
        google_token(self.user)
        self.client.post(reverse("crm:settings_google_drive"), {"next": GOOGLE_PAGE})
        with mock.patch("crm.gcal.granted_scopes", return_value={google_access.DRIVE_SCOPE}):
            r = self.client.get(reverse("crm:settings_google_drive_done"))
        self.assertRedirects(r, GOOGLE_PAGE, fetch_redirect_response=False)
        self.assertIn("Drive is connected", [str(m) for m in r.wsgi_request._messages][0])

    def test_back_from_google_without_the_drive_box_ticked_explains(self):
        google_token(self.user)
        self.client.post(reverse("crm:settings_google_drive"), {"next": GOOGLE_PAGE})
        with mock.patch("crm.gcal.granted_scopes", return_value=set()):
            r = self.client.get(reverse("crm:settings_google_drive_done"))
        self.assertIn("leave the Drive box ticked", [str(m) for m in r.wsgi_request._messages][0])

    def test_arriving_without_starting_does_nothing(self):
        r = self.client.get(reverse("crm:settings_google_drive_done"))
        self.assertRedirects(r, GOOGLE_PAGE, fetch_redirect_response=False)
        self.assertEqual(len(list(r.wsgi_request._messages)), 0)
