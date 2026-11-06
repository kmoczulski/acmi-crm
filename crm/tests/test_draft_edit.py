"""Editing a draft: "Edit draft" in the reading pane opens it in the composer, typed text only (Kellen, 2026-11-06)."""
from datetime import timedelta
from email import message_from_bytes, policy
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import gmail_api
from crm.models import Mailbox

from .test_compose import SEND, WILL
from .test_gmail_api import ME, FakeGmail, api_message
from .test_mail_actions import connected_user

DRAFT_HTML = ('<div dir="ltr">Thanks Will,<br>I will send the PDA <b>Friday</b>.<br><br>'
              '<div class="gmail_signature">Kellen Moczulski<br>ACMI Properties</div><br>'
              '<div class="gmail_quote"><div>On Mon, Nov 2, 2026 at 9:00 AM, Will Edwards wrote:</div>'
              '<blockquote style="margin:0">Can you send the PDA?</blockquote></div></div>')


@override_settings(**SEND)
class EditDraftTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API, backfill_done=True)
        now = timezone.now()
        self.original = api_message("0a01", "a1", WILL, f"Kellen <{ME}>", "PDA", body="Can you send the PDA?",
                                    when=now - timedelta(hours=5), msg_id="<pda-1@fh>")
        self.draft = api_message("0d01", "a1", f"Kellen <{ME}>", WILL, "Re: PDA", html=DRAFT_HTML, labels=("DRAFT",),
                                 when=now - timedelta(hours=1), extra_headers=[
                                     {"name": "In-Reply-To", "value": "<pda-1@fh>"},
                                     {"name": "References", "value": "<pda-1@fh>"}])
        self.fake = FakeGmail([self.original, self.draft])
        self.fake.draft_msgs["r-9"] = self.draft
        self.client.force_login(self.user)
        mock.patch.object(gmail_api, "requests", self.fake).start()
        self.addCleanup(mock.patch.stopall)
        self.url = reverse("crm:inbox_compose")

    def test_the_conversation_offers_edit_draft(self):
        page = self.client.get(reverse("crm:inbox"), {"label": "INBOX", "t": "a1"})
        self.assertEqual(page.context["reader"]["draft"]["draft_id"], "r-9")
        self.assertContains(page, "You have an unsent draft in this conversation")
        self.assertContains(page, f"{self.url}?draft=r-9")
        self.assertContains(page, "Edit draft")

    def test_a_conversation_without_a_draft_does_not(self):
        fake = FakeGmail([self.original])
        mock.patch.object(gmail_api, "requests", fake).start()
        page = self.client.get(reverse("crm:inbox"), {"label": "INBOX", "t": "a1"})
        self.assertIsNone(page.context["reader"]["draft"])
        self.assertNotContains(page, "unsent draft")

    def test_the_composer_opens_with_what_was_typed_and_not_the_signature_or_quote(self):
        r = self.client.get(self.url, {"draft": "r-9", "back": "/inbox/?t=a1"})
        self.assertEqual(r.status_code, 200)
        body = r.context["body_html"]
        self.assertIn("Thanks Will", body)
        self.assertIn("<b>Friday</b>", body)  # the formatting you typed survives
        self.assertNotIn("gmail_signature", body)
        self.assertNotIn("Kellen Moczulski", body)
        self.assertNotIn("wrote:", body)
        self.assertNotIn("Can you send the PDA", body)
        self.assertEqual(r.context["draft_id"], "r-9")  # autosave goes on updating this draft
        f = r.context["f"]
        self.assertEqual((f["subject"], f["mode"], f["reply_to_id"]), ("Re: PDA", "reply", "0a01"))
        self.assertIn("will@firehawkaerospace.com", f["to"])
        self.assertEqual(f["in_reply_to"], "<pda-1@fh>")
        self.assertEqual(r.context["original"]["from_addr"], "will@firehawkaerospace.com")  # the quote's source, not the draft

    def test_sending_the_edited_draft_carries_the_signature_and_quote_once(self):
        r = self.client.get(self.url, {"draft": "r-9"})
        post = {"to": r.context["f"]["to"], "subject": "Re: PDA", "body": "Thanks Will, sent it Friday.",
                "body_html": "Thanks Will, sent it Friday.", "mode": "reply", "reply_to_id": "0a01", "thread_id": "a1",
                "in_reply_to": "<pda-1@fh>", "references": "<pda-1@fh>", "draft_id": "r-9", "inline": "1"}
        self.assertEqual(self.client.post(self.url, post).status_code, 200)
        self.assertEqual(len(self.fake.sent), 1)
        sent = message_from_bytes(self.fake.sent[0][1], policy=policy.default)
        html = sent.get_body(preferencelist=("html",)).get_content()
        self.assertEqual(html.count("gmail_quote"), 1)
        self.assertEqual(html.count("Can you send the PDA"), 1)
        self.assertEqual(html.count("sent it Friday"), 1)

    def test_a_draft_written_in_gmail_with_plain_text_only_also_opens(self):
        plain = api_message("0d02", "a1", f"Kellen <{ME}>", WILL, "Re: PDA", body="Quick note.\n\nOn Mon, Nov 2, Will wrote:\n> hi",
                            labels=("DRAFT",), extra_headers=[{"name": "In-Reply-To", "value": "<pda-1@fh>"}])
        self.fake.draft_msgs["r-10"] = plain
        r = self.client.get(self.url, {"draft": "r-10"})
        self.assertEqual(r.context["body"].strip(), "Quick note.")

    def test_a_draft_with_files_says_to_attach_them_again(self):
        withfile = api_message("0d03", "a2", f"Kellen <{ME}>", WILL, "Plan", body="See attached.", labels=("DRAFT",),
                               attachment="plan.pdf")
        self.fake.draft_msgs["r-11"] = withfile
        r = self.client.get(self.url, {"draft": "r-11"})
        self.assertContains(r, "plan.pdf")
        self.assertContains(r, "attach them again")

    def test_a_draft_that_is_gone_goes_back_to_the_inbox_with_a_note(self):
        r = self.client.get(self.url, {"draft": "nope"}, follow=True)
        self.assertRedirects(r, reverse("crm:inbox"))
        self.assertContains(r, "That draft is gone")

    def test_a_brand_new_draft_with_no_reply_opens_as_a_new_message(self):
        fresh = api_message("0d04", "a3", f"Kellen <{ME}>", WILL, "Intro", html='<div dir="ltr">Hello Will</div>', labels=("DRAFT",))
        self.fake.draft_msgs["r-12"] = fresh
        r = self.client.get(self.url, {"draft": "r-12"})
        self.assertEqual((r.context["f"]["mode"], r.context["f"]["reply_to_id"]), ("new", ""))
        self.assertIn("Hello Will", r.context["body_html"])
