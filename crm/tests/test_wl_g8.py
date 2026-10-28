"""The two cloud gaps from the 2026-10-28 worklist (section 5), both read from a Google Drive folder: the weekly news
scans (crm/news/review.py, ACMI_NEWS_DRIVE_FOLDER) and the daily Granola JSON, which sync_all imports once per file
(crm/management/commands/import_meetings.py, ACMI_GRANOLA_DRIVE_FOLDER). Drive itself is a fake here."""
import json
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import drive
from crm.models import Company, ImportBatch, Mailbox, Meeting, NewsScan

from .test_news import ME, FakeDrafts, make_docx
from .utils import make_manager

User = get_user_model()
NEWS_FOLDER = "newsFolder0123456789"
GRANOLA_FOLDER = "granolaFolder0123456789"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUMMARY = "# Notes\n\n- Talked scope\n\n# Next Steps\n\n- **Send the deck** (Kellen)\n"


def drive_file(id_, name, mime="application/json", day=1):
    return drive.DriveFile(id_, name, mime, datetime(2026, 10, day, 12, tzinfo=dt_timezone.utc))


class FakeClient:
    """Stands in for crm.drive.Client: file bytes by id, and which ids were downloaded."""

    def __init__(self, data):
        self.data, self.downloaded = data, []

    def download(self, file_id, mime):
        self.downloaded.append(file_id)
        response = mock.Mock()
        response.iter_content = lambda size: iter([self.data[file_id]])
        return response, ""


class NewsFromDriveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="kellen", email=ME)
        Mailbox.objects.create(user=self.user, email=ME)
        self.client.force_login(self.user)
        Company.objects.create(name="Firehawk Aerospace, Inc.")
        with tempfile.TemporaryDirectory() as tmp:
            self.docx = make_docx(tmp).read_bytes()
        self.files = [drive_file("scanFileA1", "Weekly Prospect News Scan - October 14, 2026.docx", DOCX, day=14),
                      drive_file("otherFile2", "Notes.txt", "text/plain")]
        self.fake = FakeClient({"scanFileA1": self.docx})

    def drive(self):
        return mock.patch.multiple("crm.drive", list_files=mock.Mock(return_value=self.files),
                                   client=mock.Mock(return_value=self.fake))

    @override_settings(NEWS_DRIVE_FOLDER=NEWS_FOLDER, NEWS_WATCH_FOLDER="Z:/no/such/folder")
    def test_the_news_page_lists_the_drive_folder_and_import_downloads_into_a_scan(self):
        with self.drive(), mock.patch("crm.news.drafts.Drafts", FakeDrafts):
            page = self.client.get("/news/")
            self.assertContains(page, "Weekly Prospect News Scan - October 14, 2026.docx")
            self.assertNotContains(page, "Notes.txt")
            self.assertContains(page, 'value="drive:scanFileA1"')
            drive.list_files.assert_called_with(NEWS_FOLDER)
            r = self.client.post("/news/import/", {"path": "drive:scanFileA1"})
        scan = NewsScan.objects.get()
        self.assertRedirects(r, scan.get_absolute_url(), fetch_redirect_response=False)
        # The path is the Drive file id, so two scans with the same name never clash; the name is kept for show.
        self.assertEqual((scan.path, scan.file_name), ("drive:scanFileA1", "Weekly Prospect News Scan - October 14, 2026.docx"))
        self.assertEqual((scan.scan_date, scan.items.filter(kind="board").count()), ("October 14, 2026", 3))
        self.assertEqual(self.fake.downloaded, ["scanFileA1"])

    @override_settings(NEWS_DRIVE_FOLDER=NEWS_FOLDER)
    def test_opening_it_again_reuses_the_scan_without_downloading(self):
        with self.drive(), mock.patch("crm.news.drafts.Drafts", FakeDrafts):
            self.client.post("/news/import/", {"path": "drive:scanFileA1"})
            self.client.post("/news/import/", {"path": "drive:scanFileA1"})
            self.assertContains(self.client.get("/news/"), "Continue review")
        self.assertEqual(NewsScan.objects.count(), 1)
        self.assertEqual(self.fake.downloaded, ["scanFileA1"])

    @override_settings(NEWS_DRIVE_FOLDER=NEWS_FOLDER)
    def test_a_file_not_in_the_folder_is_refused(self):
        with self.drive():
            r = self.client.post("/news/import/", {"path": "drive:someoneElses1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.fake.downloaded, [])

    @override_settings(NEWS_DRIVE_FOLDER=NEWS_FOLDER)
    def test_drive_not_reachable_shows_a_message_not_an_error_page(self):
        with mock.patch("crm.drive.list_files", side_effect=drive.NotConnected):
            r = self.client.get("/news/")
        self.assertContains(r, "Could not list the news scans folder")

    @override_settings(NEWS_DRIVE_FOLDER="")
    def test_without_the_setting_the_laptop_folder_is_read_and_drive_is_not(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(NEWS_WATCH_FOLDER=tmp), \
                mock.patch("crm.drive.list_files") as listing:
            make_docx(tmp)
            self.assertContains(self.client.get("/news/"), "Weekly Prospect News Scan - October 14, 2026.docx")
        listing.assert_not_called()


class GranolaFromDriveTests(TestCase):
    def setUp(self):
        User.objects.create_user("kellen", password="x", email=ME)
        Company.objects.create(name="Booz Allen", domains=["bah.com"])
        when = (timezone.now() - timedelta(days=1)).isoformat()
        people = [{"email": ME}, {"email": "smith_j@bah.com"}]
        note = {"source": "granola", "id": "n1", "title": "Booz Allen / ACMI intro", "start": when,
                "url": "https://notes.granola.ai/d/n1", "summary": SUMMARY, "attendees": people}
        self.data = {"granolaDay1": json.dumps([note]).encode(), "brokenFile1": b"[{not json"}
        self.files = [drive_file("granolaDay1", "granola-2026-10-27.json"),
                      drive_file("readmeFile1", "README.pdf", "application/pdf")]
        self.fake = FakeClient(self.data)

    def sync(self, files=None, **patches):
        out, err = StringIO(), StringIO()
        with mock.patch("crm.mail.sync.sync_all", return_value={"k@acmigroup.com": {"new": 3}}), \
                mock.patch("crm.gcal.sync_all"), \
                mock.patch("crm.directory.sort.auto_sort", return_value={}) as sort, \
                mock.patch("crm.notify.sweep_everyone") as sweep, \
                mock.patch("crm.drive.list_files", **(patches or {"return_value": files or self.files})) as listing, \
                mock.patch("crm.drive.client", return_value=self.fake):
            call_command("sync_all", stdout=out, stderr=err)
        sort.assert_called_once()
        sweep.assert_called_once()
        return out.getvalue(), err.getvalue(), listing

    @override_settings(GRANOLA_DRIVE_FOLDER=GRANOLA_FOLDER)
    def test_sync_imports_each_new_file_once(self):
        out, err, listing = self.sync()
        listing.assert_called_with(GRANOLA_FOLDER, refresh=True)
        self.assertEqual(Meeting.objects.get().external_id, "granola:n1")
        self.assertIn("granola: 1 new file(s)", out)
        self.assertEqual(err, "")
        self.assertEqual(list(ImportBatch.objects.values_list("sheet", "filename")),
                         [("granola:granolaDay1", "granola-2026-10-27.json")])
        self.assertEqual(self.fake.downloaded, ["granolaDay1"])  # the PDF beside it is left alone
        self.sync()  # the next run finds nothing new
        self.assertEqual(self.fake.downloaded, ["granolaDay1"])
        self.assertEqual(ImportBatch.objects.count(), 1)

    @override_settings(GRANOLA_DRIVE_FOLDER=GRANOLA_FOLDER)
    def test_a_bad_file_is_logged_and_retried_and_the_good_one_still_goes_in(self):
        out, err, _ = self.sync(self.files + [drive_file("brokenFile1", "granola-2026-10-26.json")])
        self.assertIn("granola file granola-2026-10-26.json not imported", err)
        self.assertIn("k@acmigroup.com: new 3", out)
        self.assertEqual(Meeting.objects.count(), 1)
        self.assertEqual(list(ImportBatch.objects.values_list("sheet", flat=True)), ["granola:granolaDay1"])

    @override_settings(GRANOLA_DRIVE_FOLDER=GRANOLA_FOLDER)
    def test_a_file_the_import_cant_take_doesnt_hold_up_the_others(self):
        # Valid JSON, so it gets past the first read, but the note has no start: the combined import fails.
        self.data["noStartFile1"] = json.dumps([{"source": "granola", "id": "n2", "title": "x"}]).encode()
        files = self.files + [drive_file("noStartFile1", "granola-2026-10-25.json")]
        out, err, _ = self.sync(files)
        self.assertIn("granola file granola-2026-10-25.json not imported", err)
        self.assertEqual(Meeting.objects.get().external_id, "granola:n1")
        self.assertEqual(list(ImportBatch.objects.values_list("sheet", flat=True)), ["granola:granolaDay1"])
        self.sync(files)  # the bad one is tried again; the good one isn't read twice
        self.assertEqual(self.fake.downloaded.count("granolaDay1"), 1)
        self.assertEqual(ImportBatch.objects.count(), 1)

    @override_settings(GRANOLA_DRIVE_FOLDER=GRANOLA_FOLDER)
    def test_drive_failing_never_fails_the_rest_of_the_sync(self):
        out, err, _ = self.sync(side_effect=RuntimeError("Drive API has not been used in project"))
        self.assertIn("granola import failed: Drive API has not been used in project", err)
        self.assertIn("k@acmigroup.com: new 3", out)  # mail ran; sorting and reminders ran (asserted in sync())

    @override_settings(GRANOLA_DRIVE_FOLDER="")
    def test_without_the_setting_drive_is_not_read(self):
        _, _, listing = self.sync()
        listing.assert_not_called()
        self.assertFalse(Meeting.objects.exists())

    def test_the_disk_route_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "granola.json"
            path.write_bytes(self.data["granolaDay1"])
            call_command("import_meetings", str(path), "--quiet", stdout=StringIO())
        self.assertEqual(Meeting.objects.get().external_id, "granola:n1")

    def test_granola_records_stay_off_the_spreadsheet_import_page(self):
        user = make_manager(User.objects.get(username="kellen"))
        ImportBatch.objects.create(filename="granola-2026-10-27.json", sheet="granola:granolaDay1")
        ImportBatch.objects.create(filename="Contacts.xlsx", sheet="Sheet1", created_by=user,
                                   counts={"companies_new": 1, "people_new": 2, "companies_changed": 0,
                                           "people_changed": 0, "skipped": 0})
        self.client.force_login(user)
        page = self.client.get("/import/")
        self.assertContains(page, "Contacts.xlsx")
        self.assertNotContains(page, "granola-2026-10-27.json")
