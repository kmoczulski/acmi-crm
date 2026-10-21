from datetime import timedelta
from unittest.mock import patch
from email.message import EmailMessage
from email.utils import format_datetime

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from crm.mail import sync
from crm.mail.imap import MailboxError, _meta
from crm.mail.parse import new_text, parse_message
from crm.models import (
    Activity, Company, CompanyStatus, EmailTemplate, FieldHistory, MailFilter, Mailbox, Person, UnmatchedEmail,
)

ME = "kmoczulski@acmigroup.com"


def raw_email(frm, to, subject, body="Hello", cc=None, msg_id=None, in_reply_to=None, when=None, headers=None):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Subject"] = subject
    m["Date"] = format_datetime(when or (timezone.now() - timedelta(days=1)))
    m["Message-ID"] = msg_id or f"<{abs(hash((frm, to, subject, body)))}@test>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    return m.as_bytes()


def gmail():
    """Email activities only (stage changes add their own timeline events)."""
    return Activity.objects.filter(source=Activity.Source.GMAIL)


class FakeClient:
    """Stands in for ImapClient: a list of (uid, thread_id, raw bytes)."""

    def __init__(self, messages, uid_validity="1", fail=None):
        self.messages = messages
        self.uid_validity = uid_validity
        self.fail = fail

    def __enter__(self):
        if self.fail:
            raise self.fail
        return self

    def __exit__(self, *exc):
        return False

    def search(self, since=None, after_uid=0):
        return sorted(uid for uid, _, _ in self.messages if uid > after_uid)

    def fetch(self, uids):
        wanted = set(uids)
        return [parse_message(raw, uid=uid, thread_id=thr) for uid, thr, raw in self.messages if uid in wanted]


class MailSyncTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="kellen", email=ME)
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME)
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        self.person = Person.objects.create(
            first_name="Will", last_name="Edwards", email="will@firehawkaerospace.com", company=self.company,
        )

    def run_sync(self, messages, mailbox=None, **kw):
        return sync.sync_mailbox(mailbox or self.mailbox, client=FakeClient(messages, **kw))

    def test_sent_email_logs_activity_and_advances_stage(self):
        stats = self.run_sync([(1, "t1", raw_email(ME, "Will <will@firehawkaerospace.com>", "PDA for land search"))])
        act = Activity.objects.get(source=Activity.Source.GMAIL)
        self.assertEqual(act.kind, Activity.Kind.EMAIL_SENT)
        self.assertEqual(act.company, self.company)
        self.assertEqual(act.person, self.person)
        self.assertEqual(act.actor, self.user)
        self.assertIn("PDA for land search", act.summary)
        self.company.refresh_from_db()
        self.person.refresh_from_db()
        self.assertEqual(self.company.status, CompanyStatus.CONTACTED)
        self.assertEqual(self.person.touch_count, 1)
        self.assertIsNotNone(self.company.last_interaction_at)
        self.assertTrue(FieldHistory.objects.filter(field="status", new_value="Contacted").exists())
        self.assertEqual(stats["company stages advanced"], 1)

    def test_reply_in_our_thread_is_responded_and_engages(self):
        sent_id = "<sent-1@acmigroup.com>"
        self.run_sync([
            (1, "t1", raw_email(ME, "will@firehawkaerospace.com", "PDA", msg_id=sent_id)),
            (2, "t1", raw_email("will@firehawkaerospace.com", ME, "Re: PDA", body="Sounds good, send the draft.",
                                in_reply_to=sent_id)),
        ])
        reply = gmail().get(kind=Activity.Kind.REPLY)
        self.assertEqual(reply.outcome, "Responded")
        self.company.refresh_from_db()
        self.person.refresh_from_db()
        self.assertEqual(self.company.status, CompanyStatus.ENGAGED)
        self.assertEqual(self.person.last_response, "Responded")

    def test_not_interested_only_counts_new_text(self):
        sent_id = "<sent-2@acmigroup.com>"
        quoted = "Thanks, let's talk Tuesday.\n\nOn Mon, Oct 12, 2026 Kellen wrote:\n> reply STOP to unsubscribe"
        self.run_sync([
            (1, "t2", raw_email(ME, "will@firehawkaerospace.com", "Intro", msg_id=sent_id)),
            (2, "t2", raw_email("will@firehawkaerospace.com", ME, "Re: Intro", body=quoted, in_reply_to=sent_id)),
            (3, "t2", raw_email("will@firehawkaerospace.com", ME, "Re: Intro", body="Please remove me from your list.",
                                in_reply_to=sent_id, msg_id="<r3@x>")),
        ])
        outcomes = list(Activity.objects.filter(kind=Activity.Kind.REPLY).order_by("id").values_list("outcome", flat=True))
        self.assertEqual(outcomes, ["Responded", "Not Interested"])

    def test_reply_to_workbook_template_subject_counts(self):
        EmailTemplate.objects.create(template_id="T001", name="x", subject="Upcoming Groundbreakings - ACMI Properties")
        self.run_sync([(1, "t9", raw_email("will@firehawkaerospace.com", ME,
                                           "RE: Upcoming Groundbreakings - ACMI Properties", body="Interested.",
                                           in_reply_to="<old@workbook>"))])
        self.assertEqual(gmail().get().kind, Activity.Kind.REPLY)

    def test_inbound_without_our_thread_is_received(self):
        self.run_sync([(1, "t3", raw_email("will@firehawkaerospace.com", ME, "Quick question"))])
        self.assertEqual(gmail().get().kind, Activity.Kind.EMAIL_RECEIVED)

    def test_bounce_marks_email_invalid(self):
        self.run_sync([(1, "t4", raw_email("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", ME,
                                           "Delivery Status Notification (Failure)", body="Address not found",
                                           headers={"X-Failed-Recipients": "will@firehawkaerospace.com"}))])
        act = gmail().get()
        self.assertEqual((act.kind, act.outcome), (Activity.Kind.BOUNCE, "Undeliverable"))
        self.person.refresh_from_db()
        self.assertFalse(self.person.email_valid)

    def test_bounce_address_from_body(self):
        self.run_sync([(1, "t5", raw_email("postmaster@outlook.com", ME, "Undeliverable: Hello",
                                           body="Your message wasn't delivered to will@firehawkaerospace.com because"))])
        self.assertEqual(gmail().get().person, self.person)

    def test_out_of_office_is_background(self):
        self.run_sync([(1, "t6", raw_email("will@firehawkaerospace.com", ME, "Automatic reply: PDA",
                                           headers={"Auto-Submitted": "auto-replied"}))])
        act = gmail().get()
        self.assertEqual(act.kind, Activity.Kind.AUTO_REPLY)
        self.assertTrue(act.is_background)

    def test_internal_protected_and_newsletters_are_skipped(self):
        MailFilter.objects.create(pattern="secret.com", kind=MailFilter.Kind.PROTECT)
        self.run_sync([
            (1, "a", raw_email(ME, "simon@acmigroup.com", "Internal")),
            (2, "b", raw_email(ME, "ceo@secret.com", "Private")),
            (3, "c", raw_email("news@bigvendor.com", ME, "Weekly digest", headers={"List-Unsubscribe": "<mailto:x>"})),
        ])
        self.assertFalse(gmail().exists())
        self.assertFalse(UnmatchedEmail.objects.exists())

    def test_known_domain_creates_person(self):
        self.run_sync([(1, "t7", raw_email(ME, '"Doe, Jane" <jane@firehawkaerospace.com>', "Site tour"))])
        jane = Person.objects.get(email="jane@firehawkaerospace.com")
        self.assertEqual((jane.first_name, jane.last_name, jane.company), ("Jane", "Doe", self.company))

    def test_one_activity_per_company_per_email(self):
        self.run_sync([(1, "t8", raw_email(ME, "will@firehawkaerospace.com", "Group",
                                           cc="ops@firehawkaerospace.com"))])
        self.assertEqual(gmail().count(), 1)

    def test_same_email_in_two_mailboxes_logged_once(self):
        other = Mailbox.objects.create(user=get_user_model().objects.create(username="simon"), email="simon@acmigroup.com")
        raw = raw_email(ME, "will@firehawkaerospace.com", "Shared", cc="simon@acmigroup.com", msg_id="<shared@x>")
        self.run_sync([(1, "t", raw)])
        self.run_sync([(1, "t", raw)], mailbox=other)
        self.assertEqual(gmail().count(), 1)

    def test_unknown_domain_goes_to_review_then_approve(self):
        self.run_sync([
            (1, "u1", raw_email(ME, "Ann Lee <ann@newco.com>", "Hello Newco")),
            (2, "u2", raw_email("ann@newco.com", ME, "Re: Hello Newco", in_reply_to="<nope>")),
            (3, "u3", raw_email("friend@gmail.com", ME, "Dinner?")),
        ])
        self.assertEqual(UnmatchedEmail.objects.filter(domain="newco.com").count(), 2)
        self.assertFalse(UnmatchedEmail.objects.filter(domain="gmail.com").exists())
        queue = sync.review_queue()
        self.assertEqual(queue[0]["domain"], "newco.com")

        company, stats = sync.approve_domain("newco.com", company_name="Newco Inc", user=self.user)
        self.assertEqual(company.domains, ["newco.com"])
        self.assertEqual(stats["activities created"], 2)
        self.assertEqual(Person.objects.get(email="ann@newco.com").full_name, "Ann Lee")
        self.assertFalse(UnmatchedEmail.objects.exists())
        company.refresh_from_db()
        self.assertEqual(company.touch_count, 1)

    def test_domain_spelling_company_name_matches(self):
        Company.objects.create(name="Kraken Robotics, Inc")
        self.run_sync([(1, "k", raw_email(ME, "ann@krakenroboticsinc.com", "Hi"))])
        self.assertEqual(gmail().get().company.name, "Kraken Robotics, Inc")
        self.assertIn("krakenroboticsinc.com", Company.objects.get(name="Kraken Robotics, Inc").domains)

    def test_auto_resolve_existing_queue(self):
        self.run_sync([(1, "q", raw_email(ME, "bob@nomadco.com", "Hi"))])
        self.assertTrue(UnmatchedEmail.objects.exists())
        Company.objects.create(name="Nomad Co")
        self.assertEqual(sync.auto_resolve_queue(), {"nomadco.com": "Nomad Co"})
        self.assertFalse(UnmatchedEmail.objects.exists())

    def test_reply_recognised_after_domain_approved(self):
        self.run_sync([
            (1, "th", raw_email(ME, "ann@latecomer.com", "ACMI <> Latecomer", msg_id="<s1@x>")),
            (2, "th", raw_email("ann@latecomer.com", ME, "Re: ACMI <> Latecomer", body="Yes, let's meet.",
                                in_reply_to="<s1@x>")),
        ])
        company, _ = sync.approve_domain("latecomer.com", company_name="Latecomer")
        reply = gmail().get(kind=Activity.Kind.REPLY)
        self.assertEqual((reply.outcome, reply.summary[:10]), ("Responded", "Reply from"))
        company.refresh_from_db()
        self.assertEqual(company.status, CompanyStatus.ENGAGED)
        self.assertEqual(company.last_response, "Responded")

    def test_approve_attaches_domain_to_existing_company(self):
        self.run_sync([(1, "u", raw_email(ME, "x@firehawk-labs.com", "Hi"))])
        sync.approve_domain("firehawk-labs.com", company=self.company)
        self.company.refresh_from_db()
        self.assertIn("firehawk-labs.com", self.company.domains)

    def test_dismiss_creates_filter(self):
        self.run_sync([(1, "u", raw_email(ME, "lawyer@lawfirm.com", "Contract"))])
        sync.dismiss_domain("lawfirm.com")
        self.assertFalse(UnmatchedEmail.objects.exists())
        self.run_sync([(2, "v", raw_email(ME, "lawyer@lawfirm.com", "Contract 2"))])
        self.assertFalse(UnmatchedEmail.objects.exists())

    def test_resumes_from_last_uid_and_is_idempotent(self):
        msgs = [(i, f"t{i}", raw_email(ME, "will@firehawkaerospace.com", f"Note {i}")) for i in range(1, 6)]
        stats = sync.sync_mailbox(self.mailbox, client=FakeClient(msgs), max_messages=3)
        self.mailbox.refresh_from_db()
        self.assertEqual((self.mailbox.last_uid, self.mailbox.backfill_done), (3, False))
        self.assertEqual(stats["messages remaining"], 2)
        sync.sync_mailbox(self.mailbox, client=FakeClient(msgs), max_messages=3)
        self.mailbox.refresh_from_db()
        self.assertEqual((self.mailbox.last_uid, self.mailbox.backfill_done), (5, True))
        self.assertEqual(gmail().count(), 5)
        # Gmail renumbering the folder replays everything without duplicates.
        sync.sync_mailbox(self.mailbox, client=FakeClient(msgs, uid_validity="2"))
        self.assertEqual(gmail().count(), 5)

    def test_errors_are_saved_on_the_mailbox(self):
        sync.sync_mailbox(self.mailbox, client=FakeClient([], fail=MailboxError("bad password")))
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.last_error, "bad password")


