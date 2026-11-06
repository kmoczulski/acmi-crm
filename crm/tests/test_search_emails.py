"""Searching a company brings up its emails the way meetings come up (Kellen, 2026-11-06: "if I search a company
their emails should pop up like their meetings")."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Activity, Company, Firm, FirmEmail, Meeting, Person

User = get_user_model()


class EmailSearchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x")
        self.client.force_login(self.user)
        self.acme = Company.objects.create(name="Acme Aerospace")
        self.pat = Person.objects.create(full_name="Pat Rivera", email="pat@acme.test", company=self.acme)
        self.now = timezone.now()
        self.n = 0

    def email(self, subject, days_ago=1, kind="email_received", thread=None, company="acme", person="pat", **kw):
        self.n += 1
        who = "to" if kind == "email_sent" else "from"
        return Activity.objects.create(
            kind=kind, occurred_at=self.now - timedelta(days=days_ago),
            company=self.acme if company == "acme" else company, person=self.pat if person == "pat" else person,
            summary=f"Email {who} Pat Rivera: {subject}", source=Activity.Source.GMAIL, source_id=f"gmail:m{self.n}",
            message_id=f"<m{self.n}@acme.test>", thread_id=thread if thread is not None else str(1000 + self.n), **kw)

    def groups(self, q):
        data = self.client.get(reverse("crm:search"), {"q": q}).json()["groups"]
        return {g["label"]: g["items"] for g in data}

    def test_searching_a_company_lists_its_emails_newest_first_one_row_per_conversation(self):
        self.email("Lease draft 2", days_ago=1, kind="email_sent", thread="777")
        self.email("Lease draft 1", days_ago=3, thread="777")  # the same conversation, older
        self.email("Power study", days_ago=2)
        rows = self.groups("acme")["Emails"]
        self.assertEqual([r["title"] for r in rows], ["Lease draft 2", "Power study"])
        self.assertEqual(rows[0]["badge"], "Sent")
        self.assertEqual(rows[1]["badge"], "Received")
        self.assertIn("Acme Aerospace", rows[0]["sub"])
        self.assertIn("Pat Rivera", rows[0]["sub"])

    def test_a_person_search_finds_their_emails_too(self):
        self.email("Site visit")
        self.assertEqual([r["title"] for r in self.groups("pat riv")["Emails"]], ["Site visit"])

    def test_a_word_in_the_subject_finds_an_email_with_nobody_matching(self):
        other = Company.objects.create(name="Zeta Works")
        self.email("Lease redline from counsel", company=other, person=None)
        self.assertEqual([r["title"] for r in self.groups("redline")["Emails"]], ["Lease redline from counsel"])

    def test_nothing_is_listed_when_no_email_matches(self):
        self.assertNotIn("Emails", self.groups("acme"))

    def test_at_most_five_rows(self):
        for i in range(8):
            self.email(f"Note {i}", days_ago=i + 1)
        self.assertEqual(len(self.groups("acme")["Emails"]), 5)

    def test_an_email_in_your_mailbox_opens_in_mail(self):
        mine = self.email("Lease draft", thread="1234567890123")
        mine.team_members.add(self.user)
        with mock.patch("crm.mail.connect.is_on", return_value=True):
            url = self.groups("acme")["Emails"][0]["url"]
        self.assertEqual(url, f"/inbox/?label=ALL&t={1234567890123:x}")

    def test_without_a_thread_it_opens_by_message_id(self):
        mine = self.email("Lease draft", thread="")
        mine.team_members.add(self.user)
        with mock.patch("crm.mail.connect.is_on", return_value=True):
            url = self.groups("acme")["Emails"][0]["url"]
        self.assertTrue(url.startswith("/inbox/open/?m="), url)
        self.assertIn("m1%40acme.test", url)

    def test_an_email_in_someone_elses_mailbox_opens_where_it_is_logged(self):
        other = User.objects.create_user("simon", password="x")
        theirs = self.email("Lease draft")
        theirs.team_members.add(other)
        with mock.patch("crm.mail.connect.is_on", return_value=True):
            url = self.groups("acme")["Emails"][0]["url"]
        self.assertEqual(url, self.acme.get_absolute_url() + "?tab=activity")

    def test_with_mail_off_it_opens_where_it_is_logged(self):
        mine = self.email("Lease draft")
        mine.team_members.add(self.user)
        with mock.patch("crm.mail.connect.is_on", return_value=False):
            url = self.groups("acme")["Emails"][0]["url"]
        self.assertEqual(url, self.acme.get_absolute_url() + "?tab=activity")

    def test_bounces_calendar_replies_and_background_mail_stay_out(self):
        self.email("Undeliverable", kind="bounce")
        self.email("Out of office", kind="auto_reply")
        self.email("Accepted: Site walk", kind="email_received")
        self.email("Background thing", is_background=True)
        self.email("Real conversation")
        self.assertEqual([r["title"] for r in self.groups("acme")["Emails"]], ["Real conversation"])

    def test_mail_filed_on_a_directory_firm_shows_for_the_firm(self):
        firm = Firm.objects.create(name="Fortress Capital", domains=["fortress.test"])
        FirmEmail.objects.create(provider=firm, message_id="<f1>", thread_id="55", occurred_at=self.now - timedelta(days=2),
                                 kind="email_received", address="pat@fortress.test", name="Pat Fortress",
                                 subject="MEIH term sheet")
        rows = self.groups("fortress")["Emails"]
        self.assertEqual([r["title"] for r in rows], ["MEIH term sheet"])
        self.assertEqual(rows[0]["url"], firm.get_absolute_url())
        self.assertIn("Fortress Capital", rows[0]["sub"])

    def test_a_word_in_the_subject_finds_mail_filed_on_a_firm(self):
        firm = Firm.objects.create(name="Fortress Capital", domains=["fortress.test"])
        FirmEmail.objects.create(provider=firm, message_id="<f1>", thread_id="55", occurred_at=self.now - timedelta(days=2),
                                 kind="email_received", address="pat@fortress.test", subject="MEIH term sheet comments")
        rows = self.groups("term sheet")["Emails"]
        self.assertEqual([(r["title"], r["url"]) for r in rows], [("MEIH term sheet comments", firm.get_absolute_url())])

    def test_company_mail_and_firm_mail_sort_together(self):
        firm = Firm.objects.create(name="Acme Capital", domains=["acmecapital.test"])
        FirmEmail.objects.create(provider=firm, message_id="<f1>", thread_id="55", occurred_at=self.now - timedelta(days=2),
                                 kind="email_sent", address="x@acmecapital.test", subject="Loan question")
        self.email("Newest from the tenant", days_ago=1)
        self.email("Oldest from the tenant", days_ago=5)
        self.assertEqual([r["title"] for r in self.groups("acme")["Emails"]],
                         ["Newest from the tenant", "Loan question", "Oldest from the tenant"])

    def test_emails_sit_right_before_meetings(self):
        self.email("Lease draft")
        Meeting.objects.create(source="granola", external_id="g1", title="Acme kickoff", start=self.now - timedelta(days=2))
        labels = list(self.groups("acme"))
        self.assertEqual(labels.index("Emails") + 1, labels.index("Meetings"))

    def test_one_letter_finds_nothing(self):
        self.email("Lease draft")
        self.assertEqual(self.client.get(reverse("crm:search"), {"q": "a"}).json(), {"groups": []})
