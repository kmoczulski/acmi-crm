"""The UX second pass, area 1 (capture gaps and Granola): the Needs filing inbox, firms on meetings, the "no record"
marker, emailed invites, same-person suggestions and unfiled items in search, and action items into tasks. Claude is
mocked; the invite is made up, written to the iCalendar standard (RFC 5545)."""
import json
from datetime import date, datetime, time, timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm import invites, meetings, needs_filing, search
from crm.directory import sort
from crm.mail import sync
from crm.mail.parse import parse_message
from crm.models import (
    Activity, AddressSuggestion, CapitalSource, Company, DomainSort, FilingSnooze, Firm, FirmEmail, Mailbox, MailFilter,
    Meeting, Notification, Person, Project, Task, UnmatchedEmail,
)
from crm.tests.test_domain_sort import claude, result
from crm.tests.utils import make_manager

User = get_user_model()
KEY = {"ANTHROPIC_API_KEY": "test"}

# A made-up invite from a bank, the way Outlook and Google write them: folded lines, a quoted CN with a comma, a
# TZID start, an alarm inside the event.
ICS = """BEGIN:VCALENDAR\r
PRODID:-//Example Corp//Calendar 1.0//EN\r
VERSION:2.0\r
METHOD:REQUEST\r
BEGIN:VTIMEZONE\r
TZID:America/New_York\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:hp-anvil-0001@harborpoint.test\r
DTSTAMP:20261020T150000Z\r
DTSTART;TZID=America/New_York:{start}\r
DTEND;TZID=America/New_York:{end}\r
SUMMARY:Harbor Point Bank <> ACMI: Project Anvil construction lo\r
 an\r
ORGANIZER;CN=Priya Raman:mailto:priya.raman@harborpoint.test\r
ATTENDEE;CN="Raman, Priya";ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED:mailto:pr\r
 iya.raman@harborpoint.test\r
ATTENDEE;CN=Dev Shah;ROLE=OPT-PARTICIPANT:mailto:dshah@harborpoint.test\r
ATTENDEE;CN=Kellen Moczulski:mailto:kmoczulski@acmigroup.com\r
BEGIN:VALARM\r
ACTION:DISPLAY\r
DESCRIPTION:Reminder\r
TRIGGER:-PT15M\r
END:VALARM\r
END:VEVENT\r
END:VCALENDAR\r
"""


def ics(day, cancel=False):
    text = ICS.format(start=f"{day:%Y%m%d}T140000", end=f"{day:%Y%m%d}T150000")
    return text.replace("METHOD:REQUEST", "METHOD:CANCEL") if cancel else text


def raw_invite(day, forwarded=True):
    """A forwarded invite as a raw email: a text part, then the calendar part."""
    return (
        "From: Simon Shewmaker <sshewmaker@acmigroup.com>\r\n"
        "To: Kellen Moczulski <kmoczulski@acmigroup.com>\r\n"
        f"Subject: {'Fwd: ' if forwarded else ''}Harbor Point Bank <> ACMI\r\n"
        "Date: Mon, 19 Oct 2026 09:00:00 -0400\r\n"
        "Message-ID: <fwd-1@acmigroup.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="b1"\r\n\r\n'
        "--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSee below, the bank's call.\r\n"
        '--b1\r\nContent-Type: text/calendar; charset=utf-8; method=REQUEST\r\n\r\n'
        f"{ics(day)}"
        "--b1--\r\n"
    ).encode()


def at(day, hour=14, minute=0):
    from zoneinfo import ZoneInfo

    return datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo("America/New_York"))