class ParseTests(TestCase):
    def test_new_text_strips_quotes(self):
        self.assertEqual(new_text("Yes please.\nOn Tue, Kellen wrote:\n> old").strip(), "Yes please.")
        self.assertEqual(new_text("Ok\n-----Original Message-----\nFrom: x").strip(), "Ok")

    def test_html_only_body(self):
        m = EmailMessage()
        m["From"] = "a@b.com"
        m["Subject"] = "x"
        m.set_content("<p>Hello <b>there</b></p><style>p{}</style>", subtype="html")
        self.assertEqual(parse_message(m.as_bytes()).body, "Hello there")

    def test_imap_meta(self):
        uid, thr, size, labels = _meta(b'5 (UID 912 X-GM-THRID 1803 X-GM-LABELS ("\\\\Sent" \\Important) RFC822.SIZE 4410)')
        self.assertEqual((uid, thr, size), (912, "1803", 4410))
        self.assertIn("\\Important", labels)


class MailViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="kellen", email=ME)
        self.client.force_login(self.user)

    @patch.dict("os.environ", {"ACMI_IMAP_PASSWORD_KELLEN": ""})
    def test_mail_page_and_setup(self):
        self.assertEqual(self.client.get("/mail/").status_code, 200)
        self.client.post("/mail/setup/", {"email": ME, "backfill_days": "90"})
        mb = Mailbox.objects.get(user=self.user)
        self.assertEqual(mb.backfill_days, 90)
        page = self.client.get("/mail/")
        self.assertContains(page, "ACMI_IMAP_PASSWORD_KELLEN")

    def test_review_actions(self):
        mb = Mailbox.objects.create(user=self.user, email=ME)
        UnmatchedEmail.objects.create(mailbox=mb, message_id="<m>", occurred_at=timezone.now(), kind="email_sent",
                                      address="a@newco.com", domain="newco.com", subject="Hi")
        self.assertContains(self.client.get("/mail/"), "newco.com")
        self.client.post("/mail/review/approve/", {"domain": "newco.com", "name": "Newco"})
        self.assertTrue(Company.objects.filter(name="Newco", domains=["newco.com"]).exists())

    def test_bulk_review_actions(self):
        mb = Mailbox.objects.create(user=self.user, email=ME)
        for i, domain in enumerate(["cbre.com", "vendor.com", "kimley-horn.com", "secret.org"]):
            UnmatchedEmail.objects.create(mailbox=mb, message_id=f"<b{i}>", occurred_at=timezone.now(),
                                          kind="email_sent", address=f"x@{domain}", domain=domain, subject="Hi")
        Company.objects.create(name="CBRE")
        self.client.post("/mail/review/bulk/", {"action": "ignore", "domains": ["vendor.com"]})
        self.client.post("/mail/review/bulk/", {"action": "protect", "domains": ["secret.org"]})
        self.client.post("/mail/review/bulk/", {"action": "create", "domains": ["cbre.com", "kimley-horn.com"]})
        self.assertEqual(set(MailFilter.objects.values_list("pattern", "kind")),
                         {("vendor.com", "ignore"), ("secret.org", "protect")})
        self.assertEqual(Company.objects.get(name="CBRE").domains, ["cbre.com"])  # attached, not duplicated
        self.assertTrue(Company.objects.filter(name="Kimley Horn", domains=["kimley-horn.com"]).exists())
        self.assertFalse(UnmatchedEmail.objects.exists())

    @override_settings(CRON_SECRET="s3cret")
    def test_cron_endpoint_requires_secret(self):
        self.client.logout()
        self.assertEqual(self.client.post("/internal/sync-mail/").status_code, 403)
        self.assertEqual(self.client.post("/internal/sync-mail/", HTTP_X_CRON_SECRET="wrong").status_code, 403)
        self.assertEqual(self.client.post("/internal/sync-mail/", HTTP_X_CRON_SECRET="s3cret").status_code, 200)


