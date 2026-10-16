from datetime import timedelta
from email import message_from_bytes
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from crm.mail import sync
from crm.mail.parse import parse_message
from crm.models import (
    Activity, Company, EmailTemplate, Mailbox, OutreachSettings, Person, SendBatch, SendItem,
)
from crm.outreach import queue, report
from crm.outreach.sender import compose, run_batch

ME = "kmoczulski@acmigroup.com"


class FakeSmtp:
    sent = []

    def __init__(self, mailbox):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, msg):
        FakeSmtp.sent.append(msg)


class OutreachTests(TestCase):
    def setUp(self):
        from crm.tests.utils import make_manager

        self.user = make_manager(get_user_model().objects.create(username="kellen", email=ME, first_name="Kellen"))
        Mailbox.objects.create(user=self.user, email=ME)
        self.settings = OutreachSettings.load()
        self.settings.current_campaign = "Fall"
        self.settings.include_attachments = False
        self.settings.save()
        EmailTemplate.objects.create(template_id="T1", name="Fall defense", campaign="Fall", priority="High",
                                     sector_match="defense", subject="Hi {company}",
                                     body_html="<p>{greeting},</p><p>About {sector}</p>", body_plain="{greeting},")
        EmailTemplate.objects.create(template_id="T2", name="Fall general", campaign="Fall", priority="",
                                     sector_match="*", subject="ACMI and {company}", body_html="<p>Hi {first_name}</p>")
        EmailTemplate.objects.create(template_id="T0", name="Old", campaign="Summer", subject="old")
        self.co = Company.objects.create(name="Firehawk", sector="defense systems", priority="High")
        self.ben = Person.objects.create(first_name="Ben", last_name="Allen", email="ben@firehawk.com",
                                         company=self.co, priority="High")
        FakeSmtp.sent = []

    def test_template_matching_and_placeholders(self):
        q, _ = queue.build()
        entry = next(e for e in q if e.person == self.ben)
        self.assertEqual(entry.template.template_id, "T1")
        subject, html, plain = compose(entry.template, self.ben, entry.greeting)
        self.assertEqual(subject, "Hi Firehawk")
        self.assertIn("<p>Ben,</p><p>About defense systems</p>", html)
        self.assertIn("Kellen Moczulski", plain)
        other = Person.objects.create(email="info@acme.com", full_name="Info", company=Company.objects.create(name="Acme"))
        e = next(e for e in queue.build()[0] if e.person == other)
        self.assertEqual((e.template.template_id, e.greeting), ("T2", "To whom it may concern"))
        self.assertIn("Hi there", compose(e.template, other, e.greeting)[1])  # {first_name} with no name

    def test_skip_rules(self):
        now = timezone.now()
        cases = {
            "bounced": dict(email_valid=False),
            "cooldown": dict(last_interaction_at=now - timedelta(days=2)),
            "max_touches": dict(touch_count=15),
            "replied": dict(last_reply_at=now - timedelta(days=5)),
            "responded": dict(last_response="Responded"),
            "excluded": dict(flags=["Exclude"]),
        }
        for reason, fields in cases.items():
            Person.objects.create(email=f"{reason}@x.com", company=self.co, **fields)
        late = Company.objects.create(name="Tenant Co", status="tenant")
        Person.objects.create(email="t@tenant.com", company=late)
        q, skipped = queue.build()
        self.assertEqual({p.person.email for p in q}, {"ben@firehawk.com"})
        for reason in cases:
            self.assertEqual(skipped[reason], 1, reason)
        self.assertEqual(skipped["late_stage"], 1)

    def test_run_batch_sends_logs_and_sync_dedupes(self):
        batch = SendBatch.objects.create(created_by=self.user, campaign="Fall")
        SendItem.objects.create(batch=batch, person=self.ben, template=EmailTemplate.objects.get(template_id="T1"),
                                email=self.ben.email)
        with patch("crm.outreach.sender.Smtp", FakeSmtp):
            run_batch(batch.pk, sleep=lambda s: None)
            run_batch(batch.pk, sleep=lambda s: None)  # re-running never resends
        batch.refresh_from_db()
        self.assertEqual(batch.status, "done")
        self.assertEqual(len(FakeSmtp.sent), 1)
        msg = FakeSmtp.sent[0]
        sent = Activity.objects.get(kind="email_sent", person=self.ben)
        self.assertEqual((sent.campaign, sent.message_id, sent.context), ("Fall", msg["Message-ID"], "prospecting"))
        self.ben.refresh_from_db()
        self.co.refresh_from_db()
        self.assertEqual(self.ben.touch_count, 1)
        self.assertEqual(self.co.status, "contacted")

        # The Gmail sync later reads the same email from Sent: no duplicate, and a reply is recognised.
        from crm.tests.test_mail import FakeClient

        mailbox = Mailbox.objects.get(user=self.user)
        reply = (f"From: Ben Allen <ben@firehawk.com>\nTo: {ME}\nSubject: Re: {msg['Subject']}\n"
                 f"Message-ID: <r1@firehawk.com>\nIn-Reply-To: {msg['Message-ID']}\n"
                 "Date: " + msg.get("Date", "Mon, 12 Oct 2026 10:00:00 +0000") + "\n\nInterested, let's talk.").encode()
        sync.sync_mailbox(mailbox, client=FakeClient([(1, "t", msg.as_bytes()), (2, "t", reply)]))
        self.assertEqual(Activity.objects.filter(kind="email_sent", person=self.ben).count(), 1)
        self.assertEqual(Activity.objects.get(kind="reply", person=self.ben).outcome, "Responded")

    def test_send_page_requires_confirmation(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/outreach/").status_code, 200)
        with patch("crm.outreach.views.start_in_background") as start:
            self.client.post("/outreach/start/", {"limit": "5", "confirm": "yes"})
            self.assertFalse(SendBatch.objects.exists())
            r = self.client.post("/outreach/start/", {"limit": "5", "confirm": "SEND"})
            batch = SendBatch.objects.get()
            self.assertRedirects(r, batch.get_absolute_url())
            start.assert_called_once()
        self.assertEqual(batch.items.count(), 1)  # only Ben is eligible
        self.assertEqual(self.client.get(batch.get_absolute_url()).status_code, 200)
        for url in ("/outreach/templates/", f"/outreach/templates/{EmailTemplate.objects.first().pk}/",
                    "/outreach/settings/", "/reports/weekly/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_weekly_report(self):
        now = timezone.now()
        Activity.objects.create(kind="email_sent", occurred_at=now, person=self.ben, company=self.co, summary="x",
                                campaign="Fall", source="gmail", source_id="a")
        Person.objects.filter(pk=self.ben.pk).update(last_response="Responded", last_response_at=now + timedelta(hours=1))
        r = report.build(timezone.localdate())
        self.assertEqual((r["emails"], r["companies"]), (1, 1))
        self.assertEqual(r["summary"][0]["status"], "Responded")
        self.assertEqual(r["response_rate"], 100.0)
        with patch("crm.outreach.sender.Smtp", FakeSmtp):
            report.send(r, self.user, ["simon@acmigroup.com"])
        self.assertIn("WEEKLY OUTREACH REPORT", FakeSmtp.sent[-1].get_body(("html",)).get_content())