class CaptureBase(TestCase):
    def setUp(self):
        self.kellen = make_manager(User.objects.create_user("kellen", first_name="Kellen", last_name="Moczulski",
                                                            email="kmoczulski@acmigroup.com"))
        self.simon = User.objects.create_user("simon", first_name="Simon", last_name="Shewmaker",
                                              email="sshewmaker@acmigroup.com")
        self.client.force_login(self.kellen)
        self.mailbox = Mailbox.objects.create(user=self.kellen, email="kmoczulski@acmigroup.com")
        self.day = timezone.localdate() + timedelta(days=3)
        self.anvil = Project.objects.create(name="Anvil", keywords=["Project Anvil"])

    def queue(self, address, subject, n=1, name="", thread=""):
        domain = address.split("@")[1]
        for i in range(n):
            UnmatchedEmail.objects.create(mailbox=self.mailbox, message_id=f"<{address}-{i}>", thread_id=thread,
                                          occurred_at=timezone.now() - timedelta(days=i), kind="email_received",
                                          address=address, domain=domain, name=name, subject=subject)

    def meeting(self, title, attendees, when=None, source="granola", ext=None):
        m = Meeting.objects.create(source=source, external_id=ext or f"{source}:{title}", title=title,
                                   start=when or at(self.day), attendees=attendees)
        return needs_filing.link(m)