class ImapFetchTests(TestCase):
    """ImapClient.fetch against a stub of imaplib's response shapes."""

    def test_headers_first_then_bodies_only_for_inbound_people(self):
        from crm.mail.imap import ImapClient

        sent = raw_email(ME, "will@firehawkaerospace.com", "Sent one", body="SENT BODY")
        inbound = raw_email("will@firehawkaerospace.com", ME, "Inbound", body="INBOUND BODY")
        news = raw_email("news@vendor.com", ME, "Digest", body="NEWS BODY", headers={"List-Id": "<x>"})
        msgs = {11: (sent, "\\Sent"), 12: (inbound, "\\Inbox"), 13: (news, "")}
        calls = []

        class Conn:
            def uid(self, cmd, uid_set, spec):
                calls.append((uid_set, spec))
                data = []
                for uid in [int(u) for u in uid_set.split(",")]:
                    raw, label = msgs[uid]
                    part = raw.split(b"\n\n")[0] + b"\n\n" if "HEADER" in spec else raw
                    head = f"1 (X-GM-THRID 7{uid} X-GM-LABELS ({label}) RFC822.SIZE {len(raw)} ".encode()
                    # Alternate where UID appears to cover both server styles.
                    if uid % 2:
                        data += [(head + f"UID {uid} BODY[] {{{len(part)}}}".encode(), part), b")"]
                    else:
                        data += [(head + f"BODY[] {{{len(part)}}}".encode(), part), f" UID {uid})".encode()]
                return "OK", data

        mb = Mailbox(user=get_user_model()(username="kellen"), email=ME)
        client = ImapClient(mb)
        client.conn = Conn()
        out = client.fetch([11, 12, 13])
        self.assertEqual([e.uid for e in out], [11, 12, 13])
        self.assertEqual(calls[1][0], "12")  # only the inbound person-to-person message got a body fetch
        by_uid = {e.uid: e for e in out}
        self.assertEqual(by_uid[12].body, "INBOUND BODY")
        self.assertEqual(by_uid[11].body, "")
        self.assertEqual(by_uid[12].thread_id, "712")
        self.assertIn(r"\Sent", by_uid[11].labels[0])

    def test_uses_fewer_connections_when_gmail_refuses_more(self):
        from crm.mail import imap

        mb = Mailbox(user=get_user_model()(username="kellen"), email=ME)
        client = imap.ImapClient(mb)
        client.conn = object()
        opened = []

        def connect():
            if len(opened) >= 2:
                raise imap.TooManyConnections("Too many simultaneous connections")
            opened.append(object())
            return opened[-1]

        client._connect = connect
        pool, size = client._connection_pool()
        self.assertEqual(size, 3)  # main + the 2 Gmail allowed
        self.assertEqual(pool.qsize(), 3)

    def test_dropped_connection_reconnects_and_retries(self):
        import imaplib
        from crm.mail import imap

        mb = Mailbox(user=get_user_model()(username="kellen"), email=ME)
        client = imap.ImapClient(mb)
        calls = {"n": 0}

        def flaky_chunk(conn, chunk):
            calls["n"] += 1
            if conn == "dead":
                raise imaplib.IMAP4.abort("socket error: EOF")
            return ["ok"]

        client._fetch_chunk = flaky_chunk
        client._connect = lambda: "fresh"
        with patch("crm.mail.imap.time.sleep"):
            result, conn = client._fetch_with_retry("dead", "1,2")
        self.assertEqual((result, conn, calls["n"]), (["ok"], "fresh", 2))


