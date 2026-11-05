"""Undo send: Send queues the message, a notification counts down, nothing blocks the page (crm/mail/outbox.py)."""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import gmail_api, outbox
from crm.models import Mailbox, Notification, PendingSend

from .test_compose import SEND, WILL
from .test_gmail_api import ME, FakeGmail, api_message
from .test_mail_actions import connected_user


@override_settings(**SEND)
class OutboxTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API, backfill_done=True)
        now = timezone.now()
        self.fake = FakeGmail([api_message("0a01", "a1", WILL, ME, "PDA", body="Can you send it?", when=now - timedelta(hours=2),
                                           msg_id="<pda-1@fh>")])
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse("crm:inbox_compose")

    def queue(self, **extra):
        data = {"to": "will@firehawkaerospace.com", "subject": "Draft PDA", "body": "Attached.", "queue": "1", "inline": "1",
                "mode": "new", **extra}
        return self.client.post(self.url, data)

    def make_due(self, pending):
        PendingSend.objects.filter(pk=pending.pk).update(send_at=timezone.now() - timedelta(seconds=1))

    def test_send_queues_the_message_and_sends_nothing_yet(self):
        r = self.queue()
        item = r.json()["queued"]
        self.assertTrue(r.json()["ok"])
        self.assertEqual((item["status"], item["subject"], item["to"]), ("queued", "Draft PDA", "will@firehawkaerospace.com"))
        self.assertGreater(item["seconds_left"], 5)
        self.assertEqual(self.fake.sent, [])  # the page is free; Gmail has been told nothing
        self.assertEqual(PendingSend.objects.get().status, "queued")
        self.assertEqual([i["id"] for i in self.client.get(reverse("crm:outbox_pending")).json()["items"]], [item["id"]])

    def test_without_the_queue_flag_the_old_direct_send_still_works(self):
        self.client.post(self.url, {"to": "will@firehawkaerospace.com", "subject": "Hi", "body": "x", "mode": "new", "inline": "1"})
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(PendingSend.objects.count(), 0)

    def test_bad_addresses_still_answer_at_once(self):
        r = self.queue(to="not an address")
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't an email address", r.json()["error"])
        self.assertEqual(PendingSend.objects.count(), 0)

    def test_undo_within_the_window_cancels_and_drops_the_message(self):
        pid = self.queue().json()["queued"]["id"]
        r = self.client.post(reverse("crm:outbox_undo", args=[pid]))
        self.assertTrue(r.json()["ok"])
        p = PendingSend.objects.get()
        self.assertEqual((p.status, bytes(p.raw)), ("cancelled", b""))
        self.make_due(p)
        self.assertEqual(outbox.flush_due(), 0)  # an undone message is never sent
        self.assertEqual(self.fake.sent, [])

    def test_undo_after_the_time_is_too_late(self):
        pid = self.queue().json()["queued"]["id"]
        self.make_due(PendingSend.objects.get())
        r = self.client.post(reverse("crm:outbox_undo", args=[pid]))
        self.assertFalse(r.json()["ok"])
        self.assertEqual(PendingSend.objects.get().status, "queued")

    def test_the_notifications_timer_sends_it_when_due_and_not_before(self):
        pid = self.queue().json()["queued"]["id"]
        early = self.client.post(reverse("crm:outbox_commit", args=[pid])).json()["item"]
        self.assertEqual(early["status"], "queued")  # not due: nothing sent
        self.assertEqual(self.fake.sent, [])
        self.make_due(PendingSend.objects.get())
        item = self.client.post(reverse("crm:outbox_commit", args=[pid])).json()["item"]
        self.assertEqual(item["status"], "sent")
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(bytes(PendingSend.objects.get().raw), b"")  # the finished message is dropped once sent
        again = self.client.post(reverse("crm:outbox_commit", args=[pid])).json()["item"]
        self.assertEqual(again["status"], "sent")
        self.assertEqual(len(self.fake.sent), 1)  # never twice

    def test_a_page_that_closed_does_not_lose_it(self):
        """The alerts poll (any open page) and the sync both send what is due."""
        self.queue()
        self.make_due(PendingSend.objects.get())
        self.client.get(reverse("crm:notification_poll"))
        self.assertEqual(len(self.fake.sent), 1)
        self.queue(subject="Second")
        self.make_due(PendingSend.objects.get(subject="Second"))
        self.assertEqual(outbox.flush_due(), 1)  # what the 15-minute sync does for everyone
        self.assertEqual(len(self.fake.sent), 2)

    def test_two_requests_racing_still_send_once(self):
        pid = self.queue().json()["queued"]["id"]
        p = PendingSend.objects.get()
        self.make_due(p)
        PendingSend.objects.filter(pk=pid).update(status="sending")  # another request already took it
        self.assertEqual(outbox.deliver(pid)["status"], "sending")
        self.assertEqual(self.fake.sent, [])

    def test_only_the_sender_can_see_undo_or_send_their_message(self):
        pid = self.queue().json()["queued"]["id"]
        other = connected_user("simon", "simon@acmigroup.com")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("crm:outbox_undo", args=[pid])).status_code, 404)
        self.assertEqual(self.client.post(reverse("crm:outbox_commit", args=[pid])).status_code, 404)
        self.assertEqual(self.client.get(reverse("crm:outbox_pending")).json()["items"], [])

    def test_a_failed_send_keeps_the_message_and_tells_the_person(self):
        pid = self.queue().json()["queued"]["id"]
        p = PendingSend.objects.get()
        self.make_due(p)
        with mock.patch("crm.mail.outbox.compose.send", side_effect=outbox.MailboxError("Gmail refused it.")):
            item = self.client.post(reverse("crm:outbox_commit", args=[pid])).json()["item"]
        self.assertEqual((item["status"], item["error"]), ("failed", "Gmail refused it."))
        self.assertTrue(Notification.objects.filter(user=self.user, text__contains="Couldn't send").exists())
        self.assertGreater(len(bytes(PendingSend.objects.get().raw)), 0)  # nothing lost
        page = self.client.get(self.url, {"undo": pid})
        self.assertContains(page, "This wasn&#x27;t sent: Gmail refused it.")
        self.assertContains(page, "Draft PDA")

    def test_undo_puts_the_message_back_in_the_composer(self):
        pid = self.queue(subject="Keep this", body="Hello Will", cc="pat@shoreunitedbank.com").json()["queued"]["id"]
        self.client.post(reverse("crm:outbox_undo", args=[pid]))
        page = self.client.get(self.url, {"undo": pid})
        self.assertContains(page, "Keep this")
        self.assertContains(page, "Hello Will")
        self.assertContains(page, "pat@shoreunitedbank.com")
        self.assertEqual(self.client.get(self.url, {"undo": 99999}).status_code, 200)  # unknown: a blank composer, no error

    def test_a_sent_reply_lets_followups_tick_and_sends_with_its_thread(self):
        self.queue(to="will@firehawkaerospace.com", subject="Re: PDA", mode="reply", thread_id="a1", reply_to_id="0a01",
                   back="/inbox/?label=INBOX&t=a1")
        p = PendingSend.objects.get()
        self.assertEqual(p.thread_id, "a1")
        self.make_due(p)
        item = outbox.deliver(p.pk)
        self.assertEqual(item["status"], "sent")
        self.assertEqual(item["url"], "/inbox/?label=INBOX&t=a1")  # back to the thread, now with the reply in it

    def test_the_site_switch_blocks_queueing_too(self):
        with override_settings(MAIL_SEND_ENABLED=False):
            r = self.queue()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(PendingSend.objects.count(), 0)

    def test_the_page_carries_the_notification_script(self):
        page = self.client.get(reverse("crm:inbox"))
        self.assertContains(page, "outbox-config")
        self.assertContains(page, "crm/outbox.js")
