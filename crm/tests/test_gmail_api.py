"""Mail in the hub, step 2: the timeline sync reads Gmail through the Gmail API once someone's Mail is on."""
import base64
from datetime import timedelta
from email.utils import format_datetime

import requests
from allauth.socialaccount.models import SocialAccount, SocialToken
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import client as mail_client, gmail_api, sync
from crm.mail.parse import parse_message
from crm.models import Activity, Company, MailConnection, Mailbox, Notification, Person

from .test_mail import FakeClient, raw_email

ME = "kmoczulski@acmigroup.com"
GOOGLE = {"google": {"APP": {"client_id": "cid", "secret": "sec"}}}
ON = dict(SOCIALACCOUNT_PROVIDERS=GOOGLE, GOOGLE_SIGNIN_ENABLED=True, TOKEN_KEYS=[Fernet.generate_key().decode()],
          MAIL_CLIENT_ENABLED=True)


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def api_message(gid, thread_hex, frm, to, subject, body="Hello", when=None, labels=("INBOX",), msg_id=None,
                html=None, attachment=None, extra_headers=()):
    when = when or (timezone.now() - timedelta(hours=1))
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Date", "value": format_datetime(when)},
               {"name": "Message-ID", "value": msg_id or f"<{gid}@test>"}, *extra_headers]
    parts = [{"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=utf-8"}],
              "body": {"size": len(body), "data": b64(body)}}]
    if html:
        parts = [{"mimeType": "text/html", "body": {"data": b64(html)}}]
    if attachment:
        parts.append({"partId": "1", "mimeType": "application/pdf", "filename": attachment,
                      "body": {"size": 120000, "attachmentId": "att-1"}})
    return {"id": gid, "threadId": thread_hex, "labelIds": list(labels), "snippet": body[:40],
            "internalDate": str(int(when.timestamp() * 1000)),
            "payload": {"mimeType": "multipart/mixed" if attachment else "multipart/alternative", "headers": headers,
                        "parts": parts}}


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeGmail:
    """Stands in for `requests` against the Gmail API: a mailbox of messages, a profile and a change history."""

    def __init__(self, messages=(), email=ME, history_id="100"):
        self.messages = {m["id"]: m for m in messages}
        self.order = [m["id"] for m in messages]  # oldest first
        self.email, self.history_id = email, history_id
        self.history = []  # message ids added after the profile's marker
        self.history_status, self.status = 200, 200
        self.fail_on, self.calls, self.posts = set(), [], []
        self.sent, self.drafts, self.send_status = [], {}, 200
        self.send_as = [{"sendAsEmail": email, "isPrimary": True, "signature": ""}]
        self.draft_msgs = {}  # draft id -> a message (api_message shape), for the news review's drafts
        self.rewrite_message_id = False  # Gmail sometimes sends with a Message-ID of its own
        self.label_list = [{"id": "INBOX", "name": "INBOX", "type": "system"},
                           {"id": "SENT", "name": "SENT", "type": "system"},
                           {"id": "UNREAD", "name": "UNREAD", "type": "system"},
                           {"id": "Label_1", "name": "Deals/MEIH", "type": "user",
                            "color": {"backgroundColor": "#16a766", "textColor": "#ffffff"}}]
        self.inbox_unread = 0

    def _threads(self):
        """Threads newest first, each with its messages oldest first and a history id that moves with them."""
        by_thread = {}
        for gid in self.order:
            m = self.messages[gid]
            by_thread.setdefault(m["threadId"], []).append(m)
        out = [{"id": tid, "historyId": str(sum(len(m["labelIds"]) + 1 for m in ms) + len(ms) * 100),
                "messages": sorted(ms, key=lambda m: int(m["internalDate"]))} for tid, ms in by_thread.items()]
        return sorted(out, key=lambda t: -int(t["messages"][-1]["internalDate"]))

    def add(self, message):
        self.messages[message["id"]] = message
        self.order.append(message["id"])
        self.history.append(message["id"])
        self.history_id = str(int(self.history_id) + 1)

    def post(self, url, data=None, timeout=None, json=None, headers=None):
        if url.startswith(gmail_api.UPLOAD):  # sending: multipart/related, JSON metadata then the message
            return self._send(data)
        if not url.startswith(gmail_api.API):  # Google's token endpoint
            return FakeResponse({"access_token": "fresh", "expires_in": 3600})
        path = url.removeprefix(gmail_api.API)
        self.posts.append((path, json))
        if path == "/drafts":
            draft_id = f"r-{len(self.drafts) + 1}"
            self.drafts[draft_id] = json["message"]
            return FakeResponse({"id": draft_id, "message": {"id": f"dm{len(self.drafts)}"}})
        if path == "/labels":
            label = {"id": f"Label_{len(self.label_list) + 1}", "name": json["name"], "type": "user"}
            self.label_list.append(label)
            return FakeResponse(label)
        if path.startswith("/messages/") and path.endswith("/trash"):
            mid = path.split("/")[2]
            for m in [*self.messages.values(), *self.draft_msgs.values()]:
                if m["id"] == mid:
                    m["labelIds"] = [lb for lb in m["labelIds"] if lb != "DRAFT"] + ["TRASH"]
                    return FakeResponse({"id": mid})
            return FakeResponse({}, 404)
        tid, verb = path.removeprefix("/threads/").split("/")
        msgs = [m for m in self.messages.values() if m["threadId"] == tid]
        if not msgs:
            return FakeResponse({}, 404)
        add, remove = (json or {}).get("addLabelIds", []), (json or {}).get("removeLabelIds", [])
        if verb == "trash":
            add, remove = ["TRASH"], ["INBOX"]
        elif verb == "untrash":
            add, remove = ["INBOX"], ["TRASH"]
        for m in msgs:
            m["labelIds"] = [lb for lb in m["labelIds"] if lb not in remove] + [lb for lb in add if lb not in m["labelIds"]]
        return FakeResponse({"id": tid})

    def _send(self, data):
        import json as jsonlib

        meta_part, _, rest = data.partition(b"Content-Type: message/rfc822\r\n\r\n")
        meta = jsonlib.loads(meta_part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0])
        raw = rest.rsplit(b"\r\n--", 1)[0]
        self.sent.append((meta, raw))
        if self.send_status != 200:
            return FakeResponse({}, self.send_status)
        gid = f"{0x5e00 + len(self.sent):x}"
        thread = meta.get("threadId") or f"{0x7e00 + len(self.sent):x}"
        from email import message_from_bytes, policy

        parsed = message_from_bytes(raw, policy=policy.default)
        msg_id = f"<gmail-{gid}@mail.gmail.com>" if self.rewrite_message_id else str(parsed["Message-ID"])
        self.messages[gid] = api_message(gid, thread, str(parsed["From"]), str(parsed["To"] or ""),
                                         str(parsed["Subject"]), labels=("SENT",), msg_id=msg_id)
        self.order.append(gid)
        return FakeResponse({"id": gid, "threadId": thread, "labelIds": ["SENT"]})

    def put(self, url, json=None, headers=None, timeout=None):
        path = url.removeprefix(gmail_api.API)
        self.posts.append((f"PUT {path}", json))
        self.drafts[path.removeprefix("/drafts/")] = json["message"]
        return FakeResponse({"id": path.removeprefix("/drafts/")})

    def patch(self, url, json=None, headers=None, timeout=None):
        path = url.removeprefix(gmail_api.API)
        self.posts.append((f"PATCH {path}", json))
        label = next(lb for lb in self.label_list if lb["id"] == path.removeprefix("/labels/"))
        label["name"] = json["name"]
        return FakeResponse(label)

    def delete(self, url, headers=None, timeout=None):
        path = url.removeprefix(gmail_api.API)
        self.posts.append((f"DELETE {path}", None))
        self.drafts.pop(path.removeprefix("/drafts/"), None)
        return FakeResponse({}, 204)

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.removeprefix(gmail_api.API)
        self.calls.append((path, dict(params or {})))
        if self.status != 200:
            return FakeResponse({}, self.status)
        if path == "/profile":
            return FakeResponse({"emailAddress": self.email, "historyId": self.history_id})
        if path == "/messages":
            ids = list(reversed(self.order))  # newest first
            q = (params or {}).get("q", "")
            if q.startswith("rfc822msgid:"):
                want = q.removeprefix("rfc822msgid:").strip("<>")
                ids = [i for i in ids if any(h["name"] == "Message-ID" and h["value"].strip("<>") == want
                                             for h in self.messages[i]["payload"]["headers"])]
            return FakeResponse({"messages": [{"id": i} for i in ids]})
        if path == "/drafts":
            return FakeResponse({"drafts": [{"id": d, "message": {"id": m["id"]}} for d, m in self.draft_msgs.items()]})
        if path.startswith("/drafts/"):
            did = path.removeprefix("/drafts/")
            return FakeResponse({"id": did, "message": self.draft_msgs[did]}) if did in self.draft_msgs \
                else FakeResponse({}, 404)
        if path == "/settings/sendAs":
            return FakeResponse({"sendAs": self.send_as})
        if path == "/labels":
            return FakeResponse({"labels": self.label_list})
        if path.startswith("/labels/"):
            return FakeResponse({"id": path[8:], "threadsUnread": self.inbox_unread})
        if path == "/threads":
            threads = self._threads()
            if params and params.get("labelIds"):
                threads = [t for t in threads if any(params["labelIds"] in m["labelIds"] for m in t["messages"])]
            if params and params.get("q"):
                word = params["q"].lower().removeprefix("subject:").strip('"')
                threads = [t for t in threads if any(word in str(m).lower() for m in t["messages"])]
            return FakeResponse({"threads": [{"id": t["id"], "historyId": t["historyId"],
                                              "snippet": t["messages"][-1]["snippet"]} for t in threads],
                                 "resultSizeEstimate": len(threads)})
        if path.startswith("/threads/"):
            t = next((t for t in self._threads() if t["id"] == path[9:]), None)
            return FakeResponse(t) if t else FakeResponse({}, 404)
        if "/attachments/" in path:
            return FakeResponse({"data": base64.urlsafe_b64encode(b"%PDF-1.4 fake").decode()})
        if path == "/history":
            if self.history_status != 200:
                return FakeResponse({}, self.history_status)
            return FakeResponse({"history": [{"messagesAdded": [{"message": {
                "id": i, "threadId": self.messages[i]["threadId"] if i in self.messages else ""}}]} for i in self.history],
                                 "historyId": self.history_id})
        gid = path.removeprefix("/messages/")
        if gid in self.fail_on:
            raise requests.ConnectionError("dropped")
        if gid not in self.messages:
            return FakeResponse({}, 404)
        return FakeResponse(self.messages[gid])


