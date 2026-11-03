"""Mail in the hub, step 1: encrypted Google tokens, each person's on / off switch, and partners kept out."""
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialToken
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import tokens
from crm.auth import SocialAccountAdapter
from crm.mail import connect, sync
from crm.mail import views as mail_views
from crm.models import MailConnection, Mailbox
from crm.permissions import PARTNERS

User = get_user_model()
KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()
GOOGLE = {"google": {"APP": {"client_id": "cid", "secret": "sec"},
                     "SCOPE": ["profile", "email", "https://www.googleapis.com/auth/calendar.readonly"],
                     "AUTH_PARAMS": {"access_type": "offline", "include_granted_scopes": "true"}}}
ON = dict(SOCIALACCOUNT_PROVIDERS=GOOGLE, GOOGLE_SIGNIN_ENABLED=True, TOKEN_KEYS=[KEY], MAIL_CLIENT_ENABLED=True)


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


class FakeGoogle:
    """Stands in for `requests`: tokeninfo answers with `scopes`; revoke and refresh are recorded."""

    def __init__(self, scopes=(), revoke_status=200):
        self.scopes, self.revoke_status = scopes, revoke_status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", url))
        return FakeResponse({"scope": " ".join(self.scopes)})

    def post(self, url, data=None, timeout=None):
        self.calls.append(("post", url, data))
        if url == connect.REVOKE_URL:
            return FakeResponse({}, self.revoke_status)
        return FakeResponse({"access_token": "fresh", "expires_in": 3600})


def google_token(user, email=None, token="access-1", secret="refresh-1"):
    account = SocialAccount.objects.create(user=user, provider="google", uid=f"uid-{user.pk}",
                                           extra_data={"email": email or user.email})
    return SocialToken.objects.create(account=account, token=token, token_secret=secret,
                                      expires_at=timezone.now() + timedelta(hours=1))


def raw(token):
    return SocialToken.objects.filter(pk=token.pk).values_list("token", "token_secret").get()


def make_partner(user):
    user.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
    return user


class TokenEncryptionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", email="kmoczulski@acmigroup.com")

    @override_settings(TOKEN_KEYS=[KEY])
    def test_tokens_are_stored_encrypted_and_read_back_plain(self):
        t = google_token(self.user)
        stored_token, stored_secret = raw(t)
        self.assertTrue(stored_token.startswith(tokens.PREFIX))
        self.assertTrue(stored_secret.startswith(tokens.PREFIX))
        self.assertNotIn("refresh-1", stored_secret)
        self.assertEqual(t.token_secret, "refresh-1")  # the saved object stays usable
        loaded = SocialToken.objects.get(pk=t.pk)
        self.assertEqual((loaded.token, loaded.token_secret), ("access-1", "refresh-1"))

    @override_settings(TOKEN_KEYS=[KEY])
    def test_calendar_refresh_saves_the_new_access_token_encrypted(self):
        from crm import gcal

        t = google_token(self.user)
        SocialToken.objects.filter(pk=t.pk).update(expires_at=timezone.now() - timedelta(minutes=5))
        token = SocialToken.objects.get(pk=t.pk)
        with self.settings(SOCIALACCOUNT_PROVIDERS=GOOGLE):
            self.assertEqual(gcal.access_token(token, FakeGoogle()), "fresh")
        stored_token, stored_secret = raw(t)
        self.assertTrue(stored_token.startswith(tokens.PREFIX))
        self.assertEqual(SocialToken.objects.get(pk=t.pk).token, "fresh")
        self.assertEqual(SocialToken.objects.get(pk=t.pk).token_secret, "refresh-1")

    def test_without_a_key_tokens_stay_as_they_were(self):
        with self.settings(TOKEN_KEYS=[]):
            t = google_token(self.user)
            self.assertEqual(raw(t), ("access-1", "refresh-1"))

    def test_a_token_from_another_key_reads_as_empty(self):
        with self.settings(TOKEN_KEYS=[OTHER_KEY]):
            t = google_token(self.user)
        with self.settings(TOKEN_KEYS=[KEY]):
            self.assertEqual(SocialToken.objects.get(pk=t.pk).token_secret, "")

    def test_a_new_key_takes_over_and_the_old_one_still_reads(self):
        with self.settings(TOKEN_KEYS=[OTHER_KEY]):
            t = google_token(self.user)
        with self.settings(TOKEN_KEYS=[KEY, OTHER_KEY]):
            self.assertEqual(SocialToken.objects.get(pk=t.pk).token_secret, "refresh-1")

    def test_encrypt_tokens_command_encrypts_what_was_stored_plain(self):
        with self.settings(TOKEN_KEYS=[]):
            t = google_token(self.user)
        with self.settings(TOKEN_KEYS=[]):
            with self.assertRaises(CommandError):
                call_command("encrypt_tokens", stdout=StringIO())
        with self.settings(TOKEN_KEYS=[KEY]):
            out = StringIO()
            call_command("encrypt_tokens", stdout=out)
            self.assertIn("Encrypted 1 Google token", out.getvalue())
            self.assertTrue(raw(t)[1].startswith(tokens.PREFIX))
            self.assertEqual(SocialToken.objects.get(pk=t.pk).token_secret, "refresh-1")
            call_command("encrypt_tokens", stdout=out)  # again: nothing left to do
            self.assertIn("Encrypted 0 Google tokens", out.getvalue())


