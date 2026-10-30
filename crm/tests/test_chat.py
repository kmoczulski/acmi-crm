"""Chat in the hub (docs/CHAT.md): the Google Chat client, how messages read, the list of conversations with unread
markers, sending, alerts, and the pages and dock wiring. A fake Chat stands in for Google."""
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import gcal, notify, push
from crm.chat import api as chat_api, connect, service
from crm.chat.render import decorate, format_text, message_view, parse_time, stamp, when_label
from crm.models import ChatConnection, Company, Notification, Person
from crm.permissions import PARTNERS

from .test_gmail_api import ME, ON, FakeResponse
from .test_mail_actions import connected_user

SIMON = "sshewmaker@acmigroup.com"
WILL = "will@firehawkaerospace.com"
USERS = {"users/1": ("Kellen Moczulski", ME), "users/2": ("Simon Shewmaker", SIMON), "users/3": ("Will Edwards", WILL)}
SCOPES = " ".join(["openid", *connect.CHAT_SCOPES])


def error(status, message, reason=""):
    return status, {"error": {"code": status, "message": message, "details": [{"reason": reason}] if reason else []}}


class FakeChat:
    """Stands in for `requests` against the Chat API: spaces with members and messages, and Kellen's read times."""

    def __init__(self):
        self.spaces, self.members, self.messages, self.read = {}, {}, {}, {}
        self.calls, self.posts, self.n = [], [], 0
        self.fail = None  # (status, body) answered to every call
        self.orders = {"createTime desc"}  # the spellings of orderBy this fake accepts
        self.page_cap = 0  # answer in pages of this size, whatever was asked

    def add_space(self, sid, kind="SPACE", name="", people=("users/1", "users/2")):
        self.spaces[sid] = {"name": f"spaces/{sid}", "spaceType": kind, "displayName": name,
                            "spaceUri": f"https://chat.google.com/room/{sid}", "lastActiveTime": ""}
        self.members[sid] = [{"name": f"spaces/{sid}/members/{u.split('/')[1]}", "state": "JOINED",
                              "member": {"name": u, "displayName": USERS[u][0], "email": USERS[u][1], "type": "HUMAN"}}
                             for u in people]
        self.messages[sid] = []

    def set_read(self, sid, when):
        self.read[sid] = stamp(when)

    def add_message(self, sid, text, sender="users/2", when=None, reply=False, thread="", sparse=False, **extra):
        when = when or timezone.now()
        self.n += 1
        who = {"name": sender, "type": "HUMAN"} if sparse else {"name": sender, "type": "HUMAN", "displayName": USERS[sender][0],
                                                                  "email": USERS[sender][1]}
        msg = {"name": f"spaces/{sid}/messages/m{self.n}.m{self.n}", "sender": who, "createTime": stamp(when), "text": text,
               "thread": {"name": thread or f"spaces/{sid}/threads/t{self.n}"}, "threadReply": reply, **extra}
        self.messages[sid].append(msg)
        if parse_time(self.spaces[sid]["lastActiveTime"] or "1970-01-01T00:00:00Z") < when:
            self.spaces[sid]["lastActiveTime"] = stamp(when)
        return msg

    # What `requests` would do

    def _page(self, items, key, params):
        cap = self.page_cap or int(params.get("pageSize", 100))
        start = int(params.get("pageToken") or 0)
        out = {key: items[start:start + cap]}
        if start + cap < len(items):
            out["nextPageToken"] = str(start + cap)
        return FakeResponse(out)

    def _answer(self, method, path, params, body):
        self.calls.append((method, path, dict(params or {})))
        if self.fail:
            return FakeResponse(self.fail[1], self.fail[0])
        params = params or {}
        if method == "GET" and path == "/spaces":
            return self._page(list(self.spaces.values()), "spaces", params)
        m = re.fullmatch(r"/spaces/([\w-]+)", path)
        if m and method == "GET":
            return FakeResponse(self.spaces[m.group(1)]) if m.group(1) in self.spaces else FakeResponse(*error(404, "Not found")[::-1])
        m = re.fullmatch(r"/spaces/([\w-]+)/members", path)
        if m and method == "GET":
            return self._page(self.members[m.group(1)], "memberships", params) if m.group(1) in self.members else FakeResponse(*error(404, "Not found")[::-1])
        m = re.fullmatch(r"/spaces/([\w-]+)/messages", path)
        if m and m.group(1) not in self.spaces:
            return FakeResponse(*error(404, "Not found")[::-1])
        if m and method == "GET":
            order = params.get("orderBy")
            if order and order not in self.orders:
                return FakeResponse(*error(400, f"Invalid orderBy: {order}")[::-1])
            items = list(self.messages[m.group(1)])
            mt = re.fullmatch(r'createTime ([<>]) "(.+)"', params.get("filter", ""))
            if mt:
                t = parse_time(mt.group(2))
                items = [x for x in items if (parse_time(x["createTime"]) > t if mt.group(1) == ">" else parse_time(x["createTime"]) < t)]
            items.sort(key=lambda x: parse_time(x["createTime"]), reverse=params.get("orderBy") == "createTime desc")
            return self._page(items, "messages", params)
        if m and method == "POST":
            thread = ((body or {}).get("thread") or {}).get("name", "")
            self.posts.append((m.group(1), body, params))
            return FakeResponse(self.add_message(m.group(1), body["text"], sender="users/1", thread=thread, reply=bool(thread),
                                                 sparse=True))
        m = re.fullmatch(r"/users/me/spaces/([\w-]+)/spaceReadState", path)
        if m and method == "GET":
            if m.group(1) in self.read:
                return FakeResponse({"name": f"users/me/spaces/{m.group(1)}/spaceReadState", "lastReadTime": self.read[m.group(1)]})
            return FakeResponse(*error(404, "No read state")[::-1])
        if m and method == "PATCH":
            if m.group(1) not in self.spaces:
                return FakeResponse(*error(404, "Not found")[::-1])
            self.read[m.group(1)] = body["lastReadTime"]
            return FakeResponse({"lastReadTime": body["lastReadTime"]})
        return FakeResponse({}, 404)

    def get(self, url, params=None, headers=None, timeout=None):
        return self._answer("GET", url.removeprefix(chat_api.API), params, None)

    def post(self, url, params=None, json=None, headers=None, timeout=None, data=None):
        if url == gcal.TOKEN_URL:
            return FakeResponse({"access_token": "fresh", "expires_in": 3600})
        return self._answer("POST", url.removeprefix(chat_api.API), params, json)

    def patch(self, url, params=None, json=None, headers=None, timeout=None):
        return self._answer("PATCH", url.removeprefix(chat_api.API), params, json)


