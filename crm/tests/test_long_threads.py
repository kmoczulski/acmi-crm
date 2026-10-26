"""Mail in the hub, step 3b: long threads (attachments panel, versions, outline, quoted text, find, summary, files)."""
import io
import json
import zipfile
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from allauth.socialaccount.models import SocialAccount, SocialToken
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import gmail_api, render, summary, threads
from crm.models import Company, MailConnection, Mailbox, Project

from .test_gmail_api import ME, ON, FakeGmail, api_message, b64

User = get_user_model()
WILL = "Will Edwards <will@firehawkaerospace.com>"
LAWYER = "Jon Ciriello <jon@cozen.com>"


def f(name, size=100_000, mime="application/pdf", when=None, msg="m1"):
    return {"name": name, "size": size, "mime": mime, "part": "1", "message": msg, "from_name": "Will",
            "from_addr": "will@x.com", "date": when or timezone.now()}


class GroupingTests(SimpleTestCase):
    def test_versions_of_one_document_share_a_key(self):
        same = ["PSA - ACMI redline 6.docx", "PSA draft #2.docx", "PSA - ACMI redline 10.21.docx", "PSA - ACMI redline 10.24.docx", "PSA v3 (clean).docx",
                "PSA_final_20261024.docx", "PSA (1).docx", "psa REVISED.doc"]
        self.assertEqual({threads.doc_key(n) for n in same}, {("psa", "word")})
        self.assertNotEqual(threads.doc_key("PSA.pdf"), threads.doc_key("PSA.docx"))  # a PDF is its own document
        self.assertNotEqual(threads.doc_key("Survey.pdf"), threads.doc_key("Title commitment.pdf"))

    def test_group_newest_first_hides_noise_and_unchanged_reattachments(self):
        now = timezone.now()
        files = [  # newest first, as files_of gives them
            f("PSA - ACMI redline 10.24.docx", when=now, msg="m9"),
            f("image001.png", size=4000, mime="image/png", when=now),
            f("invite.ics", mime="text/calendar", when=now),
            f("Survey.pdf", when=now - timedelta(days=1), msg="m7"),
            f("PSA - ACMI redline 10.21.docx", when=now - timedelta(days=2), msg="m5"),
            f("Survey.pdf", when=now - timedelta(days=3), msg="m3"),  # the same file again, unchanged
            f("Site photo.jpg", size=2_500_000, mime="image/jpeg", when=now - timedelta(days=4)),
            f("PSA - ACMI redline 10.14.docx", when=now - timedelta(days=5), msg="m1"),
        ]
        groups, hidden = threads.group(files)
        self.assertEqual(hidden, 3)
        self.assertEqual([g["latest"]["name"] for g in groups],
                         ["PSA - ACMI redline 10.24.docx", "Survey.pdf", "Site photo.jpg"])
        self.assertEqual([x["msg"] if False else x["message"] for x in groups[0]["older"]], ["m5", "m1"])
        self.assertEqual([g["type"] for g in groups], ["word", "pdf", "image"])


class QuotedTextTests(SimpleTestCase):
    def test_gmail_outlook_and_apple_quotes_are_folded(self):
        for quoted in ('<div class="gmail_quote"><div>On Mon, Will wrote:</div><blockquote>old</blockquote></div>',
                       '<div dir="ltr" class="gmail_quote">old</div>',
                       '<blockquote type="cite">old</blockquote>',
                       '<hr><div><b>From:</b> Will<br><b>Sent:</b> Monday</div><p>old</p>',
                       '<p>-----Original Message-----</p><p>old</p>'):
            out = render.fold_quotes(f"<p>Sounds good, signing today.</p>{quoted}")
            self.assertTrue(out.startswith("<p>Sounds good, signing today.</p>" + render.QUOTE_OPEN), quoted)
            self.assertTrue(out.endswith("</details>"))

    def test_a_bare_forward_stays_open(self):
        html = '<div class="gmail_quote">---------- Forwarded message ---------<br>the deal</div>'
        self.assertEqual(render.fold_quotes(html), html)

    def test_plain_text_replies_fold_too(self):
        body, _ = render.body_html(None, {"id": "x", "payload": {"mimeType": "text/plain", "body": {
            "data": b64("Yes, Tuesday works.\n\nOn Mon, Oct 26, Will wrote:\n> Can we tour Tuesday?")}}})
        self.assertIn("Yes, Tuesday works.", body)
        self.assertIn(render.QUOTE_OPEN, body)
        self.assertIn("&gt; Can we tour Tuesday?</div></details>", body)