class SyncLockTests(TestCase):
    def test_second_sync_of_same_mailbox_is_refused(self):
        user = get_user_model().objects.create(username="kellen", email=ME)
        mb = Mailbox.objects.create(user=user, email=ME)
        Mailbox.objects.filter(pk=mb.pk).update(sync_lock_until=timezone.now() + timedelta(minutes=5))
        with self.assertRaises(sync.SyncBusy):
            sync.sync_mailbox(mb, client=FakeClient([]))
        # A stale lock (crashed run) does not block.
        Mailbox.objects.filter(pk=mb.pk).update(sync_lock_until=timezone.now() - timedelta(minutes=1))
        sync.sync_mailbox(mb, client=FakeClient([]))
        mb.refresh_from_db()
        self.assertIsNone(mb.sync_lock_until)


class NameTests(TestCase):
    def test_clean_name(self):
        from crm.names import clean_name, name_from_address

        self.assertEqual(clean_name("Doe, Jane", "jane@x.com"), ("Jane", "Doe", "Jane Doe"))
        self.assertEqual(clean_name("BEN ALLEN", "ben@x.com")[2], "Ben Allen")
        self.assertEqual(clean_name("Jane Doe (CBRE)", "j@x.com")[2], "Jane Doe")
        self.assertEqual(clean_name("tim mccarter", "t@x.com")[2], "Tim McCarter")
        self.assertIsNone(clean_name("jane@x.com", "jane@x.com"))
        self.assertIsNone(clean_name("Sales Team", "sales@x.com"))
        self.assertIsNone(clean_name("jdoe", "jdoe@x.com"))
        self.assertIsNone(clean_name("TBD", "x@y.com"))
        self.assertIsNone(clean_name("TBD TBD", "x@y.com"))
        self.assertEqual(name_from_address("tim.mccarter@1st-edge.com")[2], "Tim McCarter")
        self.assertIsNone(name_from_address("ppaulin@300below.com"))
        self.assertIsNone(name_from_address("sales.team@x.com"))
        self.assertIsNone(name_from_address("business.development@x.com"))
        self.assertIsNone(name_from_address("investor.relations@x.com"))

    def test_sync_fills_missing_name(self):
        user = get_user_model().objects.create(username="kellen", email=ME)
        mb = Mailbox.objects.create(user=user, email=ME)
        company = Company.objects.create(name="Firehawk", domains=["firehawkaerospace.com"])
        p = Person.objects.create(email="ben@firehawkaerospace.com", company=company)
        sync.sync_mailbox(mb, client=FakeClient([(1, "t", raw_email("Ben Allen <ben@firehawkaerospace.com>", ME, "Hi"))]))
        p.refresh_from_db()
        self.assertEqual(p.full_name, "Ben Allen")


