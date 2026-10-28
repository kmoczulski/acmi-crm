"""Split view: any hub page beside another (crm/static/crm/split.js). The side is a same-origin frame of the page drawn
without the top bar (?embed=1, or Sec-Fetch-Dest: iframe for pages opened inside it). Framing is allowed for the hub
itself only, and the usual sign-in, partner and permission rules apply inside the frame."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from crm.permissions import PARTNERS

from .test_gmail_api import ON
from . import test_inbox  # its InboxTests setUp, without running those tests again here

User = get_user_model()


class SplitViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("jordan", password="x", first_name="Jordan")
        self.client.force_login(self.user)

    def test_full_page_has_top_bar_split_button_and_side_pane(self):
        r = self.client.get(reverse("crm:task_list"))
        self.assertContains(r, 'class="appbar')
        self.assertContains(r, "data-split-toggle")
        self.assertContains(r, 'id="split-pane"')
        self.assertContains(r, "crm/split.js")
        self.assertContains(r, 'id="alerts-config"')
        self.assertNotContains(r, '<html lang="en" class="embedded">')

    def test_embed_flag_drops_top_bar_pane_and_alerts(self):
        for url in (reverse("crm:task_list") + "?embed=1", reverse("crm:land_search") + "?embed=1"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, '<html lang="en" class="embedded">')
            self.assertNotContains(r, 'class="appbar')
            self.assertNotContains(r, 'id="split-pane"')  # no split inside the split
            self.assertNotContains(r, 'id="alerts-config"')  # the main page polls for alerts, not both
            self.assertContains(r, "crm/split.js")  # Alt+click from the side opens on the main side

    def test_pages_opened_inside_the_frame_are_embedded_too(self):
        r = self.client.get(reverse("crm:task_list"), headers={"Sec-Fetch-Dest": "iframe"})
        self.assertContains(r, '<html lang="en" class="embedded">')
        self.assertNotContains(r, 'class="appbar')
        r = self.client.get(reverse("crm:task_list"), headers={"Sec-Fetch-Dest": "document"})
        self.assertContains(r, 'class="appbar')

    def test_other_embed_values_are_the_full_page(self):
        r = self.client.get(reverse("crm:task_list") + "?embed=0")
        self.assertContains(r, 'class="appbar')

    def test_framing_headers_allow_the_hub_only(self):
        for url in (reverse("crm:task_list"), reverse("crm:task_list") + "?embed=1", reverse("crm:land_search")):
            r = self.client.get(url)
            self.assertEqual(r["X-Frame-Options"], "SAMEORIGIN", url)
            self.assertIn("frame-ancestors 'self'", r["Content-Security-Policy"], url)
            self.assertNotIn("frame-ancestors 'none'", r["Content-Security-Policy"], url)

    def test_signed_out_embed_goes_to_sign_in(self):
        self.client.logout()
        r = self.client.get(reverse("crm:task_list") + "?embed=1")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

    def test_partner_cant_open_hub_pages_in_the_frame(self):
        partner = User.objects.create_user("fortress", password="x")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(partner)
        for url in (reverse("crm:task_list") + "?embed=1", reverse("crm:land_search") + "?embed=1"):
            r = self.client.get(url, headers={"Sec-Fetch-Dest": "iframe"})
            self.assertEqual(r.status_code, 302, url)
            self.assertEqual(r["Location"], "/room/")

    def test_manager_only_pages_stay_closed_in_the_frame(self):
        r = self.client.get(reverse("crm:settings_home") + "?embed=1")
        self.assertIn(r.status_code, (302, 403))


@override_settings(**ON)
class InboxInTheSideTests(TestCase):
    """Mail beside the Land map: the inbox in the side frame keeps its reading pane and its in-place loading."""

    setUp = test_inbox.InboxTests.setUp

    def test_inbox_embedded_with_reading_pane_and_in_place_loading(self):
        r = self.client.get(reverse("crm:inbox"), {"embed": "1", "t": "a1"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '<html lang="en" class="embedded">')
        self.assertNotContains(r, 'class="appbar')
        self.assertContains(r, 'class="mail-app has-reader"')
        self.assertContains(r, "crm/compose.js")
        self.assertEqual(r["X-Frame-Options"], "SAMEORIGIN")
        # A conversation opened from inside the frame: fetch() asks for the pane only, as on a full page.
        r = self.client.get(reverse("crm:inbox"), {"t": "b2"}, HTTP_X_MAIL_PANE="1", HTTP_SEC_FETCH_DEST="empty")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<html", r.content.decode().lower())

    def test_message_bodies_still_frame_inside_the_side(self):
        r = self.client.get(reverse("crm:inbox_body", args=["m1"]), HTTP_SEC_FETCH_DEST="iframe")
        self.assertEqual(r["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", r["Content-Security-Policy"])