class ParseTests(TestCase):
    def test_thread_ids_match_imap(self):
        self.assertEqual(gmail_api.decimal_thread_id("18c2f0a1b2c3d4e5"), str(0x18c2f0a1b2c3d4e5))
        self.assertEqual(gmail_api.hex_thread_id(gmail_api.decimal_thread_id("18c2f0a1b2c3d4e5")), "18c2f0a1b2c3d4e5")
        self.assertEqual(gmail_api.decimal_thread_id(""), "")

    def test_a_message_parses_like_imap(self):
        msg = api_message("a1", "ff", "Will Edwards <will@firehawkaerospace.com>", f"Kellen <{ME}>",
                          "Re: PDA for land search", body="Sounds good", labels=("INBOX", "IMPORTANT", "Label_7"),
                          attachment="PDA - ACMI.pdf", extra_headers=[{"name": "In-Reply-To", "value": "<x@acmi>"}])
        e = gmail_api.parse_api_message(msg)
        self.assertEqual((e.from_addr, e.from_name), ("will@firehawkaerospace.com", "Will Edwards"))
        self.assertEqual(e.to, [("Kellen", ME)])
        self.assertEqual(e.thread_id, "255")
        self.assertEqual(e.message_id, "<a1@test>")
        self.assertEqual(e.in_reply_to, "<x@acmi>")
        self.assertEqual(e.labels, ["\\Inbox", "\\Important", "Label_7"])
        self.assertEqual(e.body, "Sounds good")
        self.assertEqual(e.attachments, [("PDA - ACMI.pdf", 120000, "application/pdf")])

    def test_html_only_mail_and_non_ascii_names(self):
        msg = api_message("a2", "1", "José Núñez <jose@firehawkaerospace.com>", ME, "Café",
                          html="<p>Hola <b>mundo</b></p><style>p{}</style>")
        e = gmail_api.parse_api_message(msg)
        self.assertEqual(e.from_name, "José Núñez")
        self.assertEqual(e.subject, "Café")
        self.assertEqual(e.body, "Hola mundo")

    def test_chat_messages_are_skipped(self):
        self.assertIsNone(gmail_api.parse_api_message(api_message("a3", "1", "x@y.com", ME, "hi", labels=("CHAT",))))


