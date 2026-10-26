"""Mail in the hub, step 4: archive, read / unread, star, labels, move, Trash and snooze, with Undo."""
from datetime import datetime, timedelta
from unittest import mock

from allauth.socialaccount.models import SocialAccount, SocialToken
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import actions, gmail_api, sync
from crm.models import MailConnection, MailLabel, MailSnooze, MailThread, Mailbox
from crm.permissions import PARTNERS

from .test_gmail_api import ME, ON, FakeGmail, api_message

User = get_user_model()
WILL = "Will Edwards <will@firehawkaerospace.com>"


def connected_user(username="kellen", email=ME):
    user = User.objects.create_user(username, password="x", email=email)
    account = SocialAccount.objects.create(user=user, provider="google", uid=username, extra_data={"email": email})
    SocialToken.objects.create(account=account, token="a", token_secret="r",
                               expires_at=timezone.now() + timedelta(hours=1))
    MailConnection.objects.create(user=user, enabled=True, google_email=email)
    return user


@override_settings(**ON)
class MailActionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        now = timezone.now()
        self.fake = FakeGmail([
            api_message("0a01", "a1", WILL, ME, "PDA", when=now - timedelta(hours=3), labels=("INBOX", "UNREAD")),
            api_message("0b01", "b2", WILL, ME, "Site tour", when=now - timedelta(hours=2)),
            api_message("0c01", "c3", WILL, ME, "Newsletter", when=now - timedelta(hours=1)),
        ])
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.get(reverse("crm:inbox"))  # index the inbox and the labels

    def labels(self, tid):
        return {lb for m in self.fake.messages.values() if m["threadId"] == tid for lb in m["labelIds"]}

    def act(self, action, *threads, follow=False, **extra):
        data = {"action": action, "t": list(threads), "next": extra.pop("next", "/inbox/?label=INBOX"), **extra}
        return self.client.post(reverse("crm:inbox_act"), data, follow=follow)

    def test_archive_from_the_reading_pane_closes_it_and_offers_undo(self):
        r = self.act("archive", "b2", next="/inbox/?label=INBOX&t=b2")
        self.assertRedirects(r, "/inbox/?label=INBOX", fetch_redirect_response=False)
        self.assertNotIn("INBOX", self.labels("b2"))
        self.assertFalse(MailThread.objects.get(gmail_id="b2").in_inbox)
        page = self.client.get(r["Location"])
        self.assertContains(page, "Archived.")
        self.assertContains(page, 'name="undo" value="1"')
        self.assertNotIn("b2", [t.gmail_id for t in page.context["threads"]])
        self.assertIsNone(self.client.session.get("mail_undo"))  # shown once
        r = self.act("inbox", "b2", follow=True, undo="1")
        self.assertContains(r, "Undone.")
        self.assertIn("INBOX", self.labels("b2"))

    def test_several_at_once(self):
        self.act("unread", "b2", "c3")
        self.assertIn("UNREAD", self.labels("b2"))
        self.assertIn("UNREAD", self.labels("c3"))
        r = self.client.get(reverse("crm:inbox"))
        self.assertContains(r, "Marked unread 2 conversations.")

    def test_star_and_unstar(self):
        self.act("star", "c3")
        self.assertIn("STARRED", self.labels("c3"))
        self.assertTrue(MailThread.objects.get(gmail_id="c3").starred)
        self.act("unstar", "c3")
        self.assertNotIn("STARRED", self.labels("c3"))

    def test_labels_and_move_only_with_your_own_labels(self):
        self.act("label", "b2", label="Label_1")
        self.assertIn("Label_1", self.labels("b2"))
        self.assertIn("INBOX", self.labels("b2"))
        r = self.act("label", "b2", label="SPAM", follow=True)
        self.assertContains(r, "Pick one of your labels.")
        self.assertNotIn("SPAM", self.labels("b2"))
        self.act("move", "c3", label="Label_1")
        self.assertEqual(self.labels("c3") & {"INBOX", "Label_1"}, {"Label_1"})

    def test_trash_and_restore_never_delete(self):
        self.act("trash", "c3")
        self.assertIn("TRASH", self.labels("c3"))
        self.assertEqual(self.fake.posts[-1][0], "/threads/c3/trash")
        self.act("untrash", "c3")
        self.assertNotIn("TRASH", self.labels("c3"))
        self.assertFalse(any("delete" in path for path, _ in self.fake.posts))

    def test_snooze_takes_it_out_and_the_sync_brings_it_back_unread(self):
        r = self.act("snooze", "b2", when="tomorrow", follow=True)
        self.assertContains(r, "Snoozed until")
        snooze_label = MailLabel.objects.get(mailbox=self.mailbox, name=actions.SNOOZE_LABEL).gmail_id
        self.assertEqual(self.labels("b2") & {"INBOX", snooze_label}, {snooze_label})
        snooze = MailSnooze.objects.get(gmail_id="b2")
        self.assertEqual(timezone.localtime(snooze.until).hour, 8)
        self.act("snooze", "c3", when="tomorrow")
        self.assertEqual(sum(1 for p, _ in self.fake.posts if p == "/labels"), 1)  # the label is made once
        self.assertEqual(actions.wake_due(self.mailbox, gmail_api.GmailClient(self.user, self.fake)), 0)
        MailSnooze.objects.filter(gmail_id="b2").update(until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(actions.wake_due(self.mailbox, gmail_api.GmailClient(self.user, self.fake)), 1)
        self.assertTrue({"INBOX", "UNREAD"} <= self.labels("b2"))
        self.assertNotIn(snooze_label, self.labels("b2"))
        self.assertFalse(MailSnooze.objects.filter(gmail_id="b2").exists())
        self.assertTrue(MailThread.objects.get(gmail_id="b2").in_inbox)

    def test_snooze_to_a_picked_time_and_never_into_the_past(self):
        when = (timezone.localtime() + timedelta(days=3)).replace(second=0, microsecond=0)
        self.act("snooze", "b2", custom=when.strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(MailSnooze.objects.get(gmail_id="b2").until, when)
        r = self.act("snooze", "c3", custom="2020-01-01T08:00", follow=True)
        self.assertContains(r, "Pick when it should come back.")
        self.assertIn("INBOX", self.labels("c3"))

    def test_unsnooze(self):
        self.act("snooze", "b2", when="tomorrow")
        self.act("unsnooze", "b2")
        self.assertIn("INBOX", self.labels("b2"))
        self.assertFalse(MailSnooze.objects.exists())

    def test_opening_a_thread_marks_it_read_but_still_shows_it_as_new(self):
        r = self.client.get(reverse("crm:inbox"), {"t": "a1"})
        self.assertContains(r, '<span class="tag">New</span>', html=False)
        self.assertNotIn("UNREAD", self.labels("a1"))
        self.assertFalse(MailThread.objects.get(gmail_id="a1").unread)
        self.assertIn(("/threads/a1/modify", {"addLabelIds": [], "removeLabelIds": ["UNREAD"]}), self.fake.posts)

    def test_the_reading_pane_toolbar_and_its_keys(self):
        r = self.client.get(reverse("crm:inbox"), {"t": "b2"})
        for key in ('data-key="e"', 'data-key="s"', 'data-key="#"', 'data-key="U"'):
            self.assertContains(r, key)
        self.assertContains(r, "Deals/MEIH")  # the Label and Move to menus
        self.assertContains(r, "Tomorrow morning")

    def test_never_back_to_another_site(self):
        r = self.act("star", "b2", next="https://evil.example/")
        self.assertRedirects(r, reverse("crm:inbox"), fetch_redirect_response=False)
        r = self.act("star", "b2", next="//evil.example/")
        self.assertRedirects(r, reverse("crm:inbox"), fetch_redirect_response=False)

    def test_post_only_and_nothing_without_a_thread(self):
        self.assertEqual(self.client.get(reverse("crm:inbox_act")).status_code, 405)
        r = self.act("archive", follow=True)
        self.assertContains(r, "Pick a conversation first.")
        r = self.act("delete_forever", "b2", follow=True)
        self.assertContains(r, "Pick a conversation first.")
        self.assertEqual(self.fake.posts, [])


class SnoozeChoiceTests(TestCase):
    def test_choices_follow_the_day_and_hour(self):
        tz = timezone.get_current_timezone()
        wednesday_morning = timezone.make_aware(datetime(2026, 10, 28, 9, 30), tz)
        keys = [k for k, _, _ in actions.snooze_choices(wednesday_morning)]
        self.assertEqual(keys, ["later", "tomorrow", "weekend", "week"])
        whens = {k: timezone.localtime(w) for k, _, w in actions.snooze_choices(wednesday_morning)}
        self.assertEqual((whens["later"].hour, whens["weekend"].weekday(), whens["week"].weekday()), (12, 5, 0))
        saturday_night = timezone.make_aware(datetime(2026, 10, 31, 22, 0), tz)
        self.assertEqual([k for k, _, _ in actions.snooze_choices(saturday_night)], ["tomorrow", "week"])


@override_settings(**ON)
class ActionAccessTests(TestCase):
    def test_mail_off_and_partners_are_turned_away(self):
        user = connected_user()
        MailConnection.objects.filter(user=user).update(enabled=False)
        self.client.force_login(user)
        r = self.client.post(reverse("crm:inbox_act"), {"action": "archive", "t": "x"})
        self.assertRedirects(r, reverse("crm:mail_settings"), fetch_redirect_response=False)
        partner = connected_user("fortress", "lp@acmigroup.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(partner)
        r = self.client.post(reverse("crm:inbox_act"), {"action": "archive", "t": "x"})
        self.assertRedirects(r, "/room/", fetch_redirect_response=False)

    def test_the_sync_wakes_snoozes_in_inbox_only_mailboxes_too(self):
        user = connected_user()
        mailbox = Mailbox.objects.create(user=user, email=ME, enabled=False, backend=Mailbox.Backend.GMAIL_API)
        fake = FakeGmail([api_message("0b01", "b2", WILL, ME, "Site tour", labels=("Label_9",))])
        fake.label_list.append({"id": "Label_9", "name": actions.SNOOZE_LABEL, "type": "user"})
        MailSnooze.objects.create(mailbox=mailbox, gmail_id="b2", until=timezone.now() - timedelta(minutes=5))
        with mock.patch.object(gmail_api, "requests", fake):
            results = sync.sync_all()
        self.assertEqual(results[str(mailbox)], {"snoozed threads back": 1})
        labels = set(fake.messages["0b01"]["labelIds"])
        self.assertTrue({"INBOX", "UNREAD"} <= labels)
        self.assertNotIn("Label_9", labels)