def long_thread(n=85, thread="f00d"):
    """A deal thread with a lawyer: redlines every 10 messages, a logo on every lawyer message."""
    now = timezone.now()
    msgs = []
    for i in range(n):
        frm, to = (WILL, ME) if i % 3 == 0 else (LAWYER, ME) if i % 3 == 1 else (ME, LAWYER)
        labels = ("SENT",) if frm == ME else ("INBOX",)
        m = api_message(f"{i + 1:04x}", thread, frm, to, "Re: PSA for 4390 Indian Head Hwy" if i else
                        "PSA for 4390 Indian Head Hwy", body=f"Round {i + 1}: comments on section {i % 7}.",
                        when=now - timedelta(hours=n - i), labels=labels)
        if i % 10 == 5:
            m["payload"]["parts"].append({"partId": "2", "mimeType": "application/vnd.openxmlformats",
                                          "filename": f"PSA - ACMI redline {i + 1}.docx",
                                          "body": {"attachmentId": f"att{i}", "size": 80_000 + i}})
        if frm == LAWYER:
            m["payload"]["parts"].append({"partId": "3", "mimeType": "image/png", "filename": "image001.png",
                                          "body": {"attachmentId": f"logo{i}", "size": 3000}})
        msgs.append(m)
    msgs[-3]["labelIds"] = ["INBOX", "UNREAD"]
    msgs[-1]["labelIds"] = ["INBOX", "UNREAD"]
    return msgs