@override_settings(**ON)
class MailSwitchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", email="kmoczulski@acmigroup.com")
        self.client.force_login(self.user)

    def test_off_by_default_with_a_turn_on_button(self):
        self.assertFalse(connect.is_on(self.user))
        r = self.client.get(reverse("crm:mail_settings"))
        self.assertContains(r, "Mail is off")
        self.assertContains(r, "Turn on Mail")
        self.assertContains(self.client.get(reverse("crm:home")), reverse("crm:mail_settings"))

    def test_no_key_means_mail_cannot_be_turned_on(self):
        with self.settings(TOKEN_KEYS=[]):
            r = self.client.get(reverse("crm:mail_settings"))
            self.assertNotContains(r, "Turn on Mail")
            self.assertContains(r, "ACMI_TOKEN_KEY")
            r = self.client.post(reverse("crm:mail_turn_on"))
            self.assertRedirects(r, reverse("crm:mail_settings"), fetch_redirect_response=False)
            self.assertNotIn(connect.SESSION_KEY, self.client.session)

    def test_site_switch_hides_it_for_everyone(self):
        google_token(self.user)
        MailConnection.objects.create(user=self.user, enabled=True)
        self.assertTrue(connect.is_on(self.user))
        SocialToken.objects.filter(account__user=self.user).update(token_secret="")
        self.assertTrue(connect.is_on(self.user))
        with self.settings(MAIL_CLIENT_ENABLED=False):
            self.assertFalse(connect.is_on(self.user))
            self.assertIn("switched off for everyone", connect.unavailable_reason(self.user))

    def test_on_needs_a_google_token(self):
        MailConnection.objects.create(user=self.user, enabled=True)
        self.assertFalse(connect.is_on(self.user))

    def test_only_acmi_addresses(self):
        self.user.email = "kellen@gmail.com"
        self.user.save()
        self.assertIn("ACMI Google accounts only", connect.unavailable_reason(self.user))

    def test_turn_on_sends_the_person_to_google_for_gmail_only_on_top_of_their_grant(self):
        r = self.client.post(reverse("crm:mail_turn_on"))
        self.assertEqual(r.status_code, 302)
        url = urlparse(r["Location"])
        self.assertEqual(url.path, "/accounts/google/login/")
        q = parse_qs(url.query)
        self.assertEqual(q["process"], ["connect"])
        self.assertEqual(q["scope"], [connect.GMAIL_SCOPE])
        self.assertIn("include_granted_scopes=true", q["auth_params"][0])
        self.assertIn("prompt=consent", q["auth_params"][0])
        self.assertEqual(q["next"], [reverse("crm:mail_connected")])
        self.assertTrue(self.client.session[connect.SESSION_KEY])

    def test_coming_back_without_starting_does_nothing(self):
        r = self.client.get(reverse("crm:mail_connected"))
        self.assertRedirects(r, reverse("crm:mail_settings"), fetch_redirect_response=False)
        self.assertFalse(MailConnection.objects.exists())

    def _come_back(self, scopes):
        session = self.client.session
        session[connect.SESSION_KEY] = True
        session.save()
        with mock.patch.object(connect, "requests", FakeGoogle(scopes)):
            return self.client.get(reverse("crm:mail_connected"), follow=True)

    def test_mail_goes_on_when_google_granted_gmail(self):
        google_token(self.user)
        r = self._come_back(["openid", connect.GMAIL_SCOPE])
        self.assertContains(r, "Mail is on")
        conn = MailConnection.objects.get(user=self.user)
        self.assertTrue(conn.enabled)
        self.assertEqual(conn.google_email, "kmoczulski@acmigroup.com")
        self.assertTrue(connect.is_on(self.user))

    def test_gmail_box_unticked_leaves_mail_off(self):
        google_token(self.user)
        r = self._come_back(["https://www.googleapis.com/auth/calendar.readonly"])
        self.assertContains(r, "leave the Gmail box")
        self.assertFalse(connect.is_on(self.user))

    def test_someone_elses_google_account_is_refused(self):
        google_token(self.user, email="sshewmaker@acmigroup.com")
        with self.assertRaisesMessage(connect.MailConnectError, "sshewmaker@acmigroup.com"):
            connect.finish(self.user, FakeGoogle([connect.GMAIL_SCOPE]))
        self.assertFalse(connect.is_on(self.user))

    def test_turn_off_revokes_at_google_and_deletes_the_token(self):
        google_token(self.user)
        MailConnection.objects.create(user=self.user, enabled=True)
        mb = Mailbox.objects.create(user=self.user, email=self.user.email, backend=Mailbox.Backend.GMAIL_API,
                                    history_id="123")
        fake = FakeGoogle()
        with mock.patch.object(connect, "requests", fake):
            r = self.client.post(reverse("crm:mail_turn_off"), follow=True)
        self.assertContains(r, "calendar was disconnected too")
        self.assertIn(("post", connect.REVOKE_URL, {"token": "refresh-1"}), fake.calls)
        self.assertFalse(SocialToken.objects.filter(account__user=self.user).exists())
        self.assertTrue(SocialAccount.objects.filter(user=self.user).exists())  # Google sign-in still works
        self.assertFalse(connect.is_on(self.user))
        mb.refresh_from_db()
        self.assertEqual((mb.backend, mb.history_id), (Mailbox.Backend.IMAP, ""))

    def test_turn_off_still_deletes_the_token_when_google_is_unreachable(self):
        import requests

        google_token(self.user)
        MailConnection.objects.create(user=self.user, enabled=True)
        fake = mock.Mock(post=mock.Mock(side_effect=requests.ConnectionError("down")))
        connect.turn_off(self.user, fake)
        self.assertFalse(SocialToken.objects.filter(account__user=self.user).exists())

    def test_switching_an_account_off_turns_its_mail_off(self):
        google_token(self.user)
        MailConnection.objects.create(user=self.user, enabled=True)
        with mock.patch.object(connect, "revoke") as revoke:
            self.user.is_active = False
            self.user.save()
        revoke.assert_called_once()
        self.assertFalse(MailConnection.objects.get(user=self.user).enabled)
        self.assertFalse(SocialToken.objects.filter(account__user=self.user).exists())


