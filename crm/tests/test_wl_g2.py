"""Worklist 2026-10-28, group 2: Add note when Gmail fails (W2), and read-only grantees can't file (W6)."""
from unittest import mock

import requests
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from crm import gcal
from crm.mail import gmail_api, index
from crm.mail.inbox_views import READ_ONLY
from crm.models import Activity, Company, MailboxAccess, MailComment, MailFiling, Mailbox

from .test_gmail_api import ME, ON, FakeGmail, api_message
from .test_mail_actions import connected_user
from .test_shared_mailboxes import DEALS, SEND, shared_mailbox
from .utils import make_manager

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


@override_settings(**SEND)
class ReadOnlyGranteeCannotFileTests(TestCase):
    def setUp(self):
        cache.clear()
        self.kellen = make_manager(connected_user())
        self.simon = connected_user("simon", "sshewmaker@acmigroup.com")
        self.deals = shared_mailbox(self.kellen)
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        self.fake = FakeGmail([api_message("0a01", "a1", "Will <will@firehawkaerospace.com>", DEALS, "RFP for MEIH",
                                           msg_id="<rfp-1@fh>")], email=DEALS)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.grant = MailboxAccess.objects.create(mailbox=self.deals, user=self.simon)
        self.client.force_login(self.simon)
        self.client.get(reverse("crm:inbox"), {"mb": self.deals.pk})  # looking at the shared mailbox
        self.labels_before = list(self.fake.messages["0a01"]["labelIds"])

    def file(self):
        return self.client.post(reverse("crm:inbox_file"), {"t": "a1", "kind": "company", "pk": self.company.pk,
                                                            "next": "/inbox/?t=a1"})

    def can_send(self, yes):
        MailboxAccess.objects.filter(pk=self.grant.pk).update(can_send=yes)

    def test_read_only_cannot_file(self):
        r = self.file()
        self.assertRedirects(r, "/inbox/?t=a1", fetch_redirect_response=False)
        self.assertIn(READ_ONLY, said(r))
        self.assertFalse(MailFiling.objects.exists())
        self.assertFalse(Activity.objects.filter(company=self.company).exists())
        self.assertEqual(self.fake.messages["0a01"]["labelIds"], self.labels_before)  # Gmail labels untouched

    def test_read_only_cannot_take_a_filing_off(self):
        self.can_send(True)
        self.file()
        filing = MailFiling.objects.get()
        self.can_send(False)
        r = self.client.post(reverse("crm:inbox_unfile", args=[filing.pk]), {"next": "/inbox/?t=a1"})
        self.assertRedirects(r, "/inbox/?t=a1", fetch_redirect_response=False)
        self.assertIn(READ_ONLY, said(r))
        self.assertTrue(MailFiling.objects.filter(pk=filing.pk).exists())

    def test_send_access_can_still_file_and_unfile(self):
        self.can_send(True)
        self.file()
        filing = MailFiling.objects.get()
        self.assertTrue(Activity.objects.filter(company=self.company).exists())
        self.client.post(reverse("crm:inbox_unfile", args=[filing.pk]), {"next": "/inbox/"})
        self.assertFalse(MailFiling.objects.exists())