@override_settings(**ON)
class GmailSyncTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="kellen", email=ME)
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME, backfill_done=True,
                                              last_synced_at=timezone.now() - timedelta(hours=1))
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        Person.objects.create(first_name="Will", email="will@firehawkaerospace.com", company=self.company)

    def turn_mail_on(self):
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1", extra_data={"email": ME})
        SocialToken.objects.create(account=account, token="a", token_secret="r",
                                   expires_at=timezone.now() + timedelta(hours=1))
        MailConnection.objects.create(user=self.user, enabled=True, google_email=ME)

    def sync(self, fake, **kw):
        stats = sync.sync_mailbox(self.mailbox, client=gmail_api.GmailClient(self.user, fake), **kw)
        self.mailbox.refresh_from_db()
        return stats

    def emails(self):
        return Activity.objects.filter(source=Activity.Source.GMAIL)

    def test_mail_on_switches_the_mailbox_to_the_api_and_off_switches_it_back(self):
        sync.choose_backend(self.mailbox)
        self.assertEqual(self.mailbox.backend, Mailbox.Backend.IMAP)
        self.turn_mail_on()
        sync.choose_backend(self.mailbox)
        self.assertEqual((self.mailbox.backend, self.mailbox.history_id), (Mailbox.Backend.GMAIL_API, ""))
        MailConnection.objects.filter(user=self.user).update(enabled=False)
        self.mailbox.history_id = "55"
        sync.choose_backend(self.mailbox)
        self.assertEqual((self.mailbox.backend, self.mailbox.history_id), (Mailbox.Backend.IMAP, ""))

    def test_a_mailbox_for_another_address_stays_on_imap(self):
        self.turn_mail_on()
        self.mailbox.email = "deals@acmigroup.com"
        sync.choose_backend(self.mailbox)
        self.assertEqual(self.mailbox.backend, Mailbox.Backend.IMAP)

    def test_switching_from_imap_logs_nothing_twice(self):
        """The same mail read by IMAP and then by the API: one timeline entry each, same thread id."""
        when = timezone.now() - timedelta(hours=3)
        thread_hex = "18c2f0a1b2c3d4e5"
        imap_messages = [
            (1, gmail_api.decimal_thread_id(thread_hex),
             raw_email(ME, "Will <will@firehawkaerospace.com>", "PDA for land search", msg_id="<sent-1@acmi>", when=when)),
            (2, gmail_api.decimal_thread_id(thread_hex),
             raw_email("Will <will@firehawkaerospace.com>", ME, "Re: PDA for land search", body="Yes, send it over",
                       msg_id="<reply-1@fh>", in_reply_to="<sent-1@acmi>", when=when + timedelta(hours=1))),
        ]
        sync.sync_mailbox(self.mailbox, client=FakeClient(imap_messages))
        before = list(self.emails().order_by("pk").values_list("source_id", "kind", "thread_id"))
        self.assertEqual(len(before), 2)

        self.turn_mail_on()
        fake = FakeGmail([
            api_message("a1", thread_hex, ME, "Will <will@firehawkaerospace.com>", "PDA for land search",
                        when=when, labels=("SENT",), msg_id="<sent-1@acmi>"),
            api_message("a2", thread_hex, "Will <will@firehawkaerospace.com>", ME, "Re: PDA for land search",
                        body="Yes, send it over", when=when + timedelta(hours=1), msg_id="<reply-1@fh>",
                        extra_headers=[{"name": "In-Reply-To", "value": "<sent-1@acmi>"}]),
            api_message("a3", "abc", "Will <will@firehawkaerospace.com>", ME, "Site visit Tuesday",
                        msg_id="<new-1@fh>"),
        ])
        stats = self.sync(fake)
        self.assertEqual(self.mailbox.backend, Mailbox.Backend.GMAIL_API)
        self.assertEqual(stats["activities already logged"], 2)
        self.assertEqual(stats["activities created"], 1)
        after = list(self.emails().order_by("pk").values_list("source_id", "kind", "thread_id"))
        self.assertEqual(after[:2], before)
        self.assertEqual(after[2][2], str(0xabc))
        # The back-read started a day before the last IMAP sync and took Gmail's marker.
        query = next(p for path, p in fake.calls if path == "/messages")["q"]
        self.assertEqual(query, f"after:{timezone.localdate(timezone.now() - timedelta(hours=1)) - timedelta(days=1):%Y/%m/%d}")
        self.assertEqual(self.mailbox.history_id, "100")
        self.assertEqual(self.mailbox.last_error, "")
        # A back-read never raises new-mail alerts.
        self.assertFalse(Notification.objects.filter(kind=Notification.Kind.MAIL).exists())

    def test_later_runs_read_only_what_changed_and_alert_for_new_inbox_mail(self):
        self.turn_mail_on()
        fake = FakeGmail([api_message("a1", "1", "Will <will@firehawkaerospace.com>", ME, "Old one")])
        self.sync(fake)
        fake.add(api_message("a2", "2", "Will <will@firehawkaerospace.com>", ME, "Tour Thursday?"))
        fake.add(api_message("a3", "3", "Deals <news@firehawkaerospace.com>", ME, "Newsletter",
                             labels=("INBOX", "CATEGORY_PROMOTIONS")))
        fake.add(api_message("a4", "4", ME, "Will <will@firehawkaerospace.com>", "Sure", labels=("SENT",)))
        fake.calls.clear()
        stats = self.sync(fake)
        self.assertEqual([p for p, _ in fake.calls if p.startswith(("/history", "/messages", "/profile"))
                          and not p.startswith("/messages/")], ["/history"])
        self.assertEqual(stats["activities created"], 2)  # the newsletter is skipped, as with IMAP
        self.assertEqual(self.mailbox.history_id, "103")
        alerts = Notification.objects.filter(user=self.user, kind=Notification.Kind.MAIL)
        self.assertEqual(alerts.count(), 1)
        self.assertEqual(alerts.get().text, "Will: Tour Thursday?")
        self.assertEqual(alerts.get().url, f"{reverse('crm:inbox')}?t=2")
        # The same messages again (a repeated history page) neither log nor alert twice.
        fake.history = ["a2"]
        self.sync(fake)
        self.assertEqual(alerts.count(), 1)

    def test_an_expired_marker_reads_again_by_date(self):
        self.turn_mail_on()
        fake = FakeGmail([api_message("a1", "1", "Will <will@firehawkaerospace.com>", ME, "One")])
        self.sync(fake)
        fake.history_status = 404
        fake.add(api_message("a2", "2", "Will <will@firehawkaerospace.com>", ME, "Two"))
        stats = self.sync(fake)
        self.assertEqual(stats["activities created"], 1)
        self.assertEqual(stats["activities already logged"], 1)
        self.assertEqual(self.mailbox.last_error, "")

    def test_a_big_back_read_is_worked_through_in_batches(self):
        self.turn_mail_on()
        fake = FakeGmail([api_message(f"b{i}", str(i), "Will <will@firehawkaerospace.com>", ME, f"Message {i}",
                                      when=timezone.now() - timedelta(hours=10 - i)) for i in range(5)])
        stats = self.sync(fake, max_messages=2)
        self.assertEqual(stats["messages remaining"], 3)
        self.assertFalse(self.mailbox.backfill_done)
        self.assertEqual(self.mailbox.pending_ids, ["b2", "b3", "b4"])
        self.assertEqual(list(self.emails().order_by("occurred_at").values_list("summary", flat=True)),
                         ["Email from Will: Message 0", "Email from Will: Message 1"])
        self.sync(fake, max_messages=2)
        self.sync(fake, max_messages=2)
        self.assertTrue(self.mailbox.backfill_done)
        self.assertEqual(self.emails().count(), 5)

    def test_a_failure_part_way_keeps_the_queue(self):
        self.turn_mail_on()
        fake = FakeGmail([api_message(f"b{i}", str(i), "Will <will@firehawkaerospace.com>", ME, f"M{i}")
                          for i in range(3)])
        fake.fail_on = {"b1"}
        self.sync(fake)
        self.assertIn("ConnectionError", self.mailbox.last_error)
        self.assertEqual(self.mailbox.pending_ids, ["b0", "b1", "b2"])
        self.assertEqual(self.emails().count(), 0)
        fake.fail_on = set()
        self.sync(fake)
        self.assertEqual(self.emails().count(), 3)
        self.assertEqual(self.mailbox.pending_ids, [])

    def test_gmail_access_removed_says_what_to_do(self):
        self.turn_mail_on()
        fake = FakeGmail()
        fake.status = 403
        self.sync(fake)
        self.assertIn("Turn on Mail again", self.mailbox.last_error)

    def test_another_google_account_is_refused(self):
        self.turn_mail_on()
        self.sync(FakeGmail(email="someone@acmigroup.com"))
        self.assertIn("someone@acmigroup.com", self.mailbox.last_error)
        self.assertEqual(self.mailbox.history_id, "")

    def test_mail_page_says_how_the_mailbox_is_read(self):
        self.turn_mail_on()
        self.mailbox.backend = Mailbox.Backend.GMAIL_API
        self.mailbox.save()
        self.client.force_login(self.user)
        r = self.client.get(reverse("crm:mail"))
        self.assertContains(r, "read through")
        self.assertNotContains(r, "add your Gmail app password")


