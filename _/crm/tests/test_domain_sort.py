"""Claude sorting email domains (crm/directory/sort.py): sure vendors are filed in the directory with Undo, the rest are
suggestions on Needs filing, existing Companies are only flagged, and nothing ever creates a Company. Claude is mocked."""
import json
from datetime import datetime, time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from crm.directory import sort
from crm.models import Activity, Company, DomainSort, Firm, FirmEmail, Mailbox, Notification, Opportunity, UnmatchedEmail
from crm.tests.test_contracts import fake_claude, text_block
from crm.tests.utils import make_manager

User = get_user_model()
KEY = {"ANTHROPIC_API_KEY": "test"}


def at(day, hour=10):
    return timezone.make_aware(datetime.combine(day, time(hour)))


def result(id_, verdict, kind="", name="", confidence="high", reason="", contacts=()):
    return {"id": id_, "verdict": verdict, "kind": kind, "name": name, "confidence": confidence, "reason": reason,
            "contacts": list(contacts)}


def claude(*results):
    return fake_claude([text_block(json.dumps({"results": list(results)}))])


class SortBase(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.mailbox = Mailbox.objects.create(user=self.user, email="kmo@acmigroup.com")

    def queue(self, address, subject, snippet="", n=1, name=""):
        domain = address.split("@")[1]
        for i in range(n):
            UnmatchedEmail.objects.create(mailbox=self.mailbox, message_id=f"<{domain}-{i}>", occurred_at=at(self.today),
                                          kind="email_received", address=address, domain=domain, name=name,
                                          subject=subject, snippet=snippet)


@mock.patch.dict("os.environ", KEY)
class AutoSortTests(SortBase):
    def test_sure_vendor_is_filed_others_are_suggestions(self):
        self.queue("gus@terracon.com", "Geotech proposal for ARA", "Gus Ortiz, PE | Senior Engineer | Terracon | "
                   "301-555-0101", n=3, name="Gus Ortiz")
        self.queue("pat@firehawk.test", "Facility needs at MEIH", name="Pat Lee")
        self.queue("news@bigdigest.test", "This week in defense")
        self.queue("ann@maybe.test", "Hello")
        patch, stream = claude(
            result("terracon.com", "directory", "engineer", "Terracon", "high", "Signature: Senior Engineer, Terracon; "
                   "a geotech proposal.", [{"name": "Gus Ortiz", "title": "Senior Engineer", "email": "gus@terracon.com",
                                            "phone": "301-555-0101"},
                                           {"name": "Made Up", "title": "", "email": "x@elsewhere.test", "phone": ""}]),
            result("firehawk.test", "company", "", "Firehawk", "high", "Asks about facility space at MEIH."),
            result("bigdigest.test", "ignore", confidence="high", reason="A newsletter."),
            result("maybe.test", "directory", "vendor", "Maybe Co", "medium", "Might be a vendor."),
        )
        with patch:
            stats = sort.auto_sort()
        self.assertEqual(stats, {"sorted": 4, "filed in the directory": 1})
        sent = stream.call_args.kwargs
        self.assertEqual(sent["output_config"]["effort"], "low")
        self.assertIn("Gus Ortiz, PE", sent["messages"][0]["content"][0]["text"])

        firm = Firm.objects.get(name="Terracon")
        self.assertEqual((firm.kind, firm.domains), ("engineer", ["terracon.com"]))
        self.assertEqual([c["name"] for c in firm.contacts], ["Gus Ortiz"])  # a contact at another domain is dropped
        self.assertEqual(FirmEmail.objects.filter(provider=firm).count(), 3)
        self.assertFalse(UnmatchedEmail.objects.filter(domain="terracon.com").exists())
        note = Notification.objects.get(user=self.user)
        self.assertIn("Claude filed terracon.com", note.text)
        # nothing else moved, and no Company was made
        self.assertEqual(set(UnmatchedEmail.objects.values_list("domain", flat=True)),
                         {"firehawk.test", "bigdigest.test", "maybe.test"})
        self.assertFalse(Company.objects.exists())
        self.assertEqual(DomainSort.objects.get(domain="maybe.test").status, "suggested")
        self.assertContains(self.client.get("/needs-filing/"), 'placeholder="Firehawk"')

        # A second run doesn't ask Claude about domains it has seen.
        patch, stream = claude()
        with patch:
            self.assertEqual(sort.auto_sort()["sorted"], 0)
        stream.assert_not_called()

    def test_one_bell_note_per_run(self):
        self.queue("gus@terracon.com", "Proposal")
        self.queue("jo@gfrlaw.test", "Engagement letter")
        patch, _ = claude(result("terracon.com", "directory", "engineer", "Terracon", "high", "A proposal."),
                          result("gfrlaw.test", "directory", "law", "GFR", "high", "An engagement letter."))
        with patch:
            self.assertEqual(sort.auto_sort()["filed in the directory"], 2)
        note = Notification.objects.get(user=self.user)
        self.assertIn("Claude filed 2 domains", note.text)
        self.assertEqual(note.url, "/directory/suggestions/")

    def test_mail_review_shows_suggestions_and_file_as_claude_says(self):
        self.queue("jo@gfrlaw.test", "Engagement letter", n=2)
        patch, _ = claude(result("gfrlaw.test", "directory", "law", "Gordon Feinblatt", "medium", "An engagement letter."))
        with patch:
            sort.auto_sort()
        page = self.client.get("/needs-filing/")
        self.assertContains(page, "An engagement letter.")
        self.assertContains(page, "Accept: file as Claude says")
        s = DomainSort.objects.get()
        self.client.post(f"/directory/sorts/{s.pk}/", {"action": "file", "back": "mail"})
        firm = Firm.objects.get(name="Gordon Feinblatt")
        self.assertEqual(firm.kind, "law")
        s.refresh_from_db()
        self.assertEqual((s.status, s.decided_by), ("done", self.user))
        self.assertFalse(Notification.objects.filter(kind=Notification.Kind.DIRECTORY).exists())  # a person did it

    def test_undo_puts_the_mail_back(self):
        self.queue("gus@terracon.com", "Geotech proposal", n=2)
        patch, _ = claude(result("terracon.com", "directory", "engineer", "Terracon", "high", "A proposal.",
                                 [{"name": "Gus", "title": "", "email": "gus@terracon.com", "phone": ""}]))
        with patch:
            sort.auto_sort()
        s = DomainSort.objects.get()
        self.assertContains(self.client.get(s.firm.get_absolute_url()), "Claude filed terracon.com here")
        self.assertContains(self.client.get("/directory/suggestions/"), "terracon.com")
        self.client.post(f"/directory/sorts/{s.pk}/", {"action": "undo"})
        self.assertEqual(UnmatchedEmail.objects.filter(domain="terracon.com").count(), 2)
        self.assertFalse(Firm.objects.exists())  # the filing started it and nothing else is on it
        self.assertFalse(FirmEmail.objects.exists())
        s.refresh_from_db()
        self.assertEqual(s.status, "undone")
        patch, stream = claude()
        with patch:
            sort.auto_sort()
        stream.assert_not_called()  # never filed automatically again

    def test_undo_keeps_a_firm_that_was_already_there(self):
        firm = Firm.objects.create(name="Terracon", kind="engineer", domains=["terracon-ga.test"])
        self.queue("gus@terracon.com", "Proposal")
        patch, _ = claude(result("terracon.com", "directory", "engineer", "Terracon", "high", "A proposal."))
        with patch:
            sort.auto_sort()
        firm.refresh_from_db()
        self.assertEqual(firm.domains, ["terracon-ga.test", "terracon.com"])
        sort.undo(DomainSort.objects.get())
        firm.refresh_from_db()
        self.assertEqual(firm.domains, ["terracon-ga.test"])

    def test_claude_off_or_failing_never_breaks_the_sync(self):
        self.queue("gus@terracon.com", "Proposal")
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(sort.auto_sort()["sorted"], 0)
        patch, _ = fake_claude([text_block("not json")])
        with patch:
            self.assertEqual(sort.auto_sort()["sorted"], 0)
        self.assertTrue(UnmatchedEmail.objects.exists())


@mock.patch.dict("os.environ", KEY)
class CompanyReviewTests(SortBase):
    def setUp(self):
        super().setUp()
        self.law = Company.objects.create(name="Sample Law LLP", domains=["samplelaw.test"], sector="legal (outside counsel)")
        self.consult = Company.objects.create(name="Defense Consulting Co", sector="defense consulting")
        self.tenant = Company.objects.create(name="Tenant Engineering", sector="engineering services")
        Opportunity.objects.create(name="Space", company=self.tenant)  # a deal: never a candidate
        Company.objects.create(name="Firehawk Aerospace", sector="propulsion")  # not service-like
        Activity.objects.create(company=self.law, kind="email_received", occurred_at=at(self.today),
                                summary="Lease draft", body="Jordan Counsel, Partner", source="gmail", source_id="a1")

    def test_flags_only_and_never_moves(self):
        self.assertEqual({c.name for c in sort.company_candidates()}, {"Sample Law LLP", "Defense Consulting Co"})
        patch, stream = claude(
            result(str(self.law.pk), "directory", "law", "Sample Law", "high", "Outside counsel; lease drafts."),
            result(str(self.consult.pk), "company", "", "", "medium", "Defense consultant; could lease space."),
        )
        with patch:
            r = self.client.post("/directory/suggestions/")
        self.assertRedirects(r, "/directory/suggestions/", fetch_redirect_response=False)
        self.assertIn("Lease draft", stream.call_args.kwargs["messages"][0]["content"][0]["text"])
        self.assertEqual(Company.objects.count(), 4)  # nothing moved
        page = self.client.get("/directory/suggestions/")
        self.assertContains(page, "Outside counsel; lease drafts.")
        self.assertContains(page, f"/companies/{self.law.pk}/move-to-directory/?kind=law")
        self.assertNotContains(page, "could lease space")
        self.assertEqual(sort.company_candidates(), [])  # both checked
        move = self.client.get(f"/companies/{self.law.pk}/move-to-directory/?kind=law")
        self.assertContains(move, '<option value="law" selected>')
        s = DomainSort.objects.get(company=self.law)
        self.client.post(f"/directory/sorts/{s.pk}/", {"action": "dismiss"})
        self.assertNotContains(self.client.get("/directory/suggestions/"), "Outside counsel; lease drafts.")

    def test_only_managers_run_the_check(self):
        self.client.force_login(User.objects.create_user("simon"))
        patch, stream = claude()
        with patch:
            self.client.post("/directory/suggestions/")
        stream.assert_not_called()
