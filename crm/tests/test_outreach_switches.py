from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.forms import PersonForm
from crm.models import Activity, Company, Person
from crm.outreach import queue


class OutreachSwitchTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="kellen", email="kmoczulski@acmigroup.com")
        self.client.force_login(self.user)
        self.co = Company.objects.create(name="Acme")
        self.p = Person.objects.create(first_name="Ann", last_name="Lee", email="ann@acme.com", company=self.co,
                                       flags=["NASA Contractor"])

    def post(self, action, **extra):
        return self.client.post(reverse("crm:person_outreach", args=[self.p.pk]), {"action": action, **extra})

    def test_exclude_and_include(self):
        self.post("exclude")
        self.p.refresh_from_db()
        self.assertTrue(self.p.is_excluded)
        self.assertIn("NASA Contractor", self.p.flags)
        self.post("exclude")  # twice stays one flag
        self.p.refresh_from_db()
        self.assertEqual(self.p.flags.count("Exclude"), 1)
        _, skipped = queue.build()
        self.assertEqual(skipped["excluded"], 1)
        self.post("include")
        self.p.refresh_from_db()
        self.assertFalse(self.p.is_excluded)
        self.assertEqual(self.p.flags, ["NASA Contractor"])

    def test_bad_and_good_email(self):
        self.post("bad_email")
        self.p.refresh_from_db()
        self.assertIs(self.p.email_valid, False)
        _, skipped = queue.build()
        self.assertEqual(skipped["bounced"], 1)
        self.post("good_email")
        self.p.refresh_from_db()
        self.assertIs(self.p.email_valid, True)
        self.assertEqual(Activity.objects.filter(person=self.p, kind=Activity.Kind.FIELD_CHANGE).count(), 2)

    def test_redirects_to_safe_next_only(self):
        r = self.post("exclude", next="/companies/")
        self.assertEqual(r["Location"], "/companies/")
        r = self.post("include", next="//evil.com/")
        self.assertEqual(r["Location"], self.p.get_absolute_url())

    def test_unknown_action_and_get_refused(self):
        self.assertEqual(self.post("nope").status_code, 400)
        self.assertEqual(self.client.get(reverse("crm:person_outreach", args=[self.p.pk])).status_code, 405)

    def test_buttons_on_page_and_card(self):
        page = self.client.get(self.p.get_absolute_url()).content.decode()
        self.assertIn("Exclude from blasts", page)
        self.assertIn("Mark email bad", page)
        Person.objects.filter(pk=self.p.pk).update(flags=["Exclude"], email_valid=False)
        card = self.client.get(reverse("crm:person_card", args=[self.p.pk])).content.decode()
        self.assertIn("Include in blasts", card)
        self.assertIn("Email is good", card)

    def test_people_list_column_and_filter(self):
        bad = Person.objects.create(first_name="Bo", last_name="Bad", email="bo@acme.com", email_valid=False)
        other = Company.objects.create(name="Skip Co", flags=["Exclude"])
        via_co = Person.objects.create(first_name="Cy", last_name="Co", email="cy@skip.com", company=other)
        url = reverse("crm:person_list")
        page = self.client.get(url).content.decode()
        self.assertIn("Email blasts", page)
        self.assertIn("In blasts", page)
        self.assertIn("Email bad", page)
        self.assertIn("Company excluded", page)

        def names(blasts):
            html = self.client.get(url, {"blasts": blasts}).content.decode()
            return {p.full_name for p in (self.p, bad, via_co) if p.get_absolute_url() in html}

        self.assertEqual(names("bad"), {bad.full_name})
        self.assertEqual(names("excluded"), {via_co.full_name})
        self.assertEqual(names("in"), {self.p.full_name})

    def test_new_address_clears_bounce(self):
        Person.objects.filter(pk=self.p.pk).update(email_valid=False)
        self.p.refresh_from_db()
        form = PersonForm({"first_name": "Ann", "last_name": "Lee", "email": "ann.lee@acme.com",
                           "email_valid": "false", "company": self.co.pk}, instance=self.p)
        # "false" posted unchanged: the select still shows the old value, so a new address resets it.
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.p.refresh_from_db()
        self.assertIsNone(self.p.email_valid)