@override_settings(**ON)
class NewMailAlertTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="kellen", email=ME)
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME)
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1")
        SocialToken.objects.create(account=account, token="a", token_secret="r")

    def email(self, **kw):
        m = api_message("c1", "1f", kw.pop("frm", "Will <will@x.com>"), ME, "Hello", **kw)
        return gmail_api.parse_api_message(m)

    def test_only_recent_primary_inbox_mail_from_someone_else_alerts(self):
        now = timezone.now()
        self.assertTrue(mail_client.wants_alert(self.mailbox, self.email(), now))
        self.assertFalse(mail_client.wants_alert(self.mailbox, self.email(labels=("INBOX", "CATEGORY_UPDATES")), now))
        self.assertTrue(mail_client.wants_alert(self.mailbox, self.email(labels=("INBOX", "CATEGORY_PERSONAL")), now))
        self.assertFalse(mail_client.wants_alert(self.mailbox, self.email(labels=("Label_1",)), now))
        self.assertFalse(mail_client.wants_alert(self.mailbox, self.email(labels=("INBOX", "SPAM")), now))
        self.assertFalse(mail_client.wants_alert(self.mailbox, self.email(frm=ME), now))
        self.assertFalse(mail_client.wants_alert(self.mailbox, self.email(when=now - timedelta(days=3)), now))

    def test_nothing_while_mail_is_off(self):
        self.assertEqual(mail_client.on_new_mail(self.mailbox, [self.email()]), 0)
        MailConnection.objects.create(user=self.user, enabled=True)
        self.assertEqual(mail_client.on_new_mail(self.mailbox, [self.email()]), 1)

    def test_an_alert_opens_gmail_but_no_other_outside_site(self):
        self.client.force_login(self.user)
        gmail = Notification.objects.create(user=self.user, kind=Notification.Kind.MAIL,
                                            url="https://mail.google.com/mail/u/x/#all/1f")
        other = Notification.objects.create(user=self.user, kind=Notification.Kind.MAIL, url="https://evil.example/")
        sneaky = Notification.objects.create(user=self.user, kind=Notification.Kind.MAIL, url="//evil.example/")
        r = self.client.get(reverse("crm:notification_open", args=[gmail.pk]))
        self.assertEqual(r["Location"], gmail.url)
        for n in (other, sneaky):
            r = self.client.get(reverse("crm:notification_open", args=[n.pk]))
            self.assertEqual(r["Location"], reverse("crm:notification_list"))