class InviteTests(CaptureBase):
    def test_ics_is_read_to_the_standard(self):
        (ev,) = invites.parse_ics(ics(self.day))
        self.assertEqual(ev["uid"], "hp-anvil-0001@harborpoint.test")
        self.assertEqual(ev["title"], "Harbor Point Bank <> ACMI: Project Anvil construction loan")  # unfolded
        self.assertEqual(ev["start"], at(self.day))
        self.assertEqual(ev["end"] - ev["start"], timedelta(hours=1))
        self.assertEqual(ev["organizer"], {"name": "Priya Raman", "email": "priya.raman@harborpoint.test"})
        self.assertEqual([a["name"] for a in ev["attendees"]], ["Raman, Priya", "Dev Shah", "Kellen Moczulski"])
        self.assertEqual(ev["method"], "REQUEST")

    def test_a_forwarded_invite_reaches_the_calendar_through_the_mail_sync(self):
        email = parse_message(raw_invite(self.day))
        self.assertEqual(len(email.calendar), 1)
        recorder = sync.process_messages(self.mailbox, [email])
        self.assertEqual(recorder.stats["meetings from invites"], 1)
        m = Meeting.objects.get()
        self.assertEqual((m.source, m.external_id), ("invite", "invite:hp-anvil-0001@harborpoint.test"))
        self.assertEqual(set(m.team.all()), {self.kellen})
        self.assertEqual(m.unfiled_domains, ["harborpoint.test"])
        self.assertEqual(len(m.unfiled), 2)
        # The same invite in a teammate's mailbox (or read again) is still one meeting.
        sync.process_messages(self.mailbox, [parse_message(raw_invite(self.day))])
        self.assertEqual(Meeting.objects.count(), 1)

    def test_the_gmail_api_reader_keeps_the_calendar_part(self):
        import base64

        from crm.mail.gmail_api import parse_api_message

        enc = lambda t: base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")  # noqa: E731
        email = parse_api_message({"id": "18c0", "threadId": "18c0", "labelIds": ["INBOX"], "payload": {
            "mimeType": "multipart/mixed", "headers": [
                {"name": "From", "value": "priya.raman@harborpoint.test"}, {"name": "To", "value": "kmoczulski@acmigroup.com"},
                {"name": "Subject", "value": "Harbor Point Bank <> ACMI"}, {"name": "Date", "value": "Mon, 19 Oct 2026 09:00:00 -0400"}],
            "parts": [{"mimeType": "text/plain", "body": {"data": enc("Talk soon")}},
                      {"mimeType": "text/calendar", "headers": [{"name": "Content-Type", "value": "text/calendar; charset=utf-8"}],
                       "body": {"data": enc(ics(self.day))}},
                      {"mimeType": "application/ics", "filename": "invite.ics", "body": {"attachmentId": "a1", "size": 900}}]}})
        self.assertEqual(len(email.calendar), 1)
        self.assertEqual(email.body, "Talk soon")
        self.assertEqual(invites.from_email(email, self.mailbox), (1, 0))

    def test_calendar_event_then_granola_note_take_over_the_invite(self):
        invites.save(invites.parse_ics(ics(self.day))[0], self.kellen)
        row = Meeting.objects.get()
        # Our calendar has our own title for it; the shared outside attendee says it's the same meeting.
        meetings.import_meetings([{"source": "calendar", "id": "evt9", "title": "Lender call",
                                   "start": at(self.day, 14, 5).isoformat(),
                                   "attendees": [{"email": "kmoczulski@acmigroup.com"},
                                                 {"email": "priya.raman@harborpoint.test"}]}])
        m = Meeting.objects.get()
        self.assertEqual((m.pk, m.source, m.external_id), (row.pk, "calendar", "calendar:evt9"))
        self.assertIn("dshah@harborpoint.test", {a["email"] for a in m.attendees})  # the invite's guests are kept
        meetings.import_meetings([{"source": "granola", "id": "n9", "title": "Lender call",
                                   "start": at(self.day, 14).isoformat(), "summary": "# Notes\n\n- Rates",
                                   "attendees": [{"email": "kmoczulski@acmigroup.com"},
                                                 {"email": "priya.raman@harborpoint.test"}]}])
        m = Meeting.objects.get()
        self.assertEqual((m.pk, m.source, m.calendar_id), (row.pk, "granola", "evt9"))

    def test_an_invite_for_a_meeting_already_on_the_calendar_adds_its_guests(self):
        cal = self.meeting("Lender call", [{"email": "kmoczulski@acmigroup.com"},
                                           {"email": "priya.raman@harborpoint.test"}], source="calendar", ext="calendar:e1")
        self.assertEqual(invites.save(invites.parse_ics(ics(self.day))[0]), "merged")
        cal.refresh_from_db()
        self.assertEqual(Meeting.objects.count(), 1)
        self.assertEqual(cal.title, "Lender call")
        self.assertEqual(len(cal.attendees), 3)  # Dev Shah is new

    def test_cancel_removes_the_invite_and_replies_never_add_one(self):
        invites.save(invites.parse_ics(ics(self.day))[0])
        reply = ics(self.day + timedelta(days=1)).replace("METHOD:REQUEST", "METHOD:REPLY").replace("hp-anvil-0001", "x")
        self.assertEqual(invites.save(invites.parse_ics(reply)[0]), "")
        self.assertEqual(invites.save(invites.parse_ics(ics(self.day, cancel=True))[0]), "cancelled")
        self.assertFalse(Meeting.objects.exists())

    def test_google_invitation_subject_without_a_calendar_part(self):
        email = parse_message((
            "From: Priya Raman <priya.raman@harborpoint.test>\r\nTo: kmoczulski@acmigroup.com\r\n"
            f"Subject: Invitation: Anvil financing @ {self.day:%a %b} {self.day.day}, {self.day.year} 2:30pm - 3:30pm"
            " (EDT) (kmoczulski@acmigroup.com)\r\n"
            "Message-ID: <inv-2@harborpoint.test>\r\n\r\nGuests\r\npriya.raman@harborpoint.test\r\ndshah@harborpoint.test\r\n"
        ).encode())
        self.assertTrue(invites.is_invitation(email.subject))
        added, _ = invites.from_email(email, self.mailbox)
        self.assertEqual(added, 1)
        m = Meeting.objects.get()
        self.assertEqual(m.title, "Anvil financing")
        self.assertEqual(m.start, at(self.day, 14, 30))
        self.assertEqual({a["email"] for a in m.outside_attendees}, {"priya.raman@harborpoint.test",
                                                                     "dshah@harborpoint.test"})

    def test_an_internal_only_invite_is_left_to_the_calendar_sync(self):
        text = ics(self.day).replace("harborpoint.test", "acmigroup.com")
        self.assertEqual(invites.save(invites.parse_ics(text)[0]), "")
        self.assertFalse(Meeting.objects.exists())