class FakeGoogleToken:
    """Google's tokeninfo answer for the connect check."""

    def __init__(self, scope=SCOPES):
        self.scope = scope

    def get(self, url, params=None, timeout=None):
        return FakeResponse({"scope": self.scope})


@override_settings(**ON)
class ChatTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = connected_user()
        self.user.first_name, self.user.last_name = "Kellen", "Moczulski"
        self.user.save()
        self.now = timezone.now()
        self.conn = ChatConnection.objects.create(user=self.user, enabled=True, google_email=ME,
                                                  turned_on_at=self.now - timedelta(days=2),
                                                  alerted_through=self.now - timedelta(hours=1))
        self.fake = FakeChat()
        self.fake.add_space("AAA", "SPACE", "Campus ops", ("users/1", "users/2", "users/3"))
        self.fake.add_space("BBB", "DIRECT_MESSAGE", "", ("users/1", "users/2"))
        self.fake.add_space("CCC", "GROUP_CHAT", "", ("users/1", "users/2", "users/3"))
        self.fake.add_message("AAA", "Survey is booked for *Tuesday*.", when=self.now - timedelta(days=1))
        self.fake.add_message("BBB", "Got a minute?", when=self.now - timedelta(minutes=10))
        self.fake.add_message("CCC", "Can you send the PDA?", sender="users/3", when=self.now - timedelta(hours=3))
        self.fake.set_read("AAA", self.now)  # Campus ops is read; the direct message and the group chat are not
        self.fake.set_read("CCC", self.now - timedelta(hours=5))
        chat_api.ORDER_DESC[:] = chat_api.ORDER_SPELLINGS
        patcher = mock.patch.object(chat_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(chat_api.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.client.force_login(self.user)

    def chat_client(self):
        return service.client_for(self.user)


class ClientTests(ChatTestCase):
    def test_spaces_come_back_across_pages(self):
        self.fake.page_cap = 2
        names = [s["name"] for s in self.chat_client().spaces()]
        self.assertEqual(sorted(names), ["spaces/AAA", "spaces/BBB", "spaces/CCC"])
        self.assertEqual(len([c for c in self.fake.calls if c[1] == "/spaces"]), 2)

    def test_messages_newest_first_asked_oldest_first_returned(self):
        for n in range(5):
            self.fake.add_message("AAA", f"m{n}", when=self.now - timedelta(minutes=50 - n))
        got = self.chat_client().messages("spaces/AAA", limit=3)
        self.assertEqual([m["text"] for m in got], ["m2", "m3", "m4"])
        self.assertEqual(self.fake.calls[-1][2]["orderBy"], "createTime desc")

    def test_googles_spelling_of_newest_first_is_found_and_kept(self):
        self.fake.orders = {"DESC"}
        client = self.chat_client()
        self.assertEqual([m["text"] for m in client.messages("spaces/AAA", limit=3)], ["Survey is booked for *Tuesday*."])
        asked = lambda: [c[2]["orderBy"] for c in self.fake.calls if c[1] == "/spaces/AAA/messages"]  # noqa: E731
        self.assertEqual(asked(), ["createTime desc", "DESC"])
        client.messages("spaces/AAA", limit=3)
        self.assertEqual(asked(), ["createTime desc", "DESC", "DESC"])  # the one that worked goes first next time

    def test_a_400_that_is_not_about_the_order_is_still_an_error(self):
        self.fake.fail = error(400, "Bad request")
        with self.assertRaises(chat_api.ChatError) as caught:
            self.chat_client().messages("spaces/AAA")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(len(self.fake.calls), len(chat_api.ORDER_SPELLINGS))  # each spelling tried once, then given up

    def test_messages_after_and_before_a_time(self):
        first = self.fake.add_message("AAA", "one", when=self.now - timedelta(minutes=30))
        self.fake.add_message("AAA", "two", when=self.now - timedelta(minutes=20))
        self.fake.add_message("AAA", "three", when=self.now - timedelta(minutes=10))
        client = self.chat_client()
        self.assertEqual([m["text"] for m in client.messages("spaces/AAA", after=first["createTime"])], ["two", "three"])
        self.assertEqual([m["text"] for m in client.messages("spaces/AAA", before=first["createTime"])][-1:], ["Survey is booked for *Tuesday*."])

    def test_sending_posts_text_once_with_a_request_id_and_can_answer_in_a_thread(self):
        client = self.chat_client()
        client.send("spaces/AAA", "On it")
        space, body, params = self.fake.posts[-1]
        self.assertEqual((space, body), ("AAA", {"text": "On it"}))
        self.assertTrue(params["requestId"])
        self.assertNotIn("messageReplyOption", params)
        client.send("spaces/AAA", "Reply", thread="spaces/AAA/threads/t1")
        _, body, params = self.fake.posts[-1]
        self.assertEqual(body["thread"], {"name": "spaces/AAA/threads/t1"})
        self.assertEqual(params["messageReplyOption"], "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD")

    def test_space_ids_are_checked_before_they_reach_a_url(self):
        self.assertEqual(chat_api.space_name("AAA"), "spaces/AAA")
        self.assertEqual(chat_api.space_name("spaces/AAA"), "spaces/AAA")
        for bad in ("", "../x", "a/b", "AAA?x=1", "x" * 65, None):
            self.assertEqual(chat_api.space_name(bad), "")

    def test_what_went_wrong_is_said_in_plain_words(self):
        client = self.chat_client()
        self.fake.fail = error(403, "Google Chat API has not been used in project 1 before or it is disabled.", "SERVICE_DISABLED")
        with self.assertRaisesMessage(chat_api.ChatError, "Ask Mel"):
            client.spaces()
        self.fake.fail = error(403, "Request had insufficient authentication scopes.", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
        with self.assertRaisesMessage(chat_api.ChatError, "doesn't have the Chat permission"):
            client.spaces()
        self.fake.fail = error(400, "Google Chat app not found. To create a Chat app, you must turn on the Chat API and configure the app.")
        with self.assertRaisesMessage(chat_api.ChatError, "Configuration page"):
            client.send("spaces/AAA", "hi")
        self.fake.fail = error(404, "Not found")
        with self.assertRaises(chat_api.ChatNotFound):
            client.get_space("spaces/ZZZ")

    def test_a_busy_chat_is_retried_then_reported(self):
        self.fake.fail = error(429, "Quota exceeded")
        with self.assertRaisesMessage(chat_api.ChatError, "busy"):
            self.chat_client().spaces()
        self.assertEqual(len(self.fake.calls), chat_api.RETRIES + 1)

    def test_an_expired_access_token_is_refreshed_once(self):
        answers = iter([FakeResponse({}, 401)])
        real = self.fake._answer
        self.fake._answer = lambda *a, **k: next(answers, None) or real(*a, **k)
        self.assertEqual(len(self.chat_client().spaces()), 3)

    def test_no_google_token_means_a_plain_message(self):
        User.objects.create_user("nobody", password="x")
        with self.assertRaises(chat_api.ChatError):
            chat_api.ChatClient(User.objects.get(username="nobody"))

    def test_read_state_is_empty_when_chat_has_none(self):
        self.assertEqual(self.chat_client().read_state("spaces/BBB"), {})


class RenderTests(TestCase):
    def test_formatting_and_links(self):
        out = str(format_text("*bold* and _italic_ and ~gone~ and `code`\nnext https://acmi.com/a_b_c?x=1&y=2, ok"))
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<em>italic</em>", out)
        self.assertIn("<s>gone</s>", out)
        self.assertIn("<code>code</code>", out)
        self.assertIn("<br>", out)
        self.assertIn('<a href="https://acmi.com/a_b_c?x=1&amp;y=2" target="_blank" rel="noopener noreferrer">https://acmi.com/a_b_c?x=1&amp;y=2</a>,', out)
        self.assertNotIn("<em>b</em>", out)  # underscores inside a link are not italics

    def test_everything_from_chat_is_escaped(self):
        out = str(format_text('<script>alert(1)</script> <img src=x onerror=y> [x](javascript:alert(1)) javascript:alert(1)'))
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertNotIn("<a ", out)  # no link was made out of a javascript: address
        self.assertIn("&lt;script&gt;", out)

    def test_custom_links_and_mentions(self):
        out = str(format_text("See <https://acmi.com/plan|the plan> and ask <users/2> or <users/all> or <users/9>",
                              {"users/2": "Simon Shewmaker"}))
        self.assertIn('<a href="https://acmi.com/plan" target="_blank" rel="noopener noreferrer">the plan</a>', out)
        self.assertIn('<span class="chat-mention">@Simon Shewmaker</span>', out)
        self.assertIn('<span class="chat-mention">@all</span>', out)
        self.assertIn('<span class="chat-mention">@someone</span>', out)

    def test_an_unsafe_link_label_stays_escaped(self):
        out = str(format_text('<https://acmi.com/x"onmouseover="y|<b>hi</b>>'))
        self.assertIn('href="https://acmi.com/x&quot;onmouseover=&quot;y"', out)  # the quotes can't end the attribute
        self.assertNotIn("<b>", out)

    def test_times_with_nanoseconds(self):
        self.assertEqual(parse_time("2026-10-29T19:00:00.123456789Z").microsecond, 123456)
        self.assertEqual(parse_time("2026-10-29T19:00:00Z").hour, 19)
        self.assertIsNone(parse_time("soon"))
        self.assertIsNone(parse_time(""))

    def test_a_message_names_its_sender_from_the_members_and_knows_which_are_mine(self):
        directory = {"users/3": {"name": "Will Edwards", "email": WILL}}
        theirs = message_view({"name": "m", "sender": {"name": "users/3", "type": "HUMAN"}, "text": "hi", "createTime": "2026-10-29T19:00:00Z"},
                              directory, ME)
        self.assertEqual((theirs["name"], theirs["email"], theirs["mine"]), ("Will Edwards", WILL, False))
        mine = message_view({"name": "m", "sender": {"name": "users/1", "email": ME.upper(), "displayName": "Kellen"}}, {}, ME)
        self.assertTrue(mine["mine"])
        bot = message_view({"name": "m", "sender": {"name": "users/app", "type": "BOT"}, "cardsV2": [{}]}, {}, ME)
        self.assertEqual(bot["name"], "A Chat app")
        self.assertIn("A card", str(bot["html"]))

    def test_groups_and_days(self):
        base = timezone.now().replace(hour=12, minute=0)

        def v(uid, minutes, day=0):
            t = base + timedelta(minutes=minutes, days=day)
            return {"uid": uid, "time": t, "reply": False}

        views = decorate([v("a", 0), v("a", 2), v("a", 9), v("b", 10), v("b", 11, day=1)])
        self.assertEqual([x["head"] for x in views], [True, False, True, True, True])
        self.assertEqual([x["new_day"] for x in views], [True, False, False, False, True])

    def test_when_labels(self):
        now = timezone.now()
        self.assertRegex(when_label(now), r"^\d{1,2}:\d\d (am|pm)$")
        self.assertEqual(when_label(now - timedelta(days=1)), "Yesterday")
        self.assertEqual(when_label(None), "")


class ConnectTests(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.conn.delete()

    def test_partners_and_a_switched_off_chat_cannot_turn_it_on(self):
        self.assertEqual(connect.unavailable_reason(self.user), "")
        with override_settings(CHAT_ENABLED=False):
            self.assertIn("switched off", connect.unavailable_reason(self.user))
            self.assertEqual(connect.status(self.user), (False, False))
        partner = User.objects.create_user("p", password="x", email="p@bank.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.assertIn("isn't available", connect.unavailable_reason(partner))

    def test_turn_on_goes_to_google_for_the_chat_permissions(self):
        r = self.client.post(reverse("crm:chat_turn_on"))
        self.assertEqual(r.status_code, 302)
        for scope in connect.CHAT_SCOPES:
            self.assertIn(scope.replace(":", "%3A").replace("/", "%2F"), r["Location"])
        self.assertIn("include_granted_scopes", r["Location"])
        self.assertTrue(self.client.session[connect.SESSION_KEY])

    def test_finishing_checks_the_permissions_and_switches_chat_on(self):
        conn = connect.finish(self.user, FakeGoogleToken())
        self.assertTrue(conn.enabled and conn.can_read_state)
        self.assertEqual(conn.google_email, ME)
        self.assertLess(abs((conn.alerted_through - timezone.now()).total_seconds()), 5)  # alerts start now, not at the past
        self.assertEqual(connect.status(self.user), (True, True))

    def test_without_every_permission_or_with_the_wrong_account_it_stays_off(self):
        with self.assertRaisesMessage(connect.ChatConnectError, "all the Chat permissions"):
            connect.finish(self.user, FakeGoogleToken("openid " + connect.SCOPE_SPACES))
        account = gcal.token_for(self.user).account
        account.extra_data = {"email": "someone@acmigroup.com"}
        account.save()
        with self.assertRaisesMessage(connect.ChatConnectError, "your own account"):
            connect.finish(self.user, FakeGoogleToken())
        self.assertFalse(ChatConnection.objects.filter(user=self.user, enabled=True).exists())

    def test_the_read_state_permission_is_optional(self):
        scopes = " ".join(["openid", *connect.NEEDED])
        self.assertFalse(connect.finish(self.user, FakeGoogleToken(scopes)).can_read_state)

    def test_coming_back_from_google(self):
        s = self.client.session
        s[connect.SESSION_KEY] = True
        s.save()
        with mock.patch.object(connect, "finish", return_value=None) as finish:
            r = self.client.get(reverse("crm:chat_connected"))
        self.assertRedirects(r, reverse("crm:chat"), fetch_redirect_response=False)
        finish.assert_called_once()
        r = self.client.get(reverse("crm:chat_connected"))  # a second visit does nothing
        self.assertEqual(finish.call_count, 1)
        s = self.client.session
        s[connect.SESSION_KEY] = True
        s.save()
        with mock.patch.object(connect, "finish", side_effect=connect.ChatConnectError("Google said no.")):
            r = self.client.get(reverse("crm:chat_connected"), follow=True)
        self.assertContains(r, "Google said no.")
        self.assertEqual(ChatConnection.objects.get(user=self.user).last_error, "Google said no.")

    def test_turning_off_only_switches_the_hub_off(self):
        connect.finish(self.user, FakeGoogleToken())
        r = self.client.post(reverse("crm:chat_turn_off"), follow=True)
        self.assertContains(r, "Chat is off")
        self.assertFalse(connect.is_on(self.user))
        self.assertTrue(gcal.connected(self.user))  # Gmail and Calendar keep working


class ServiceTests(ChatTestCase):
    def test_conversations_are_named_sorted_and_marked(self):
        ov = service.overview(self.user, self.chat_client())
        titles = [(r["id"], r["title"], r["unread"]) for r in ov["rows"]]
        self.assertEqual(titles, [("BBB", "Simon Shewmaker", True), ("CCC", "Simon Shewmaker, Will Edwards", True),
                                  ("AAA", "Campus ops", False)])  # newest first; the group chat is named for the others
        self.assertEqual(ov["unread"], 2)
        self.assertEqual(ov["rows"][0]["icon"], "👤")

    def test_unread_means_newer_than_what_was_read_and_old_conversations_never_are(self):
        self.fake.add_space("OLD", "SPACE", "Old news")
        self.fake.add_message("OLD", "ancient", when=self.now - timedelta(days=60))
        rows = {r["id"]: r for r in service.overview(self.user, self.chat_client())["rows"]}
        self.assertFalse(rows["OLD"]["unread"])
        self.fake.set_read("BBB", self.now)
        cache.clear()
        self.assertFalse({r["id"]: r for r in service.overview(self.user, self.chat_client())["rows"]}["BBB"]["unread"])

    def test_a_conversation_with_no_read_time_is_unread_only_when_fresh(self):
        self.fake.add_space("NEW", "DIRECT_MESSAGE", "", ("users/1", "users/3"))
        self.fake.add_message("NEW", "hello", sender="users/3", when=self.now - timedelta(minutes=5))
        self.fake.add_space("STALE", "DIRECT_MESSAGE", "", ("users/1", "users/3"))
        self.fake.add_message("STALE", "hello", sender="users/3", when=self.now - timedelta(days=5))
        rows = {r["id"]: r for r in service.overview(self.user, self.chat_client())["rows"]}
        self.assertTrue(rows["NEW"]["unread"])
        self.assertFalse(rows["STALE"]["unread"])

    def test_without_the_read_state_permission_there_are_no_markers(self):
        self.conn.can_read_state = False
        self.conn.save()
        self.assertEqual(service.overview(self.user, self.chat_client())["unread"], 0)
        self.assertFalse([c for c in self.fake.calls if "spaceReadState" in c[1]])

    def test_when_chat_wont_say_what_was_read_there_is_no_marker(self):
        with mock.patch.object(chat_api.ChatClient, "read_state", side_effect=chat_api.ChatError("no")):
            self.assertEqual(service.overview(self.user, self.chat_client())["unread"], 0)

    def test_the_list_is_kept_for_a_few_seconds_and_muting_changes_it_at_once(self):
        client = self.chat_client()
        service.overview(self.user, client)
        n = len(self.fake.calls)
        service.overview(self.user, client)
        self.assertEqual(len(self.fake.calls), n)  # the dock, a box and the page all ask: Google is asked once
        self.assertTrue(service.toggle_mute(self.user, "BBB"))
        rows = {r["id"]: r for r in service.overview(self.user, client)["rows"]}
        self.assertTrue(rows["BBB"]["muted"])
        self.assertEqual(service.overview(self.user, client)["unread"], 1)  # a muted conversation doesn't count
        self.assertFalse(service.toggle_mute(self.user, "BBB"))

    def test_a_read_conversation_is_not_asked_about_again_soon_but_an_unread_one_is(self):
        client = self.chat_client()
        asked = lambda sid: len([c for c in self.fake.calls if c[1] == f"/users/me/spaces/{sid}/spaceReadState"])  # noqa: E731
        with mock.patch.object(service, "READ_TTL", 0):  # unread ones are asked about again at once
            service.overview(self.user, client)
            read, unread = asked("AAA"), asked("BBB")
            service.invalidate(self.user)
            service.overview(self.user, client)
        self.assertEqual(asked("AAA"), read)  # Campus ops is read and nothing new: not asked again
        self.assertEqual(asked("BBB"), unread + 1)  # the direct message is unread: asked again

    def test_names_fall_back_when_emails_are_missing(self):
        for member in self.fake.members["BBB"]:
            member["member"].pop("email")
        rows = {r["id"]: r for r in service.overview(self.user, self.chat_client())["rows"]}
        self.assertEqual(rows["BBB"]["title"], "Simon Shewmaker")  # not "Kellen Moczulski, Simon Shewmaker"

    def test_loading_a_conversation_names_senders_who_only_have_an_id(self):
        self.fake.add_message("AAA", "from a sparse sender", sender="users/3", sparse=True)
        views, raw, directory = service.load(self.user, self.chat_client(), "spaces/AAA")
        self.assertEqual(views[-1]["name"], "Will Edwards")
        self.assertEqual(directory["users/3"]["email"], WILL)
        self.assertTrue(views[0]["head"])

    def test_posting_shows_as_yours_and_marks_the_conversation_read(self):
        view = service.post(self.user, self.chat_client(), "spaces/BBB", "Yes, calling now")
        self.assertTrue(view["mine"])
        self.assertEqual(view["name"], "Kellen Moczulski")  # the answer to a post names only an id
        self.assertGreaterEqual(parse_time(self.fake.read["BBB"]), parse_time(view["iso"]))

    def test_alerts_are_for_new_messages_from_others_only_and_never_repeat(self):
        self.fake.add_message("BBB", "Are you there?", when=self.now - timedelta(minutes=2))
        self.fake.add_message("BBB", "(me)", sender="users/1", when=self.now - timedelta(minutes=1))
        self.fake.add_message("CCC", "Before Chat was turned on", sender="users/3", when=self.now - timedelta(hours=2))
        cache.clear()
        made = service.alert(self.user)
        self.assertEqual(made, 2)  # "Got a minute?" and "Are you there?"; not my own, not the one from before
        note = Notification.objects.filter(kind=Notification.Kind.CHAT).order_by("id")[0]
        self.assertEqual(note.text, "Simon Shewmaker: Got a minute?")
        self.assertEqual(note.url, reverse("crm:chat") + "?s=BBB")
        self.assertEqual(notify.window_url(note), reverse("crm:chat_window") + "?s=BBB")
        cache.clear()
        self.assertEqual(service.alert(self.user), 0)
        self.assertEqual(Notification.objects.filter(kind=Notification.Kind.CHAT).count(), 2)

    def test_a_space_alert_names_the_space_and_a_muted_one_stays_quiet(self):
        self.fake.add_message("AAA", "Site walk moved to 9", when=self.now - timedelta(minutes=3))
        self.fake.read["AAA"] = stamp(self.now - timedelta(days=1))
        service.toggle_mute(self.user, "BBB")
        cache.clear()
        service.alert(self.user)
        texts = list(Notification.objects.filter(kind=Notification.Kind.CHAT).values_list("text", flat=True))
        self.assertIn("Simon Shewmaker in Campus ops: Site walk moved to 9", texts)
        self.assertFalse([t for t in texts if "Got a minute" in t])

    def test_the_sweep_never_stops_for_one_persons_chat_trouble(self):
        self.fake.fail = error(403, "disabled", "SERVICE_DISABLED")
        service.sweep(self.user)  # logs it; raises nothing
        notify.sweep_everyone()

    def test_chat_off_means_no_alerts(self):
        self.conn.enabled = False
        self.conn.save()
        self.fake.add_message("BBB", "hello?", when=self.now - timedelta(minutes=1))
        self.assertEqual(service.alert(self.user), 0)


class PageTests(ChatTestCase):
    def test_the_page_lists_conversations_with_unread_ones_marked(self):
        r = self.client.get(reverse("crm:chat"))
        self.assertContains(r, "Campus ops")
        self.assertContains(r, "Simon Shewmaker, Will Edwards")
        self.assertContains(r, "chat-row unread", count=2)
        self.assertContains(r, "Pick a conversation.")

    def test_a_conversation_shows_its_messages_marks_them_read_and_names_the_crm_people(self):
        company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        Person.objects.create(first_name="Will", last_name="Edwards", email=WILL, company=company)
        r = self.client.get(reverse("crm:chat"), {"s": "CCC"})
        self.assertContains(r, "Can you send the PDA?")
        self.assertContains(r, "Will Edwards")
        self.assertContains(r, "Firehawk Aerospace")
        self.assertGreater(parse_time(self.fake.read["CCC"]), self.now - timedelta(hours=1))  # opening it marked it read

    def test_the_page_swaps_the_right_side_in_place(self):
        r = self.client.get(reverse("crm:chat"), {"s": "AAA"}, headers={"X-Chat-Pane": "1"})
        self.assertContains(r, "Survey is booked for")
        self.assertContains(r, "<strong>Tuesday</strong>")
        self.assertNotContains(r, "chat-app")  # just the conversation, not the page around it
        r = self.client.get(reverse("crm:chat"), {"s": "ZZZ"}, headers={"X-Chat-Pane": "1"})
        self.assertEqual(r.status_code, 404)

    def test_a_conversation_that_isnt_yours_is_just_not_found(self):
        r = self.client.get(reverse("crm:chat"), {"s": "ZZZ"})
        self.assertContains(r, "isn&#x27;t in your Chat")
        for url in ("chat_messages", "chat_send", "chat_read", "chat_mute"):
            r = self.client.post(reverse(f"crm:{url}"), {"s": "ZZZ", "text": "x"}) if url != "chat_messages" else \
                self.client.get(reverse("crm:chat_messages"), {"s": "ZZZ"})
            self.assertEqual(r.status_code, 404, url)
        self.assertEqual(self.client.get(reverse("crm:chat_messages"), {"s": "../x"}).status_code, 404)
        self.assertEqual(self.fake.posts, [])

    def test_google_trouble_is_shown_on_the_page_in_words(self):
        self.fake.fail = error(403, "Google Chat API has not been used in project 1 before or it is disabled.", "SERVICE_DISABLED")
        r = self.client.get(reverse("crm:chat"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Ask Mel")

    def test_a_box_gets_the_conversation_without_the_top_bar(self):
        r = self.client.get(reverse("crm:chat_window"), {"s": "BBB", "w": "c1"})
        self.assertContains(r, 'data-window="c1"')
        self.assertContains(r, 'data-window-title="Simon Shewmaker"')
        self.assertContains(r, "data-chat-box")
        self.assertContains(r, "Got a minute?")
        self.assertNotContains(r, 'class="appbar')
        r = self.client.get(reverse("crm:chat_window"), {"w": "c1"})
        self.assertContains(r, "Pick a conversation from the Chat list.")

    def test_the_docks_list_box(self):
        r = self.client.get(reverse("crm:chat_list"), {"window": "1", "w": "chats"})
        self.assertContains(r, "data-chat-list")
        self.assertContains(r, "Campus ops")
        self.assertNotContains(r, 'class="appbar')

    def test_chat_off_shows_how_to_turn_it_on(self):
        self.conn.enabled = False
        self.conn.save()
        r = self.client.get(reverse("crm:chat"))
        self.assertContains(r, "Turn on Chat")
        self.assertNotContains(r, "Campus ops")
        self.assertEqual(self.client.get(reverse("crm:chat_poll")).status_code, 403)
        self.assertEqual(self.fake.calls, [])  # nothing asked of Google

    def test_the_polling_call_for_new_messages(self):
        mid = self.fake.add_message("AAA", "first", when=self.now - timedelta(minutes=5))
        self.fake.add_message("AAA", "second", sender="users/3", when=self.now - timedelta(minutes=4))
        d = self.client.get(reverse("crm:chat_messages"), {"s": "AAA", "after": mid["createTime"]}).json()
        self.assertEqual(d["count"], 1)
        self.assertIn("second", d["html"])
        self.assertNotIn("first", d["html"])
        self.assertTrue(d["from_others"])
        self.assertEqual(d["latest"], self.fake.messages["AAA"][-1]["createTime"])

    def test_grouping_carries_on_across_a_refresh(self):
        first = self.fake.add_message("AAA", "one", sender="users/3", when=self.now - timedelta(minutes=5))
        self.fake.add_message("AAA", "two", sender="users/3", when=self.now - timedelta(minutes=4))
        d = self.client.get(reverse("crm:chat_messages"), {
            "s": "AAA", "after": first["createTime"], "prev_uid": "users/3", "prev_time": first["createTime"]}).json()
        self.assertNotIn("chat-who", d["html"])  # the same person a minute later: no new name line

    def test_earlier_messages(self):
        for n in range(45):
            self.fake.add_message("AAA", f"old {n}", when=self.now - timedelta(days=2, minutes=60 - n))
        r = self.client.get(reverse("crm:chat"), {"s": "AAA"})
        self.assertContains(r, "Earlier messages")  # more than a page: the button is there
        earliest = re.search(r'data-earliest="([^"]+)"', r.content.decode()).group(1)
        d = self.client.get(reverse("crm:chat_messages"), {"s": "AAA", "before": earliest}).json()
        self.assertGreater(d["count"], 0)
        self.assertIn("old", d["html"])

    def test_sending(self):
        r = self.client.post(reverse("crm:chat_send"), {"s": "AAA", "text": " On it, thanks <b>x</b> "})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertIn("On it, thanks &lt;b&gt;x&lt;/b&gt;", d["html"])
        self.assertIn("You", d["html"])
        self.assertEqual(self.fake.posts[-1][1]["text"], "On it, thanks <b>x</b>")
        self.assertEqual(self.client.post(reverse("crm:chat_send"), {"s": "AAA", "text": "  "}).status_code, 400)
        self.assertEqual(self.client.post(reverse("crm:chat_send"), {"s": "AAA", "text": "x" * 4001}).status_code, 400)
        self.assertEqual(self.client.get(reverse("crm:chat_send"), {"s": "AAA"}).status_code, 405)

    def test_only_a_real_thread_name_is_passed_on(self):
        self.client.post(reverse("crm:chat_send"), {"s": "AAA", "text": "a", "thread": "spaces/AAA/threads/t1"})
        self.assertEqual(self.fake.posts[-1][1]["thread"], {"name": "spaces/AAA/threads/t1"})
        self.client.post(reverse("crm:chat_send"), {"s": "AAA", "text": "b", "thread": "../../x"})
        self.assertNotIn("thread", self.fake.posts[-1][1])

    def test_a_chat_that_wont_post_says_why(self):
        self.fake.fail = error(400, "Google Chat app not found. To create a Chat app, you must turn on the Chat API.")
        r = self.client.post(reverse("crm:chat_send"), {"s": "AAA", "text": "hi"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("Configuration page", r.json()["error"])

    def test_read_mute_and_rows(self):
        self.assertTrue(self.client.post(reverse("crm:chat_read"), {"s": "BBB"}).json()["ok"])
        self.assertGreater(parse_time(self.fake.read["BBB"]), self.now - timedelta(minutes=5))
        self.assertTrue(self.client.post(reverse("crm:chat_mute"), {"s": "BBB"}).json()["muted"])
        d = self.client.get(reverse("crm:chat_rows"), {"current": "BBB"}).json()
        self.assertIn("chat-row on", d["html"])
        self.assertIn("🔕", d["html"])

    def test_the_dock_poll_counts_unread_and_makes_alerts(self):
        self.fake.add_message("BBB", "Ping", when=self.now - timedelta(minutes=1))
        d = self.client.get(reverse("crm:chat_poll")).json()
        self.assertEqual(d["unread"], 2)
        self.assertEqual(sorted(d["ids"]), ["BBB", "CCC"])
        self.assertEqual(d["alerts"], 2)  # Got a minute? and Ping
        self.assertEqual(self.client.get(reverse("crm:chat_poll")).json()["alerts"], 0)

    def test_signed_out_and_partners_get_nothing(self):
        self.client.logout()
        self.assertNotEqual(self.client.get(reverse("crm:chat")).status_code, 200)
        partner = User.objects.create_user("p", password="x", email="p@bank.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(partner)
        self.assertNotEqual(self.client.get(reverse("crm:chat")).status_code, 200)
        self.assertNotEqual(self.client.get(reverse("crm:chat_poll")).status_code, 200)


class WiringTests(ChatTestCase):
    def test_the_dock_gets_chat_when_it_is_on(self):
        r = self.client.get(reverse("crm:home"))
        self.assertContains(r, f'data-chat-url="{reverse("crm:chat_window")}"')
        self.assertContains(r, f'data-chat-list-url="{reverse("crm:chat_list")}"')
        self.assertContains(r, f'data-chat-poll-url="{reverse("crm:chat_poll")}"')
        self.assertContains(r, f'data-chat-page-url="{reverse("crm:chat")}"')  # the pinned Chat box hides on the Chat page
        self.assertContains(r, f'href="{reverse("crm:chat")}" class="tn-link')

    def test_with_chat_off_there_is_a_link_to_turn_it_on_but_no_dock_chat(self):
        self.conn.enabled = False
        self.conn.save()
        r = self.client.get(reverse("crm:home"))
        self.assertContains(r, f'href="{reverse("crm:chat")}" class="tn-link')
        self.assertNotContains(r, "data-chat-list-url")

    def test_chat_off_for_everyone_hides_it(self):
        with override_settings(CHAT_ENABLED=False):
            r = self.client.get(reverse("crm:home"))
        self.assertNotContains(r, f'href="{reverse("crm:chat")}" class="tn-link')

    def test_alerts_open_the_conversation_in_a_box(self):
        before = notify.send(self.user, Notification.Kind.DUE, "Due today: x", "/tasks/1/")
        self.fake.add_message("BBB", "Ping", when=self.now - timedelta(minutes=1))
        cache.clear()
        service.alert(self.user)
        note = Notification.objects.filter(kind=Notification.Kind.CHAT).first()
        window = f'{reverse("crm:chat_window")}?s=BBB'
        self.assertEqual(notify.window_url(note), window)
        items = self.client.get(reverse("crm:notification_poll"), {"after": before.pk}).json()["items"]
        self.assertIn(window, [i["window"] for i in items])  # the live poll names the box too
        self.assertContains(self.client.get(reverse("crm:notification_list")), f'data-window="{window}"')
        self.assertEqual(push.payload(note)["window"], window)

    def test_chat_alerts_have_their_own_switch(self):
        kinds = [kind for kind, _ in push.KINDS]
        self.assertIn(Notification.Kind.CHAT, kinds)
        self.assertContains(self.client.get(reverse("crm:notification_settings")), "New chat message")

    def test_the_service_worker_knows_the_chat_box_address_too(self):
        script = self.client.get(reverse("crm:service_worker")).content.decode()
        self.assertIn("hub-window", script)  # the same message opens a mail or a chat box
