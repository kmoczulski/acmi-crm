"""Worklist 2026-10-28, group 6: partners' passwords only on the room's page (W8), push endpoints only on the
browsers' push services (W9), and card link attachments only http(s) (W10)."""
import json

from django.contrib.auth import authenticate, get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from crm import push
from crm.auth import PartnerBackend
from crm.boards import logic
from crm.models import CardAttachment, PushSubscription

from .test_data_room import PASSWORD, DataRoomTestCase

User = get_user_model()
KEYS = {"p256dh": "BKey", "auth": "authsecret"}


class PartnerSignInOnlyInTheRoomTests(DataRoomTestCase):
    """W8: Django's authenticate() tries every backend, so the partner backend has to check where it's called from."""

    def test_partner_password_is_refused_off_the_room_page(self):
        factory = RequestFactory()
        creds = {"username": "pat@fortress.com", "password": PASSWORD}
        self.assertIsNone(PartnerBackend().authenticate(factory.post("/accounts/login/"), **creds))
        self.assertIsNone(PartnerBackend().authenticate(factory.post("/admin/login/"), **creds))
        self.assertIsNone(PartnerBackend().authenticate(None, **creds))
        self.assertEqual(PartnerBackend().authenticate(factory.post("/room/login/"), **creds), self.partner)
        self.assertIsNone(authenticate(factory.post("/accounts/login/"), **creds))

    def test_team_sign_in_page_does_not_let_a_partner_in(self):
        for local_login in (True, False):
            with self.subTest(local_login=local_login), override_settings(LOCAL_LOGIN_ENABLED=local_login):
                self.client.logout()
                self.client.post(reverse("account_login"), {"login": "pat@fortress.com", "password": PASSWORD})
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_room_sign_in_still_works(self):
        self.client.logout()
        r = self.client.post(reverse("crm:room_login"), {"email": "pat@fortress.com", "password": PASSWORD})
        self.assertRedirects(r, reverse("crm:room_home"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.partner.pk)


class PushEndpointTests(TestCase):
    """W9: the server POSTs to every saved endpoint, so only the browsers' push services are kept."""

    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.client.force_login(self.user)

    def subscribe(self, endpoint):
        return self.client.post(reverse("crm:push_subscribe"), json.dumps({"endpoint": endpoint, "keys": KEYS}),
                                content_type="application/json")

    def test_browsers_push_services_are_accepted(self):
        for endpoint in ("https://fcm.googleapis.com/fcm/send/abc",
                         "https://updates.push.services.mozilla.com/wpush/v2/abc",
                         "https://wns2-par02p.notify.windows.com/w/?token=abc",
                         "https://web.push.apple.com/QGx",
                         "https://api.push.apple.com/3/device/abc"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.subscribe(endpoint).status_code, 200)
        self.assertEqual(PushSubscription.objects.count(), 5)

    def test_other_https_addresses_are_refused(self):
        for endpoint in ("https://10.0.0.7:8443/internal/hook",
                         "https://evil.example/fcm.googleapis.com",
                         "https://fcm.googleapis.com.evil.example/x",
                         "https://notfcm.googleapis.com/x",
                         "https://fcm.googleapis.com@evil.example/x",
                         "https://fcm.googleapis.com:8443/x",
                         "http://fcm.googleapis.com/fcm/send/abc",
                         "https://fcm.googleapis.com:notaport/x"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.subscribe(endpoint).status_code, 400)
                self.assertIsNone(push.subscribe(self.user, {"endpoint": endpoint, "keys": KEYS}))
        self.assertFalse(PushSubscription.objects.exists())


class CardLinkAttachmentTests(TestCase):
    """W10: the card prints a link attachment as an href, so only web links are saved."""

    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen", last_name="M")
        self.client.force_login(self.user)
        self.board = logic.create_board("Anvil", self.user, template="deal")
        self.card = logic.add_card(self.board.lists.first(), "Send the pro forma", self.user)

    def attach(self, url):
        return self.client.post(reverse("crm:card_attach", args=[self.card.pk]), {"action": "add", "url": url,
                                                                                  "name": "Link"}, follow=True)

    def test_only_http_and_https_links_are_saved(self):
        for url in ("javascript:alert(1)", "JavaScript:fetch('/x')", "data:text/html,<script>1</script>",
                    "vbscript:x", "//evil.example/x", "ftp://files.example/x"):
            with self.subTest(url=url):
                r = self.attach(url)
                self.assertContains(r, "A link has to start with http:// or https://.")
        self.assertFalse(CardAttachment.objects.exists())
        self.attach("https://drive.example/x/budget")
        self.attach("HTTP://intranet.example/plan")
        self.assertEqual(sorted(CardAttachment.objects.values_list("url", flat=True)),
                         ["HTTP://intranet.example/plan", "https://drive.example/x/budget"])
