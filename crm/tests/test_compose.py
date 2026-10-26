"""Mail in the hub, step 5: new message, reply, reply all, forward, attachments, drafts, signature, sending."""
from datetime import timedelta
from email import message_from_bytes, policy
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import prefs
from crm.mail import compose, gmail_api, sync
from crm.models import Activity, Company, Mailbox, Person
from crm.permissions import PARTNERS

from .test_gmail_api import ME, ON, FakeGmail, api_message
from .test_mail_actions import connected_user

WILL = "Will Edwards <will@firehawkaerospace.com>"
SEND = dict(ON, MAIL_SEND_ENABLED=True)


def headers(m, **extra):
    for k, v in extra.items():
        m["payload"]["headers"].append({"name": k.replace("_", "-"), "value": v})
    return m


@override_settings(**SEND)
class ComposeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.user.first_name, self.user.last_name = "Kellen", "Moczulski"
        self.user.save()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API,
                                              backfill_done=True)
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        self.will = Person.objects.create(first_name="Will", email="will@firehawkaerospace.com", company=self.company)
        now = timezone.now()
        self.fake = FakeGmail([
            headers(api_message("0a01", "a1", WILL, f"Kellen <{ME}>, Simon <sshewmaker@acmigroup.com>",
                                "PDA for land search", body="Can you send the PDA?", when=now - timedelta(hours=2),
                                msg_id="<pda-1@fh>", attachment="Site plan v3.pdf"),
                    Cc="Pat <pat@shoreunitedbank.com>", References="<older@fh>"),
            api_message("0b01", "b2", ME, WILL, "Tour", body="Tuesday?", when=now - timedelta(hours=1),
                        labels=("SENT",), msg_id="<tour-1@acmi>"),
        ])
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sent(self, n=-1):
        meta, raw = self.fake.sent[n]
        return meta, message_from_bytes(raw, policy=policy.default)

    def post(self, **data):
        return self.client.post(reverse("crm:inbox_compose"), data)

    # Who a reply goes to

    def test_reply_reply_all_and_forward_fill_the_form(self):
        msg = self.fake.messages["0a01"]
        f = compose.reply_fields(self.mailbox, msg, "reply")
        self.assertEqual((f["to"], f["cc"], f["subject"]), ("Will Edwards <will@firehawkaerospace.com>", "",
                                                           "Re: PDA for land search"))
        self.assertEqual((f["thread_id"], f["in_reply_to"], f["references"]), ("a1", "<pda-1@fh>",
                                                                             "<older@fh> <pda-1@fh>"))
        f = compose.reply_fields(self.mailbox, msg, "all")
        self.assertEqual(f["cc"], "Simon <sshewmaker@acmigroup.com>, Pat <pat@shoreunitedbank.com>")  # never me
        f = compose.reply_fields(self.mailbox, msg, "forward")
        self.assertEqual((f["to"], f["subject"], f["thread_id"]), ("", "Fwd: PDA for land search", ""))
        self.assertEqual([a["name"] for a in f["forward_files"]], ["Site plan v3.pdf"])
        # Replying to my own message goes to the people I wrote to.
        f = compose.reply_fields(self.mailbox, self.fake.messages["0b01"], "reply")
        self.assertEqual(f["to"], "Will Edwards <will@firehawkaerospace.com>")

    def test_bad_addresses_are_caught_before_sending(self):
        with self.assertRaisesMessage(compose.ComposeError, "isn't an email address"):
            compose.parse_recipients("will@firehawk, pat@shoreunitedbank.com")
        self.assertEqual(compose.parse_recipients("Will <will@x.com>; pat@y.com".replace(";", ",")),
                         ["Will <will@x.com>", "pat@y.com"])

    def test_the_form_for_a_reply_and_for_an_address(self):
        r = self.client.get(reverse("crm:inbox_compose"), {"reply": "0a01", "mode": "all"})
        self.assertContains(r, 'value="Re: PDA for land search"')
        self.assertContains(r, "Reply all")
        r = self.client.get(reverse("crm:inbox_compose"), {"to": "will@firehawkaerospace.com"})
        self.assertContains(r, 'value="will@firehawkaerospace.com"')
        self.assertEqual(r.context["first_name"], "Will")
        self.assertEqual(self.client.get(reverse("crm:inbox_compose"), {"reply": "ffff"}).status_code, 404)

    # Sending

    def test_a_new_message_with_a_file_goes_out_signed_and_lands_on_the_timeline(self):
        prefs.set_pref(self.user, "mail_signature", "Kellen Moczulski\nACMI Properties")
        r = self.client.post(reverse("crm:inbox_compose"), {
            "mode": "new", "to": "Will <will@firehawkaerospace.com>", "subject": "LOI draft", "body": "Attached.",
            "files": SimpleUploadedFile("LOI v1.pdf", b"%PDF-1.4 loi", content_type="application/pdf"),
        })
        meta, msg = self.sent()
        self.assertEqual(meta, {})  # a new thread
        self.assertEqual(msg["From"], f"Kellen Moczulski <{ME}>")
        self.assertEqual(msg["To"], "Will <will@firehawkaerospace.com>")
        self.assertIn("Attached.\n\nKellen Moczulski\nACMI Properties", msg.get_body(("plain",)).get_content())
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["LOI v1.pdf"])
        self.assertTrue(r["Location"].startswith(reverse("crm:inbox") + "?label=SENT&t="))
        act = Activity.objects.get(source=Activity.Source.GMAIL, kind=Activity.Kind.EMAIL_SENT)
        self.assertEqual((act.company, act.person, act.message_id), (self.company, self.will, msg["Message-ID"]))
        self.assertEqual(act.actor, self.user)
        # The sync reading the same email later doesn't log it again.
        gid = next(g for g, m in self.fake.messages.items() if m["labelIds"] == ["SENT"] and g not in ("0b01",))
        self.mailbox.history_id = "100"
        self.mailbox.save()
        self.fake.history = [gid]
        stats = sync.sync_mailbox(self.mailbox, client=gmail_api.GmailClient(self.user, self.fake))
        self.assertEqual(stats["activities already logged"], 1)
        self.assertEqual(Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT).count(), 1)

    def test_a_reply_stays_in_the_thread_quotes_the_original_and_comes_back_to_it(self):
        back = "/inbox/?label=INBOX&t=a1"
        r = self.post(mode="reply", reply_to_id="0a01", to="Will <will@firehawkaerospace.com>",
                      subject="Re: PDA for land search", body="Here it is.", thread_id="a1",
                      in_reply_to="<pda-1@fh>", references="<older@fh> <pda-1@fh>", back=back)
        self.assertRedirects(r, back, fetch_redirect_response=False)
        meta, msg = self.sent()
        self.assertEqual(meta, {"threadId": "a1"})
        self.assertEqual((msg["In-Reply-To"], msg["References"]), ("<pda-1@fh>", "<older@fh> <pda-1@fh>"))
        plain = msg.get_body(("plain",)).get_content()
        self.assertIn("Here it is.", plain)
        self.assertIn("wrote:\n> Can you send the PDA?", plain)
        self.assertIn('<div class="gmail_quote">', msg.get_body(("html",)).get_content())

    def test_forward_keeps_only_the_ticked_original_files(self):
        self.post(mode="forward", reply_to_id="0a01", to="pat@shoreunitedbank.com", subject="Fwd: PDA",
                  body="FYI", forward_part="1")
        _, msg = self.sent()
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["Site plan v3.pdf"])
        self.assertIn("---------- Forwarded message ---------", msg.get_body(("plain",)).get_content())
        self.post(mode="forward", reply_to_id="0a01", to="pat@shoreunitedbank.com", subject="Fwd: PDA", body="FYI")
        _, msg = self.sent()
        self.assertEqual(list(msg.iter_attachments()), [])

    def test_mistakes_come_back_with_what_was_typed(self):
        r = self.post(mode="new", to="will@firehawk", subject="Hi", body="Keep this text")
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "isn&#x27;t an email address", status_code=400)
        self.assertContains(r, "Keep this text", status_code=400)
        r = self.post(mode="new", to="", subject="Hi", body="x")
        self.assertContains(r, "Add at least one recipient.", status_code=400)
        self.assertEqual(self.fake.sent, [])

    def test_too_big_to_send(self):
        with mock.patch.object(compose, "MAX_BYTES", 10):
            r = self.client.post(reverse("crm:inbox_compose"), {
                "mode": "new", "to": "will@firehawkaerospace.com", "subject": "x", "body": "x",
                "files": SimpleUploadedFile("big.pdf", b"0123456789ABC")})
        self.assertContains(r, "25 MB", status_code=400)
        self.assertEqual(self.fake.sent, [])

    def test_a_gmail_error_while_sending_is_never_retried(self):
        self.fake.send_status = 500
        r = self.post(mode="new", to="will@firehawkaerospace.com", subject="x", body="x")
        self.assertContains(r, "Check your Sent folder", status_code=400)
        self.assertEqual(len(self.fake.sent), 1)

    @override_settings(MAIL_SEND_ENABLED=False)
    def test_the_site_switch_blocks_sending(self):
        r = self.post(mode="new", to="will@firehawkaerospace.com", subject="x", body="x")
        self.assertContains(r, "Sending is switched off", status_code=400)
        self.assertEqual(self.fake.sent, [])

    def test_an_inbox_only_mailbox_sends_but_logs_nothing(self):
        Mailbox.objects.filter(pk=self.mailbox.pk).update(enabled=False)
        self.post(mode="new", to="will@firehawkaerospace.com", subject="x", body="x")
        self.assertEqual(len(self.fake.sent), 1)
        self.assertFalse(Activity.objects.filter(source=Activity.Source.GMAIL).exists())

    # Drafts

    def test_drafts_save_as_you_type_and_go_away_when_sent(self):
        data = {"mode": "reply", "reply_to_id": "0a01", "to": "will@firehawkaerospace.com", "subject": "Re: PDA",
                "body": "Working on it", "thread_id": "a1"}
        r = self.client.post(reverse("crm:inbox_draft"), data)
        draft = r.json()["draft"]
        self.assertIn(draft, self.fake.drafts)
        self.assertEqual(self.fake.posts[-1][1]["message"]["threadId"], "a1")
        r = self.client.post(reverse("crm:inbox_draft"), {**data, "body": "Almost done", "draft_id": draft})
        self.assertEqual(r.json()["draft"], draft)
        self.assertEqual(self.fake.posts[-1][0], f"PUT /drafts/{draft}")
        self.post(**{**data, "body": "Done", "draft_id": draft})
        self.assertNotIn(draft, self.fake.drafts)
        self.assertEqual(self.fake.posts[-1][0], f"DELETE /drafts/{draft}")

    def test_a_draft_without_recipients_and_discard(self):
        r = self.client.post(reverse("crm:inbox_draft"), {"mode": "new", "subject": "Idea", "body": "later"})
        draft = r.json()["draft"]
        r = self.client.post(reverse("crm:inbox_draft_discard"), {"draft_id": draft, "back": "/inbox/"})
        self.assertRedirects(r, "/inbox/", fetch_redirect_response=False)
        self.assertNotIn(draft, self.fake.drafts)

    # Around the hub

    def test_reply_buttons_keys_and_the_email_links_on_records(self):
        r = self.client.get(reverse("crm:inbox"), {"t": "a1"})
        for key in ('data-key="r"', 'data-key="a"', 'data-key="f"', 'data-key="c"'):
            self.assertContains(r, key)
        self.assertContains(r, f'{reverse("crm:inbox_compose")}?reply=0a01&mode=reply')
        r = self.client.get(self.will.get_absolute_url())
        self.assertContains(r, f'{reverse("crm:inbox_compose")}?to=will%40firehawkaerospace.com')
        r = self.client.get(self.company.get_absolute_url(), {"tab": "people"})
        self.assertContains(r, "will%40firehawkaerospace.com")

    def test_signature_is_saved_on_the_mail_page(self):
        r = self.client.post(reverse("crm:mail_signature"), {"signature": "K. Moczulski"}, follow=True)
        self.assertContains(r, "Signature saved.")
        self.assertEqual(compose.signature(self.user), "K. Moczulski")
        self.assertContains(self.client.get(reverse("crm:inbox_compose")), "K. Moczulski")


@override_settings(**ON)
class ComposeAccessTests(TestCase):
    def test_nothing_is_sent_while_the_tests_run(self):
        from django.conf import settings

        self.assertFalse(settings.MAIL_SEND_ENABLED)

    def test_partners_and_mail_off(self):
        partner = connected_user("fortress", "lp@acmigroup.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(partner)
        for url in (reverse("crm:inbox_compose"), reverse("crm:inbox_draft")):
            self.assertRedirects(self.client.post(url, {"to": "x@y.com"}), "/room/", fetch_redirect_response=False)
        user = connected_user()
        from crm.models import MailConnection

        MailConnection.objects.filter(user=user).update(enabled=False)
        self.client.force_login(user)
        r = self.client.get(reverse("crm:inbox_compose"))
        self.assertRedirects(r, reverse("crm:mail_settings"), fetch_redirect_response=False)
