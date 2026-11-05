"""Follow-ups never wait on a meeting invite we sent, whichever way the calendar program wrote it (Kellen, 2026-11-05)."""
from datetime import timedelta
from email.utils import format_datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from crm import pipeline
from crm.mail.parse import parse_message
from crm.mail.sync import CALENDAR_INVITE, process_messages
from crm.models import Activity, Company, Mailbox, Person

User = get_user_model()

ICS = ("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:x1@acmi\r\nDTSTART:20261120T150000Z\r\n"
       "DTEND:20261120T160000Z\r\nSUMMARY:PDA review\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def sent_email(subject, calendar, days_ago=6, thread="777", msg_id="<m1@acmigroup.com>"):
    when = format_datetime(timezone.now() - timedelta(days=days_ago))
    parts = ("--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSee you then.\r\n"
             + (f"--b\r\nContent-Type: text/calendar; charset=utf-8; method=REQUEST\r\n\r\n{ICS}" if calendar else "")
             + "--b--\r\n")
    raw = ("From: Kellen Moczulski <kmoczulski@acmigroup.com>\r\nTo: Will Edwards <will@firehawkaerospace.com>\r\n"
           f"Subject: {subject}\r\nDate: {when}\r\nMessage-ID: {msg_id}\r\nMIME-Version: 1.0\r\n"
           f'Content-Type: multipart/mixed; boundary="b"\r\n\r\n{parts}').encode()
    return parse_message(raw, uid=1, thread_id=thread, labels=["\\Sent"])


class InviteFollowUpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", email="kmoczulski@acmigroup.com", first_name="Kellen")
        self.mailbox = Mailbox.objects.create(user=self.user, email="kmoczulski@acmigroup.com")
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"], owner=self.user)
        Person.objects.create(first_name="Will", last_name="Edwards", email="will@firehawkaerospace.com", company=self.company)

    def log(self, *emails):
        rec = process_messages(self.mailbox, list(emails))
        rec.finish()

    def waiting(self):
        return [e["act"] for e in pipeline.follow_ups(self.user)["them"]]

    def test_an_invite_with_only_a_calendar_part_is_marked_and_never_waited_on(self):
        """Outlook / Apple invites: a plain title as the subject and no file name on the calendar part."""
        self.log(sent_email("PDA review", calendar=True))
        act = Activity.objects.get(kind=Activity.Kind.EMAIL_SENT)
        self.assertEqual(act.outcome, CALENDAR_INVITE)
        self.assertEqual(self.waiting(), [])

    def test_a_google_invitation_subject_is_marked_too(self):
        self.log(sent_email("Invitation: PDA review @ Fri Nov 20, 2026 9am (CST) (will@firehawkaerospace.com)", calendar=False))
        self.assertEqual(Activity.objects.get(kind=Activity.Kind.EMAIL_SENT).outcome, CALENDAR_INVITE)
        self.assertEqual(self.waiting(), [])

    def test_an_ordinary_email_is_still_waited_on(self):
        self.log(sent_email("Draft PDA for you", calendar=False, msg_id="<m2@acmigroup.com>", thread="888"))
        act = Activity.objects.get(kind=Activity.Kind.EMAIL_SENT)
        self.assertEqual(act.outcome, "")
        self.assertEqual([a.pk for a in self.waiting()], [act.pk])

    def test_an_invite_after_a_real_email_leaves_the_real_one_waiting(self):
        self.log(sent_email("Draft PDA for you", calendar=False, days_ago=8, thread="999", msg_id="<m3@acmigroup.com>"))
        self.log(sent_email("PDA review", calendar=True, days_ago=5, thread="999", msg_id="<m4@acmigroup.com>"))
        waiting = self.waiting()
        self.assertEqual([a.summary.split(": ", 1)[-1] for a in waiting], ["Draft PDA for you"])

    def test_an_accepted_reply_to_our_invite_is_not_something_to_answer(self):
        now = timezone.now()
        Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=now - timedelta(days=2), company=self.company,
                                summary="Reply from Will Edwards: Accepted: PDA review @ Fri Nov 20", outcome="Responded",
                                thread_id="555", source=Activity.Source.GMAIL, source_id="acc1")
        real = Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=now - timedelta(days=2), company=self.company,
                                       summary="Reply from Will Edwards: Re: Draft PDA", outcome="Responded",
                                       thread_id="556", source=Activity.Source.GMAIL, source_id="rep1")
        self.assertEqual([a.pk for a in pipeline.awaiting_reply(self.user)], [real.pk])
