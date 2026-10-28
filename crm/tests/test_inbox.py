"""Mail in the hub, step 3: the inbox (folders, list, split-inbox tabs, search, reading pane, attachments)."""
import base64
from datetime import timedelta
from unittest import mock

from allauth.socialaccount.models import SocialAccount, SocialToken
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import gmail_api, inbox_views, index, sync
from crm.models import Activity, Company, Firm, MailConnection, MailLabel, MailThread, Mailbox, Person, Project
from crm.permissions import PARTNERS

from .test_gmail_api import ME, ON, FakeGmail, api_message, b64

User = get_user_model()
WILL = "Will Edwards <will@firehawkaerospace.com>"


def html_message(gid, thread, html, **kw):
    m = api_message(gid, thread, WILL, ME, kw.pop("subject", "Site plan"), **kw)
    m["payload"]["parts"] = [{"partId": "0", "mimeType": "text/html", "body": {"data": b64(html)}},
                             {"partId": "1", "mimeType": "image/png", "filename": "",
                              "headers": [{"name": "Content-ID", "value": "<logo1>"}],
                              "body": {"attachmentId": "img-att", "size": 900}},
                             {"partId": "2", "mimeType": "application/pdf", "filename": "Site plan v3.pdf",
                              "body": {"attachmentId": "pdf-att", "size": 250000}},
                             {"partId": "3", "mimeType": "text/html", "filename": "invoice.html",
                              "body": {"attachmentId": "html-att", "size": 100}}]
    m["payload"]["mimeType"] = "multipart/mixed"
    return m