@override_settings(**ON)
class PartnersNeverReachMailTests(TestCase):
    def setUp(self):
        self.partner = make_partner(User.objects.create_user("fortress", password="x", email="lp@acmigroup.com"))

    def test_middleware_sends_a_partner_back_to_the_room(self):
        self.client.force_login(self.partner)
        for name in ("mail_settings", "mail_connected"):
            r = self.client.get(reverse(f"crm:{name}"))
            self.assertRedirects(r, "/room/", fetch_redirect_response=False)
        r = self.client.post(reverse("crm:mail_turn_on"))
        self.assertRedirects(r, "/room/", fetch_redirect_response=False)

    def test_the_mail_views_refuse_a_partner_themselves(self):
        from django.http import Http404

        rf = RequestFactory()
        for view, method in ((mail_views.mail_settings, "get"), (mail_views.mail_turn_on, "post"),
                             (mail_views.mail_connected, "get"), (mail_views.mail_turn_off, "post")):
            request = getattr(rf, method)("/mail-setup/")
            request.user = self.partner
            with self.assertRaises(Http404):
                view(request)

    def test_a_partner_can_never_turn_mail_on(self):
        self.assertIn("isn't available", connect.unavailable_reason(self.partner))
        MailConnection.objects.create(user=self.partner, enabled=True)
        self.assertFalse(connect.is_on(self.partner))
        google_token(self.partner)
        with self.assertRaises(connect.MailConnectError):
            connect.finish(self.partner, FakeGoogle([connect.GMAIL_SCOPE]))

    def test_moving_someone_into_partners_turns_their_mail_off(self):
        user = User.objects.create_user("simon", password="x", email="sshewmaker@acmigroup.com")
        google_token(user)
        MailConnection.objects.create(user=user, enabled=True)
        with mock.patch.object(connect, "revoke"):
            make_partner(user)
        self.assertFalse(MailConnection.objects.get(user=user).enabled)
        self.assertFalse(SocialToken.objects.filter(account__user=user).exists())

    def test_the_mail_sync_skips_a_partners_mailbox(self):
        Mailbox.objects.create(user=self.partner, email=self.partner.email)
        with mock.patch.object(sync, "sync_mailbox") as sync_mailbox:
            self.assertEqual(sync.sync_all(), {})
        sync_mailbox.assert_not_called()


