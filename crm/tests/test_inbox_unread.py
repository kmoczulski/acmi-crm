"""The inbox's Unread button (Kellen, 2026-11-06: "I cant view unread emails very easily"): one click in the list head shows only
the conversations with something unread, in the Inbox, in a split-inbox tab, in any folder and beside a search."""
from datetime import timedelta
from unittest import mock

from allauth.socialaccount.models import SocialAccount, SocialToken
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import gmail_api, inbox_views
from crm.models import Company, MailConnection, Mailbox

from .test_gmail_api import ME, ON, FakeGmail, api_message

User = get_user_model()
WILL = "Will Edwards <will@firehawkaerospace.com>"
UNREAD = ("INBOX", "UNREAD")


@override_settings(**ON)
class UnreadFilterTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("kellen", password="x", email=ME)
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1", extra_data={"email": ME})
        SocialToken.objects.create(account=account, token="a", token_secret="r",
                                   expires_at=timezone.now() + timedelta(hours=1))
        MailConnection.objects.create(user=self.user, enabled=True, google_email=ME)
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        now = timezone.now()
        # Newest first: e5 (read, a tenant), d4 (UNREAD, a tenant), c3 (UNREAD, a stranger), b2 (read, a bank),
        # a1 (UNREAD: its newest message is, its first was read, a tenant).
        self.fake = FakeGmail([
            api_message("m1", "a1", WILL, ME, "PDA for land search", when=now - timedelta(hours=5)),
            api_message("m2", "a1", WILL, ME, "Re: PDA for land search", when=now - timedelta(hours=3), labels=UNREAD),
            api_message("m3", "b2", "Pat <pat@shoreunitedbank.com>", ME, "Term sheet", when=now - timedelta(hours=2)),
            api_message("m4", "c3", "News <news@example.com>", ME, "Weekly digest", when=now - timedelta(hours=1), labels=UNREAD),
            api_message("m5", "d4", WILL, ME, "Utility letters", when=now - timedelta(minutes=30), labels=UNREAD),
            api_message("m6", "e5", WILL, ME, "Closing date", when=now - timedelta(minutes=10)),
        ])
        self.fake.inbox_unread = 3
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, **params):
        return self.client.get(reverse("crm:inbox"), params)

    def ids(self, response):
        return [t.gmail_id for t in response.context["threads"]]

    def test_the_button_shows_only_conversations_with_something_unread(self):
        self.assertEqual(self.ids(self.get()), ["e5", "d4", "c3", "b2", "a1"])
        r = self.get(unread="1")
        self.assertEqual(self.ids(r), ["d4", "c3", "a1"])  # a1 counts: one of its messages is unread
        self.assertTrue(all(t.unread for t in r.context["threads"]))
        path, params = [c for c in self.fake.calls if c[0] == "/threads"][-1]
        self.assertEqual((params["labelIds"], params["q"]), ("INBOX", "is:unread"))  # asked of Gmail, in the Inbox only

    def test_the_button_is_in_the_list_head_and_flips_the_filter(self):
        r = self.get()
        self.assertContains(r, '<a class="btn btn-small mail-unread-toggle" href="?unread=1" title="Show only conversations you haven\'t read">')
        self.assertNotContains(r, 'aria-current="true"')
        r = self.get(unread="1")
        self.assertContains(r, 'class="btn btn-small mail-unread-toggle btn-primary" href="?"')  # pressed: a click goes back to all
        self.assertContains(r, 'aria-current="true"')
        self.assertTrue(r.context["unread_only"])

    def test_the_button_keeps_the_folder_tab_and_search_but_starts_a_fresh_list(self):
        # Opening a conversation, a later page and the order are left behind; the folder, tab and words stay.
        r = self.get(label="INBOX", tab="tenants", unread="1", t="a1", p="x", o="5", order="oldest")
        self.assertEqual(r.context["unread_toggle"], "?label=INBOX&tab=tenants")
        r = self.get(label="STARRED", q="PDA", t="a1")
        self.assertEqual(r.context["unread_toggle"], "?label=STARRED&q=PDA&unread=1")

    def test_the_filter_works_in_the_split_inbox_tabs(self):
        self.get()  # a first look indexes the threads, which is where the tabs read from
        self.assertEqual(self.ids(self.get(tab="tenants")), ["e5", "d4", "a1"])
        self.assertEqual(self.ids(self.get(tab="tenants", unread="1")), ["d4", "a1"])
        self.assertEqual(self.ids(self.get(tab="other", unread="1")), ["c3"])
        self.assertEqual(self.ids(self.get(tab="firms", unread="1")), [])

    def test_the_filter_follows_you_through_tabs_rows_and_older_pages(self):
        self.get()
        r = self.get(unread="1")
        self.assertContains(r, 'href="?label=INBOX&tab=tenants&unread=1"')  # every tab keeps it
        self.assertContains(r, 'href="?label=INBOX&unread=1&t=d4"')  # so does every conversation you open
        with mock.patch.object(inbox_views, "PAGE", 1):
            r = self.get(tab="tenants", unread="1")
        self.assertEqual(self.ids(r), ["d4"])
        self.assertContains(r, 'href="?label=INBOX&tab=tenants&unread=1&o=1"')  # Older
        self.assertContains(self.get(unread="1", t="a1"), 'unread=1&amp;t=a1&order=oldest')  # the reading pane's own links

    def test_with_a_search_it_narrows_the_search_to_the_unread(self):
        r = self.get(q="PDA", unread="1")
        self.assertEqual(self.ids(r), ["a1"])
        path, params = [c for c in self.fake.calls if c[0] == "/threads"][-1]
        self.assertIn("PDA", params["q"])
        self.assertIn("is:unread", params["q"])
        self.assertContains(r, '<input type="hidden" name="unread" value="1">')  # a new search keeps the filter
        self.assertNotContains(self.get(q="PDA"), 'name="unread"')

    def test_nothing_unread_says_so(self):
        for message in self.fake.messages.values():
            message["labelIds"] = [lb for lb in message["labelIds"] if lb != "UNREAD"]
        r = self.get(unread="1")
        self.assertEqual(self.ids(r), [])
        self.assertContains(r, "Nothing unread here. You're all caught up.")

    def test_anything_but_1_means_no_filter(self):
        for value in ("0", "yes", ""):
            self.assertEqual(self.ids(self.get(unread=value)), ["e5", "d4", "c3", "b2", "a1"], value)
