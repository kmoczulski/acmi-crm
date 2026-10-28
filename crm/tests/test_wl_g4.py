"""Worklist 2026-10-28, group 4. W5: removing a firm contact from a page that's out of date removes no one, instead of
whoever has moved into that row."""
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase

from crm.models import Firm
from crm.tests.utils import make_manager

User = get_user_model()


def person(name):
    return {"name": name, "title": "", "email": f"{name.lower()}@gfrlaw.com", "phone": ""}


class FirmContactRemoveTests(TestCase):
    def setUp(self):
        self.client.force_login(make_manager(User.objects.create_user("kellen", first_name="Kellen")))
        self.firm = Firm.objects.create(name="GFR Law", kind="law", domains=["gfrlaw.com"],
                                        contacts=[person("Ann"), person("Bob"), person("Cy")])

    def remove(self, i, c):
        return self.client.post(self.firm.get_absolute_url(), {
            "action": "contact_delete", "i": i, "name": c["name"], "email": c["email"]}, follow=True)

    def names(self):
        self.firm.refresh_from_db()
        return [c["name"] for c in self.firm.contacts]

    def test_page_posts_name_and_email_with_the_index(self):
        html = self.client.get(self.firm.get_absolute_url()).content.decode()
        self.assertIn('name="i" value="1"><input type="hidden" name="name" value="Bob">'
                      '<input type="hidden" name="email" value="bob@gfrlaw.com">', html)

    def test_stale_tab_removes_nobody(self):
        # Tab A shows Ann, Bob, Cy. Tab B removes Ann. Tab A then clicks the cross on Bob, its row 1, where Cy is now.
        self.remove(0, person("Ann"))
        self.assertEqual(self.names(), ["Bob", "Cy"])
        r = self.remove(1, person("Bob"))
        self.assertEqual(self.names(), ["Bob", "Cy"])
        self.assertIn("That list changed. Nothing was removed.", [str(m) for m in get_messages(r.wsgi_request)])

    def test_matching_row_is_removed(self):
        self.remove(1, person("Bob"))
        self.assertEqual(self.names(), ["Ann", "Cy"])

    def test_index_past_the_end_removes_nobody(self):
        self.remove(5, person("Bob"))
        self.assertEqual(self.names(), ["Ann", "Bob", "Cy"])

    def test_contact_without_email_or_title_still_removes(self):
        # Contacts moved over from a Company (directory/logic.py) can carry None for the fields they lack.
        self.firm.contacts = [{"name": "Dee", "title": None, "email": None, "phone": None}]
        self.firm.save()
        self.client.post(self.firm.get_absolute_url(), {"action": "contact_delete", "i": 0, "name": "Dee", "email": ""})
        self.assertEqual(self.names(), [])
