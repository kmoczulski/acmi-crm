"""The Granola text export (crm/granola_text.py) and loading it from the Drive folder, as the signed-in person.
Drive is a fake here; the sample is made up in the export's format."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from crm import drive, granola_text, meetings
from crm.management.commands import import_meetings as cmd
from crm.models import Company, ImportBatch, Meeting, Person

User = get_user_model()

EXPORT = """GRANOLA MEETING NOTES EXPORT - 2 meetings
FORMAT: made up

=== MEETING ===
ID: g-1
TITLE: Fortress / ACMI: Pipeline Review
DATE: 2026-11-04 10:00 AM CST
URL: https://notes.granola.ai/d/g-1
ATTENDEES: Kellen Moczulski, Simon Shewmaker (ACMI); Chase Romney, Nobody Known (Fortress)
--- NOTES ---
PIPELINE
- Two deals flagged
NEXT STEPS
- Send comps (Kellen): Fortress asked for them.
=== END ===

=== MEETING ===
ID: g-2
TITLE: Weekly
DATE: 2026-10-20 2:30 PM CDT
URL: https://notes.granola.ai/d/g-2
ATTENDEES: Kellen Moczulski, Mguerrero (ACMI)
--- NOTES ---
Weekly notes
=== END ===

=== MEETING ===
ID: g-3
TITLE: Broken date
DATE: sometime
ATTENDEES: Kellen Moczulski (ACMI)
--- NOTES ---
x
=== END ===
"""


class ParseTests(TestCase):
    def test_blocks_dates_and_attendees(self):
        rows = granola_text.parse(EXPORT)
        self.assertEqual([r["id"] for r in rows], ["g-1", "g-2"])
        self.assertEqual(rows[0]["start"], "2026-11-04T10:00:00-06:00")
        self.assertEqual(rows[1]["start"], "2026-10-20T14:30:00-05:00")  # CDT is an hour ahead of CST
        self.assertEqual(rows[0]["url"], "https://notes.granola.ai/d/g-1")
        self.assertIn("NEXT STEPS", rows[0]["summary"])
        self.assertEqual([a["name"] for a in rows[0]["attendees"]],
                         ["Kellen Moczulski", "Simon Shewmaker", "Chase Romney", "Nobody Known"])
        self.assertEqual(granola_text.problems(EXPORT)[0][0], "Broken date")

    def test_index_file_and_cut_off_file_read_as_empty(self):
        self.assertEqual(granola_text.parse("GRANOLA MEETING INDEX\n2026-11-04 | Title | https://x"), [])
        self.assertEqual(granola_text.parse(EXPORT.split("=== END ===")[0]), [])  # no END: dropped, not half-read


class ImportTests(TestCase):
    def setUp(self):
        User.objects.create_user("kellen", email="kmoczulski@acmigroup.com", first_name="Kellen",
                                 last_name="Moczulski", password="x")
        User.objects.create_user("simon", email="sshewmaker@acmigroup.com", first_name="Simon", last_name="Shewmaker",
                                 password="x")
        self.fortress = Company.objects.create(name="Fortress Investment Group")
        Person.objects.create(first_name="Chase", last_name="Romney", email="chase@fortress.test",
                              company=self.fortress)

    def rows(self):
        return granola_text.resolve_attendees(granola_text.parse(EXPORT))

    def test_names_resolve_to_emails_and_the_person_links_the_company(self):
        first = self.rows()[0]
        emails = {a.get("email") for a in first["attendees"]}
        self.assertEqual(emails, {"kmoczulski@acmigroup.com", "sshewmaker@acmigroup.com", "chase@fortress.test", None})
        self.assertEqual(first["groups"], ["Fortress"])
        stats = meetings.import_meetings(self.rows())
        self.assertEqual(stats["calendar"], 2)
        m = Meeting.objects.get(external_id="granola:g-1")
        self.assertEqual(m.team.count(), 2)
        self.assertIn(self.fortress, m.companies.all())
        self.assertEqual(m.start, datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(m.action_items[0]["title"], "Send comps")

    def test_company_label_alone_links_when_one_company_has_the_name(self):
        Company.objects.create(name="Fortress")
        Person.objects.all().delete()
        meetings.import_meetings(self.rows())
        self.assertEqual(list(Meeting.objects.get(external_id="granola:g-1").companies.all()),
                         [Company.objects.get(name="Fortress")])

    def test_reimport_updates_in_place(self):
        meetings.import_meetings(self.rows())
        meetings.import_meetings(self.rows())
        self.assertEqual(Meeting.objects.filter(source="granola").count(), 2)


class FakeClient:
    def __init__(self, files):
        self.files = files

    def children(self, folder_id):
        return [{"id": i, "name": n, "mimeType": "text/plain", "modifiedTime": t} for i, n, t, _ in self.files]

    def download(self, file_id, mime):
        body = next(b for i, _, _, b in self.files if i == file_id)
        return SimpleNamespace(iter_content=lambda size: [body.encode()]), ""


@override_settings(GRANOLA_DRIVE_FOLDER="FOLDER12345", GRANOLA_DRIVE_USER="kmoczulski@acmigroup.com")
class DriveFolderTests(TestCase):
    def setUp(self):
        User.objects.create_user("kellen", email="kmoczulski@acmigroup.com", first_name="Kellen",
                                 last_name="Moczulski", password="x")
        self.seen = []

    def run_import(self, files):
        def make(as_user=""):
            self.seen.append(as_user)
            return FakeClient(files)

        with mock.patch("crm.drive.client", side_effect=make), mock.patch("crm.notify.send"):
            return cmd.import_from_drive()

    def test_reads_as_the_signed_in_person_and_skips_the_index(self):
        files = [("F1", "granola_meetings_full_notes_a.txt", "2026-11-04T18:00:00Z", EXPORT),
                 ("F2", "granola_meeting_index_a.txt", "2026-11-04T18:00:00Z", "GRANOLA MEETING INDEX\n2026-11-04 | T | u")]
        out = self.run_import(files)
        self.assertEqual(out["failed"], [])
        self.assertEqual(out["stats"]["calendar"], 2)
        self.assertEqual(set(self.seen), {"kmoczulski@acmigroup.com"})
        self.assertEqual(Meeting.objects.count(), 2)

    def test_same_file_is_not_read_twice_but_a_rewritten_one_is(self):
        files = [("F1", "notes.txt", "2026-11-04T18:00:00Z", EXPORT)]
        self.run_import(files)
        self.assertEqual(self.run_import(files)["files"], [])
        files = [("F1", "notes.txt", "2026-11-05T18:00:00Z", EXPORT.replace("Two deals", "Three deals"))]
        out = self.run_import(files)
        self.assertEqual(out["files"], ["notes.txt"])
        self.assertIn("Three deals", Meeting.objects.get(external_id="granola:g-1").summary)
        self.assertEqual(ImportBatch.objects.filter(sheet__startswith="granola:").count(), 2)

    def test_no_google_token_for_the_person_says_so(self):
        with self.assertRaises(drive.NotConnected):
            drive._user_session("kmoczulski@acmigroup.com")
