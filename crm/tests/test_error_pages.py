"""The friendly 404 page and the skip link (usability pass, 2026-11)."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings


class ErrorPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("kellen", password="x"))

    @override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
    def test_unknown_page_shows_the_friendly_404(self):
        r = self.client.get("/no-such-page-here/")
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "We can't find that page", status_code=404)

    def test_every_page_has_a_skip_link_to_main(self):
        r = self.client.get("/")
        self.assertContains(r, 'href="#main"')
        self.assertContains(r, 'id="main"')
