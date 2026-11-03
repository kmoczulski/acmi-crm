"""Phone numbers from the sender's own email signature onto People (crm/mail/signature_phones.py)."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from crm.mail import signature_phones as sp
from crm.models import Activity, Company, Mailbox, Person
from crm.tests.test_mail import ME, FakeClient, raw_email

WILL = """Sounds good, send the draft over.

Best,
Will Edwards
VP, Business Development | Firehawk Aerospace
M: (301) 555-0142
O 301.555.0100 x204
Fax: 301-555-0199
www.firehawkaerospace.com
This email and any attachments are confidential and intended only for the addressee. Call 800-555-0000.
"""

QUOTED = """Thanks, talk Monday.

Will

On Mon, Oct 26, 2026 at 9:00 AM Kellen Moczulski <kmoczulski@acmigroup.com> wrote:
> See attached.
> Kellen Moczulski
> Cell: 410-555-0177
"""


class SignatureTests(TestCase):
    def test_labels_kept_fax_and_footer_left_out(self):
        found = sp.phones_in(sp.signature(WILL, "Will Edwards"))
        self.assertEqual(found, [("301-555-0142", "mobile"), ("301-555-0100 x204", "office")])

    def test_only_the_senders_own_part(self):
        self.assertEqual(sp.phones_in(sp.signature(QUOTED, "Will Edwards")), [])  # Kellen's number is quoted

    def test_no_sign_off_uses_the_last_lines_and_ignores_numbers_in_the_text(self):
        body = "Ref 2026-10-27 order 12345678.\n" * 30 + "Pat Lane\n+1 (703) 555-0123\n"
        self.assertEqual(sp.phones_in(sp.signature(body, "Pat Lane")), [("703-555-0123", "")])
        self.assertEqual(sp.phones_in(["Call ext 1234 or 555-0100", "on 10/27/2026"]), [])

    def test_merge_adds_never_replaces_and_skips_known_numbers(self):
        p = Person(full_name="Will", phones=["(301) 555-0142"])
        self.assertIsNone(sp.merge(p, [("301-555-0142", "mobile")]))  # the same number, written differently
        self.assertEqual(sp.merge(p, [("301-555-0100", "office"), ("410-555-0177", "")], skip={"4105550177"}),
                         ["(301) 555-0142", "301-555-0100 (office)"])


class SignatureSyncTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="kellen", email=ME)
        self.mailbox = Mailbox.objects.create(user=self.user, email=ME)
        co = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])
        self.will = Person.objects.create(first_name="Will", last_name="Edwards", email="will@firehawkaerospace.com",
                                          company=co)

    def test_the_sync_adds_the_senders_numbers(self):
        from crm.mail import sync

        stats = sync.sync_mailbox(self.mailbox, client=FakeClient([
            (1, "t1", raw_email("will@firehawkaerospace.com", ME, "Re: PDA", body=WILL))]))
        self.will.refresh_from_db()
        self.assertEqual(self.will.phones, ["301-555-0142 (mobile)", "301-555-0100 x204 (office)"])
        self.assertEqual(stats["phones from signatures"], 1)

    def test_a_number_in_three_domains_signatures_is_nobodys(self):
        for i, domain in enumerate(["a.test", "b.test", "c.test"]):
            p = Person.objects.create(full_name=f"P{i}", email=f"p{i}@{domain}")
            Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=timezone.now(), person=p, source="gmail",
                                    source_id=f"s{i}", summary="Reply", body=f"Ok.\n\nThanks,\nP{i}\nT: 202-555-0188")
        self.assertIn("2025550188", sp.team_numbers())
        call_command("signature_phones", "--commit", stdout=StringIO())
        self.assertFalse(Person.objects.exclude(phones=[]).exists())

    def test_the_command_reads_saved_mail_dry_run_first(self):
        Activity.objects.create(kind=Activity.Kind.EMAIL_RECEIVED, occurred_at=timezone.now(), person=self.will,
                                source="gmail", source_id="x1", summary="Email from Will: Hi", body=WILL)
        # ACMI's own number, from the team's sent mail, never lands on a contact.
        Activity.objects.create(kind=Activity.Kind.EMAIL_SENT, occurred_at=timezone.now(), source="gmail",
                                source_id="x2", summary="Email to Will", body="Hi Will\n\nBest,\nKellen\nM: 301-555-0142")
        out = StringIO()
        call_command("signature_phones", stdout=out)
        self.assertIn("people changed: 1", out.getvalue())
        self.will.refresh_from_db()
        self.assertEqual(self.will.phones, [])
        call_command("signature_phones", "--commit", stdout=StringIO())
        self.will.refresh_from_db()
        self.assertEqual(self.will.phones, ["301-555-0100 x204 (office)"])
