"""Gmail push: watches on each mailbox, and the Pub/Sub endpoint that runs a one-mailbox sync the moment Gmail says
something changed."""
import base64
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import gmail_api, push, sync
from crm.models import Mailbox

User = get_user_model()
TOPIC = "projects/p/topics/gmail"
SA = "pusher@p.iam.gserviceaccount.com"
PUSH = dict(GMAIL_PUSH_TOPIC=TOPIC, GMAIL_PUSH_SA=SA, GMAIL_PUSH_AUDIENCE="https://hub.example/internal/gmail-push/")


def pubsub_body(address, history_id="123"):
    data = base64.b64encode(json.dumps({"emailAddress": address, "historyId": history_id}).encode()).decode()
    return json.dumps({"message": {"data": data, "messageId": "1"}, "subscription": "projects/p/subscriptions/s"})


class FakeWatchClient:
    def __init__(self, expiration_ms="1893456000000", fail=None):
        self.expiration_ms, self.fail, self.topics = expiration_ms, fail, []

    def watch(self, topic):
        if self.fail:
            raise self.fail
        self.topics.append(topic)
        return {"historyId": "9", "expiration": self.expiration_ms}


def mailbox(email="a@acmigroup.com", backend=Mailbox.Backend.GMAIL_API, expires=None, enabled=True):
    user = User.objects.create_user(email.split("@")[0], email, "pw")
    return Mailbox.objects.create(user=user, email=email, backend=backend, enabled=enabled, watch_expires_at=expires)


class ClientTests(TestCase):
    def test_watch_posts_the_topic_and_the_labels(self):
        client = gmail_api.GmailClient.__new__(gmail_api.GmailClient)
        client.post = mock.Mock(return_value={"historyId": "1", "expiration": "2"})
        self.assertEqual(client.watch(TOPIC)["expiration"], "2")
        path, body = client.post.call_args.args
        self.assertEqual(path, "/watch")
        self.assertEqual(body["topicName"], TOPIC)
        self.assertEqual(body["labelIds"], ["INBOX", "SENT"])


class WatchTests(TestCase):
    @override_settings(**PUSH)
    def test_start_watch_saves_when_it_runs_out(self):
        mb = mailbox()
        client = FakeWatchClient("1893456000000")
        expires = push.start_watch(mb, client=client)
        mb.refresh_from_db()
        self.assertEqual(client.topics, [TOPIC])
        self.assertEqual(mb.watch_expires_at, expires)
        self.assertEqual(expires.year, 2030)

    @override_settings(GMAIL_PUSH_TOPIC="")
    def test_nothing_happens_without_a_topic(self):
        mb = mailbox()
        self.assertIsNone(push.start_watch(mb, client=FakeWatchClient()))
        self.assertEqual(push.renew_watches(), {})

    @override_settings(**PUSH)
    def test_imap_mailboxes_get_no_watch(self):
        mb = mailbox(backend=Mailbox.Backend.IMAP)
        self.assertIsNone(push.start_watch(mb, client=FakeWatchClient()))

    @override_settings(**PUSH)
    def test_renew_picks_only_missing_or_expiring_watches(self):
        soon = mailbox("soon@acmigroup.com", expires=timezone.now() + timedelta(hours=10))
        none = mailbox("none@acmigroup.com")
        fine = mailbox("fine@acmigroup.com", expires=timezone.now() + timedelta(days=6))
        off = mailbox("off@acmigroup.com", enabled=False)
        client = FakeWatchClient()
        with mock.patch("crm.mail.gmail_api.client_for", return_value=client):
            out = push.renew_watches()
        self.assertEqual(set(out), {str(soon), str(none)})
        for box in (soon, none, fine, off):
            box.refresh_from_db()
        self.assertEqual(soon.watch_expires_at.year, 2030)
        self.assertEqual(none.watch_expires_at.year, 2030)
        self.assertLess(fine.watch_expires_at, timezone.now() + timedelta(days=7))
        self.assertIsNone(off.watch_expires_at)

    @override_settings(**PUSH)
    def test_one_failing_mailbox_does_not_stop_the_others(self):
        bad = mailbox("bad@acmigroup.com")
        good = mailbox("good@acmigroup.com")
        clients = {bad.pk: FakeWatchClient(fail=gmail_api.MailboxError("Google refused")), good.pk: FakeWatchClient()}
        with mock.patch("crm.mail.gmail_api.client_for", side_effect=lambda mb, session=None: clients[mb.pk]):
            out = push.renew_watches()
        good.refresh_from_db()
        self.assertIn("not started", out[str(bad)])
        self.assertIsNotNone(good.watch_expires_at)

    @override_settings(**PUSH)
    def test_the_sync_renews_watches(self):
        with mock.patch.object(push, "renew_watches", return_value={"a@acmigroup.com": None}) as renew:
            results = sync.sync_all()
        renew.assert_called_once()
        self.assertIn("push watch", results["a@acmigroup.com"])


