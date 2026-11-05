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

    def test_page_with_a_queued_person_without_a_company(self):
        # The queue takes people with no company; the page's priority column used to fail on them (server error).
        Person.objects.create(first_name="Ann", email="ann@gmail.com", priority="")
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/outreach/"), "ann@gmail.com")

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
                    "/settings/outreach/", "/reports/weekly/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_plain_text_converts_to_email_html(self):
        from crm.outreach import richtext

        src = "{greeting},\n\nSee **bold**, _italic_ and [site](https://a.com/x_y?q=1&b=2) <b>\n\n- one\n- two"
        html = richtext.to_html(src)
        self.assertEqual(html, '<p>{greeting},</p><p>See <strong>bold</strong>, <em>italic</em> and '
                               '<a href="https://a.com/x_y?q=1&amp;b=2">site</a> &lt;b&gt;</p><ul><li>one</li><li>two</li></ul>')
        self.assertIn("site (https://a.com/x_y?q=1&b=2)", richtext.to_plain(src))
        self.assertNotIn("**", richtext.to_plain(src))
        self.assertNotIn("<a ", richtext.to_html("[x](javascript:alert(1))"))  # only http(s) and mailto links

    def test_template_editor_saves_from_text_keeps_old_html_and_previews_live(self):
        self.client.force_login(self.user)
        old = EmailTemplate.objects.get(template_id="T1")
        url = f"/outreach/templates/{old.pk}/"
        page = self.client.get(url)
        self.assertContains(page, "data-wrap")  # the formatting toolbar
        self.assertContains(page, "This template was written as HTML")
        fields = {"template_id": "T1", "name": "Fall defense", "campaign": "Fall", "priority": "High", "sector_match": "defense",
                  "subject": "Hi {company}", "body_source": page.context["form"].initial["body_source"],
                  "body_html": old.body_html, "body_plain": old.body_plain, "active": "on"}
        self.client.post(url, fields)  # saved without touching the text: original HTML stays
        old.refresh_from_db()
        self.assertEqual((old.body_html, old.body_source), ("<p>{greeting},</p><p>About {sector}</p>", ""))
        self.client.post(url, {**fields, "body_source": "{greeting},\n\nA **quick** note"})
        old.refresh_from_db()
        self.assertEqual(old.body_html, "<p>{greeting},</p><p>A <strong>quick</strong> note</p>")
        self.assertEqual(old.body_plain, "{greeting},\n\nA quick note")
        r = self.client.post("/outreach/templates/preview/", {"body_source": "Hi {first_name}, _hello_", "subject": "S {company}"})
        data = r.json()
        self.assertIn("Hi Ben, <em>hello</em>", data["html"])
        self.assertEqual(data["subject"], "S Firehawk")

    def test_template_pdf_is_sent_and_a_test_goes_out_from_the_editor(self):
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        self.client.force_login(self.user)
        t = EmailTemplate.objects.get(template_id="T1")
        url = f"/outreach/templates/{t.pk}/"
        base = {"template_id": "T1", "name": "Fall defense", "campaign": "Fall", "priority": "High", "sector_match": "defense",
                "subject": "Hi {company}", "body_source": "{greeting}, see the brochure", "active": "on"}
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            bad = self.client.post(url, {**base, "attachment": SimpleUploadedFile("x.pdf", b"not a pdf")})
            self.assertContains(bad, "Upload a PDF file")
            pdf = SimpleUploadedFile("Brochure.pdf", b"%PDF-1.4 fake")
            FakeSmtp.sent = []
            with patch("crm.outreach.sender.Smtp", FakeSmtp):
                r = self.client.post(url, {**base, "attachment": pdf, "action": "test", "sample": str(self.ben.pk)})
            self.assertEqual(r.status_code, 302)
            t.refresh_from_db()
            self.assertTrue(t.attachment.name.endswith(".pdf"))
            msg = FakeSmtp.sent[-1]
            self.assertTrue(msg["Subject"].startswith("[TEST] Hi Firehawk"))
            self.assertIn("Brochure", [p.get_filename() or "" for p in msg.iter_attachments()][0])
            page = self.client.get(url)
            self.assertContains(page, "Save and send me a test")
            self.assertContains(page, "PDF to include in the email")

    def test_weekly_report(self):
        now = timezone.now()
        Activity.objects.create(kind="email_sent", occurred_at=now, person=self.ben, company=self.co, summary="x",
                                campaign="Fall", source="gmail", source_id="a", thread_id="t1")
        Activity.objects.create(kind="reply", occurred_at=now + timedelta(hours=1), person=self.ben, company=self.co,
                                summary="Re: x", outcome="Responded", source="gmail", source_id="b", thread_id="t1")
        r = report.build(timezone.localdate())
        self.assertEqual((r["emails"], r["companies"]), (1, 1))
        self.assertEqual(r["summary"][0]["status"], "Responded")
        self.assertEqual(r["response_rate"], 100.0)
        with patch("crm.outreach.sender.Smtp", FakeSmtp):
            report.send(r, self.user, ["simon@acmigroup.com"])
        self.assertIn("WEEKLY OUTREACH REPORT", FakeSmtp.sent[-1].get_body(("html",)).get_content())

    def test_weekly_report_is_campaign_and_news_outreach_only(self):
        from crm.models import CompanyStatus, NewsItem, NewsScan, Opportunity

        now = timezone.now()
        tenant = Company.objects.create(name="Tenant Co", status=CompanyStatus.TENANT)
        signed = Company.objects.create(name="Signed Co")
        Opportunity.objects.create(name="Building 2", company=signed, stage="signed")
        news_co = Company.objects.create(name="NewsCo")
        amy = Person.objects.create(first_name="Amy", email="amy@newsco.com", company=news_co)
        for i, co in enumerate((tenant, signed)):
            Activity.objects.create(kind="email_sent", occurred_at=now, company=co, summary="Campaign email",
                                    campaign="Fall", source="gmail", source_id=f"c{i}")
        # one-to-one email about a tenant's building, and a reply to it: not outreach
        Activity.objects.create(kind="email_sent", occurred_at=now, person=self.ben, company=self.co,
                                summary="Email to Ben: Building 2 punch list", source="gmail", source_id="d", thread_id="t9")
        Activity.objects.create(kind="reply", occurred_at=now + timedelta(hours=1), person=self.ben, company=self.co,
                                summary="Re: punch list", outcome="Responded", source="gmail", source_id="e", thread_id="t9")
        scan = NewsScan.objects.create(path="/x", file_name="x")
        NewsItem.objects.create(scan=scan, kind="board", company_name="NewsCo", company=news_co, decision="proceed",
                                draft_subject="Congrats on the Navy award")
        Activity.objects.create(kind="email_sent", occurred_at=now, person=amy, company=news_co,
                                summary="Email to Amy: Congrats on the Navy award", source="gmail", source_id="f")
        r = report.build(timezone.localdate())
        self.assertEqual((r["emails"], r["news_emails"], r["left_out"]), (1, 1, 2))  # the news email only
        self.assertEqual(r["positive_replies"], [])  # the punch-list reply isn't outreach
        self.assertEqual(r["summary"][0]["status"], "No Reply")


class OutreachHomeTests(TestCase):
    def test_the_queue_preview_shows_a_person_with_no_company(self):
        from django.urls import reverse

        from crm.tests.utils import make_manager

        user = make_manager(get_user_model().objects.create(username="kellen", email=ME, first_name="Kellen"))
        self.client.force_login(user)
        settings = OutreachSettings.load()
        settings.current_campaign = "Fall"
        settings.save()
        EmailTemplate.objects.create(template_id="T2", name="Fall general", campaign="Fall", priority="",
                                     sector_match="*", subject="ACMI and you", body_html="<p>Hi {first_name}</p>")
        Person.objects.create(first_name="Solo", last_name="Founder", email="solo@example.com")
        r = self.client.get(reverse("crm:outreach_home"))
        self.assertContains(r, "Solo Founder")
