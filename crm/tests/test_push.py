"""Browser notifications (Web Push): per-person choices, subscriptions, sending from notify.send, partners left out."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from crm import notify, push
from crm.management.commands.vapid_keys import make_keys
from crm.models import Notification, PushSubscription, Task
from crm.permissions import PARTNERS

User = get_user_model()
PUBLIC, PRIVATE = make_keys()
KEYS = {"VAPID_PUBLIC_KEY": PUBLIC, "VAPID_PRIVATE_KEY": PRIVATE}
SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123", "keys": {"p256dh": "BKey", "auth": "authsecret"}}
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


class PushTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.other = User.objects.create_user("simon", password="x", first_name="Simon")
        self.client.force_login(self.user)

    def subscribe(self, data=SUB):
        return self.client.post(reverse("crm:push_subscribe"), json.dumps(data), content_type="application/json",
                                HTTP_USER_AGENT=CHROME)


class ChoiceTests(PushTestCase):
    def test_everything_is_on_until_switched_off(self):
        self.assertTrue(all(c["hub"] and c["push"] for c in push.choices(self.user).values()))
        self.assertTrue(push.wants(self.user, Notification.Kind.MAIL, "push"))

    def test_settings_page_saves_each_switch(self):
        r = self.client.get(reverse("crm:notification_settings"))
        self.assertContains(r, "Turn on browser notifications")
        self.assertContains(r, 'name="mail:push"')
        posted = {f"{k}:{c}": "on" for k, _ in push.KINDS for c in push.CHANNELS}
        del posted["stage:push"], posted["comment:hub"]
        self.client.post(reverse("crm:notification_settings"), posted)
        self.assertFalse(push.wants(self.user, "stage", "push"))
        self.assertTrue(push.wants(self.user, "stage", "hub"))
        self.assertFalse(push.wants(self.user, "comment", "hub"))
        self.assertTrue(push.wants(self.other, "stage", "push"))  # only mine changed

    def test_hub_off_keeps_it_out_of_the_bell_but_still_keyed(self):
        push.save_choices(self.user, {"due:push": "on"})
        n = notify.send(self.user, Notification.Kind.DUE, "Due today: x", "/tasks/1/", key="due:1")
        self.assertFalse(n.in_hub)
        self.assertEqual(notify.unread_count(self.user), 0)
        self.assertIsNone(notify.send(self.user, Notification.Kind.DUE, "Due today: x", "/tasks/1/", key="due:1"))
        r = self.client.get(reverse("crm:notification_list") + "?show=all")
        self.assertNotContains(r, "Due today: x")

    def test_both_off_sends_nothing(self):
        push.save_choices(self.user, {})
        self.assertIsNone(notify.send(self.user, Notification.Kind.STAGE, "moved", actor=self.other))
        self.assertFalse(Notification.objects.exists())


class SubscribeTests(PushTestCase):
    def test_subscribe_saves_device_and_moves_a_shared_browser(self):
        r = self.subscribe()
        self.assertEqual(r.json()["device"], "Chrome on Windows")
        self.client.force_login(self.other)
        self.subscribe()
        sub = PushSubscription.objects.get()
        self.assertEqual(sub.user, self.other)

    def test_bad_subscription_refused(self):
        self.assertEqual(self.subscribe({"endpoint": "http://insecure/x", "keys": SUB["keys"]}).status_code, 400)
        self.assertEqual(self.subscribe({"endpoint": SUB["endpoint"]}).status_code, 400)
        self.assertFalse(PushSubscription.objects.exists())

    def test_unsubscribe_and_remove_only_touch_my_own(self):
        self.subscribe()
        mine = PushSubscription.objects.get()
        self.client.force_login(self.other)
        self.client.post(reverse("crm:push_unsubscribe"), json.dumps({"endpoint": SUB["endpoint"]}),
                         content_type="application/json")
        self.assertEqual(self.client.post(reverse("crm:push_device_remove", args=[mine.pk])).status_code, 404)
        self.assertTrue(PushSubscription.objects.exists())
        self.client.force_login(self.user)
        self.client.post(reverse("crm:push_unsubscribe"), json.dumps({"endpoint": SUB["endpoint"]}),
                         content_type="application/json")
        self.assertFalse(PushSubscription.objects.exists())

    def test_service_worker_served_at_root_without_sign_in(self):
        self.client.logout()
        r = self.client.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/javascript")
        self.assertIn("notificationclick", r.content.decode())
        self.assertIn("openWindow", r.content.decode())


class PartnerTests(PushTestCase):
    def setUp(self):
        super().setUp()
        self.partner = User.objects.create_user("pat@fortress.com", password="x")
        self.partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])

    def test_partner_cannot_subscribe_or_reach_settings_or_service_worker(self):
        self.client.force_login(self.partner)
        self.assertRedirects(self.subscribe(), "/room/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("crm:notification_settings")), "/room/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/sw.js").status_code, 302)
        self.assertIsNone(push.subscribe(self.partner, SUB))
        self.assertFalse(PushSubscription.objects.exists())

    @override_settings(**KEYS)
    def test_partner_with_a_stray_subscription_gets_no_push(self):
        PushSubscription.objects.create(user=self.partner, endpoint=SUB["endpoint"], p256dh="k", auth="a")
        with mock.patch("pywebpush.webpush") as wp:
            self.assertEqual(push.deliver(self.partner, {"title": "x"}), 0)
        wp.assert_not_called()


@override_settings(**KEYS)
class SendTests(PushTestCase):
    def setUp(self):
        super().setUp()
        self.subscribe()

    def test_notify_send_pushes_after_commit_with_a_link_that_opens_the_record(self):
        task = Task.objects.create(title="Call Fortress", assignee=self.user)
        with mock.patch("pywebpush.webpush") as wp, self.captureOnCommitCallbacks(execute=True):
            notify.task_assigned(task, self.other)
        wp.assert_called_once()
        kwargs = wp.call_args.kwargs
        data = json.loads(kwargs["data"])
        n = Notification.objects.get()
        self.assertEqual(data["title"], "Task assigned")
        self.assertIn("Call Fortress", data["body"])
        self.assertEqual(data["url"], reverse("crm:notification_open", args=[n.pk]))
        self.assertEqual(kwargs["subscription_info"]["endpoint"], SUB["endpoint"])
        self.assertEqual(kwargs["vapid_private_key"], PRIVATE)
        self.assertIsNotNone(PushSubscription.objects.get().last_sent_at)
        r = self.client.get(data["url"])  # clicking it opens the task and marks it read
        self.assertRedirects(r, task.get_absolute_url(), fetch_redirect_response=False)

    def test_push_off_for_a_kind_sends_none(self):
        push.save_choices(self.user, {"assigned:hub": "on"})
        task = Task.objects.create(title="x", assignee=self.user)
        with mock.patch("pywebpush.webpush") as wp, self.captureOnCommitCallbacks(execute=True):
            notify.task_assigned(task, self.other)
        wp.assert_not_called()
        self.assertTrue(Notification.objects.get().in_hub)

    def test_hub_off_push_links_straight_to_the_record(self):
        push.save_choices(self.user, {"stage:push": "on"})
        n = notify.send(self.user, Notification.Kind.STAGE, "moved", "/opportunities/5/", self.other)
        self.assertEqual(push.payload(n)["url"], "/opportunities/5/")

    def test_gone_subscription_is_removed(self):
        from pywebpush import WebPushException

        gone = WebPushException("gone", response=mock.Mock(status_code=410))
        with mock.patch("pywebpush.webpush", side_effect=gone):
            self.assertEqual(push.deliver(self.user, {"title": "x"}), 0)
        self.assertFalse(PushSubscription.objects.exists())

    def test_other_failure_keeps_the_subscription_and_never_raises(self):
        with mock.patch("pywebpush.webpush", side_effect=OSError("network down")):
            self.assertEqual(push.deliver(self.user, {"title": "x"}), 0)
        self.assertTrue(PushSubscription.objects.exists())

    def test_test_button(self):
        with mock.patch("pywebpush.webpush") as wp:
            r = self.client.post(reverse("crm:push_test"))
        self.assertEqual(r.json()["sent"], 1)
        self.assertEqual(json.loads(wp.call_args.kwargs["data"])["title"], "Test notification")

    def test_new_mail_hook(self):
        with mock.patch("pywebpush.webpush") as wp, self.captureOnCommitCallbacks(execute=True):
            notify.new_mail(self.user, "Jane at Fortress", "LOI draft", "/mail/thread/9/", "msg-1")
            notify.new_mail(self.user, "Jane at Fortress", "LOI draft", "/mail/thread/9/", "msg-1")
            notify.new_mail(self.user, "Jane at Fortress", "Re: LOI draft", "/mail/thread/9/", "msg-2")
        self.assertEqual(Notification.objects.filter(kind=Notification.Kind.MAIL).count(), 2)
        self.assertEqual(wp.call_count, 2)


class NoKeysTests(PushTestCase):
    @override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
    def test_without_keys_nothing_is_pushed_and_page_says_so(self):
        self.subscribe()
        with mock.patch("pywebpush.webpush") as wp, self.captureOnCommitCallbacks(execute=True):
            notify.send(self.user, Notification.Kind.STAGE, "moved", actor=self.other)
        wp.assert_not_called()
        r = self.client.get(reverse("crm:notification_settings"))
        self.assertContains(r, 'data-ready="0"')


class SweepTests(PushTestCase):
    def test_sweep_reminds_the_team_once(self):
        from django.utils import timezone

        Task.objects.create(title="Send LOI", assignee=self.other, due_date=timezone.localdate())
        call_command("notify_sweep")
        call_command("notify_sweep")
        self.assertEqual(Notification.objects.filter(user=self.other, kind="due").count(), 1)

    def test_vapid_keys_command_prints_env_lines(self):
        from io import StringIO

        out = StringIO()
        call_command("vapid_keys", stdout=out)
        self.assertIn("ACMI_VAPID_PUBLIC_KEY=", out.getvalue())
        self.assertIn("ACMI_VAPID_PRIVATE_KEY=", out.getvalue())