class LinkTests(CaptureBase):
    def test_import_links_firms_and_marks_who_has_no_record(self):
        firm = Firm.objects.create(name="Harbor Point Bank", kind="bank", domains=["harborpoint.test"])
        Company.objects.create(name="Firehawk", domains=["firehawk.test"])
        meetings.import_meetings([{"source": "granola", "id": "g1", "title": "Anvil sync",
                                   "start": at(self.day).isoformat(), "summary": "# Notes\n\n- x",
                                   "attendees": [{"email": "kmoczulski@acmigroup.com"},
                                                 {"email": "priya.raman@harborpoint.test"},
                                                 {"email": "pat@firehawk.test"},
                                                 {"email": "jo@gmail.com"},
                                                 {"name": "Lee Ortiz", "email": "lee@terracon.test"}]}])
        m = Meeting.objects.get()
        self.assertEqual(list(m.firms.all()), [firm])
        self.assertEqual(m.companies.count(), 1)
        self.assertEqual(m.unfiled, [{"name": "Lee Ortiz", "email": "lee@terracon.test"}])  # free mail isn't counted
        # The calendar row shows the firm and its type, and the marker with a File link.
        page = self.client.get(reverse("crm:meeting_list") + f"?month={self.day:%Y-%m}")
        self.assertContains(page, "Harbor Point Bank · bank")
        self.assertContains(page, "1 outside attendee, no record")
        self.assertContains(page, reverse("crm:needs_filing") + "?domain=terracon.test")
        panel = self.client.get(reverse("crm:meeting_panel", args=[m.pk]))
        self.assertContains(panel, "chip-firm")
        self.assertContains(panel, "No record")
        # The week widget, when the meeting is this week.
        m.start = timezone.now() + timedelta(hours=1)
        m.save()
        m.team.add(self.kellen)
        home = self.client.get(reverse("crm:home") + "?cal=all")
        self.assertContains(home, "week-firm")

    def test_upcoming_meetings_on_firm_capital_and_build_pages(self):
        firm = Firm.objects.create(name="Harbor Point Bank", kind="bank", domains=["harborpoint.test"])
        CapitalSource.objects.create(project=self.anvil, provider=firm)
        from crm.models import TeamMember

        TeamMember.objects.create(project=self.anvil, role="civil", firm="Bohler")
        bohler = Firm.objects.get(name="Bohler")
        bohler.domains = ["bohler.test"]
        bohler.save()
        self.meeting("Rate call", [{"email": "priya.raman@harborpoint.test"}], ext="granola:a")
        self.meeting("Site plan review", [{"email": "eng@bohler.test"}], ext="granola:b")
        self.assertContains(self.client.get(firm.get_absolute_url()), "Rate call")
        self.assertContains(self.client.get(reverse("crm:project_capital", args=[self.anvil.pk])), "Rate call")
        self.assertContains(self.client.get(reverse("crm:project_build", args=[self.anvil.pk])), "Site plan review")

    def test_backfill_command_is_a_dry_run_unless_applied(self):
        firm = Firm.objects.create(name="Harbor Point Bank", kind="bank", domains=["harborpoint.test"])
        m = Meeting.objects.create(source="granola", external_id="granola:old", title="Old call", start=at(self.day),
                                   attendees=[{"email": "priya.raman@harborpoint.test"}, {"email": "x@nobody.test"}])
        out = StringIO()
        call_command("link_meetings", stdout=out)
        self.assertIn("Would link 1 firm link", out.getvalue())
        self.assertFalse(m.firms.exists())
        call_command("link_meetings", "--apply", stdout=StringIO())
        m.refresh_from_db()
        self.assertEqual(list(m.firms.all()), [firm])
        self.assertEqual(m.unfiled_domains, ["nobody.test"])