class VerifyTests(TestCase):
    def request(self, token="tok"):
        return RequestFactory().post("/internal/gmail-push/", HTTP_AUTHORIZATION=f"Bearer {token}" if token else "")

    def claims(self, **extra):
        return {"email": SA, "email_verified": True, **extra}

    @override_settings(**PUSH)
    def test_the_subscriptions_service_account_passes(self):
        with mock.patch("crm.mail.push._verify_token", return_value=self.claims()) as check:
            self.assertTrue(push.verified(self.request()))
        self.assertEqual(check.call_args.args, ("tok", PUSH["GMAIL_PUSH_AUDIENCE"]))

    @override_settings(**PUSH)
    def test_another_account_or_a_bad_token_fails(self):
        with mock.patch("crm.mail.push._verify_token", return_value=self.claims(email="x@evil.com")):
            self.assertFalse(push.verified(self.request()))
        with mock.patch("crm.mail.push._verify_token", return_value=self.claims(email_verified=False)):
            self.assertFalse(push.verified(self.request()))
        with mock.patch("crm.mail.push._verify_token", side_effect=ValueError("bad")):
            self.assertFalse(push.verified(self.request()))

    @override_settings(**PUSH)
    def test_no_bearer_token_fails_without_asking_google(self):
        with mock.patch("crm.mail.push._verify_token") as check:
            self.assertFalse(push.verified(self.request(token="")))
        check.assert_not_called()

    @override_settings(GMAIL_PUSH_SA="", GMAIL_PUSH_AUDIENCE="")
    def test_unconfigured_push_accepts_nothing(self):
        with mock.patch("crm.mail.push._verify_token", return_value=self.claims()):
            self.assertFalse(push.verified(self.request()))


class EndpointTests(TestCase):
    url = "/internal/gmail-push/"

    def post(self, body):
        return self.client.post(self.url, data=body, content_type="application/json", HTTP_AUTHORIZATION="Bearer t")

    def test_a_request_not_from_the_subscription_is_refused(self):
        with mock.patch.object(push, "verified", return_value=False), \
                mock.patch.object(push, "sync_for") as run:
            r = self.post(pubsub_body("a@acmigroup.com"))
        self.assertEqual(r.status_code, 403)
        run.assert_not_called()

    def test_it_is_reachable_without_signing_in(self):
        self.assertEqual(reverse("crm:gmail_push"), self.url)
        with mock.patch.object(push, "verified", return_value=False):
            self.assertEqual(self.post("{}").status_code, 403)  # the auth check answered, not a login redirect

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_a_notification_syncs_that_mailbox(self):
        mb = mailbox()
        with mock.patch.object(push, "verified", return_value=True), \
                mock.patch.object(sync, "sync_mailbox", return_value={}) as run:
            r = self.post(pubsub_body("A@acmigroup.com"))
        self.assertEqual(r.status_code, 204)
        self.assertEqual(run.call_args.args[0], mb)

    def test_an_unknown_address_or_a_junk_body_is_acknowledged_and_ignored(self):
        mailbox()
        with mock.patch.object(push, "verified", return_value=True), \
                mock.patch.object(sync, "sync_mailbox") as run:
            self.assertEqual(self.post(pubsub_body("someone@else.com")).status_code, 204)
            self.assertEqual(self.post("not json").status_code, 204)
            self.assertEqual(self.post(json.dumps({"message": {"data": "!!"}})).status_code, 204)
        run.assert_not_called()

    def test_an_imap_or_disabled_mailbox_is_not_synced(self):
        mailbox("imap@acmigroup.com", backend=Mailbox.Backend.IMAP)
        mailbox("off@acmigroup.com", enabled=False)
        with mock.patch.object(push, "verified", return_value=True), \
                mock.patch.object(sync, "sync_mailbox") as run:
            self.post(pubsub_body("imap@acmigroup.com"))
            self.post(pubsub_body("off@acmigroup.com"))
        run.assert_not_called()

    def test_a_sync_already_running_is_not_an_error(self):
        mailbox()
        with mock.patch.object(push, "verified", return_value=True), \
                mock.patch.object(sync, "sync_mailbox", side_effect=sync.SyncBusy("busy")):
            self.assertEqual(self.post(pubsub_body("a@acmigroup.com")).status_code, 204)


class ParseTests(TestCase):
    def test_the_address_comes_out_lower_case(self):
        self.assertEqual(push.notified_address(pubsub_body("Boss@AcmiGroup.com")), "boss@acmigroup.com")

    def test_junk_gives_nothing(self):
        for body in ("", "[]", "{}", '{"message": {}}', '{"message": {"data": 5}}'):
            self.assertEqual(push.notified_address(body), "")
