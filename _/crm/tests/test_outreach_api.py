"""Mail in the hub, step 8: outreach, test sends, the weekly report, campus updates and the news review's drafts go
through the Gmail API for people with Mail on, so the app password can be retired."""
from email import message_from_bytes, policy
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from crm.mail import gmail_api, outbound, sync
from crm.models import (
    Activity, Company, EmailTemplate, MailConnection, Mailbox, OutreachSettings, Person, SendBatch, SendItem,
)
from crm.news import drafts as gmail_drafts
from crm.outreach import sender

from .test_gmail_api import ME, ON, FakeGmail, api_message
from .test_mail_actions import connected_user


def no_smtp(*args, **kwargs):
    raise AssertionError("SMTP must not be used when Mail in the hub is on")


@override_settings(**ON)
class OutreachThroughGmailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API,
                                              backfill_done=True)
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        self.will = Person.objects.create(first_name="Will", email="will@firehawkaerospace.com", company=self.company)
        self.pat = Person.objects.create(first_name="Pat", email="pat@firehawkaerospace.com", company=self.company)
        self.template = EmailTemplate.objects.create(template_id="T1", name="Intro", subject="Campus space for {company}",
                                                     body_html="<p>Hi {first_name}</p>", body_plain="Hi {first_name}")
        OutreachSettings.objects.update_or_create(pk=1, defaults={"sender_name": "Kellen Moczulski", "delay_seconds": 0})
        self.fake = FakeGmail()
        patchers = [mock.patch.object(gmail_api, "requests", self.fake),
                    mock.patch("crm.outreach.sender.smtplib.SMTP", side_effect=no_smtp)]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def batch(self, *people):
        b = SendBatch.objects.create(created_by=self.user, campaign="Fall")
        for person in people:
            SendItem.objects.create(batch=b, person=person, email=person.email, template=self.template)
        return b

    def test_who_goes_through_the_api(self):
        self.assertTrue(outbound.uses_api(self.mailbox))
        MailConnection.objects.filter(user=self.user).update(enabled=False)
        self.assertFalse(outbound.uses_api(self.mailbox))
        self.assertTrue(outbound.uses_api(Mailbox(shared=True, email="deals@acmigroup.com")))

    def test_a_batch_goes_through_gmail_and_is_logged_with_gmails_own_ids(self):
        self.fake.rewrite_message_id = True
        b = sender.run_batch(self.batch(self.will, self.pat).pk, sleep=lambda s: None)
        self.assertEqual(b.status, SendBatch.Status.DONE)
        self.assertEqual(len(self.fake.sent), 2)
        msg = message_from_bytes(self.fake.sent[0][1], policy=policy.default)
        self.assertEqual(msg["Subject"], "Campus space for Firehawk Aerospace")
        item = SendItem.objects.get(person=self.will)
        self.assertTrue(item.message_id.startswith("<gmail-"))  # what Gmail really sent with
        act = Activity.objects.get(message_id=item.message_id)
        self.assertEqual((act.company, act.campaign, act.kind), (self.company, "Fall", Activity.Kind.EMAIL_SENT))
        self.assertNotEqual(act.thread_id, "")
        # The sync reading the Sent copies later logs nothing new.
        self.mailbox.history_id = "100"
        self.mailbox.save()
        self.fake.history = [g for g, m in self.fake.messages.items() if "SENT" in m["labelIds"]]
        stats = sync.sync_mailbox(self.mailbox, client=gmail_api.GmailClient(self.user, self.fake))
        self.assertEqual(stats.get("activities created", 0), 0)
        self.assertEqual(stats["activities already logged"], 2)

    def test_a_gmail_failure_mid_batch_stops_it_and_leaves_that_email_unconfirmed(self):
        self.fake.send_status = 500
        b = sender.run_batch(self.batch(self.will, self.pat).pk, sleep=lambda s: None)
        self.assertEqual(b.status, SendBatch.Status.FAILED)
        self.assertIn("Check your Sent folder", b.error)
        self.assertEqual(len(self.fake.sent), 1)  # never retried, and Pat isn't tried at all
        self.assertEqual(SendItem.objects.get(person=self.will).status, SendItem.Status.SENDING)
        self.assertEqual(SendItem.objects.get(person=self.pat).status, SendItem.Status.PENDING)

    def test_lost_permission_fails_the_batch(self):
        self.fake.send_status = 403
        b = sender.run_batch(self.batch(self.will).pk, sleep=lambda s: None)
        self.assertEqual(b.status, SendBatch.Status.FAILED)
        self.assertIn("Turn on Mail again", b.error)

    def test_test_send_weekly_report_and_campus_update(self):
        sender.send_test(self.user, self.template, self.will, ME)
        self.assertTrue(message_from_bytes(self.fake.sent[-1][1], policy=policy.default)["Subject"].startswith("[TEST]"))
        from crm.outreach import report

        report.send(report.build(), self.user, ["simon@acmigroup.com"])
        self.assertIn("Weekly Outreach Report", message_from_bytes(self.fake.sent[-1][1], policy=policy.default)["Subject"])
        from crm import campus_ai
        from crm.models import Campus, CampusReport

        campus = Campus.objects.create(code="MEIH", name="MEIH")
        r = CampusReport.objects.create(campus=campus, week_of=timezone.localdate(), body="All good.",
                                        recipients="simon@acmigroup.com")
        campus_ai.send(r, self.user)
        self.assertEqual(message_from_bytes(self.fake.sent[-1][1], policy=policy.default)["Subject"],
                         f"MEIH update, week of {timezone.localdate():%b %d}")

    def test_without_mail_on_the_app_password_path_is_unchanged(self):
        MailConnection.objects.filter(user=self.user).update(enabled=False)
        with self.assertRaisesMessage(sender.SendError, "No Gmail app password"):
            sender.send_test(self.user, self.template, self.will, ME)
        self.assertEqual(self.fake.sent, [])


@override_settings(**ON)
class NewsDraftsThroughGmailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        self.fake = FakeGmail()
        self.fake.draft_msgs["r-1"] = api_message("d0a1", "f1", ME, "Pat <pat@newco.io>", "Congrats on the expansion",
                                                  html="<p>Hi Pat, congrats</p>", labels=("DRAFT",))
        p = mock.patch.object(gmail_api, "requests", self.fake)
        p.start()
        self.addCleanup(p.stop)

    def test_own_mailbox_uses_the_api_and_someone_elses_does_not(self):
        self.assertIsInstance(gmail_drafts.open_drafts(self.mailbox, self.user), gmail_drafts.ApiDrafts)
        other = connected_user("simon", "sshewmaker@acmigroup.com")
        self.assertIsInstance(gmail_drafts.open_drafts(self.mailbox, other), gmail_drafts.Drafts)

    def test_list_edit_and_trash(self):
        with gmail_drafts.open_drafts(self.mailbox, self.user) as box:
            found = box.list()
            self.assertEqual([(d["uid"], d["subject"], d["to"]) for d in found],
                             [("r-1", "Congrats on the expansion", "Pat <pat@newco.io>")])
            self.assertIn("congrats", found[0]["html"])
            self.assertEqual(box.update("r-1", "pat@newco.io", "Congrats!", "<p>Hi Pat</p>"), "r-1")
            self.assertEqual(self.fake.posts[-1][0], "PUT /drafts/r-1")
            self.assertEqual(box.trash(["r-1"]), {"r-1": "trashed"})
        self.assertIn("TRASH", self.fake.draft_msgs["r-1"]["labelIds"])
        self.assertFalse(any(p.startswith("DELETE") for p, _ in self.fake.posts))  # trashed, never deleted