@override_settings(**ON)
class LongThreadPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("kellen", password="x", email=ME)
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1", extra_data={"email": ME})
        SocialToken.objects.create(account=account, token="a", token_secret="r",
                                   expires_at=timezone.now() + timedelta(hours=1))
        MailConnection.objects.create(user=self.user, enabled=True, google_email=ME)
        Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        self.fake = FakeGmail(long_thread())
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, **params):
        return self.client.get(reverse("crm:inbox"), {"t": "f00d", **params})

    def test_newest_first_with_one_attachments_panel(self):
        r = self.open()
        reader = r.context["reader"]
        self.assertEqual(len(reader["messages"]), 85)
        self.assertEqual(reader["messages"][0]["id"], "0055")  # message 85, newest
        self.assertEqual(reader["latest"], "0055")
        groups = reader["file_groups"]
        self.assertEqual(len(groups), 1)  # nine redlines, one document
        self.assertEqual(groups[0]["latest"]["name"], "PSA - ACMI redline 76.docx")
        self.assertEqual(len(groups[0]["older"]), 7)
        self.assertEqual(reader["files_hidden"], 28)  # the lawyer's logo on every message
        self.assertContains(r, "Download all")
        self.assertContains(r, 'data-goto="004c"')  # Go to message 76
        self.assertContains(r, "7 earlier versions")

    def test_outline_people_and_new_since_you_last_read(self):
        reader = self.open().context["reader"]
        outline = reader["outline"]
        self.assertEqual([o["n"] for o in outline[:3]], [1, 2, 3])
        self.assertEqual(outline[5]["files"], 1)
        self.assertEqual(outline[4]["files"], 0)  # the lawyer's logo doesn't count
        self.assertEqual({a for a, _ in reader["people"]}, {"will@firehawkaerospace.com", "jon@cozen.com", ME})
        self.assertEqual(reader["first_unread"], "0053")  # the older of the two unread
        r = self.open()
        self.assertNotContains(r, "New since you last read")  # newest first: the new ones are already on top
        self.assertContains(r, "All 85 messages")
        self.assertNotContains(r, "image001.png")  # logos left out of each message's files too
        r = self.open(order="oldest")
        self.assertContains(r, 'id="new-since"')

    def test_oldest_first_is_remembered(self):
        reader = self.open(order="oldest").context["reader"]
        self.assertEqual(reader["messages"][0]["id"], "0001")
        reader = self.open().context["reader"]
        self.assertEqual(reader["order"], "oldest")
        self.assertEqual(self.open(order="newest").context["reader"]["messages"][0]["id"], "0055")

    def test_find_in_thread_has_each_messages_text(self):
        r = self.open()
        texts = json.loads(r.context["reader"]["texts"] and json.dumps(r.context["reader"]["texts"]))
        self.assertIn("round 85: comments on section 0.", texts["0055"])
        self.assertContains(r, 'id="mail-texts"')

    def test_download_all_zips_the_newest_version_of_each_document(self):
        r = self.client.get(reverse("crm:inbox_zip", args=["f00d"]))
        self.assertEqual(r["Content-Type"], "application/zip")
        self.assertIn("PSA for 4390 Indian Head Hwy - files.zip", r["Content-Disposition"])
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        self.assertEqual(names, ["PSA - ACMI redline 76.docx"])

    def test_other_parts_of_a_split_conversation(self):
        for m in long_thread(n=60, thread="beef"):
            m["id"] = "b" + m["id"]
            self.fake.messages[m["id"]] = m
            self.fake.order.append(m["id"])
        parts = self.open().context["reader"]["other_parts"]
        self.assertEqual([t.gmail_id for t in parts], ["beef"])
        listing = [p for path, p in self.fake.calls if path == "/threads" and p.get("q")][-1]
        self.assertEqual(listing["q"], 'subject:"PSA for 4390 Indian Head Hwy"')

    def test_short_threads_skip_the_outline_and_the_search_for_other_parts(self):
        self.fake.add(api_message("aaaa", "cafe", WILL, ME, "Quick one"))
        r = self.client.get(reverse("crm:inbox"), {"t": "cafe"})
        self.assertNotContains(r, "All 1 messages")
        self.assertEqual(r.context["reader"]["other_parts"], [])

    def test_summarize_is_only_on_click_and_links_the_messages(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.assertNotContains(self.open(), "data-summarize=\"")  # no button without a key
        answer = {"where_it_stands": "Redline 76 is with Cozen.", "next_step": "Sign by Friday.",
                  "open_items": [{"who": "Jon Ciriello", "what": "Comments on section 6", "message": 77}],
                  "documents": [{"name": "PSA - ACMI redline 76.docx", "message": 76, "note": "latest"}]}
        fake_message = SimpleNamespace(stop_reason="end_turn",
                                       content=[SimpleNamespace(type="text", text=json.dumps(answer))])
        stream = mock.MagicMock()
        stream.__enter__.return_value.get_final_message.return_value = fake_message
        anthropic_client = mock.MagicMock()
        anthropic_client.beta.messages.stream.return_value = stream
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), \
                mock.patch("anthropic.Anthropic", return_value=anthropic_client):
            self.assertContains(self.open(), "data-summarize=\"")
            self.assertEqual(self.client.get(reverse("crm:inbox_summary", args=["f00d"])).status_code, 405)
            r = self.client.post(reverse("crm:inbox_summary", args=["f00d"]))
            again = self.client.post(reverse("crm:inbox_summary", args=["f00d"]))
        data = r.json()
        self.assertEqual(data["where_it_stands"], "Redline 76 is with Cozen.")
        self.assertEqual(data["documents"][0]["id"], "004c")
        self.assertEqual(data["open_items"][0]["id"], "004d")
        self.assertEqual(again.json(), data)
        self.assertEqual(anthropic_client.beta.messages.stream.call_count, 1)  # the second answer came from the cache
        sent = anthropic_client.beta.messages.stream.call_args.kwargs["messages"][0]["content"]
        self.assertIn("[85] From Will Edwards <will@firehawkaerospace.com>", sent)
        self.assertIn("Attachments: PSA - ACMI redline 76.docx", sent)

    def test_summarize_without_a_key_says_so(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            r = self.client.post(reverse("crm:inbox_summary", args=["f00d"]))
        self.assertEqual(r.status_code, 503)
        self.assertIn("ANTHROPIC_API_KEY", r.json()["error"])


@override_settings(**ON)
class FilesViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("kellen", password="x", email=ME)
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1", extra_data={"email": ME})
        SocialToken.objects.create(account=account, token="a", token_secret="r",
                                   expires_at=timezone.now() + timedelta(hours=1))
        MailConnection.objects.create(user=self.user, enabled=True, google_email=ME)
        Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        self.fake = FakeGmail([
            api_message("0a01", "a1", WILL, ME, "Site plan", attachment="Site plan v2.pdf",
                        when=timezone.now() - timedelta(days=2)),
            api_message("0a02", "a2", WILL, ME, "Site plan again", attachment="Site plan v3.pdf"),
        ])
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_attachment_grouped_and_narrowed_by_record(self):
        company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        project = Project.objects.create(name="The Forge", keywords=["4390 Indian Head"])
        r = self.client.get(reverse("crm:inbox_files"), {"type": "pdf", "company": company.pk,
                                                         "project": project.pk, "q": "plan"})
        self.assertEqual(r.status_code, 200)
        groups = r.context["groups"]
        self.assertEqual([g["latest"]["name"] for g in groups], ["Site plan v3.pdf"])
        self.assertEqual([x["name"] for x in groups[0]["older"]], ["Site plan v2.pdf"])
        q = [p for path, p in self.fake.calls if path == "/messages"][-1]["q"]
        self.assertEqual(q, 'has:attachment filename:pdf plan (from:(firehawkaerospace.com) OR '
                            'to:(firehawkaerospace.com)) ("The Forge" OR "4390 Indian Head")')
        self.assertContains(r, reverse("crm:inbox_part", args=["0a02", "1"]))

    def test_files_page_needs_mail_on(self):
        MailConnection.objects.filter(user=self.user).update(enabled=False)
        self.assertRedirects(self.client.get(reverse("crm:inbox_files")), reverse("crm:mail_settings"),
                             fetch_redirect_response=False)