@mock.patch.dict("os.environ", KEY)
class NeedsFilingTests(CaptureBase):
    def bank(self, confidence="medium"):
        self.queue("priya.raman@harborpoint.test", "Project Anvil term sheet", n=2, name="Priya Raman")
        self.meeting("Harbor Point / ACMI", [{"email": "priya.raman@harborpoint.test", "name": "Priya Raman"},
                                             {"email": "kmoczulski@acmigroup.com"}])
        DomainSort.objects.create(domain="harborpoint.test", verdict="directory", kind="bank", name="Harbor Point Bank",
                                  confidence=confidence, reason="Signature: VP, Harbor Point Bank.")

    def test_one_card_per_domain_with_the_verdict_as_the_headline(self):
        self.bank()
        self.queue("news@digest.test", "This week", n=5)
        page = self.client.get(reverse("crm:needs_filing"))
        self.assertEqual(page.context["total"], 2)
        first = page.context["cards"][0]
        self.assertEqual(first.domain, "harborpoint.test")  # a bank with a meeting beats a busier newsletter
        self.assertEqual((first.count, len(first.meetings)), (2, 1))
        self.assertEqual(first.project, self.anvil)
        self.assertContains(page, "<strong>Looks like a bank.</strong> File Harbor Point Bank as a lender on Anvil?")
        self.assertContains(page, "Not now")
        self.assertContains(page, "Never")
        # The old address keeps working, and the badge counts both.
        self.assertRedirects(self.client.get("/mail/"), "/needs-filing/", fetch_redirect_response=False)
        self.assertEqual(needs_filing.count(), 2)
        self.assertEqual(page.context["mail_queue_count"], 2)
        # ?domain= shows one card (the calendar's File link)
        one = self.client.get(reverse("crm:needs_filing") + "?domain=digest.test")
        self.assertEqual([c.domain for c in one.context["cards"]], ["digest.test"])

    def test_accept_files_the_firm_links_the_meetings_and_adds_a_capital_source(self):
        self.bank()
        r = self.client.post(reverse("crm:needs_filing_act"), {"domain": "harborpoint.test", "action": "accept",
                                                               "project": self.anvil.pk})
        self.assertRedirects(r, reverse("crm:needs_filing"), fetch_redirect_response=False)
        firm = Firm.objects.get(name="Harbor Point Bank")
        self.assertEqual((firm.kind, firm.domains), ("bank", ["harborpoint.test"]))
        self.assertEqual(FirmEmail.objects.filter(provider=firm).count(), 2)
        m = Meeting.objects.get()
        self.assertEqual(list(m.firms.all()), [firm])
        self.assertEqual(m.unfiled, [])
        source = CapitalSource.objects.get()
        self.assertEqual((source.project, source.provider, source.status), (self.anvil, firm, "identified"))
        self.assertEqual(DomainSort.objects.get().status, "done")
        self.assertFalse(Company.objects.exists())
        self.assertEqual(needs_filing.count(), 0)

    def test_change_can_add_a_company_or_file_another_type(self):
        self.queue("pat@firehawk.test", "Space at MEIH")
        self.client.post(reverse("crm:needs_filing_act"), {"domain": "firehawk.test", "action": "accept", "as": "company",
                                                           "name": "Firehawk Aerospace"})
        self.assertTrue(Company.objects.filter(name="Firehawk Aerospace", domains=["firehawk.test"]).exists())
        self.queue("lee@terracon.test", "Geotech proposal")
        self.client.post(reverse("crm:needs_filing_act"), {"domain": "terracon.test", "action": "accept",
                                                           "as": "directory", "kind": "engineer", "name": "Terracon"})
        self.assertEqual(Firm.objects.get(name="Terracon").kind, "engineer")
        self.assertFalse(CapitalSource.objects.exists())  # an engineer isn't a capital source

    def test_not_now_and_never(self):
        self.bank()
        self.client.post(reverse("crm:needs_filing_act"), {"domain": "harborpoint.test", "action": "later"})
        self.assertEqual(needs_filing.count(), 0)
        FilingSnooze.objects.update(until=timezone.localdate() - timedelta(days=1))
        self.assertEqual(needs_filing.count(), 1)  # back after two weeks
        self.client.post(reverse("crm:needs_filing_act"), {"domain": "harborpoint.test", "action": "never"})
        self.assertTrue(MailFilter.objects.filter(pattern="harborpoint.test", kind="ignore").exists())
        self.assertFalse(UnmatchedEmail.objects.exists())
        self.assertEqual(Meeting.objects.get().unfiled, [])  # its meetings stop asking too
        self.assertEqual(needs_filing.count(), 0)

    def test_the_page_is_paginated(self):
        for i in range(needs_filing.PER_PAGE + 3):
            self.queue(f"a@d{i}.test", "Hi")
        page = self.client.get(reverse("crm:needs_filing"))
        self.assertEqual(len(page.context["cards"]), needs_filing.PER_PAGE)
        self.assertEqual(len(self.client.get(reverse("crm:needs_filing") + "?page=2").context["cards"]), 3)

    def test_claude_files_a_medium_bank_once_a_meeting_exists_and_undo_unlinks_it(self):
        self.queue("priya.raman@harborpoint.test", "Term sheet")
        patch, _ = claude(result("harborpoint.test", "directory", "bank", "Harbor Point Bank", "medium", "A term sheet."))
        with patch:
            self.assertEqual(sort.auto_sort()["filed in the directory"], 0)  # medium and never met: a suggestion
        self.meeting("Harbor Point call", [{"email": "priya.raman@harborpoint.test"}])
        patch, stream = claude()
        with patch:
            self.assertEqual(sort.auto_sort()["filed in the directory"], 1)
        stream.assert_not_called()  # the earlier verdict is used
        firm = Firm.objects.get(name="Harbor Point Bank")
        m = Meeting.objects.get()
        self.assertEqual(list(m.firms.all()), [firm])
        self.assertFalse(Company.objects.exists())
        s = DomainSort.objects.get()
        self.assertTrue(sort.undo(s, self.kellen))
        m.refresh_from_db()
        self.assertFalse(m.firms.exists())
        self.assertEqual(m.unfiled_domains, ["harborpoint.test"])

    def test_a_domain_only_met_at_a_meeting_is_sorted_and_filed(self):
        self.meeting("Kickoff", [{"email": "jan@gfrlaw.test", "name": "Jan Ruiz"}])
        patch, stream = claude(result("gfrlaw.test", "directory", "law", "Gordon Feinblatt", "medium", "A law firm."))
        with patch:
            self.assertEqual(sort.auto_sort(), {"sorted": 1, "filed in the directory": 1})
        self.assertIn("Meeting", stream.call_args.kwargs["messages"][0]["content"][0]["text"])
        self.assertEqual(Meeting.objects.get().firms.get().name, "Gordon Feinblatt")

    def test_managers_get_a_daily_note(self):
        self.meeting("Call", [{"email": "x@nobody.test"}], when=timezone.now() + timedelta(days=1))
        from crm import notify

        notify.due_sweep(self.kellen)
        notify.due_sweep(self.kellen)
        notify.due_sweep(self.simon)  # not a Manager
        note = Notification.objects.get()
        self.assertEqual(note.user, self.kellen)
        self.assertIn("1 meeting this week with outside people and no record", note.text)


