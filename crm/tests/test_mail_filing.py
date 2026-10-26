"""Mail in the hub, step 6: File to a record, CRM labels in Gmail, team notes, Open in Mail."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import prefs, projects
from crm.mail import crm_labels, filing, gmail_api, index
from crm.models import (
    Activity, Company, Firm, FirmEmail, MailCrmLabel, MailCrmLabelOnThread, MailFiling, MailThread, Mailbox,
    Notification, Opportunity, Person, Project, UnmatchedEmail,
)
from crm.permissions import PARTNERS

from .test_gmail_api import ME, ON, FakeGmail, api_message
from .test_mail_actions import connected_user

SAM = "Sam Ortiz <sam.ortiz@gmail.com>"


@override_settings(**ON)
class FilingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        self.project = Project.objects.create(name="MEIH")
        self.firm = Firm.objects.create(name="Cozen O'Connor", kind=Firm.Kind.LAW, domains=["cozen.com"])
        now = timezone.now()
        self.fake = FakeGmail([
            api_message("0a01", "a1", SAM, ME, "Site for our plant", body="We need 40 acres.",
                        when=now - timedelta(hours=3), msg_id="<sam-1@gmail>"),
            api_message("0a02", "a1", ME, SAM, "Re: Site for our plant", body="Let's talk.",
                        when=now - timedelta(hours=2), labels=("SENT",), msg_id="<me-1@acmi>"),
            api_message("0b01", "b2", "Jon <jon@cozen.com>", ME, "Title commitment", msg_id="<jon-1@cozen>"),
        ])
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = gmail_api.GmailClient(self.user, self.fake)
        index.refresh(self.mailbox, self.api, ["a1", "b2"])

    def file(self, kind, pk, t="a1", **extra):
        return self.client.post(reverse("crm:inbox_file"), {"t": t, "kind": kind, "pk": pk,
                                                            "next": f"/inbox/?label=INBOX&t={t}", **extra})

    def emails(self, **filters):
        return Activity.objects.filter(source=Activity.Source.GMAIL, **filters)

    def test_the_picker_finds_records_of_every_kind(self):
        Opportunity.objects.create(name="Firehawk MEIH lease", company=self.company)
        r = self.client.get(reverse("crm:inbox_file_options"), {"q": "fire"})
        kinds = {o["kind"] for o in r.json()["options"]}
        self.assertEqual(kinds, {"company", "opportunity"})
        self.assertEqual(self.client.get(reverse("crm:inbox_file_options"), {"q": "f"}).json()["options"], [])

    def test_filing_a_gmail_sender_to_a_company_puts_the_thread_on_its_timeline(self):
        r = self.file("company", self.company.pk)
        self.assertRedirects(r, "/inbox/?label=INBOX&t=a1", fetch_redirect_response=False)
        acts = self.emails(company=self.company).order_by("occurred_at")
        self.assertEqual([a.kind for a in acts], [Activity.Kind.EMAIL_RECEIVED, Activity.Kind.EMAIL_SENT])
        self.assertEqual(acts[0].source_id, f"gmail:<sam-1@gmail>|c{self.company.pk}")
        self.assertEqual(acts[0].thread_id, str(0xa1))
        f = MailFiling.objects.get()
        self.assertEqual((f.target, f.subject, f.filed_by), (self.company, "Site for our plant", self.user))
        self.file("company", self.company.pk)  # again: nothing twice
        self.assertEqual(self.emails(company=self.company).count(), 2)
        r = self.client.get(reverse("crm:inbox"), {"t": "a1"})
        self.assertContains(r, "Filed to")
        self.client.post(reverse("crm:inbox_unfile", args=[f.pk]), {"next": "/inbox/"})
        self.assertFalse(self.emails(company=self.company).exists())
        self.assertFalse(MailFiling.objects.exists())

    def test_mail_the_sync_already_logged_is_not_logged_again_or_removed(self):
        logged = Activity.objects.create(kind=Activity.Kind.EMAIL_RECEIVED, occurred_at=timezone.now(),
                                         company=self.company, summary="x", source=Activity.Source.GMAIL,
                                         source_id=f"gmail:<sam-1@gmail>|c{self.company.pk}",
                                         message_id="<sam-1@gmail>", thread_id=str(0xa1))
        self.file("company", self.company.pk)
        f = MailFiling.objects.get()
        self.assertNotIn(logged.pk, f.added_activities)
        filing.unfile(f)
        self.assertTrue(Activity.objects.filter(pk=logged.pk).exists())

    def test_filing_to_a_deal_links_the_thread_to_it(self):
        deal = Opportunity.objects.create(name="Firehawk MEIH lease", company=self.company)
        self.file("opportunity", deal.pk)
        self.assertEqual(self.emails(opportunity=deal).count(), 2)
        filing.unfile(MailFiling.objects.get())
        self.assertFalse(self.emails(opportunity=deal).exists())

    def test_filing_to_a_project_survives_the_keyword_tagger(self):
        self.file("project", self.project.pk)
        acts = self.emails(projects=self.project)
        self.assertEqual(acts.count(), 2)
        projects.tag(thread_ids=[str(0xa1)])  # no MEIH keyword in the thread, and it stays tagged
        self.assertEqual(self.emails(projects=self.project).count(), 2)
        self.assertEqual(set(acts.values_list("context", flat=True)), {Activity.Context.PROJECT})
        filing.unfile(MailFiling.objects.get())
        self.assertFalse(self.emails(projects=self.project).exists())

    def test_filing_to_a_directory_firm_never_makes_a_company(self):
        self.file("firm", self.firm.pk, t="b2")
        self.assertEqual(FirmEmail.objects.filter(provider=self.firm).count(), 1)
        self.assertFalse(Company.objects.exclude(pk=self.company.pk).exists())
        filing.unfile(MailFiling.objects.get())
        self.assertFalse(FirmEmail.objects.exists())

    def test_a_domain_waiting_in_mail_review_can_come_along(self):
        self.fake.add(api_message("0c01", "c3", "Pat <pat@newco.io>", ME, "Intro", msg_id="<pat-1@newco>"))
        UnmatchedEmail.objects.create(message_id="<older@newco>", address="pat@newco.io", domain="newco.io",
                                      occurred_at=timezone.now() - timedelta(days=9), kind="email_received",
                                      mailbox=self.mailbox, subject="Earlier note")
        self.file("company", self.company.pk, t="c3", attach_domain="1")
        self.company.refresh_from_db()
        self.assertIn("newco.io", self.company.domains)
        self.assertFalse(UnmatchedEmail.objects.exists())
        self.assertTrue(self.emails(company=self.company, message_id="<older@newco>").exists())

    def test_bad_picks_and_partners(self):
        r = self.file("company", 99999)
        self.assertEqual(MailFiling.objects.count(), 0)
        self.assertRedirects(r, "/inbox/?label=INBOX&t=a1", fetch_redirect_response=False)
        partner = connected_user("fortress", "lp@acmigroup.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(partner)
        self.assertRedirects(self.file("company", self.company.pk), "/room/", fetch_redirect_response=False)


@override_settings(**ON)
class CrmLabelTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        self.project = Project.objects.create(name="MEIH")
        self.fake = FakeGmail([api_message("0a01", "a1", "Will <will@firehawkaerospace.com>", ME, "PDA",
                                           msg_id="<will-1@fh>")])
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = gmail_api.GmailClient(self.user, self.fake)
        index.refresh(self.mailbox, self.api, ["a1"])

    def thread_labels(self):
        return {lb for m in self.fake.messages.values() if m["threadId"] == "a1" for lb in m["labelIds"]}

    def name_of(self, label_id):
        return next(lb["name"] for lb in self.fake.label_list if lb["id"] == label_id)

    def logged(self):
        Activity.objects.create(kind=Activity.Kind.EMAIL_RECEIVED, occurred_at=timezone.now(), company=self.company,
                                summary="x", source=Activity.Source.GMAIL, source_id="gmail:<will-1@fh>|c1",
                                message_id="<will-1@fh>", thread_id=str(0xa1)).projects.add(self.project)

    def test_projects_and_companies_with_an_open_deal_get_labels_with_their_parents(self):
        self.logged()
        Opportunity.objects.create(name="Lease", company=self.company)
        n = crm_labels.sync_threads(self.mailbox, self.api, list(MailThread.objects.all()))
        self.assertEqual(n, 1)
        names = {self.name_of(i) for i in self.thread_labels() if i.startswith("Label_")}
        self.assertEqual(names, {"ACMI/Projects/MEIH", "ACMI/Tenants/Firehawk Aerospace"})
        self.assertTrue({"ACMI", "ACMI/Projects", "ACMI/Tenants"} <= {lb["name"] for lb in self.fake.label_list})
        crm_labels.sync_threads(self.mailbox, self.api, list(MailThread.objects.all()))
        self.assertEqual(sum(1 for p, _ in self.fake.posts if p == "/labels"), 5)  # made once

    def test_a_company_without_an_open_deal_gets_no_label(self):
        self.logged()
        crm_labels.sync_threads(self.mailbox, self.api, list(MailThread.objects.all()))
        names = {self.name_of(i) for i in self.thread_labels() if i.startswith("Label_")}
        self.assertEqual(names, {"ACMI/Projects/MEIH"})

    def test_taken_off_in_gmail_stays_off(self):
        self.logged()
        crm_labels.sync_threads(self.mailbox, self.api, list(MailThread.objects.all()))
        label = MailCrmLabel.objects.get(project=self.project)
        self.api.modify_thread("a1", remove=[label.gmail_label_id])
        rows = list(index.refresh(self.mailbox, self.api, ["a1"]).values())
        crm_labels.sync_threads(self.mailbox, self.api, rows)
        self.assertNotIn(label.gmail_label_id, self.thread_labels())
        self.assertTrue(MailCrmLabelOnThread.objects.get(label=label).removed)

    def test_added_in_gmail_files_the_thread(self):
        self.fake.add(api_message("0b01", "b2", "Will <will@firehawkaerospace.com>", ME, "Site visit",
                                  msg_id="<will-2@fh>"))
        self.logged()
        crm_labels.sync_threads(self.mailbox, self.api, list(MailThread.objects.all()))
        label = MailCrmLabel.objects.get(project=self.project)
        self.api.modify_thread("b2", add=[label.gmail_label_id])  # the person labels another thread in Gmail
        rows = list(index.refresh(self.mailbox, self.api, ["b2"]).values())
        crm_labels.sync_threads(self.mailbox, self.api, rows)
        f = MailFiling.objects.get(gmail_id="b2")
        self.assertEqual((f.project, f.source), (self.project, MailFiling.Source.GMAIL))

    def test_renaming_a_project_renames_its_label(self):
        self.logged()
        crm_labels.sync_threads(self.mailbox, self.api, list(MailThread.objects.all()))
        Project.objects.filter(pk=self.project.pk).update(name="MEIH Campus")
        crm_labels.rename_all(self.mailbox, self.api)
        label = MailCrmLabel.objects.get(project=self.project)
        self.assertEqual(self.name_of(label.gmail_label_id), "ACMI/Projects/MEIH Campus")

    def test_the_switch(self):
        self.logged()
        prefs.set_pref(self.user, "mail_crm_labels", False)
        self.assertEqual(crm_labels.sync_threads(self.mailbox, self.api, list(MailThread.objects.all())), 0)
        self.assertFalse(MailCrmLabel.objects.exists())
        self.client.force_login(self.user)
        r = self.client.post(reverse("crm:mail_crm_labels"), {"on": "1"}, follow=True)
        self.assertContains(r, "CRM labels in Gmail are on.")
        self.assertTrue(crm_labels.enabled(self.user))


@override_settings(**ON)
class NotesAndOpenInMailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.kellen = connected_user()
        self.simon = connected_user("simon", "sshewmaker@acmigroup.com")
        self.simon.first_name = "Simon"
        self.simon.save()
        self.dave = connected_user("dave", "ddowell@acmigroup.com")
        for u in (self.kellen, self.simon, self.dave):
            Mailbox.objects.create(user=u, email=u.email, backend=Mailbox.Backend.GMAIL_API)
        # The same email in two mailboxes, under different Gmail ids.
        self.fake = FakeGmail([api_message("0a01", "a1", "Will <will@x.com>", ME, "Survey", msg_id="<survey-1@x>"),
                               api_message("0f01", "f9", "Will <will@x.com>", ME, "Survey", msg_id="<survey-1@x>")])
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_note_is_seen_by_its_author_and_whoever_it_mentions(self):
        self.client.force_login(self.kellen)
        self.client.post(reverse("crm:inbox_note"), {"t": "a1", "body": "@Simon please check section 4 of the survey",
                                                     "next": "/inbox/?t=a1"})
        note = Notification.objects.get(user=self.simon, kind=Notification.Kind.MENTION)
        self.assertIn("on “Survey”: @Simon please check section 4", note.text)
        self.assertEqual(note.url, f"{reverse('crm:inbox_open')}?m=<survey-1@x>")
        self.assertContains(self.client.get(reverse("crm:inbox"), {"t": "a1"}), "please check section 4")
        self.client.force_login(self.simon)
        self.assertContains(self.client.get(reverse("crm:inbox"), {"t": "f9"}), "please check section 4")
        self.client.force_login(self.dave)
        self.assertNotContains(self.client.get(reverse("crm:inbox"), {"t": "f9"}), "please check section 4")

    def test_open_in_mail_finds_the_email_in_your_own_mailbox(self):
        self.client.force_login(self.kellen)
        r = self.client.get(reverse("crm:inbox_open"), {"m": "<survey-1@x>"})
        self.assertRedirects(r, f"{reverse('crm:inbox')}?label=ALL&t=f9", fetch_redirect_response=False)
        r = self.client.get(reverse("crm:inbox_open"), {"m": "<nope@x>"}, follow=True)
        self.assertContains(r, "That email isn&#x27;t in your mailbox.")

    def test_timelines_link_to_mail(self):
        company = Company.objects.create(name="X Corp", domains=["x.com"])
        Person.objects.create(first_name="Will", email="will@x.com", company=company)
        Activity.objects.create(kind=Activity.Kind.EMAIL_RECEIVED, occurred_at=timezone.now(), company=company,
                                summary="Email from Will: Survey", source=Activity.Source.GMAIL,
                                source_id="gmail:<survey-1@x>|c1", message_id="<survey-1@x>", thread_id="161")
        self.client.force_login(self.kellen)
        r = self.client.get(company.get_absolute_url(), {"tab": "activity"})
        self.assertContains(r, f"{reverse('crm:inbox_open')}?m=%3Csurvey-1%40x%3E")
