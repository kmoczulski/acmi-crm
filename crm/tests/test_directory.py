"""The directory: consultants, vendors, lawyers, engineers and lenders are Firms, never Companies or People, so they
can't reach outreach. Their mail is filed on the firm; a Company can be moved over; project team firms link here."""
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from crm.forms import CompanyForm
from crm.mail import sync
from crm.models import (
    Activity, Comment, Company, Firm, FirmEmail, Mailbox, Meeting, Note, Opportunity, Person, Project, TeamMember,
    UnmatchedEmail,
)
from crm.tests.utils import make_manager

User = get_user_model()


def at(day, hour=10):
    return timezone.make_aware(datetime.combine(day, time(hour)))


class DirectoryBase(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.bank = Firm.objects.create(name="Truist Bank", kind="bank", domains=["truist.com"])
        self.terracon = Firm.objects.create(name="Terracon", kind="engineer", domains=["terracon.com"])


class FirmTests(DirectoryBase):
    def test_adding_a_firm_never_makes_a_company_or_person(self):
        r = self.client.post("/directory/", {"name": "Gordon Feinblatt", "kind": "law", "domains_text": "gfrlaw.com",
                                             "website": "", "notes": ""})
        firm = Firm.objects.get(name="Gordon Feinblatt")
        self.assertRedirects(r, firm.get_absolute_url(), fetch_redirect_response=False)
        self.client.post(firm.get_absolute_url(), {"action": "contact_add", "name": "Ann Lee", "email": "ann@gfrlaw.com"})
        self.assertFalse(Company.objects.exists())
        self.assertFalse(Person.objects.exists())
        self.assertFalse(firm.is_capital)
        self.assertTrue(self.bank.is_capital)

    def test_list_groups_and_pages_render(self):
        r = self.client.get("/directory/?group=services")
        self.assertContains(r, "Terracon")
        self.assertNotContains(r, "Truist Bank")
        r = self.client.get("/directory/?group=capital")
        self.assertContains(r, "Truist Bank")
        self.assertNotContains(r, "Terracon")
        self.assertContains(self.client.get("/directory/?q=terracon.com"), "Terracon")
        for url in ("/directory/", "/directory/?kind=engineer", self.terracon.get_absolute_url(),
                    self.bank.get_absolute_url()):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertRedirects(self.client.get("/capital/providers/"), "/directory/?group=capital",
                             fetch_redirect_response=False)

    def test_names_for_a_capital_source_are_only_lenders_and_investors(self):
        self.assertEqual(self.client.get("/directory/names/?group=capital").json(), ["Truist Bank"])
        self.assertEqual(self.client.get("/directory/names/").json(), ["Terracon", "Truist Bank"])

    def test_a_companys_domain_cant_go_on_a_firm(self):
        Company.objects.create(name="Cozen O'Connor", domains=["cozen.com"])
        self.client.post(self.terracon.get_absolute_url(), {"action": "details", "name": "Terracon", "kind": "engineer",
                                                            "domains_text": "terracon.com, cozen.com"})
        self.terracon.refresh_from_db()
        self.assertEqual(self.terracon.domains, ["terracon.com"])

    def test_a_firms_domain_cant_go_on_a_company(self):
        form = CompanyForm({"name": "Terracon Consultants", "status": "lead", "domains": "terracon.com"})
        self.assertFalse(form.is_valid())
        self.assertIn("domains", form.errors)
        with self.assertRaises(ValueError):
            sync.approve_domain("terracon.com")


class MailTests(DirectoryBase):
    def test_mail_sync_files_consultant_mail_on_the_firm(self):
        from crm.tests.test_mail import ME, FakeClient, raw_email

        # A Company whose name the domain spells would normally pick the domain up; a firm's domain never does.
        Company.objects.create(name="Terracon")
        mailbox = Mailbox.objects.create(user=self.user, email=ME)
        sync.sync_mailbox(mailbox, client=FakeClient([
            (1, "t1", raw_email("Gus <gus@terracon.com>", ME, "Geotech proposal, ARA")),
            (2, "t2", raw_email("Sam <sam@unknownco.com>", ME, "Hello")),
        ]))
        filed = FirmEmail.objects.get()
        self.assertEqual((filed.provider, filed.subject), (self.terracon, "Geotech proposal, ARA"))
        self.assertFalse(Person.objects.filter(email="gus@terracon.com").exists())
        self.assertEqual(Company.objects.get(name="Terracon").domains, [])
        self.assertEqual(list(UnmatchedEmail.objects.values_list("domain", flat=True)), ["unknownco.com"])

    def test_filing_a_review_queue_domain_in_the_directory(self):
        mailbox = Mailbox.objects.create(user=self.user, email="kmo@acmigroup.com")
        for i in (1, 2):
            UnmatchedEmail.objects.create(mailbox=mailbox, message_id=f"<q{i}>", occurred_at=at(self.today),
                                          kind="email_received", address="jo@greystar.com", domain="greystar.com",
                                          subject="Rents")
        r = self.client.post("/directory/file-domain/", {"domain": "greystar.com", "name": "Greystar",
                                                         "kind": "re_broker"})
        self.assertRedirects(r, "/mail/", fetch_redirect_response=False)
        firm = Firm.objects.get(name="Greystar")
        self.assertEqual((firm.kind, firm.domains), ("re_broker", ["greystar.com"]))
        self.assertEqual(FirmEmail.objects.filter(provider=firm).count(), 2)
        self.assertFalse(UnmatchedEmail.objects.exists())
        self.assertFalse(Company.objects.exists())
        # filing into a firm that already exists adds the domain to it
        UnmatchedEmail.objects.create(mailbox=mailbox, message_id="<q3>", occurred_at=at(self.today),
                                      kind="email_received", address="x@terracon-ga.com", domain="terracon-ga.com")
        self.client.post("/directory/file-domain/", {"domain": "terracon-ga.com", "name": "terracon", "kind": "other"})
        self.terracon.refresh_from_db()
        self.assertEqual(self.terracon.domains, ["terracon-ga.com", "terracon.com"])

    def test_mail_page_offers_file_in_directory(self):
        UnmatchedEmail.objects.create(mailbox=Mailbox.objects.create(user=self.user, email="kmo@acmigroup.com"),
                                      message_id="<q1>", occurred_at=at(self.today), kind="email_received",
                                      address="jo@greystar.com", domain="greystar.com")
        self.assertContains(self.client.get("/mail/"), "File in directory")


class MoveTests(DirectoryBase):
    def setUp(self):
        super().setUp()
        self.cozen = Company.objects.create(name="Cozen O'Connor", domains=["cozen.com"], flags=["Exclude"],
                                            sector="legal (outside counsel)", website="https://www.cozen.com",
                                            notes="Our outside counsel.")
        self.jc = Person.objects.create(full_name="Jonathan Ciriello", email="jciriello@cozen.com", title="Partner",
                                        company=self.cozen, phones=["215-555-0100"])
        Activity.objects.create(kind="email_received", occurred_at=at(self.today - timedelta(days=2)),
                                company=self.cozen, person=self.jc, summary="MBI B101 lease draft",
                                body="Attached is the draft.", source="gmail", source_id="g1", message_id="<c1>")
        Activity.objects.create(kind="call", occurred_at=at(self.today - timedelta(days=5)), company=self.cozen,
                                summary="Call about the ARA lease", source="user")
        Note.objects.create(company=self.cozen, body="Billing rate $650/hr")
        Comment.objects.create(company=self.cozen, body="@kellen check engagement letter", author=self.user)
        self.meeting = Meeting.objects.create(source="granola", external_id="m1", title="Cozen / ACMI: MBI B101",
                                              start=at(self.today - timedelta(days=1)),
                                              attendees=[{"name": "Jonathan", "email": "jciriello@cozen.com"}])
        self.meeting.companies.add(self.cozen)

    def test_move_turns_the_company_into_a_firm(self):
        r = self.client.get(f"/companies/{self.cozen.pk}/move-to-directory/")
        self.assertContains(r, "Jonathan Ciriello")
        r = self.client.post(f"/companies/{self.cozen.pk}/move-to-directory/", {"kind": "law"})
        firm = Firm.objects.get(name="Cozen O'Connor")
        self.assertRedirects(r, firm.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual((firm.kind, firm.domains, firm.website), ("law", ["cozen.com"], "https://www.cozen.com"))
        self.assertEqual(firm.contacts, [{"name": "Jonathan Ciriello", "title": "Partner",
                                          "email": "jciriello@cozen.com", "phone": "215-555-0100"}])
        email = FirmEmail.objects.get(provider=firm)
        self.assertEqual((email.address, email.subject, email.message_id),
                         ("jciriello@cozen.com", "MBI B101 lease draft", "<c1>"))
        for text in ("Our outside counsel.", "call: Call about the ARA lease", "note: Billing rate $650/hr",
                     "comment: @kellen check engagement letter"):
            self.assertIn(text, firm.notes)
        self.assertFalse(Company.objects.filter(pk=self.cozen.pk).exists())
        self.assertFalse(Person.objects.exists())
        self.assertFalse(Activity.objects.filter(source_id="g1").exists())
        self.assertTrue(Meeting.objects.filter(pk=self.meeting.pk).exists())
        page = self.client.get(firm.get_absolute_url())
        self.assertContains(page, "Cozen / ACMI: MBI B101")
        self.assertContains(page, "MBI B101 lease draft")

    def test_later_mail_from_the_moved_domain_is_filed_on_the_firm(self):
        from crm.tests.test_mail import ME, FakeClient, raw_email

        self.client.post(f"/companies/{self.cozen.pk}/move-to-directory/", {"kind": "law"})
        sync.sync_mailbox(Mailbox.objects.create(user=self.user, email=ME), client=FakeClient([
            (1, "t1", raw_email("Jonathan Ciriello <jciriello@cozen.com>", ME, "Revised lease")),
        ]))
        self.assertTrue(FirmEmail.objects.filter(subject="Revised lease", provider__name="Cozen O'Connor").exists())
        self.assertFalse(Company.objects.exists())
        self.assertFalse(Person.objects.exists())

    def test_move_joins_a_firm_of_the_same_name(self):
        firm = Firm.objects.create(name="cozen o'connor", kind="law", domains=["cozenlaw.com"])
        self.client.post(f"/companies/{self.cozen.pk}/move-to-directory/", {"kind": "consultant"})
        firm.refresh_from_db()
        self.assertEqual((firm.kind, firm.domains), ("law", ["cozen.com", "cozenlaw.com"]))
        self.assertEqual(Firm.objects.count(), 3)

    def test_a_tenant_deal_blocks_the_move(self):
        Opportunity.objects.create(name="Cozen space", company=self.cozen)
        self.assertContains(self.client.get(f"/companies/{self.cozen.pk}/move-to-directory/"), "1 tenant deal")
        self.client.post(f"/companies/{self.cozen.pk}/move-to-directory/", {"kind": "law"})
        self.assertTrue(Company.objects.filter(pk=self.cozen.pk).exists())
        self.assertFalse(Firm.objects.filter(name="Cozen O'Connor").exists())

    def test_only_managers_move(self):
        self.client.force_login(User.objects.create_user("simon"))
        self.client.post(f"/companies/{self.cozen.pk}/move-to-directory/", {"kind": "law"})
        self.assertFalse(Firm.objects.filter(name="Cozen O'Connor").exists())
        self.assertTrue(Company.objects.filter(pk=self.cozen.pk).exists())

    def test_company_page_links_to_the_move(self):
        self.assertContains(self.client.get(self.cozen.get_absolute_url()), "Move to directory")


class TeamTests(DirectoryBase):
    def test_a_team_firm_is_kept_in_the_directory(self):
        project = Project.objects.create(name="ARA Facility")
        civil = TeamMember.objects.create(project=project, role="civil", firm="  Bohler  Engineering ")
        self.assertEqual((civil.firm, civil.directory.name, civil.directory.kind),
                         ("Bohler Engineering", "Bohler Engineering", "engineer"))
        geo = TeamMember.objects.create(project=project, role="geotech", firm="terracon")
        self.assertEqual(geo.directory, self.terracon)
        geo.firm = "ECS"
        geo.save()
        self.assertEqual(geo.directory.name, "ECS")
        self.assertFalse(Company.objects.exists())
        page = self.client.get(self.terracon.get_absolute_url())
        self.assertEqual(page.status_code, 200)
        page = self.client.get(civil.directory.get_absolute_url())
        self.assertContains(page, "ARA Facility: Civil engineer")
        build = self.client.get(f"/projects/{project.pk}/build/")
        self.assertContains(build, civil.directory.get_absolute_url())
        self.assertContains(build, 'list="firm-names"')

    def test_team_add_through_the_build_page(self):
        project = Project.objects.create(name="NSIH")
        self.client.post(f"/projects/{project.pk}/build/", {"action": "team_add", "role": "attorney",
                                                            "firm": "Cozen O'Connor", "status": "engaged"})
        firm = Firm.objects.get(name="Cozen O'Connor")
        self.assertEqual(firm.kind, "law")
        self.assertEqual(firm.team_roles.get().project, project)
