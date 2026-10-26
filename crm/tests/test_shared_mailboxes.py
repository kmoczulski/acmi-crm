"""Mail in the hub, step 7: shared mailboxes (connect once as the address, grants, read-only vs send, the log)."""
from datetime import timedelta
from email import message_from_bytes, policy
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import tokens
from crm.mail import client as mail_client, gmail_api, shared, sync
from crm.models import (
    Activity, Company, MailboxAccess, MailboxAccessLog, Mailbox, MailThread, Notification, Person, SharedMailboxToken,
)
from crm.permissions import PARTNERS

from .test_gmail_api import ON, FakeGmail, FakeResponse, api_message
from .test_mail_actions import connected_user
from .utils import make_manager

DEALS = "deals@acmigroup.com"
SEND = dict(ON, MAIL_SEND_ENABLED=True)


def shared_mailbox(connected_by=None):
    mb = Mailbox.objects.create(shared=True, email=DEALS, name="Deals", backend=Mailbox.Backend.GMAIL_API,
                                backfill_done=True)
    SharedMailboxToken.objects.create(mailbox=mb, token="sa", token_secret="sr", connected_by=connected_by,
                                      expires_at=timezone.now() + timedelta(hours=1))
    return mb


class FakeGoogleSignIn:
    """Google's token and userinfo endpoints for the shared-mailbox sign-in."""

    def __init__(self, email=DEALS, scope=f"openid email {'https://www.googleapis.com/auth/gmail.modify'}",
                 refresh="shared-refresh"):
        self.email, self.scope, self.refresh = email, scope, refresh
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(("post", url, data))
        if url == shared.TOKEN_URL:
            return FakeResponse({"access_token": "shared-access", "refresh_token": self.refresh, "expires_in": 3600,
                                 "scope": self.scope})
        return FakeResponse({})

    def get(self, url, headers=None, timeout=None):
        return FakeResponse({"email": self.email, "email_verified": True})


@override_settings(**ON)
class ConnectTests(TestCase):
    def setUp(self):
        self.kellen = make_manager(connected_user())
        self.client.force_login(self.kellen)

    def test_a_manager_is_sent_to_google_as_the_shared_address(self):
        r = self.client.post(reverse("crm:mail_shared_connect"), {"email": "Deals@acmigroup.com"})
        url = urlparse(r["Location"])
        q = parse_qs(url.query)
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", shared.AUTH_URL)
        self.assertEqual(q["login_hint"], [DEALS])
        self.assertIn("https://www.googleapis.com/auth/gmail.modify", q["scope"][0])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertEqual(self.client.session[shared.SESSION_KEY]["email"], DEALS)

    def test_what_cannot_be_connected(self):
        for email, why in (("deals@gmail.com", "ACMI address"), ("kmoczulski@acmigroup.com", "own address")):
            r = self.client.post(reverse("crm:mail_shared_connect"), {"email": email}, follow=True)
            self.assertContains(r, why)
        with self.settings(TOKEN_KEYS=[]):
            r = self.client.post(reverse("crm:mail_shared_connect"), {"email": DEALS}, follow=True)
            self.assertContains(r, "encryption key")

    def test_only_managers(self):
        simon = connected_user("simon", "sshewmaker@acmigroup.com")
        self.client.force_login(simon)
        self.client.post(reverse("crm:mail_shared_connect"), {"email": DEALS})
        self.assertNotIn(shared.SESSION_KEY, self.client.session)
        self.assertNotContains(self.client.get(reverse("crm:mail_settings")), "Connect a shared mailbox")

    def _come_back(self, google, state=None):
        self.client.post(reverse("crm:mail_shared_connect"), {"email": DEALS})
        state = state or self.client.session[shared.SESSION_KEY]["state"]
        with mock.patch.object(shared, "requests", google):
            return self.client.get(reverse("crm:mail_shared_done"), {"code": "c0de", "state": state}, follow=True)

    def test_coming_back_keeps_the_permission_encrypted(self):
        r = self._come_back(FakeGoogleSignIn())
        self.assertContains(r, f"{DEALS} is connected")
        mb = Mailbox.objects.get(shared=True)
        self.assertEqual((mb.email, mb.name, mb.user, mb.backend), (DEALS, "Deals", None, Mailbox.Backend.GMAIL_API))
        token = SharedMailboxToken.objects.get(mailbox=mb)
        self.assertEqual((token.token_secret, token.connected_by), ("shared-refresh", self.kellen))
        raw = SharedMailboxToken.objects.filter(pk=token.pk).values_list("token_secret", flat=True).get()
        self.assertTrue(raw.startswith(tokens.PREFIX))

    def test_coming_back_refuses_the_wrong_account_no_gmail_or_a_forged_state(self):
        self.assertContains(self._come_back(FakeGoogleSignIn(email="kmoczulski@acmigroup.com")), "Sign in as the shared")
        self.assertContains(self._come_back(FakeGoogleSignIn(scope="openid email")), "Leave the Gmail box ticked")
        self.assertContains(self._come_back(FakeGoogleSignIn(), state="forged"), "didn&#x27;t start here")
        self.assertFalse(Mailbox.objects.filter(shared=True).exists())