class DeleteTests(TestCase):
    def setUp(self):
        from crm.tests.utils import make_manager

        self.user = make_manager(get_user_model().objects.create(username="kellen", email=ME))
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="Firehawk Aerospace")

    def test_delete_person_keeps_company_emails(self):
        p = Person.objects.create(email="x@f.com", company=self.company)
        Activity.objects.create(kind="email_sent", occurred_at=timezone.now(), summary="s", company=self.company,
                                person=p, source="gmail", source_id="m1")
        r = self.client.post(f"/people/{p.pk}/delete/")
        self.assertRedirects(r, f"/companies/{self.company.pk}/")
        self.assertFalse(Person.objects.filter(pk=p.pk).exists())
        self.assertTrue(Activity.objects.filter(source_id="m1", person=None, company=self.company).exists())

    def test_delete_company_optionally_with_people(self):
        Person.objects.create(email="y@f.com", company=self.company)
        self.client.post(f"/companies/{self.company.pk}/delete/", {"people": "delete"})
        self.assertFalse(Company.objects.exists())
        self.assertFalse(Person.objects.exists())
        self.assertEqual(self.client.get("/companies/", follow=True).status_code, 200)

    def test_person_page_has_delete_button(self):
        p = Person.objects.create(company=self.company)
        self.assertContains(self.client.get(f"/people/{p.pk}/"), "Delete person")