class SamePersonAndSearchTests(CaptureBase):
    def test_a_free_mail_sender_with_a_known_name_is_suggested_and_added(self):
        co = Company.objects.create(name="Firehawk", domains=["firehawk.test"])
        pat = Person.objects.create(full_name="Pat Lee", email="pat@firehawk.test", company=co)
        event = sync.EmailEvent(mailbox=self.mailbox, message_id="<p1>", thread_id="", occurred_at=timezone.now(),
                                subject="From my phone", body="Hi", kind="email_received", outcome="",
                                address="patlee77@gmail.com", name="Pat Lee")
        recorder = sync.Recorder()
        sync._park(event, recorder.matcher, recorder)
        s = AddressSuggestion.objects.get()
        self.assertEqual(s.person, pat)
        page = self.client.get(reverse("crm:needs_filing"))
        self.assertContains(page, "Same person, add this address")
        self.client.post(reverse("crm:same_person", args=[s.pk]), {"action": "add"})
        pat.refresh_from_db()
        self.assertEqual(pat.other_emails, ["patlee77@gmail.com"])
        self.assertTrue(Activity.objects.filter(person=pat, company=co, message_id="<p1>").exists())

    def test_not_them_stops_the_suggestion(self):
        pat = Person.objects.create(full_name="Pat Lee", email="pat@firehawk.test")
        event = sync.EmailEvent(mailbox=self.mailbox, message_id="<p1>", thread_id="", occurred_at=timezone.now(),
                                subject="Hi", body="", kind="email_received", outcome="", address="pl@gmail.com",
                                name="Pat Lee")
        self.assertIsNotNone(sync.same_person(event))
        s = AddressSuggestion.objects.get()
        self.client.post(reverse("crm:same_person", args=[s.pk]), {"action": "dismiss"})
        event.message_id = "<p2>"
        self.assertIsNone(sync.same_person(event))
        self.assertEqual(pat.other_emails, [])

    def test_search_finds_unfiled_senders_attendees_and_firm_contacts(self):
        self.queue("priya.raman@harborpoint.test", "Term sheet", name="Priya Raman")
        self.meeting("Kickoff", [{"email": "jan@gfrlaw.test", "name": "Jan Ruiz"}])
        Firm.objects.create(name="Terracon", kind="engineer", contacts=[{"name": "Gus Ortiz", "title": "PE",
                                                                          "email": "gus@terracon.test"}])
        groups = {g["label"]: g["items"] for g in search.results("Priya")}
        self.assertEqual(groups["Not filed yet"][0]["badge"], "Not filed yet")
        self.assertIn("?domain=harborpoint.test", groups["Not filed yet"][0]["url"])
        groups = {g["label"]: g["items"] for g in search.results("Jan Ruiz")}
        self.assertEqual(groups["Not filed yet"][0]["title"], "Jan Ruiz")
        groups = {g["label"]: g["items"] for g in search.results("Gus")}
        self.assertEqual(groups["Directory contacts"][0]["sub"], "PE · Terracon")