class ConnectAdapterTests(TestCase):
    """Connecting Google attaches it to the signed-in account: never a partner's, never another address."""

    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", email="kmoczulski@acmigroup.com")

    def _connect(self, user, google_email):
        request = RequestFactory().get("/accounts/google/login/callback/")
        request.user = user
        sociallogin = SimpleNamespace(
            user=SimpleNamespace(email=google_email), email_addresses=[SimpleNamespace(verified=True)],
            account=SimpleNamespace(extra_data={"email_verified": True}), state={"process": "connect"},
        )
        SocialAccountAdapter().pre_social_login(request, sociallogin)

    def test_own_account_connects(self):
        self._connect(self.user, "KMoczulski@acmigroup.com")  # no exception

    def test_another_acmi_address_is_refused(self):
        with self.assertRaises(ImmediateHttpResponse):
            self._connect(self.user, "sshewmaker@acmigroup.com")

    def test_a_partner_cannot_connect_google(self):
        make_partner(self.user)
        with self.assertRaises(ImmediateHttpResponse):
            self._connect(self.user, "kmoczulski@acmigroup.com")


@override_settings(**ON)
class PartnersGroupFromTheOtherSideTests(TestCase):
    def test_adding_people_to_the_partners_group_turns_their_mail_off(self):
        user = User.objects.create_user("simon", password="x", email="sshewmaker@acmigroup.com")
        MailConnection.objects.create(user=user, enabled=True)
        with mock.patch.object(connect, "revoke"):
            Group.objects.get_or_create(name=PARTNERS)[0].user_set.add(user)
        self.assertFalse(MailConnection.objects.get(user=user).enabled)

    def test_other_groups_leave_mail_alone(self):
        user = User.objects.create_user("simon", password="x", email="sshewmaker@acmigroup.com")
        MailConnection.objects.create(user=user, enabled=True)
        Group.objects.get_or_create(name="Managers")[0].user_set.add(user)
        user.groups.add(Group.objects.get_or_create(name="Other")[0])
        self.assertTrue(MailConnection.objects.get(user=user).enabled)