@override_settings(**ON)
class InboxTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("kellen", password="x", email=ME)
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1", extra_data={"email": ME})
        SocialToken.objects.create(account=account, token="a", token_secret="r",
                                   expires_at=timezone.now() + timedelta(hours=1))
        MailConnection.objects.create(user=self.user, enabled=True, google_email=ME)
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        self.firm = Firm.objects.create(name="Shore United Bank", kind=Firm.Kind.BANK, domains=["shoreunitedbank.com"])
        now = timezone.now()
        self.fake = FakeGmail([
            api_message("m1", "a1", WILL, ME, "PDA for land search", when=now - timedelta(hours=5), body="First"),
            api_message("m2", "a1", ME, WILL, "Re: PDA for land search", when=now - timedelta(hours=4),
                        labels=("SENT",), body="Second"),
            api_message("m3", "a1", WILL, ME, "Re: PDA for land search", when=now - timedelta(hours=3),
                        labels=("INBOX", "UNREAD"), body="Third and newest"),
            api_message("m4", "b2", "Pat <pat@shoreunitedbank.com>", ME, "Term sheet", when=now - timedelta(hours=2),
                        attachment="Term sheet.pdf"),
            api_message("m5", "c3", "News <news@example.com>", ME, "Weekly digest", when=now - timedelta(hours=1)),
        ])
        self.fake.inbox_unread = 1
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, **params):
        return self.client.get(reverse("crm:inbox"), params)

    def thread_fetches(self):
        return [p for p, _ in self.fake.calls if p.startswith("/threads/")]

    def test_needs_mail_on(self):
        MailConnection.objects.filter(user=self.user).update(enabled=False)
        r = self.get()
        self.assertRedirects(r, reverse("crm:mail_settings"), fetch_redirect_response=False)
        self.assertNotContains(self.client.get(reverse("crm:home")), reverse("crm:inbox"))

    def test_mail_link_in_the_top_bar_when_on(self):
        self.assertContains(self.client.get(reverse("crm:home")), f'href="{reverse("crm:inbox")}"')

    def test_inbox_lists_threads_newest_first_with_folders_and_crm_tags(self):
        r = self.get()
        self.assertEqual(r.status_code, 200)
        rows = r.context["threads"]
        self.assertEqual([t.gmail_id for t in rows], ["c3", "b2", "a1"])
        a1 = rows[2]
        self.assertTrue(a1.unread)
        self.assertEqual(a1.message_count, 3)
        self.assertEqual(a1.subject, "PDA for land search")
        self.assertEqual([s[1] for s in a1.senders], ["will@firehawkaerospace.com", ME])
        self.assertIn(("company", "Firehawk Aerospace", self.company.get_absolute_url()), a1.chips)
        self.assertEqual(rows[1].chips[0][:2], ("firm", "Shore United Bank"))
        self.assertTrue(rows[1].has_attachments)
        self.assertEqual(rows[0].chips, [])
        self.assertContains(r, "Deals/MEIH")  # the person's own Gmail label
        self.assertContains(r, '<span class="mail-count">1</span>', html=False)
        self.assertNotContains(r, "UNREAD")
        self.assertEqual(MailThread.objects.filter(mailbox=self.mailbox).count(), 3)

    def test_only_changed_threads_are_read_again(self):
        self.get()
        self.assertEqual(len(self.thread_fetches()), 3)
        self.fake.calls.clear()
        self.get()
        self.assertEqual(self.thread_fetches(), [])
        self.fake.messages["m4"]["labelIds"] = ["INBOX", "UNREAD"]
        self.fake.calls.clear()
        self.get()
        self.assertEqual(self.thread_fetches(), ["/threads/b2"])
        self.assertTrue(MailThread.objects.get(gmail_id="b2").unread)

    def test_timeline_project_and_deal_tags(self):
        project = Project.objects.create(name="MEIH")
        act = Activity.objects.create(kind=Activity.Kind.EMAIL_RECEIVED, occurred_at=timezone.now(),
                                      company=self.company, summary="x", source=Activity.Source.GMAIL,
                                      source_id="gmail:x|c1", thread_id=str(int("a1", 16)))
        act.projects.add(project)
        rows = self.get().context["threads"]
        kinds = [c[0] for c in rows[2].chips]
        self.assertEqual(kinds[:2], ["project", "company"])
        t = MailThread.objects.get(gmail_id="a1")
        self.assertTrue(t.is_tenant and t.is_project)

    def test_split_inbox_tabs(self):
        self.get()
        tenants = [t.gmail_id for t in self.get(tab="tenants").context["threads"]]
        firms = [t.gmail_id for t in self.get(tab="firms").context["threads"]]
        other = [t.gmail_id for t in self.get(tab="other").context["threads"]]
        self.assertEqual((tenants, firms, other), (["a1"], ["b2"], ["c3"]))
        r = self.get(tab="tenants")
        self.assertContains(r, "Capital &amp; directory")
        self.assertTrue(r.context["indexing"])  # the sync hasn't warmed the inbox yet

    def test_search_uses_gmail_search_across_all_mail(self):
        r = self.get(q="term sheet")
        self.assertEqual([t.gmail_id for t in r.context["threads"]], ["b2"])
        listing = [p for path, p in self.fake.calls if path == "/threads"][-1]
        self.assertEqual(listing.get("q"), "term sheet")
        self.assertNotIn("labelIds", listing)
        self.assertEqual(r.context["tabs"], [])

    def test_search_by_company_name_also_finds_its_addresses(self):
        r = self.get(q="firehawk")
        q = [p for path, p in self.fake.calls if path == "/threads"][-1]["q"]
        self.assertTrue(q.startswith("{firehawk ") and "from:firehawkaerospace.com" in q and "to:firehawkaerospace.com" in q, q)
        self.assertEqual(r.context["widened"], [("company", "Firehawk Aerospace")])
        self.get(q="firehawk", exact="1")  # "just the words"
        self.assertEqual([p for path, p in self.fake.calls if path == "/threads"][-1]["q"], "firehawk")
        self.get(q="from:pat has:attachment")  # Gmail operators are left as typed
        self.assertEqual([p for path, p in self.fake.calls if path == "/threads"][-1]["q"], "from:pat has:attachment")

    def test_search_in_this_folder_only(self):
        self.get(q="term sheet", **{"in": "SENT"})
        listing = [p for path, p in self.fake.calls if path == "/threads"][-1]
        self.assertIn("SENT", str(listing.get("labelIds")))

    def test_address_suggestions(self):
        Person.objects.create(full_name="Will Edwards", email="will@firehawkaerospace.com", company=self.company)
        User.objects.create_user("david", email="ddenardo@acmigroup.com", first_name="David", last_name="DeNardo")
        self.get()  # indexes the threads, so senders are known
        url = reverse("crm:inbox_suggest")
        people = self.client.get(url, {"q": "wil"}).json()["people"]
        self.assertEqual(people[0], {"name": "Will Edwards", "email": "will@firehawkaerospace.com", "detail": "Firehawk Aerospace"})
        self.assertEqual(self.client.get(url, {"q": "dav"}).json()["people"][0]["email"], "ddenardo@acmigroup.com")
        self.assertEqual(self.client.get(url, {"q": "pat@"}).json()["people"][0]["email"], "pat@shoreunitedbank.com")
        self.assertEqual(self.client.get(url, {"q": "x"}).json()["people"], [])  # one letter: nothing yet
        records = self.client.get(url, {"q": "shore", "records": "1"}).json()["records"]
        self.assertEqual(records, [{"kind": "firm", "label": "Shore United Bank"}])

    def test_partner_accounts_are_never_suggested_as_teammates(self):
        partner = User.objects.create_user("pat@fortress.com", email="pat@fortress.com", first_name="Pat", last_name="Partner")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        people = self.client.get(reverse("crm:inbox_suggest"), {"q": "partner"}).json()["people"]
        self.assertEqual(people, [])

    def test_a_company_name_suggests_its_people(self):
        # Kellen: "type a company name into the To section of the email and select a person".
        Person.objects.create(full_name="Will Edwards", email="will@firehawkaerospace.com", company=self.company)
        Person.objects.create(full_name="Ann Lee", email="ann@firehawkaerospace.com", company=self.company)
        Person.objects.create(full_name="Old Address", email="old@firehawkaerospace.com", company=self.company,
                              email_valid=False)
        people = self.client.get(reverse("crm:inbox_suggest"), {"q": "fireh"}).json()["people"]
        self.assertEqual(people[0]["emails"], ["ann@firehawkaerospace.com", "will@firehawkaerospace.com"])
        self.assertTrue(people[0]["name"].startswith("Everyone at Firehawk Aerospace (2)"))
        self.assertEqual({p["email"] for p in people[1:3]}, {"will@firehawkaerospace.com", "ann@firehawkaerospace.com"})
        self.assertTrue(all(p["detail"] == "Firehawk Aerospace" for p in people[1:3]))
        self.assertNotIn("old@firehawkaerospace.com", {p["email"] for p in people})  # bounced

    def test_other_folders(self):
        r = self.get(label="SENT")
        self.assertEqual([t.gmail_id for t in r.context["threads"]], ["a1"])
        self.assertEqual(r.context["label_name"], "Sent")

    def test_reading_pane_newest_first_with_attachments(self):
        r = self.get(t="b2")
        reader = r.context["reader"]
        self.assertEqual(reader["subject"], "Term sheet")
        self.assertEqual(reader["chips"][0][:2], ("firm", "Shore United Bank"))
        self.assertContains(r, "Term sheet.pdf")
        r = self.get(t="a1")
        msgs = r.context["reader"]["messages"]
        self.assertEqual([m["id"] for m in msgs], ["m3", "m2", "m1"])
        self.assertEqual([m["open"] for m in msgs], [True, False, False])
        self.assertContains(r, reverse("crm:inbox_body", args=["m3"]))
        self.assertContains(r, f"https://mail.google.com/mail/u/{ME}/#all/a1")

    def test_someone_elses_thread_is_not_found(self):
        self.assertEqual(self.get(t="ffff").status_code, 404)

    def test_body_frame_is_cleaned_and_holds_remote_images_back(self):
        self.fake.add(html_message(
            "h1", "d4", '<p onclick="steal()">Hi <script>alert(1)</script><img src="https://tracker.example/p.gif" '
            'width="1"><img src="cid:logo1"><a href="https://acmigroup.com">site</a></p>'))
        r = self.client.get(reverse("crm:inbox_body", args=["h1"]))
        body = r.content.decode()
        self.assertNotIn("<script", body)
        self.assertNotIn("onclick", body)
        self.assertNotIn("tracker.example", body)
        self.assertIn('data-blocked="1"', body)
        self.assertIn(reverse("crm:inbox_part", args=["h1", "1"]) + "?inline=1", body)
        self.assertIn('<base target="_blank">', body)
        policy = r["Content-Security-Policy"]
        self.assertIn("img-src 'self' data:;", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("sandbox", policy)
        self.assertEqual(r["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(r["X-Blocked-Images"], "1")
        r = self.client.get(reverse("crm:inbox_body", args=["h1"]), {"images": "1"})
        self.assertIn("tracker.example", r.content.decode())
        self.assertIn("https:", r["Content-Security-Policy"])

    def test_plain_text_mail_is_escaped(self):
        self.fake.add(api_message("p1", "e5", WILL, ME, "Plain", body="<b>not bold</b> & more"))
        body = self.client.get(reverse("crm:inbox_body", args=["p1"])).content.decode()
        self.assertIn("&lt;b&gt;not bold&lt;/b&gt; &amp; more", body)

    def test_attachments_download_and_never_run(self):
        self.fake.add(html_message("h1", "d4", "<p>x</p>"))
        r = self.client.get(reverse("crm:inbox_part", args=["h1", "2"]))
        self.assertEqual(r["Content-Type"], "application/octet-stream")
        self.assertIn('attachment; filename="Site plan v3.pdf"', r["Content-Disposition"])
        self.assertEqual(r.content, b"%PDF-1.4 fake")
        r = self.client.get(reverse("crm:inbox_part", args=["h1", "3"]), {"inline": "1"})  # an HTML file
        self.assertEqual(r["Content-Type"], "application/octet-stream")
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))
        r = self.client.get(reverse("crm:inbox_part", args=["h1", "1"]), {"inline": "1"})  # an inline picture
        self.assertEqual(r["Content-Type"], "image/png")
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertEqual(self.client.get(reverse("crm:inbox_part", args=["h1", "9"])).status_code, 404)

    def test_gmail_down_shows_a_message_not_an_error_page(self):
        self.fake.status = 503
        with mock.patch("crm.mail.gmail_api.time.sleep"):
            r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Gmail is busy right now")

    def test_gmail_failing_after_the_page_loaded_is_still_words_not_an_error_page(self):
        """The reading frame, an attachment, Download all, Summarize and opening a reply each call Gmail on their
        own; when it fails they say so (the frame used to show Django's error page inside the inbox)."""
        self.fake.status = 503
        with mock.patch("crm.mail.gmail_api.time.sleep"):
            r = self.client.get(reverse("crm:inbox_body", args=["m1"]))
            self.assertEqual(r.status_code, 502)
            self.assertContains(r, "Gmail is busy right now", status_code=502)
            self.assertIn("default-src 'none'", r["Content-Security-Policy"])
            r = self.client.get(reverse("crm:inbox_part", args=["m4", "1"]))
            self.assertEqual((r.status_code, r["Content-Type"]), (502, "text/plain; charset=utf-8"))
            r = self.client.get(reverse("crm:inbox_zip", args=["a1"]))
            self.assertRedirects(r, reverse("crm:inbox") + "?t=a1", fetch_redirect_response=False)
            r = self.client.post(reverse("crm:inbox_summary", args=["a1"]))
            self.assertEqual(r.status_code, 503)
            self.assertIn("Gmail is busy", r.json()["error"])
            r = self.client.get(reverse("crm:inbox_compose"), {"reply": "m1", "mode": "reply"})
            self.assertRedirects(r, reverse("crm:inbox"), fetch_redirect_response=False)
            r = self.client.get(reverse("crm:inbox_reply_fields"), {"reply": "m1"})
            self.assertEqual(r.status_code, 503)
            self.assertIn("Gmail is busy", r.json()["error"])

    def test_a_person_without_a_timeline_mailbox_gets_an_inbox_only_one(self):
        self.mailbox.delete()
        self.assertEqual(self.get().status_code, 200)
        mb = Mailbox.objects.get(user=self.user)
        self.assertFalse(mb.enabled)  # the timeline sync stays off for them
        self.assertEqual(mb.backend, Mailbox.Backend.GMAIL_API)

    def test_the_index_is_private(self):
        self.assertNotIn(MailThread, admin.site._registry)
        self.assertNotIn(MailLabel, admin.site._registry)


@override_settings(**ON)
class InboxPartnerTests(TestCase):
    def test_partners_never_reach_the_inbox(self):
        partner = User.objects.create_user("fortress", password="x", email="lp@acmigroup.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        MailConnection.objects.create(user=partner, enabled=True)
        self.client.force_login(partner)
        for url in (reverse("crm:inbox"), reverse("crm:inbox_body", args=["x"]),
                    reverse("crm:inbox_part", args=["x", "1"])):
            self.assertRedirects(self.client.get(url), "/room/", fetch_redirect_response=False)
        from django.http import Http404

        request = RequestFactory().get("/inbox/")
        request.user = partner
        with self.assertRaises(Http404):
            inbox_views.inbox(request)


@override_settings(**ON)
class InboxSyncTests(TestCase):
    """The sync keeps the index current: changed threads re-read, the inbox warmed once, old rows dropped."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("kellen", password="x", email=ME)
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1", extra_data={"email": ME})
        SocialToken.objects.create(account=account, token="a", token_secret="r",
                                   expires_at=timezone.now() + timedelta(hours=1))
        MailConnection.objects.create(user=self.user, enabled=True, google_email=ME)
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backfill_done=True,
                                              last_synced_at=timezone.now() - timedelta(hours=1))
        Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        Person.objects.create(first_name="Will", email="will@firehawkaerospace.com",
                              company=Company.objects.get(name="Firehawk Aerospace"))

    def sync(self, fake):
        sync.sync_mailbox(self.mailbox, client=gmail_api.GmailClient(self.user, fake))
        self.mailbox.refresh_from_db()

    def test_first_sync_warms_the_inbox_and_later_ones_refresh_what_changed(self):
        fake = FakeGmail([api_message(f"m{i}", f"e{i}", WILL, ME, f"Subject {i}",
                                      when=timezone.now() - timedelta(hours=i)) for i in range(1, 4)])
        self.sync(fake)
        self.assertIsNotNone(self.mailbox.inbox_indexed_at)
        self.assertEqual(MailThread.objects.filter(mailbox=self.mailbox, in_inbox=True).count(), 3)
        self.assertTrue(MailThread.objects.get(gmail_id="e1").is_tenant)
        fake.add(api_message("m9", "e9", WILL, ME, "Brand new"))
        fake.messages["m9"]["labelIds"] = ["INBOX", "UNREAD"]
        self.sync(fake)
        self.assertTrue(MailThread.objects.get(gmail_id="e9").unread)

    def test_old_untouched_rows_are_dropped(self):
        old = timezone.now() - timedelta(days=index.KEEP_DAYS + 5)
        t = MailThread.objects.create(mailbox=self.mailbox, gmail_id="old", last_message_at=old)
        MailThread.objects.filter(pk=t.pk).update(fetched_at=old)
        MailThread.objects.create(mailbox=self.mailbox, gmail_id="recent", last_message_at=timezone.now())
        index.prune(self.mailbox)
        self.assertEqual(list(MailThread.objects.values_list("gmail_id", flat=True)), ["recent"])
