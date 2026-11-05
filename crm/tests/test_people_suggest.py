"""Suggest people to add, from who keeps writing to you (feedback 2026-11-02, item 4)."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.mail import people_suggest
from crm.models import Company, MailThread, Mailbox, Person

User = get_user_model()


class PeopleSuggestTests(TestCase):
    def setUp(self):
        self.me = User.objects.create_user("kellen", password="x", email="kellen@acmigroup.com")
        self.box = Mailbox.objects.create(user=self.me, email="kellen@acmigroup.com", backend=Mailbox.Backend.GMAIL_API)
        self.client.force_login(self.me)
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        self.n = 0

    def thread(self, *senders, days=3):
        self.n += 1
        return MailThread.objects.create(mailbox=self.box, gmail_id=f"t{self.n}", subject="Re: PDA",
                                         senders=[list(s) for s in senders],
                                         last_message_at=timezone.now() - timedelta(days=days))

    def test_an_address_in_several_conversations_is_suggested_with_its_company(self):
        will = ("Will Edwards", "will@firehawkaerospace.com")
        self.thread(will)
        self.thread(will, ("Kellen", "kellen@acmigroup.com"), days=1)
        out = people_suggest.suggest(self.me)
        self.assertEqual([(p["email"], p["threads"]) for p in out], [("will@firehawkaerospace.com", 2)])
        self.assertEqual(out[0]["company"]["name"], "Firehawk Aerospace")

    def test_left_out_who_is_known_internal_automated_free_mail_or_only_once(self):
        Person.objects.create(first_name="Ann", last_name="Lee", email="ann@firehawkaerospace.com")
        Person.objects.create(first_name="Bo", last_name="Kim", email="bo@x.com", other_emails=["bo@firehawkaerospace.com"])
        for who in (("Ann", "ann@firehawkaerospace.com"), ("Bo", "bo@firehawkaerospace.com"),
                    ("Simon", "simon@acmigroup.com"), ("Bot", "no-reply@firehawkaerospace.com"),
                    ("Gmail guy", "guy@gmail.com"), ("Once", "once@firehawkaerospace.com")):
            self.thread(who, days=2)
            if who[0] != "Once":
                self.thread(who, days=5)
        self.assertEqual(people_suggest.suggest(self.me), [])

    def test_old_conversations_do_not_count(self):
        old = ("Old Friend", "old@firehawkaerospace.com")
        self.thread(old, days=400)
        self.thread(old, days=300)
        self.assertEqual(people_suggest.suggest(self.me), [])

    def test_dismiss_hides_the_address_for_this_person(self):
        will = ("Will Edwards", "will@firehawkaerospace.com")
        self.thread(will)
        self.thread(will)
        self.client.post(reverse("crm:person_suggest_dismiss"), {"email": "Will@FirehawkAerospace.com"})
        self.assertEqual(people_suggest.suggest(self.me), [])

    def test_one_click_add_makes_the_person_on_the_company(self):
        r = self.client.post(reverse("crm:person_suggest_add"),
                             {"email": "will@firehawkaerospace.com", "name": "Will Edwards", "company": self.company.pk})
        person = Person.objects.get(email="will@firehawkaerospace.com")
        self.assertRedirects(r, person.get_absolute_url())
        self.assertEqual((person.full_name, person.company, person.created_by), ("Will Edwards", self.company, self.me))
        again = self.client.post(reverse("crm:person_suggest_add"), {"email": "WILL@firehawkaerospace.com"})
        self.assertRedirects(again, person.get_absolute_url())  # no duplicate
        self.assertEqual(Person.objects.filter(email="will@firehawkaerospace.com").count(), 1)
        self.assertEqual(self.client.get(reverse("crm:person_suggest_add")).status_code, 405)

    def test_the_dashboard_widget_shows_the_suggestion(self):
        will = ("Will Edwards", "will@firehawkaerospace.com")
        self.thread(will)
        self.thread(will)
        r = self.client.get(reverse("crm:home"))
        self.assertContains(r, "People you email")
        self.assertContains(r, "will@firehawkaerospace.com")
        self.assertContains(r, reverse("crm:person_suggest_add"))
