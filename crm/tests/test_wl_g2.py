"""Worklist 2026-10-28, group 2: Add note when Gmail fails (W2), and read-only grantees can't file (W6)."""
from unittest import mock

import requests
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from crm import gcal
from crm.mail import gmail_api, index
from crm.models import MailComment, Mailbox

from .test_gmail_api import ME, ON, FakeGmail, api_message
from .test_mail_actions import connected_user

SAM = "Sam Ortiz <sam.ortiz@gmail.com>"


def said(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


@override_settings(**ON)
class AddNoteWhenGmailFailsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        self.fake = FakeGmail([api_message("0a01", "a1", SAM, ME, "Site for our plant", msg_id="<sam-1@gmail>")])
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        index.refresh(self.mailbox, gmail_api.GmailClient(self.user, self.fake), ["a1"])
        cache.clear()  # so the note has to ask Gmail for the thread

    def note(self):
        return self.client.post(reverse("crm:inbox_note"), {"t": "a1", "body": "Call Sam back", "next": "/inbox/?t=a1"})

    def test_a_refused_token_goes_back_with_the_reason(self):
        with mock.patch("crm.gcal.access_token", side_effect=gcal.CalendarError("refused")):
            r = self.note()
        self.assertRedirects(r, "/inbox/?t=a1", fetch_redirect_response=False)
        self.assertTrue(any("Google didn't accept" in m for m in said(r)))
        self.assertFalse(MailComment.objects.exists())

    def test_gmail_out_of_reach_goes_back_with_a_message(self):
        with mock.patch("crm.mail.render.get_thread", side_effect=requests.ConnectionError()):
            r = self.note()
        self.assertRedirects(r, "/inbox/?t=a1", fetch_redirect_response=False)
        self.assertIn("Couldn't reach Gmail. Try again in a moment.", said(r))
        self.assertFalse(MailComment.objects.exists())

    def test_the_note_still_saves_when_gmail_answers(self):
        r = self.note()
        self.assertRedirects(r, "/inbox/?t=a1", fetch_redirect_response=False)
        self.assertEqual(MailComment.objects.get().body, "Call Sam back")