class ActionItemTests(CaptureBase):
    SUMMARY = ("# Next Steps\n\n- **Send the site plan by Friday** (Simon)\n- **Call Harbor Point** [Kellen]\n"
               "- **Book the site visit** (Kellen)\n  Before Oct 30.\n")

    def granola(self, id_, start, summary=None):
        return {"source": "granola", "id": id_, "title": f"Anvil sync {id_}", "start": start.isoformat(),
                "summary": summary or self.SUMMARY,
                "attendees": [{"email": "kmoczulski@acmigroup.com"}, {"email": "sshewmaker@acmigroup.com"}]}

    def test_due_dates_from_the_words(self):
        wed = date(2026, 10, 21)
        cases = {"by Friday": date(2026, 10, 23), "by tomorrow": date(2026, 10, 22), "end of week": date(2026, 10, 23),
                 "next week": date(2026, 10, 26), "by Oct 30": date(2026, 10, 30), "by 11/2": date(2026, 11, 2),
                 "by Wednesday": wed, "EOD": wed, "end of month": date(2026, 10, 31), "next Friday": None,
                 "Send the deck": None, "by Jan 5": date(2027, 1, 5)}
        for text, want in cases.items():
            self.assertEqual(meetings.due_from_text(text, wed), want, text)
        self.assertEqual(meetings.due_from_text("by next Friday", wed), date(2026, 10, 30))

    def test_bracketed_owner(self):
        items = meetings.action_items(self.SUMMARY)
        self.assertEqual([i["owner"] for i in items], ["Simon", "Kellen", "Kellen"])
        self.assertEqual(meetings.owner_for("Simon", [self.kellen, self.simon]), self.simon)
        self.assertEqual(meetings.owner_for("Kellen Moczulski", [self.kellen, self.simon]), self.kellen)
        self.assertIsNone(meetings.owner_for("Dave", [self.kellen, self.simon]))

    def test_make_tasks_for_all_items_and_the_same_item_next_meeting_joins_it(self):
        start = timezone.make_aware(datetime(2026, 10, 21, 10))  # a Wednesday
        meetings.import_meetings([self.granola("a", start)])
        m = Meeting.objects.get()
        panel = self.client.get(reverse("crm:meeting_panel", args=[m.pk]))
        self.assertContains(panel, "Make tasks for all 3 items")
        self.client.post(reverse("crm:meeting_make_tasks", args=[m.pk]))
        tasks = {t.title: t for t in Task.objects.all()}
        self.assertEqual(len(tasks), 3)
        plan = tasks["Send the site plan by Friday"]
        self.assertEqual((plan.assignee, plan.due_date), (self.simon, date(2026, 10, 23)))
        self.assertEqual(tasks["Book the site visit"].due_date, date(2026, 10, 30))  # from the detail line
        self.assertEqual(tasks["Call Harbor Point"].assignee, self.kellen)
        self.assertEqual(list(plan.meetings.all()), [m])
        self.assertEqual(Notification.objects.filter(user=self.simon, kind="assigned").count(), 1)
        # Two days later the same items come up again: no new tasks, the meeting joins the old ones.
        meetings.import_meetings([self.granola("b", start + timedelta(days=2))])
        m2 = Meeting.objects.get(external_id="granola:b")
        self.client.post(reverse("crm:meeting_make_tasks", args=[m2.pk]))
        self.assertEqual(Task.objects.count(), 3)
        self.assertEqual(set(plan.meetings.all()), {m, m2})
        self.assertEqual(plan.meeting, m)
        panel = self.client.get(reverse("crm:meeting_panel", args=[m2.pk]))
        self.assertContains(panel, "also from Anvil sync a")
        self.assertContains(self.client.get(plan.get_absolute_url()), "Anvil sync b")

    def test_a_different_owner_is_a_different_task(self):
        start = timezone.now()
        meetings.import_meetings([self.granola("a", start)])
        m = Meeting.objects.get()
        self.client.post(reverse("crm:meeting_make_task", args=[m.pk]), {"index": 1, "assignee": self.simon.pk})
        meetings.import_meetings([self.granola("b", start + timedelta(days=1))])
        m2 = Meeting.objects.get(external_id="granola:b")
        self.client.post(reverse("crm:meeting_make_task", args=[m2.pk]), {"index": 1})
        self.assertEqual(Task.objects.filter(title="Call Harbor Point").count(), 2)

    def test_import_is_idempotent_by_granola_id_and_posts_one_note(self):
        path = self.settings_dir() / "granola.json"
        rows = [self.granola("a", timezone.now()),
                {**self.granola("c", timezone.now() + timedelta(hours=3)), "summary": "# Notes\n\n- intro",
                 "attendees": [{"email": "kmoczulski@acmigroup.com"}, {"email": "x@nobody.test"}]}]
        path.write_text(json.dumps(rows), encoding="utf-8")
        out = StringIO()
        call_command("import_meetings", str(path), stdout=out)
        self.assertIn("1 meeting need filing", out.getvalue())
        m = Meeting.objects.get(external_id="granola:a")
        meetings.make_task(m, 0, self.kellen)
        call_command("import_meetings", str(path), stdout=StringIO())
        self.assertEqual(Meeting.objects.count(), 2)
        m.refresh_from_db()
        self.assertTrue(m.action_items[0]["task_id"])  # the task made in between is kept
        notes = list(Notification.objects.filter(user=self.kellen, kind="meeting").order_by("pk"))
        self.assertEqual(len([n for n in notes if n.text.startswith("Granola import")]), 2)  # one per run
        self.assertIn("2 meetings (2 new, 0 updated)", notes[0].text)
        self.assertIn("1 meeting need filing", notes[0].text)
        self.assertIn("(0 new, 2 updated)", [n for n in notes if n.text.startswith("Granola import")][1].text)

    def settings_dir(self):
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d