@override_settings(**SEND)
class UsingASharedMailboxTests(TestCase):
    def setUp(self):
        cache.clear()
        self.kellen = make_manager(connected_user())
        self.simon = connected_user("simon", "sshewmaker@acmigroup.com")
        self.deals = shared_mailbox(self.kellen)
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        Person.objects.create(first_name="Will", email="will@firehawkaerospace.com", company=self.company)
        self.fake = FakeGmail([api_message("0a01", "a1", "Will <will@firehawkaerospace.com>", DEALS, "RFP for MEIH",
                                           msg_id="<rfp-1@fh>", labels=("INBOX", "UNREAD"))], email=DEALS)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def grant(self, user, can_send=False):
        self.client.force_login(self.kellen)
        self.client.post(reverse("crm:mail_shared_grant", args=[self.deals.pk]),
                         {"user": user.pk, "can_send": "1" if can_send else "0"})
        self.client.force_login(user)

    def test_roles(self):
        self.assertIsNone(shared.role(self.simon, self.deals))
        MailboxAccess.objects.create(mailbox=self.deals, user=self.simon)
        self.assertEqual(shared.role(self.simon, self.deals), "read")
        MailboxAccess.objects.filter(user=self.simon).update(can_send=True)
        self.assertEqual(shared.role(self.simon, self.deals), "send")
        SharedMailboxToken.objects.filter(mailbox=self.deals).delete()
        self.assertIsNone(shared.role(self.simon, self.deals))

    def test_read_access_shows_the_mailbox_but_changes_nothing(self):
        self.grant(self.simon)
        r = self.client.get(reverse("crm:inbox"), {"mb": self.deals.pk, "t": "a1"})
        self.assertEqual(r.context["mailbox"], self.deals)
        self.assertContains(r, "read only")
        self.assertNotContains(r, 'data-key="e"')
        self.assertNotContains(r, "data-inline")
        self.assertIn("UNREAD", self.fake.messages["0a01"]["labelIds"])  # reading doesn't mark it read for everyone
        r = self.client.post(reverse("crm:inbox_act"), {"action": "archive", "t": "a1", "next": "/inbox/"}, follow=True)
        self.assertContains(r, "You can read this shared mailbox, but not send")
        self.assertIn("INBOX", self.fake.messages["0a01"]["labelIds"])
        r = self.client.post(reverse("crm:inbox_compose"), {"mode": "new", "to": "will@firehawkaerospace.com",
                                                            "subject": "x", "body": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(list(MailboxAccessLog.objects.values_list("action", flat=True)), ["open"])

    def test_send_access_replies_as_the_shared_address(self):
        self.grant(self.simon, can_send=True)
        self.client.get(reverse("crm:inbox"), {"mb": self.deals.pk})
        r = self.client.post(reverse("crm:inbox_compose"), {
            "mode": "reply", "reply_to_id": "0a01", "to": "will@firehawkaerospace.com", "subject": "Re: RFP for MEIH",
            "body": "Attached.", "thread_id": "a1", "back": "/inbox/?t=a1"})
        self.assertEqual(r.status_code, 302)
        msg = message_from_bytes(self.fake.sent[-1][1], policy=policy.default)
        self.assertEqual(msg["From"], f"Deals <{DEALS}>")
        act = Activity.objects.get(kind=Activity.Kind.EMAIL_SENT)
        self.assertEqual((act.company, act.actor), (self.company, self.simon))
        self.client.post(reverse("crm:inbox_act"), {"action": "archive", "t": "a1", "next": "/inbox/"})
        self.assertNotIn("INBOX", self.fake.messages["0a01"]["labelIds"])
        self.assertEqual(set(MailboxAccessLog.objects.values_list("action", flat=True)), {"send", "change"})
        self.client.force_login(self.kellen)
        r = self.client.get(reverse("crm:mail_shared_log", args=[self.deals.pk]))
        self.assertContains(r, "Sent")
        self.assertContains(r, "Re: RFP for MEIH")

    def test_the_log_and_settings_survive_a_deleted_teammate(self):
        MailboxAccessLog.objects.create(mailbox=self.deals, user=None, action="send", detail="Re: RFP")
        SharedMailboxToken.objects.filter(mailbox=self.deals).update(connected_by=None)
        self.client.force_login(self.kellen)
        r = self.client.get(reverse("crm:mail_shared_log", args=[self.deals.pk]))
        self.assertContains(r, "(deleted)")
        r = self.client.get(reverse("crm:mail_settings"))
        self.assertContains(r, "someone no longer on the team")

    def test_the_switcher_and_going_back_to_your_own(self):
        self.grant(self.simon)
        r = self.client.get(reverse("crm:inbox"), {"mb": self.deals.pk})
        self.assertEqual(r.context["mailbox"], self.deals)
        self.assertContains(r, "👥 Deals")
        self.assertEqual(self.client.get(reverse("crm:inbox")).context["mailbox"], self.deals)  # kept in the session
        r = self.client.get(reverse("crm:inbox"), {"mb": "own"})
        self.assertEqual(r.context["mailbox"].user, self.simon)

    def test_someone_with_only_a_shared_mailbox_still_gets_mail(self):
        from crm.models import MailConnection

        MailConnection.objects.filter(user=self.simon).update(enabled=False)
        self.grant(self.simon)
        self.assertContains(self.client.get(reverse("crm:home")), f'href="{reverse("crm:inbox")}"')
        self.assertEqual(self.client.get(reverse("crm:inbox")).context["mailbox"], self.deals)

    def test_no_access_no_mailbox(self):
        self.client.force_login(self.simon)
        r = self.client.get(reverse("crm:inbox"), {"mb": self.deals.pk})
        self.assertNotEqual(r.context["mailbox"], self.deals)

    def test_partners_are_never_given_access(self):
        partner = connected_user("fortress", "lp@acmigroup.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(self.kellen)
        r = self.client.post(reverse("crm:mail_shared_grant", args=[self.deals.pk]), {"user": partner.pk}, follow=True)
        self.assertContains(r, "Pick someone on the team.")
        MailboxAccess.objects.create(mailbox=self.deals, user=partner)  # even if one slipped in
        self.assertIsNone(shared.role(partner, self.deals))

    def test_new_mail_alerts_everyone_given_the_mailbox(self):
        MailboxAccess.objects.create(mailbox=self.deals, user=self.simon)
        MailboxAccess.objects.create(mailbox=self.deals, user=self.kellen, can_send=True)
        email = gmail_api.parse_api_message(self.fake.messages["0a01"])
        self.assertEqual(mail_client.on_new_mail(self.deals, [email]), 2)
        note = Notification.objects.get(user=self.simon, kind=Notification.Kind.MAIL)
        self.assertIn("[Deals] RFP for MEIH", note.text)
        self.assertEqual(note.url, f"{reverse('crm:inbox')}?mb={self.deals.pk}&t=a1")

    def test_the_sync_reads_it_with_its_own_permission(self):
        results = sync.sync_all()
        self.assertIn(str(self.deals), results)
        self.deals.refresh_from_db()
        self.assertEqual(self.deals.last_error, "")
        self.assertTrue(Activity.objects.filter(company=self.company, message_id="<rfp-1@fh>").exists())
        act = Activity.objects.get(message_id="<rfp-1@fh>")
        self.assertFalse(act.team_members.exists())  # no one person's connection

    def test_disconnect(self):
        MailboxAccess.objects.create(mailbox=self.deals, user=self.simon)
        MailThread.objects.create(mailbox=self.deals, gmail_id="a1")
        self.client.force_login(self.kellen)
        google = FakeGoogleSignIn()
        with mock.patch.object(shared, "requests", google):
            self.client.post(reverse("crm:mail_shared_update", args=[self.deals.pk]), {"action": "disconnect"})
        self.assertIn(("post", shared.REVOKE_URL, {"token": "sr"}), google.calls)
        self.deals.refresh_from_db()
        self.assertFalse(self.deals.enabled)
        self.assertFalse(SharedMailboxToken.objects.exists())
        self.assertFalse(MailboxAccess.objects.exists())
        self.assertFalse(MailThread.objects.exists())

    def test_rename_and_timeline_switch(self):
        self.client.force_login(self.kellen)
        self.client.post(reverse("crm:mail_shared_update", args=[self.deals.pk]), {"name": "Deal desk"})
        self.deals.refresh_from_db()
        self.assertEqual((self.deals.name, self.deals.enabled), ("Deal desk", False))