class RateLimitTests(TestCase):
    """Gmail says "slow down" with a 403 too; that's a wait-and-retry, not a lost permission."""

    def setUp(self):
        self.user = get_user_model().objects.create(username="kellen", email=ME)
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1")
        SocialToken.objects.create(account=account, token="a", token_secret="r",
                                   expires_at=timezone.now() + timedelta(hours=1))

    def session(self, *responses):
        answers = list(responses)
        return type("S", (), {"get": lambda self, *a, **k: answers.pop(0), "post": lambda self, *a, **k: None})()

    def limited(self):
        return FakeResponse({"error": {"code": 403, "errors": [{"reason": "userRateLimitExceeded"}]}}, 403)

    def test_rate_limit_is_retried(self):
        from unittest import mock

        client = gmail_api.GmailClient(self.user, self.session(self.limited(), FakeResponse({"ok": 1})))
        with mock.patch.object(gmail_api.time, "sleep") as sleep:
            self.assertEqual(client.get("/profile"), {"ok": 1})
        sleep.assert_called_once()

    def test_a_real_refusal_says_turn_on_mail_again(self):
        refused = FakeResponse({"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}}, 403)
        client = gmail_api.GmailClient(self.user, self.session(refused))
        with self.assertRaisesMessage(gmail_api.MailboxError, "Turn on Mail again"):
            client.get("/profile")

    def test_still_busy_after_retries_waits_for_the_next_sync(self):
        from unittest import mock

        client = gmail_api.GmailClient(self.user, self.session(*[self.limited() for _ in range(gmail_api.RETRIES + 1)]))
        with mock.patch.object(gmail_api.time, "sleep"):
            with self.assertRaisesMessage(gmail_api.MailboxError, "Gmail is busy"):
                client.get("/profile")