class TeamConnectionTests(TestCase):
    def test_teammates_and_strength(self):
        from crm.interactions import team_connections

        kellen = get_user_model().objects.create(username="kellen", first_name="Kellen", email=ME)
        simon = get_user_model().objects.create(username="simon", first_name="Simon", email="simon@acmigroup.com")
        mk = Mailbox.objects.create(user=kellen, email=ME)
        ms = Mailbox.objects.create(user=simon, email="simon@acmigroup.com")
        company = Company.objects.create(name="Firehawk", domains=["firehawkaerospace.com"])
        ben = Person.objects.create(email="ben@firehawkaerospace.com", full_name="Ben Allen", company=company)
        thread = [
            (1, "t", raw_email(ME, "ben@firehawkaerospace.com", "PDA", cc="simon@acmigroup.com", msg_id="<p1@x>")),
        ] + [
            (i, "t", raw_email("ben@firehawkaerospace.com", ME, f"Re: PDA {i}", in_reply_to="<p1@x>", msg_id=f"<r{i}@x>"))
            for i in range(2, 6)
        ]
        sync.sync_mailbox(mk, client=FakeClient(thread))
        sync.sync_mailbox(ms, client=FakeClient(thread[:1]))  # Simon was only cc'd on the first email

        team = team_connections(company=company)
        self.assertEqual([t["user"].username for t in team], ["kellen", "simon"])
        self.assertEqual((team[0]["strength"], team[0]["inbound"]), ("Strong", 4))
        self.assertEqual((team[1]["strength"], team[1]["sent"]), ("Weak", 1))
        self.assertEqual(team[0]["contacts"][0]["person__full_name"], "Ben Allen")
        company.refresh_from_db()
        self.assertEqual((company.strongest_connection_user, company.connection_strength), (kellen, "Strong"))
        self.assertEqual(gmail().filter(kind="email_sent").get().team_members.count(), 2)

        self.client.force_login(kellen)
        page = self.client.get(f"/companies/{company.pk}/?tab=activity")
        self.assertContains(page, "Team connections")
        self.assertContains(page, "Kellen, Simon →")  # sent email shows both teammates
