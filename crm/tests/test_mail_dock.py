"""The mail dock (static/crm/dock.js): the compose and conversation pages drawn for a box at the bottom of the page,
their JSON answers, and the new-email alerts that open a box instead of leaving the page."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import notify, push
from crm.mail import gmail_api
from crm.models import Mailbox, MailboxAccess, Notification

from .test_gmail_api import ME, ON, FakeGmail, api_message
from .test_mail_actions import connected_user
from .test_shared_mailboxes import DEALS, shared_mailbox
from .utils import make_manager

WILL = "Will Edwards <will@firehawkaerospace.com>"
SEND = dict(ON, MAIL_SEND_ENABLED=True)


@override_settings(**SEND)
class DockTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API,
                                              backfill_done=True)
        self.fake = FakeGmail([
            api_message("0a01", "a1", WILL, f"Kellen <{ME}>", "PDA for land search", body="Can you send the PDA?",
                        when=timezone.now() - timedelta(hours=2), msg_id="<pda-1@fh>", attachment="Site plan v3.pdf",
                        labels=("INBOX", "UNREAD")),
        ])
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)


class ComposeBoxTests(DockTestCase):
    def test_a_box_gets_the_compose_page_without_the_top_bar(self):
        r = self.client.get(reverse("crm:inbox_compose"), {"window": "1", "w": "ab12", "to": "will@firehawkaerospace.com"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'data-window="ab12"')
        self.assertContains(r, 'name="dock" value="1"')  # the server answers JSON and leaves no "Sent." flash behind
        self.assertContains(r, 'value="will@firehawkaerospace.com"')
        self.assertContains(r, "compose.js")
        self.assertNotContains(r, 'class="appbar')
        self.assertNotContains(r, "page-head")

    def test_the_full_page_is_unchanged(self):
        r = self.client.get(reverse("crm:inbox_compose"))
        self.assertContains(r, 'class="appbar')
        self.assertContains(r, "page-head")
        self.assertNotContains(r, "data-window")
        self.assertNotContains(r, 'name="dock"')

    def test_the_box_id_is_cleaned(self):
        r = self.client.get(reverse("crm:inbox_compose"), {"window": "1", "w": '"><script>x</script>'})
        self.assertNotContains(r, "<script>x")
        self.assertContains(r, 'data-window="scriptxscript"')

    def test_a_reply_box_is_filled_in_and_titled_like_the_page(self):
        r = self.client.get(reverse("crm:inbox_compose"), {"window": "1", "w": "r1", "reply": "0a01", "mode": "reply"})
        self.assertContains(r, 'value="Re: PDA for land search"')
        self.assertContains(r, 'data-title="Re: PDA for land search"')
        self.assertContains(r, "will@firehawkaerospace.com")

    def test_a_new_box_is_titled_new_message(self):
        r = self.client.get(reverse("crm:inbox_compose"), {"window": "1", "w": "n1"})
        self.assertContains(r, 'data-title="New message"')

    def test_sending_from_a_box_answers_json_and_leaves_no_flash(self):
        r = self.client.post(reverse("crm:inbox_compose"), {
            "mode": "new", "to": "will@firehawkaerospace.com", "subject": "Hi", "body": "Hello", "inline": "1", "dock": "1"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIn("label=SENT", data["url"])
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(list(get_messages(r.wsgi_request)), [])  # nothing for the next page to show later

    def test_the_reading_panes_reply_box_still_flashes_sent(self):
        r = self.client.post(reverse("crm:inbox_compose"), {
            "mode": "new", "to": "will@firehawkaerospace.com", "subject": "Hi", "body": "Hello", "inline": "1"})
        self.assertTrue(r.json()["ok"])
        self.assertEqual([str(m) for m in get_messages(r.wsgi_request)], ["Sent."])

    def test_a_bad_address_in_a_box_comes_back_as_json(self):
        r = self.client.post(reverse("crm:inbox_compose"), {
            "mode": "new", "to": "not an address", "subject": "x", "body": "x", "inline": "1", "dock": "1"})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["error"])
        self.assertEqual(self.fake.sent, [])

    def test_discarding_in_a_box_answers_json_and_deletes_the_draft(self):
        r = self.client.post(reverse("crm:inbox_draft"), {"mode": "new", "subject": "Idea", "body": "later"})
        draft = r.json()["draft"]
        r = self.client.post(reverse("crm:inbox_draft_discard"), {"draft_id": draft, "inline": "1"})
        self.assertEqual(r.json(), {"ok": True})
        self.assertNotIn(draft, self.fake.drafts)

    def test_a_shared_mailbox_is_named_above_the_form(self):
        deals = shared_mailbox(make_manager(connected_user("boss", "boss@acmigroup.com")))
        MailboxAccess.objects.create(mailbox=deals, user=self.user, can_send=True)
        with mock.patch.object(gmail_api, "requests", FakeGmail([], email=DEALS)):
            self.client.get(reverse("crm:inbox"), {"mb": deals.pk})  # the session now looks at the shared mailbox
            r = self.client.get(reverse("crm:inbox_compose"), {"window": "1", "w": "s1"})
        self.assertContains(r, f"Sending from the shared mailbox Deals ({DEALS})")
        r = self.client.get(reverse("crm:inbox_compose"), {"mb": "own"})  # back to your own: no note
        self.assertNotContains(r, "Sending from the shared mailbox")


    def test_a_box_says_what_is_wrong_instead_of_redirecting_to_the_inbox(self):
        self.fake.fail_on = {"0a01"}  # Gmail drops the connection reading the email being replied to
        r = self.client.get(reverse("crm:inbox_compose"), {"window": "1", "w": "dn", "reply": "0a01"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "reach Gmail")
        self.assertNotContains(r, 'class="appbar')
        r = self.client.get(reverse("crm:inbox_compose"), {"reply": "0a01"})  # the full page still goes back to the inbox
        self.assertRedirects(r, reverse("crm:inbox"), fetch_redirect_response=False)

    def test_read_only_access_gets_a_notice_in_the_box_and_the_inbox_for_the_full_page(self):
        deals = shared_mailbox(make_manager(connected_user("boss", "boss@acmigroup.com")))
        MailboxAccess.objects.create(mailbox=deals, user=self.user)  # read, not send
        with mock.patch.object(gmail_api, "requests", FakeGmail([], email=DEALS)):
            self.client.get(reverse("crm:inbox"), {"mb": deals.pk})
            r = self.client.get(reverse("crm:inbox_compose"), {"window": "1", "w": "ro"})
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "You can read this shared mailbox, but not send")
            self.assertNotContains(r, 'class="appbar')
            r = self.client.get(reverse("crm:inbox_compose"))
        self.assertRedirects(r, reverse("crm:inbox"), fetch_redirect_response=False)


class ConversationBoxTests(DockTestCase):
    def test_a_conversation_opens_in_a_box(self):
        r = self.client.get(reverse("crm:inbox_window"), {"t": "a1", "w": "t1"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "PDA for land search")
        self.assertContains(r, 'data-window="t1"')
        self.assertContains(r, 'data-dock-reply="0a01"')  # Reply, Reply all and Forward open boxes of their own
        self.assertContains(r, "data-mail-frame")
        self.assertContains(r, "Site plan v3.pdf")
        self.assertContains(r, "Open in Mail")
        self.assertNotContains(r, 'class="appbar')

    def test_opening_it_marks_it_read_like_the_inbox(self):
        self.assertIn("UNREAD", self.fake.messages["0a01"]["labelIds"])
        self.client.get(reverse("crm:inbox_window"), {"t": "a1"})
        self.assertNotIn("UNREAD", self.fake.messages["0a01"]["labelIds"])

    def test_a_message_id_finds_its_conversation(self):
        r = self.client.get(reverse("crm:inbox_window"), {"m": "<pda-1@fh>"})
        self.assertContains(r, "PDA for land search")

    def test_an_email_that_is_not_there_says_so_in_the_box(self):
        for query in ({"t": "zz"}, {"m": "<nope@x>"}, {}):
            r = self.client.get(reverse("crm:inbox_window"), query)
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "That email isn&#x27;t in your mailbox.")

    def test_gmail_trouble_is_shown_in_the_box_not_as_an_error_page(self):
        self.fake.status = 500
        r = self.client.get(reverse("crm:inbox_window"), {"t": "a1"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'class="mail-error"')

    def test_someone_without_mail_is_sent_to_set_it_up(self):
        self.client.force_login(User.objects.create_user("nomail", password="x"))
        r = self.client.get(reverse("crm:inbox_window"), {"t": "a1"})
        self.assertRedirects(r, reverse("crm:mail_settings"), fetch_redirect_response=False)

    def test_read_only_access_has_no_reply_buttons(self):
        deals = shared_mailbox(make_manager(connected_user("boss", "boss@acmigroup.com")))
        MailboxAccess.objects.create(mailbox=deals, user=self.user)  # read, not send
        fake = FakeGmail([api_message("0d01", "d4", WILL, DEALS, "RFP for MEIH", labels=("INBOX",))], email=DEALS)
        with mock.patch.object(gmail_api, "requests", fake):
            r = self.client.get(reverse("crm:inbox_window"), {"mb": deals.pk, "t": "d4"})
        self.assertContains(r, "RFP for MEIH")
        self.assertNotContains(r, "data-dock-reply")


class AlertTests(DockTestCase):
    def mail_note(self, url="/inbox/?mb=7&t=a1"):
        notify.new_mail(self.user, "Will", "PDA", url, "<m1@x>")
        return Notification.objects.get(user=self.user, key="mail:<m1@x>")

    def test_a_new_email_alert_names_its_box(self):
        note = self.mail_note()
        self.assertEqual(notify.window_url(note), reverse("crm:inbox_window") + "?mb=7&t=a1")
        due = notify.send(self.user, Notification.Kind.DUE, "Due today: x", "/tasks/1/")
        self.assertEqual(notify.window_url(due), "")  # other alerts still go to their page
        gmail = Notification.objects.create(user=self.user, kind=Notification.Kind.MAIL, text="x",
                                            url="https://mail.google.com/mail/u/0/#inbox/a1")
        self.assertEqual(notify.window_url(gmail), "")
        no_thread = Notification.objects.create(user=self.user, kind=Notification.Kind.MAIL, text="x", url="/inbox/")
        self.assertEqual(notify.window_url(no_thread), "")

    def test_the_poll_carries_the_box_address(self):
        first = notify.send(self.user, Notification.Kind.DUE, "Due today: x", "/tasks/1/")
        self.mail_note("/inbox/?t=a1")
        items = self.client.get(reverse("crm:notification_poll"), {"after": first.pk}).json()["items"]
        self.assertEqual([i["window"] for i in items], [reverse("crm:inbox_window") + "?t=a1"])
        self.assertTrue(items[0]["url"].startswith("/notifications/"))  # still marks it read for the fallback

    def test_the_bells_list_marks_new_email_for_the_dock(self):
        self.mail_note("/inbox/?t=a1")
        notify.send(self.user, Notification.Kind.DUE, "Due today: x", "/tasks/1/")
        r = self.client.get(reverse("crm:notification_list"))
        self.assertContains(r, f'data-window="{reverse("crm:inbox_window")}?t=a1"')
        self.assertEqual(r.content.decode().count("data-window="), 1)

    def test_a_browser_notification_carries_the_box_address(self):
        note = self.mail_note("/inbox/?t=a1")
        self.assertEqual(push.payload(note)["window"], reverse("crm:inbox_window") + "?t=a1")
        self.assertEqual(push.payload(title="t", body="b", url="/")["window"], "")
        script = self.client.get(reverse("crm:service_worker")).content.decode()
        self.assertIn("hub-window", script)  # clicking it tells an open hub page to open the box
        self.assertIn("/?dock=", script)  # and with none open, the hub opens with the box

    def test_opening_an_alert_by_its_link_still_marks_it_read(self):
        note = self.mail_note("/inbox/?t=a1")
        r = self.client.get(reverse("crm:notification_open", args=[note.pk]))  # what the dock's fetch does
        self.assertRedirects(r, "/inbox/?t=a1", fetch_redirect_response=False)
        note.refresh_from_db()
        self.assertIsNotNone(note.read_at)


class PageWiringTests(DockTestCase):
    def test_pages_load_the_dock_for_people_with_mail(self):
        r = self.client.get(reverse("crm:home"))
        self.assertContains(r, "crm/dock.js")
        self.assertContains(r, f'data-compose-url="{reverse("crm:inbox_compose")}"')
        self.assertContains(r, f'data-thread-url="{reverse("crm:inbox_window")}"')

    def test_not_for_people_without_mail(self):
        self.client.force_login(User.objects.create_user("nomail", password="x"))
        r = self.client.get(reverse("crm:home"))
        self.assertNotContains(r, "crm/dock.js")

    def test_the_inbox_new_message_button_is_still_a_compose_link(self):
        r = self.client.get(reverse("crm:inbox"))
        self.assertContains(r, f'href="{reverse("crm:inbox_compose")}" class="mail-compose-btn"')  # dock.js turns the click into a box
