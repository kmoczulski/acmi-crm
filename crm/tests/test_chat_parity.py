"""Chat in the hub, the way Google Chat's own web app works (docs/CHAT.md): reactions, quoting, files, editing and deleting,
@mentions' markup, starting conversations, searching messages and catching what others changed. The fake Chat in
test_chat.py stands in for Google."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from crm.chat import api as chat_api, connect, service
from crm.chat.api import BadRequest, ChatError
from crm.chat.render import message_view, plain_text, stamp
from crm.permissions import PARTNERS

from .test_chat import SIMON, WILL, ChatTestCase, FakeGoogleToken, MediaResponse, error
from .test_gmail_api import ME

DIRECTORY = {"users/1": {"name": "Kellen Moczulski", "email": ME}, "users/2": {"name": "Simon Shewmaker", "email": SIMON}}
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def raw_message(**extra):
    return {"name": "spaces/AAA/messages/m1.m1", "sender": {"name": "users/2", "type": "HUMAN", "displayName": "Simon Shewmaker",
            "email": SIMON}, "createTime": "2026-10-29T15:00:00.123456789Z", "text": "Plan attached",
            "thread": {"name": "spaces/AAA/threads/t1"}, **extra}


# ---------------------------------------------------------------------------
# How a message looks
# ---------------------------------------------------------------------------


class RenderTests(SimpleTestCase):
    def view(self, **extra):
        return message_view(raw_message(**extra), DIRECTORY, ME)

    def test_emoji_become_chips_with_the_persons_own_marked(self):
        v = self.view(emojiReactionSummaries=[
            {"emoji": {"unicode": "👍"}, "reactionCount": 2},
            {"emoji": {"customEmoji": {"uid": "u1", "emojiName": ":party:", "temporaryImageUri": "https://img.example/p.png"}}, "reactionCount": 1},
            {"emoji": {"unicode": "🎉"}, "reactionCount": 0}])
        chips = message_view(raw_message(emojiReactionSummaries=[
            {"emoji": {"unicode": "👍"}, "reactionCount": 2}, {"emoji": {"unicode": "😮"}, "reactionCount": 1}]), DIRECTORY, ME,
            mine_reactions={"👍"})["reactions"]
        self.assertEqual([(c["label"], c["count"], c["mine"]) for c in chips], [("👍", 2, True), ("😮", 1, False)])
        self.assertEqual([(c["label"], c["count"], c["custom"]) for c in v["reactions"]], [("👍", 2, False), (":party:", 1, True)])
        self.assertEqual(v["reactions"][1]["image"], "https://img.example/p.png")  # shown, not clickable

    def test_a_quoted_message_names_its_author_and_a_forward_says_where_it_came_from(self):
        quote = {"name": "spaces/AAA/messages/m0.m0", "lastUpdateTime": "x", "quotedMessageSnapshot": {"sender": "users/2", "text": "Gate code is 4471"}}
        v = self.view(quotedMessageMetadata=quote)
        self.assertEqual(v["quote"], {"id": "spaces/AAA/messages/m0.m0", "name": "Simon Shewmaker", "text": "Gate code is 4471",
                                      "forwarded": False, "from_space": ""})
        quote["quotedMessageSnapshot"]["sender"] = "Pat Lee"  # Chat may send a name instead of an id
        self.assertEqual(self.view(quotedMessageMetadata=quote)["quote"]["name"], "Pat Lee")
        fwd = {**quote, "quoteType": "FORWARD", "forwardedMetadata": {"spaceDisplayName": "Capital"},
               "quotedMessageSnapshot": {"sender": "users/2", "text": "plain", "formattedText": "with *markup*"}}
        v = self.view(quotedMessageMetadata=fwd)
        self.assertEqual((v["quote"]["forwarded"], v["quote"]["from_space"], v["quote"]["text"]), (True, "Capital", "with markup"))
        self.assertIsNone(self.view()["quote"])

    def test_files_come_through_the_hub_and_only_plain_pictures_are_shown_in_the_page(self):
        def ref(name, kind, res="spaces/AAA/attachments/a1"):
            return {"contentName": name, "contentType": kind, "attachmentDataRef": {"resourceName": res}}
        files = self.view(attachment=[ref("plan.png", "image/png"), ref("logo.svg", "image/svg+xml"), ref("page.html", "text/html"),
                                      ref("Budget.xlsx", "application/vnd.ms-excel"),
                                      {"contentName": "Term sheet", "driveDataRef": {"driveFileId": "D1"}},
                                      {"contentName": "Old", "downloadUri": "http://insecure.example/x"},
                                      {"contentName": "Seen", "downloadUri": "https://chat.google.com/get?x=1"}])["attachments"]
        picture = files[0]
        self.assertEqual(picture["kind"], "image")
        self.assertEqual(picture["src"], "/chat/file/?r=spaces%2FAAA%2Fattachments%2Fa1&n=plan.png")
        self.assertEqual(picture["url"], picture["src"] + "&d=1")
        for risky in files[1:3]:  # script can hide in these: never inline
            self.assertEqual((risky["kind"], risky["src"]), ("file", ""))
        self.assertEqual((files[3]["icon"], files[2]["kind"]), ("📊", "file"))
        self.assertEqual((files[4]["kind"], files[4]["url"]), ("drive", "https://drive.google.com/file/d/D1/view"))
        self.assertEqual(files[5]["url"], "")  # not https
        self.assertEqual(files[6]["url"], "https://chat.google.com/get?x=1")

    def test_a_name_that_could_be_a_path_is_never_asked_for(self):
        v = self.view(attachment=[{"contentName": "x", "contentType": "image/png", "attachmentDataRef": {"resourceName": "../../etc/passwd"}}])
        self.assertEqual((v["attachments"][0]["src"], v["attachments"][0]["url"]), ("", ""))

    def test_edited_deleted_and_who_may_change_a_message(self):
        self.assertFalse(self.view()["edited"])
        self.assertTrue(self.view(lastUpdateTime="2026-10-29T15:05:00Z")["edited"])
        self.assertFalse(self.view(lastUpdateTime="2026-10-29T15:00:00.5Z")["edited"])  # made, not edited
        mine = message_view(raw_message(sender={"name": "users/1", "type": "HUMAN"}), DIRECTORY, ME)
        self.assertTrue(mine["mine"] and mine["can_change"])
        self.assertFalse(self.view()["can_change"])
        gone = message_view(raw_message(sender={"name": "users/1"}, deleteTime="2026-10-29T16:00:00Z", lastUpdateTime="2026-10-29T15:30:00Z",
                                        emojiReactionSummaries=[{"emoji": {"unicode": "👍"}, "reactionCount": 1}],
                                        quotedMessageMetadata={"name": "spaces/AAA/messages/m0.m0"}), DIRECTORY, ME)
        self.assertEqual((gone["deleted"], gone["can_change"], gone["reactions"], gone["quote"], gone["edited"]), (True, False, [], None, False))

    def test_what_a_reply_needs_to_quote_and_edit_a_message(self):
        v = self.view(formattedText="Plan <users/2> *attached*", lastUpdateTime="2026-10-29T15:05:00Z")
        self.assertEqual(v["raw"], "Plan <users/2> *attached*")  # the edit box starts from Chat's own markup
        self.assertEqual(v["quote_time"], "2026-10-29T15:05:00Z")  # what Chat checks a quote against: the last update
        self.assertEqual(self.view()["quote_time"], "2026-10-29T15:00:00.123456789Z")  # else the creation time, exactly as given
        self.assertEqual(self.view(text="", attachment=[{"contentName": "a.pdf"}])["snippet"], "📎 a.pdf")
        self.assertEqual(len(self.view(text="x" * 400)["snippet"]), 160)

    def test_a_quote_and_a_snippet_are_plain_words_without_chats_markup(self):
        names = {"users/2": "Simon Shewmaker"}
        self.assertEqual(plain_text("Hey <users/2>, see *this* and `that` at <https://x.com/a|the plan> or <https://y.org> _now_ ~not~ <users/all>", names),
                         "Hey @Simon Shewmaker, see this and that at the plan or https://y.org now not @all")
        self.assertEqual(plain_text("<users/9> hi"), "@someone hi")
        v = self.view(text="*Gate* code <users/2>", quotedMessageMetadata={"name": "spaces/AAA/messages/m0.m0",
                      "quotedMessageSnapshot": {"sender": "users/2", "text": "_Please_ `do` <users/1>"}})
        self.assertEqual(v["snippet"], "Gate code @Simon Shewmaker")
        self.assertEqual(v["quote"]["text"], "Please do @Kellen Moczulski")

    def test_a_profile_picture_is_used_only_from_https(self):
        self.assertEqual(self.view(sender={"name": "users/2", "avatarUrl": "https://lh3.example/a.png", "displayName": "S"})["avatar"], "https://lh3.example/a.png")
        self.assertEqual(self.view(sender={"name": "users/2", "avatarUrl": "javascript:alert(1)", "displayName": "S"})["avatar"], "")


# ---------------------------------------------------------------------------
# The Chat API, as Google documents it
# ---------------------------------------------------------------------------


class ClientTests(ChatTestCase):
    def test_messages_are_read_in_chat_syntax_and_deleted_ones_only_when_asked_for(self):
        client = self.chat_client()
        client.messages("spaces/AAA", limit=5, deleted=True)
        params = self.fake.calls[-1][2]
        self.assertEqual((params["markupSyntax"], params["showDeleted"]), ("MARKUP_SYNTAX_CHAT", "true"))
        client.messages("spaces/AAA", limit=5)
        self.assertNotIn("showDeleted", self.fake.calls[-1][2])

    def test_a_message_can_carry_a_quote_and_files_and_is_sent_in_chat_syntax(self):
        client = self.chat_client()
        target = self.fake.add_message("AAA", "Gate code is 4471")
        token = client.upload("spaces/AAA", "plan.pdf", "application/pdf", b"%PDF-1")["attachmentUploadToken"]
        client.send("spaces/AAA", "On it", quote={"name": target["name"], "lastUpdateTime": target["createTime"]}, attachments=[token])
        body = self.fake.posts[-1][1]
        self.assertEqual(body["quotedMessageMetadata"], {"name": target["name"], "lastUpdateTime": target["createTime"]})
        self.assertEqual(body["attachment"], [{"attachmentDataRef": {"attachmentUploadToken": token}}])
        self.assertEqual(body["markupSyntax"], "MARKUP_SYNTAX_CHAT")

    def test_a_chat_that_refuses_the_syntax_setting_still_gets_the_message(self):
        self.fake.markup_ok = False
        self.chat_client().send("spaces/AAA", "Hello")
        self.assertEqual(len(self.fake.posts), 2)
        self.assertNotIn("markupSyntax", self.fake.posts[-1][1])

    def test_any_other_400_is_not_retried(self):
        self.fake.fail = error(400, "Message too long")
        with self.assertRaisesMessage(ChatError, "too long"):
            self.chat_client().send("spaces/AAA", "x")
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_message_is_edited_with_a_text_mask_and_deleted_with_or_without_its_replies(self):
        client = self.chat_client()
        msg = self.fake.add_message("AAA", "typo", sender="users/1")
        answer = client.update_message(msg["name"], "fixed")
        call = [c for c in self.fake.calls if c[0] == "PATCH" and c[1] == "/" + msg["name"]][-1]
        self.assertEqual(call[2], {"updateMask": "text"})
        self.assertEqual((answer["text"], self.fake.find_message(msg["name"])["text"]), ("fixed", "fixed"))
        client.delete_message(msg["name"])
        self.assertEqual(self.fake.calls[-1], ("DELETE", "/" + msg["name"], {}))
        other = self.fake.add_message("AAA", "with replies", sender="users/1")
        self.fake.add_message("AAA", "reply", thread=other["thread"]["name"], reply=True)
        with self.assertRaisesMessage(ChatError, "has replies"):
            client.delete_message(other["name"])
        client.delete_message(other["name"], force=True)
        self.assertEqual(self.fake.calls[-1][2], {"force": "true"})

    def test_reactions_are_listed_by_emoji_and_person_added_and_taken_away(self):
        client = self.chat_client()
        m = self.fake.add_message("AAA", "ship it")
        self.fake.add_reaction(m, "👍", "users/2")
        self.fake.add_reaction(m, "👍", "users/3")
        self.fake.add_reaction(m, "🎉", "users/2")
        self.assertEqual(len(client.reactions(m["name"])), 3)
        self.assertEqual(len(client.reactions(m["name"], emoji="👍")), 2)
        mine = client.reactions(m["name"], emoji="👍", user="users/2")
        self.assertEqual(len(mine), 1)
        self.assertEqual(self.fake.calls[-1][2]["filter"], 'emoji.unicode = "👍" AND user.name = "users/2"')
        made = client.react(m["name"], "🙏")
        self.assertEqual((made["emoji"], made["user"]["name"]), ({"unicode": "🙏"}, "users/1"))
        client.unreact(made["name"])
        self.assertEqual(len(client.reactions(m["name"], emoji="🙏")), 0)

    def test_names_and_emoji_that_reach_a_request_are_checked(self):
        self.assertEqual(chat_api.message_name("spaces/AAA/messages/m1.m1"), "spaces/AAA/messages/m1.m1")
        for bad in ("", "spaces/AAA/messages/../x", "spaces/A B/messages/x", "spaces/AAA/messages/x/reactions/y", None):
            self.assertEqual(chat_api.message_name(bad), "", bad)
        self.assertTrue(chat_api.reaction_name("spaces/AAA/messages/m1.m1/reactions/r1"))
        self.assertEqual(chat_api.reaction_name("spaces/AAA/messages/m1.m1"), "")
        self.assertEqual(chat_api.media_name("spaces/AAA/attachments/a1=="), "spaces/AAA/attachments/a1==")
        for bad in ("../x", "a/../b", "a b", "", "x" * 2000):
            self.assertEqual(chat_api.media_name(bad), "", bad)
        self.assertTrue(all(chat_api.emoji_ok(e) for e in ("👍", "❤️", "🧑‍💻")))
        self.assertFalse(any(chat_api.emoji_ok(e) for e in ("", 'a"b', "a\\b", "x" * 17, "a\nb")))

    def test_a_file_goes_up_as_multipart_with_its_name_and_comes_back_down(self):
        client = self.chat_client()
        ref = client.upload("spaces/AAA", "site plan.pdf", "application/pdf", b"%PDF-1.4 data\r\nmore")
        self.assertTrue(ref["attachmentUploadToken"] and ref["resourceName"].startswith("spaces/AAA/attachments/"))
        space, name, kind, data, query = self.fake.uploaded[-1]
        self.assertEqual((space, name, kind, data, query), ("AAA", "site plan.pdf", "application/pdf", b"%PDF-1.4 data\r\nmore", {"uploadType": "multipart"}))
        stream = client.download(ref["resourceName"])
        self.assertEqual(b"".join(stream.iter_content(4)), data)
        self.assertEqual(stream.headers["Content-Type"], "application/pdf")
        self.assertEqual(self.fake.calls[-1][2], {"alt": "media"})

    def test_conversations_are_found_made_and_searched_the_documented_way(self):
        client = self.chat_client()
        self.assertEqual(client.find_dm(SIMON)["name"], "spaces/BBB")
        self.assertIsNone(client.find_dm("nobody@acmigroup.com"))
        client.set_up("GROUP_CHAT", [SIMON, WILL])
        self.assertEqual(self.fake.setups[-1]["space"], {"spaceType": "GROUP_CHAT"})
        self.assertEqual([m["member"] for m in self.fake.setups[-1]["memberships"]],
                         [{"name": f"users/{SIMON}", "type": "HUMAN"}, {"name": f"users/{WILL}", "type": "HUMAN"}])
        client.set_up("SPACE", [SIMON], "Land team")
        self.assertEqual(self.fake.setups[-1]["space"], {"spaceType": "SPACE", "displayName": "Land team"})
        client.set_up("DIRECT_MESSAGE", ["new@acmigroup.com"])
        self.assertEqual(self.fake.setups[-1]["space"], {"spaceType": "DIRECT_MESSAGE", "singleUserBotDm": False})
        self.assertTrue(self.fake.setups[-1]["requestId"])
        client.search("survey", limit=500)
        self.assertEqual(self.fake.searches[-1], {"filter": "survey", "pageSize": 100, "orderBy": "createTime desc", "markupSyntax": "MARKUP_SYNTAX_CHAT"})


# ---------------------------------------------------------------------------
# The logic
# ---------------------------------------------------------------------------


class ServiceTests(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.space = "spaces/AAA"
        self.theirs = self.fake.add_message("AAA", "Gate code is 4471", sender="users/2", when=self.now - timedelta(minutes=30))
        self.mine = self.fake.add_message("AAA", "On my way", sender="users/1", when=self.now - timedelta(minutes=20))

    def views(self):
        views, raw, directory = service.load(self.user, self.chat_client(), self.space, limit=40)
        return {v["id"]: v for v in views}

    def asked_for_reactions(self):
        return [c for c in self.fake.calls if c[0] == "GET" and c[1].endswith("/reactions")]

    def test_the_emoji_the_person_gave_are_marked_and_asked_once_a_minute(self):
        self.fake.add_reaction(self.theirs, "👍", "users/1")
        self.fake.add_reaction(self.theirs, "👍", "users/2")
        self.fake.add_reaction(self.theirs, "🎉", "users/2")
        chips = self.views()[self.theirs["name"]]["reactions"]
        self.assertEqual([(c["label"], c["count"], c["mine"]) for c in chips], [("👍", 2, True), ("🎉", 1, False)])
        self.assertEqual(len(self.asked_for_reactions()), 1)  # one question for the one message that has emoji
        self.assertEqual(self.asked_for_reactions()[0][2]["filter"], 'user.name = "users/1"')
        self.views()
        self.assertEqual(len(self.asked_for_reactions()), 1)  # kept for a minute
        cache.clear()
        self.views()
        self.assertEqual(len(self.asked_for_reactions()), 2)

    def test_without_addresses_in_the_member_list_nothing_is_marked_and_nothing_extra_is_asked(self):
        for member in self.fake.members["AAA"]:
            member["member"].pop("email")
        self.fake.add_reaction(self.theirs, "👍", "users/1")
        cache.clear()
        chips = self.views()[self.theirs["name"]]["reactions"]
        self.assertEqual([(c["count"], c["mine"]) for c in chips], [(1, False)])
        self.assertEqual(self.asked_for_reactions(), [])

    def test_clicking_an_emoji_puts_it_on_and_clicking_it_again_takes_it_off(self):
        client = self.chat_client()
        first = service.toggle_reaction(self.user, client, self.space, self.theirs["name"], "👍")
        self.assertTrue(first["on"])
        self.assertEqual([(c["label"], c["count"], c["mine"]) for c in first["reactions"]], [("👍", 1, True)])
        second = service.toggle_reaction(self.user, client, self.space, self.theirs["name"], "👍")
        self.assertFalse(second["on"])
        self.assertEqual(second["reactions"], [])
        self.fake.add_reaction(self.theirs, "👍", "users/2")  # someone else's stays when you take yours away
        service.toggle_reaction(self.user, client, self.space, self.theirs["name"], "👍")
        third = service.toggle_reaction(self.user, client, self.space, self.theirs["name"], "👍")
        self.assertEqual([(c["count"], c["mine"]) for c in third["reactions"]], [(1, False)])

    def test_only_a_real_emoji_on_a_message_of_this_conversation_can_be_toggled(self):
        client = self.chat_client()
        with self.assertRaisesMessage(BadRequest, "isn't an emoji"):
            service.toggle_reaction(self.user, client, self.space, self.theirs["name"], 'a"b')
        other = self.fake.add_message("BBB", "elsewhere")
        with self.assertRaisesMessage(BadRequest, "isn't in this conversation"):
            service.toggle_reaction(self.user, client, self.space, other["name"], "👍")

    def test_when_the_person_cannot_be_told_from_the_others_an_emoji_already_there_is_said_so(self):
        for member in self.fake.members["AAA"]:
            member["member"].pop("email")
        cache.clear()
        client = self.chat_client()
        service.toggle_reaction(self.user, client, self.space, self.theirs["name"], "👍")  # learns nothing from a member list
        with self.assertRaisesMessage(ChatError, "already there"):
            service.toggle_reaction(self.user, client, self.space, self.theirs["name"], "👍")

    def test_who_gave_which_emoji(self):
        self.fake.add_reaction(self.theirs, "👍", "users/2")
        self.fake.add_reaction(self.theirs, "👍", "users/1")
        self.fake.add_reaction(self.theirs, "🎉", "users/3")
        got = service.reactors(self.user, self.chat_client(), self.space, self.theirs["name"])
        self.assertEqual({r["label"]: r["names"] for r in got}, {"👍": ["You", "Simon Shewmaker"], "🎉": ["Will Edwards"]})

    def test_a_quote_is_made_from_the_message_as_it_is_now(self):
        self.theirs["lastUpdateTime"] = stamp(self.now)  # edited since the page was drawn
        view = service.post(self.user, self.chat_client(), self.space, "Thanks", quote=self.theirs["name"])
        meta = self.fake.posts[-1][1]["quotedMessageMetadata"]
        self.assertEqual(meta, {"name": self.theirs["name"], "lastUpdateTime": stamp(self.now)})
        self.assertEqual((view["quote"]["name"], view["quote"]["text"]), ("Simon Shewmaker", "Gate code is 4471"))
        self.assertEqual(len(self.fake.posts), 1)

    def test_a_quote_chat_will_not_take_goes_as_words_so_the_reply_still_says_what_it_answers(self):
        self.fake.quotes = "reject"
        view = service.post(self.user, self.chat_client(), self.space, "Thanks", quote=self.theirs["name"])
        self.assertEqual(len(self.fake.posts), 2)
        self.assertNotIn("quotedMessageMetadata", self.fake.posts[-1][1])
        self.assertEqual(self.fake.posts[-1][1]["text"], "↪ Simon Shewmaker: “Gate code is 4471”\n\nThanks")
        self.assertIsNone(view["quote"])
        self.assertIn("Gate code is 4471", view["text"])

    def test_a_quote_chat_took_but_left_off_is_added_in_words_afterwards(self):
        self.fake.quotes = "drop"
        view = service.post(self.user, self.chat_client(), self.space, "Thanks", quote=self.theirs["name"])
        self.assertEqual(len(self.fake.posts), 1)
        self.assertTrue(any(c[0] == "PATCH" for c in self.fake.calls))
        self.assertTrue(view["text"].startswith("↪ Simon Shewmaker"))

    def test_a_message_that_is_gone_or_elsewhere_cannot_be_quoted(self):
        gone = self.fake.add_message("AAA", "oops")
        self.chat_client().delete_message(gone["name"])
        with self.assertRaisesMessage(BadRequest, "deleted"):
            service.post(self.user, self.chat_client(), self.space, "re", quote=gone["name"])
        other = self.fake.add_message("BBB", "elsewhere")
        with self.assertRaisesMessage(BadRequest, "isn't in this conversation"):
            service.post(self.user, self.chat_client(), self.space, "re", quote=other["name"])
        self.assertEqual(self.fake.posts, [])

    def test_files_go_on_the_message_and_it_comes_back_whole(self):
        client = self.chat_client()
        picture = service.upload(self.user, client, self.space, "pic.png", "image/png", PNG)
        plan = service.upload(self.user, client, self.space, "plan.pdf", "application/pdf", b"%PDF")
        view = service.post(self.user, client, self.space, "", attachments=[picture["token"], plan["token"]])
        self.assertEqual([(a["name"], a["kind"]) for a in view["attachments"]], [("pic.png", "image"), ("plan.pdf", "file")])
        self.assertEqual(len(self.fake.posts[-1][1]["attachment"]), 2)
        self.assertTrue(any(c[0] == "GET" and c[1] == "/" + view["id"] for c in self.fake.calls))  # read back for the full picture

    def test_more_files_than_chat_allows_on_one_message_are_cut_off(self):
        client = self.chat_client()
        tokens = [service.upload(self.user, client, self.space, f"f{n}.txt", "text/plain", b"x")["token"] for n in range(12)]
        service.post(self.user, client, self.space, "lots", attachments=tokens)
        self.assertEqual(len(self.fake.posts[-1][1]["attachment"]), service.MAX_FILES)

    def test_a_file_chat_does_not_give_a_token_for_is_an_error(self):
        with mock.patch.object(chat_api.ChatClient, "upload", return_value={}):
            with self.assertRaisesMessage(ChatError, "didn't take that file"):
                service.upload(self.user, self.chat_client(), self.space, "x.txt", "text/plain", b"x")

    def test_the_persons_own_message_can_be_edited_and_says_so(self):
        view = service.edit(self.user, self.chat_client(), self.space, self.mine["name"], "On my way, *10 minutes*")
        self.assertEqual(self.fake.find_message(self.mine["name"])["text"], "On my way, *10 minutes*")
        self.assertTrue(view["edited"] and view["mine"])
        self.assertEqual(view["raw"], "On my way, *10 minutes*")
        self.assertIn("<strong>10 minutes</strong>", str(view["html"]))

    def test_nobody_else_s_message_can_be_edited_or_deleted_and_the_refusal_is_clear(self):
        for action in (lambda: service.edit(self.user, self.chat_client(), self.space, self.theirs["name"], "mine now"),
                       lambda: service.remove(self.user, self.chat_client(), self.space, self.theirs["name"])):
            with self.assertRaisesMessage(BadRequest, "only change your own"):
                action()
        self.assertEqual(self.fake.find_message(self.theirs["name"])["text"], "Gate code is 4471")
        self.assertFalse([c for c in self.fake.calls if c[0] in ("PATCH", "DELETE") and "spaceReadState" not in c[1]])

    def test_deleting_takes_the_message_away_and_a_thread_needs_the_person_to_say_so(self):
        client = self.chat_client()
        service.remove(self.user, client, self.space, self.mine["name"])
        shown = self.views()[self.mine["name"]]
        self.assertTrue(shown["deleted"])  # a "message deleted" line, as in Chat
        with self.assertRaisesMessage(BadRequest, "already deleted"):
            service.remove(self.user, client, self.space, self.mine["name"])
        root = self.fake.add_message("AAA", "thread root", sender="users/1")
        self.fake.add_message("AAA", "a reply", thread=root["thread"]["name"], reply=True)
        with self.assertRaisesMessage(ChatError, "has replies"):
            service.remove(self.user, client, self.space, root["name"])
        service.remove(self.user, client, self.space, root["name"], force=True)
        self.assertTrue(self.fake.find_message(root["name"]).get("deleteTime"))

    def test_the_tail_is_re_read_and_only_a_change_by_someone_else_redraws_it(self):
        client = self.chat_client()
        first = service.refresh_tail(self.user, client, self.space)
        self.assertTrue(first["changed"])  # nothing was known
        again = service.refresh_tail(self.user, client, self.space, first["sig"])
        self.assertEqual((again["changed"], again["sig"]), (False, first["sig"]))
        self.fake.add_reaction(self.theirs, "👍", "users/2")
        cache.clear()
        changed = service.refresh_tail(self.user, client, self.space, first["sig"])
        self.assertTrue(changed["changed"])
        self.assertNotEqual(changed["sig"], first["sig"])
        self.theirs["text"] = "Gate code is 5522"
        self.theirs["lastUpdateTime"] = stamp(self.now)
        self.assertNotEqual(service.refresh_tail(self.user, client, self.space)["sig"], changed["sig"])  # an edit shows too

    def test_a_chat_with_one_person_opens_the_direct_message_that_is_there_and_needs_no_permission(self):
        row = service.start_chat(self.user, self.chat_client(), SIMON)
        self.assertEqual((row["id"], row["title"]), ("BBB", "Simon Shewmaker"))
        self.assertEqual(self.fake.setups, [])

    def test_a_new_chat_needs_the_permission_and_then_is_made_as_the_right_kind(self):
        client = self.chat_client()
        with self.assertRaises(service.NeedsPermission):
            service.start_chat(self.user, client, WILL)
        self.assertEqual(self.fake.setups, [])
        dm = service.start_chat(self.user, client, WILL, allowed=True)
        self.assertEqual(self.fake.setups[-1]["space"]["spaceType"], "DIRECT_MESSAGE")
        self.assertEqual((dm["title"], dm["id"].startswith("NEW")), ("Will Edwards", True))
        group = service.start_chat(self.user, client, f"{SIMON}, {WILL}, {ME}", allowed=True)  # the person themself is left out
        self.assertEqual(self.fake.setups[-1]["space"], {"spaceType": "GROUP_CHAT"})
        self.assertEqual(len(self.fake.setups[-1]["memberships"]), 2)
        space = service.start_chat(self.user, client, SIMON, name="  Land   team ", allowed=True)
        self.assertEqual(self.fake.setups[-1]["space"], {"spaceType": "SPACE", "displayName": "Land team"})
        self.assertEqual(space["title"], "Land team")
        self.assertTrue(group["id"].startswith("NEW"))

    def test_starting_a_chat_checks_who_with(self):
        client = self.chat_client()
        for people, words in (("", "at least one person"), (ME, "at least one person"), ("simon@", "isn't an email"),
                              (",".join(f"p{n}@acmigroup.com" for n in range(50)), "up to 49")):
            with self.subTest(people=people), self.assertRaisesMessage(BadRequest, words):
                service.start_chat(self.user, client, people, allowed=True)
        self.assertEqual(service.clean_people("A@x.com, b@x.com;a@x.com", me="b@x.com"), ["a@x.com"])
        with self.assertRaises(BadRequest):
            service.start_chat(self.user, client, "", allowed=True)

    def test_teammates_to_start_a_chat_with_are_acmi_people_other_than_the_person(self):
        User.objects.create_user("simon", email=SIMON, first_name="Simon", last_name="Shewmaker", password="x")
        User.objects.create_user("outside", email="o@gmail.com", password="x")
        partner = User.objects.create_user("p", email="p@acmigroup.com", password="x")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.assertEqual(service.teammates(self.user), [{"email": SIMON, "name": "Simon Shewmaker"}])

    def test_messages_are_found_with_where_they_are_and_the_words_marked(self):
        self.fake.add_message("AAA", "Budget for the survey *crew* <b>now</b>", sender="users/2")
        self.fake.add_message("CCC", "survey results", sender="users/3")
        found = service.search(self.user, self.chat_client(), "Survey budget")
        self.assertEqual(len(found["results"]), 1)
        hit = found["results"][0]
        self.assertEqual((hit["space"], hit["title"], hit["who"]), ("AAA", "Campus ops", "Simon Shewmaker"))
        self.assertIn("<mark>survey</mark>", str(hit["html"]))
        self.assertIn("&lt;b&gt;now&lt;/b&gt;", str(hit["html"]))  # what people wrote is never markup
        self.assertIn("crew", str(hit["html"]))
        self.assertNotIn("*", str(hit["html"]))  # and Chat's own markers are not shown as letters
        self.assertEqual(self.fake.searches[-1]["filter"], "Survey budget")
        both = service.search(self.user, self.chat_client(), "survey")
        self.assertEqual({r["space"] for r in both["results"]}, {"AAA", "CCC"})

    def test_what_is_typed_into_search_cannot_become_a_filter(self):
        self.assertEqual(service.search_query('sender.name = "x" (a) <b>'), 'sender.name "x" a b')
        self.assertEqual(service.search_query('"land search" budget'), '"land search" budget')
        for short in ("", " ", "a", '()'):
            with self.assertRaises(BadRequest):
                service.search(self.user, self.chat_client(), short)


# ---------------------------------------------------------------------------
# The pages and calls
# ---------------------------------------------------------------------------


class PageTests(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.theirs = self.fake.add_message("AAA", "Gate code is 4471", sender="users/2", when=self.now - timedelta(minutes=30))
        self.mine = self.fake.add_message("AAA", "On my way", sender="users/1", when=self.now - timedelta(minutes=20))

    def post(self, name, **fields):
        return self.client.post(reverse(f"crm:{name}"), {"s": "AAA", **fields})

    def test_a_conversation_has_every_button_and_what_the_script_needs(self):
        self.fake.add_reaction(self.theirs, "👍", "users/2")
        self.fake.add_reaction(self.theirs, "👍", "users/1")
        page = self.client.get(reverse("crm:chat"), {"s": "AAA"})
        self.assertEqual(page.status_code, 200)
        for needle in ('data-act="react"', 'data-act="quote"', 'data-fmt="*"', 'data-fmt="link"', "data-emoji-insert", "data-attach", "data-file-input",
                       'data-max-mb="25"', f'data-react-url="{reverse("crm:chat_react")}"', f'data-upload-url="{reverse("crm:chat_upload")}"',
                       f'data-refresh-url="{reverse("crm:chat_refresh")}"', 'id="chat-members"', 'class="chat-chip mine"'):
            self.assertContains(page, needle)
        convo = page.context["convo"]
        self.assertEqual([m["name"] for m in convo["members"]], ["Simon Shewmaker", "Will Edwards"])  # not the person; for @mentions
        self.assertTrue(convo["sig"] and convo["everyone"] and not convo["can_create"])
        body = page.content.decode()
        mine_at, theirs_at = body.index(self.mine["name"]), body.index(self.theirs["name"])
        self.assertIn('data-act="edit"', body[mine_at:mine_at + 3500].split("</div>\n</div>")[0] + body[mine_at:mine_at + 3500])
        self.assertEqual(body.count('data-act="edit"'), 1)  # only the person's own message can be edited or deleted
        self.assertEqual(body.count('data-act="delete"'), 1)
        self.assertGreater(theirs_at, 0)

    def test_a_direct_message_has_no_everyone_to_mention(self):
        self.assertFalse(self.client.get(reverse("crm:chat"), {"s": "BBB"}).context["convo"]["everyone"])

    def test_a_search_result_opens_the_conversation_at_that_message(self):
        for n in range(30):
            self.fake.add_message("AAA", f"older {n}", when=self.now - timedelta(days=2, minutes=60 - n))
        target = self.fake.add_message("AAA", "the one found", when=self.now - timedelta(days=1, hours=5))
        for n in range(5):
            self.fake.add_message("AAA", f"after {n}", when=self.now - timedelta(days=1, hours=4, minutes=-n))
        page = self.client.get(reverse("crm:chat"), {"s": "AAA", "at": target["createTime"]})
        convo = page.context["convo"]
        self.assertEqual(convo["hit"], target["name"])
        texts = [m["text"] for m in convo["messages"]]
        self.assertIn("the one found", texts)
        self.assertEqual(texts.index("the one found"), service_around())  # the 15 before it, then it
        self.assertTrue(convo["more"] and convo["sig"] == "")  # older ones to load; no tail refresh away from the newest
        self.assertContains(page, f'data-hit="{target["name"]}"')
        self.assertEqual(self.client.get(reverse("crm:chat"), {"s": "AAA", "at": "garbage"}).context["convo"]["hit"], "")

    def test_sending_with_files_and_a_quote(self):
        client = self.chat_client()
        token = service.upload(self.user, client, "spaces/AAA", "pic.png", "image/png", PNG)["token"]
        r = self.post("chat_send", text="", files=json.dumps([token]), quote=self.theirs["name"])
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "chat-img")
        self.assertContains(r, "chat-quote")
        self.assertEqual(self.post("chat_send", text="", files="[]").status_code, 400)  # nothing to send
        self.assertEqual(self.post("chat_send", text="hi", files="not json").status_code, 200)  # a bad list is ignored
        self.assertEqual(self.post("chat_send", text="hi", quote="spaces/BBB/messages/x.x").status_code, 400)

    def test_reacting_answers_the_emoji_row_to_draw(self):
        r = self.post("chat_react", m=self.theirs["name"], emoji="🙏")
        d = r.json()
        self.assertTrue(d["on"])
        self.assertIn('class="chat-chip mine"', d["html"])
        self.assertFalse(self.post("chat_react", m=self.theirs["name"], emoji="🙏").json()["on"])
        self.assertEqual(self.post("chat_react", m=self.theirs["name"], emoji='"').status_code, 400)
        self.assertEqual(self.client.post(reverse("crm:chat_react"), {"s": "../x", "m": "x", "emoji": "👍"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("crm:chat_react")).status_code, 405)

    def test_who_reacted(self):
        self.fake.add_reaction(self.theirs, "🎉", "users/3")
        d = self.client.get(reverse("crm:chat_reactions"), {"s": "AAA", "m": self.theirs["name"]}).json()
        self.assertEqual(d["emojis"], [{"label": "🎉", "names": ["Will Edwards"]}])

    def test_editing_and_deleting(self):
        r = self.post("chat_edit", m=self.mine["name"], text="On my way, *soon*")
        d = r.json()
        self.assertEqual((d["raw"], d["edited"]), ("On my way, *soon*", True))
        self.assertIn("<strong>soon</strong>", d["html"])
        self.assertEqual(self.post("chat_edit", m=self.mine["name"], text="  ").status_code, 400)
        self.assertEqual(self.post("chat_edit", m=self.mine["name"], text="x" * 4001).status_code, 400)
        refused = self.post("chat_edit", m=self.theirs["name"], text="mine")
        self.assertEqual((refused.status_code, refused.json()["error"]), (400, "You can only change your own messages."))
        self.assertEqual(self.post("chat_delete", m=self.theirs["name"]).status_code, 400)
        self.assertEqual(self.post("chat_delete", m=self.mine["name"]).json(), {"ok": True})
        self.assertTrue(self.fake.find_message(self.mine["name"]).get("deleteTime"))

    def test_uploading_a_file(self):
        f = SimpleUploadedFile("../evil name?.pdf", b"%PDF-1.4", content_type="application/pdf")
        d = self.client.post(reverse("crm:chat_upload"), {"s": "AAA", "file": f}).json()
        self.assertTrue(d["token"])
        self.assertEqual((d["name"], d["size"], d["type"]), ("evil name.pdf", 8, "application/pdf"))
        self.assertEqual(self.fake.uploaded[-1][:3], ("AAA", "evil name.pdf", "application/pdf"))
        empty = self.client.post(reverse("crm:chat_upload"), {"s": "AAA", "file": SimpleUploadedFile("e.txt", b"")})
        self.assertEqual((empty.status_code, empty.json()["error"]), (400, "That file is empty."))
        self.assertEqual(self.client.post(reverse("crm:chat_upload"), {"s": "AAA"}).status_code, 400)
        with override_settings(CHAT_MAX_UPLOAD_MB=1):
            big = self.client.post(reverse("crm:chat_upload"), {"s": "AAA", "file": SimpleUploadedFile("b.bin", b"0" * (1024 * 1024 + 1))})
        self.assertEqual(big.status_code, 400)
        self.assertIn("bigger than 1 MB", big.json()["error"])
        self.assertEqual(len(self.fake.uploaded), 1)

    def stored(self, name, kind, data):
        return self.chat_client().upload("spaces/AAA", name, kind, data)["resourceName"]

    def test_a_picture_is_shown_in_the_page_and_everything_else_is_only_downloaded(self):
        res = self.stored("pic.png", "image/png", PNG)
        r = self.client.get(reverse("crm:chat_attachment"), {"r": res, "n": "pic.png"})
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/png"))
        self.assertEqual(b"".join(r.streaming_content), PNG)
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", r["Content-Security-Policy"])
        self.assertEqual(r["Content-Length"], str(len(PNG)))
        forced = self.client.get(reverse("crm:chat_attachment"), {"r": res, "n": "pic.png", "d": "1"})
        self.assertEqual((forced["Content-Type"], forced["Content-Disposition"].startswith("attachment")), ("application/octet-stream", True))
        for kind, name in (("text/html", "page.html"), ("image/svg+xml", "logo.svg"), ("application/pdf", "a.pdf")):
            other = self.client.get(reverse("crm:chat_attachment"), {"r": self.stored(name, kind, b"<script>alert(1)</script>"), "n": name})
            self.assertEqual((other["Content-Type"], other["Content-Disposition"].startswith("attachment")), ("application/octet-stream", True), kind)

    def test_a_download_chat_compressed_is_passed_on_without_a_length_that_would_be_wrong(self):
        packed = MediaResponse("image/png", PNG)
        packed.headers["Content-Encoding"] = "gzip"
        with mock.patch.object(chat_api.ChatClient, "download", return_value=packed):
            r = self.client.get(reverse("crm:chat_attachment"), {"r": "spaces/AAA/attachments/x"})
        self.assertEqual(b"".join(r.streaming_content), PNG)
        self.assertFalse(r.has_header("Content-Length"))
        self.assertTrue(packed.closed)  # the connection to Google is let go when the download ends

    def test_a_file_name_in_the_address_is_only_a_name(self):
        res = self.stored("a.png", "image/png", PNG)
        r = self.client.get(reverse("crm:chat_attachment"), {"r": res, "n": "..\\..\\etc/pass\r\nwd.png"})
        self.assertNotIn("..", r["Content-Disposition"])
        self.assertNotIn("\n", r["Content-Disposition"])
        self.assertIn("wd.png", r["Content-Disposition"])

    def test_a_file_that_is_not_there_or_not_allowed_is_not_found(self):
        for res in ("spaces/AAA/attachments/none", "../../x", ""):
            self.assertEqual(self.client.get(reverse("crm:chat_attachment"), {"r": res}).status_code, 404, res)
        self.conn.enabled = False
        self.conn.save()
        self.assertEqual(self.client.get(reverse("crm:chat_attachment"), {"r": self.stored("a.png", "image/png", PNG)}).status_code, 404)

    def test_when_chat_cannot_be_reached_the_file_says_so(self):
        res = self.stored("a.png", "image/png", PNG)
        self.fake.fail = error(500, "Backend Error")
        r = self.client.get(reverse("crm:chat_attachment"), {"r": res})
        self.assertEqual(r.status_code, 502)

    def test_starting_chats_over_http(self):
        r = self.client.post(reverse("crm:chat_new"), {"people": SIMON})
        self.assertEqual(r.json(), {"ok": True, "id": "BBB", "title": "Simon Shewmaker"})
        refused = self.client.post(reverse("crm:chat_new"), {"people": WILL})
        self.assertEqual((refused.status_code, refused.json()["needs_permission"]), (403, True))
        self.assertEqual(self.fake.setups, [])
        self.conn.can_create = True
        self.conn.save()
        made = self.client.post(reverse("crm:chat_new"), {"people": f"{SIMON},{WILL}", "name": ""})
        self.assertEqual(made.status_code, 200)
        self.assertEqual(self.fake.setups[-1]["space"]["spaceType"], "GROUP_CHAT")
        self.assertEqual(self.client.post(reverse("crm:chat_new"), {"people": "nope"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("crm:chat_new")).status_code, 405)

    def test_searching_over_http(self):
        self.fake.add_message("AAA", "the survey crew is booked", sender="users/2")
        d = self.client.get(reverse("crm:chat_search"), {"q": "survey"}).json()
        self.assertEqual(d["count"], 2)  # the first message of the conversation says survey too
        self.assertIn("<mark>survey</mark>", d["html"])
        self.assertIn("?s=AAA&amp;at=", d["html"])
        self.assertEqual(self.client.get(reverse("crm:chat_search"), {"q": "a"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("crm:chat_search"), {"q": "zzzz"}).json()["count"], 0)
        self.assertIn("No messages match", self.client.get(reverse("crm:chat_search"), {"q": "zzzz"}).json()["html"])

    def test_refreshing_a_conversation(self):
        first = self.post("chat_refresh", sig="").json()
        self.assertTrue(first["changed"])
        self.assertTrue(first["html"] and first["first"].startswith("spaces/AAA/messages/"))
        same = self.post("chat_refresh", sig=first["sig"]).json()
        self.assertEqual((same["changed"], same["sig"]), (False, first["sig"]))
        self.fake.add_reaction(self.theirs, "🎉", "users/3")
        cache.clear()
        again = self.post("chat_refresh", sig=first["sig"]).json()
        self.assertTrue(again["changed"])
        self.assertIn("🎉", again["html"])

    def test_the_new_calls_are_for_signed_in_team_members_with_chat_on(self):
        calls = [("get", "chat_reactions"), ("post", "chat_react"), ("post", "chat_edit"), ("post", "chat_delete"), ("post", "chat_upload"),
                 ("get", "chat_attachment"), ("post", "chat_new"), ("get", "chat_search"), ("post", "chat_refresh")]
        self.client.logout()
        for method, name in calls:
            self.assertNotEqual(getattr(self.client, method)(reverse(f"crm:{name}"), {"s": "AAA"}).status_code, 200, name)
        partner = User.objects.create_user("p", password="x", email="p@bank.com")
        partner.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(partner)
        for method, name in calls:
            self.assertNotEqual(getattr(self.client, method)(reverse(f"crm:{name}"), {"s": "AAA"}).status_code, 200, name)
        self.client.force_login(self.user)
        self.conn.enabled = False
        self.conn.save()
        for method, name in calls:
            if name != "chat_attachment":
                self.assertEqual(getattr(self.client, method)(reverse(f"crm:{name}"), {"s": "AAA"}).status_code, 403, name)
        self.assertEqual(self.fake.uploaded, [])

    def test_the_page_and_the_dock_can_start_a_chat_and_search(self):
        User.objects.create_user("simon", email=SIMON, first_name="Simon", last_name="Shewmaker", password="x")
        for url in (reverse("crm:chat"), reverse("crm:chat_list")):
            page = self.client.get(url)
            for needle in ("data-new-chat", "data-new-dialog", "data-search-hint", "data-results", reverse("crm:chat_search"), reverse("crm:chat_new"),
                           'value="sshewmaker@acmigroup.com"', "Allow starting chats", "emoji.js"):
                self.assertContains(page, needle, msg_prefix=url)
            self.assertNotContains(page, f'value="{ME}"', msg_prefix=url)  # not the person themself
        self.conn.can_create = True
        self.conn.save()
        self.assertNotContains(self.client.get(reverse("crm:chat")), "Allow starting chats")
        self.assertContains(self.client.get(reverse("crm:chat_window"), {"s": "AAA"}), "emoji.js")


def service_around():
    from crm.chat import views
    return views.AROUND


class PermissionTests(ChatTestCase):
    def test_starting_conversations_is_a_permission_the_person_may_leave_out(self):
        self.assertIn(connect.SCOPE_CREATE, connect.CHAT_SCOPES)
        self.assertNotIn(connect.SCOPE_CREATE, connect.NEEDED)  # opening and answering chats works without it
        self.assertTrue(connect.finish(self.user, FakeGoogleToken()).can_create)
        without = " ".join(["openid", *connect.NEEDED])
        self.assertFalse(connect.finish(self.user, FakeGoogleToken(without)).can_create)
        self.assertTrue(connect.is_on(self.user))
