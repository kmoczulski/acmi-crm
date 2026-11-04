"""Drive folders on companies and projects, current versions, sharing a Drive file in the data room, and serving it to
the partner through the hub (crm/drive.py, crm/dataroom/drive_views.py). Drive itself is a fake here."""
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from crm import drive
from crm.dataroom import logic
from crm.models import Campus, Company, DataRoom, DataRoomFile, DataRoomMember, DataRoomView, Document, Opportunity

from .utils import make_manager

User = get_user_model()
FOLDER = drive.FOLDER


def item(id_, name, mime="application/pdf", modified="2026-10-01T12:00:00Z"):
    return {"id": id_, "name": name, "mimeType": mime, "modifiedTime": modified,
            "webViewLink": f"https://drive.google.com/file/d/{id_}/view"}


class FakeDrive:
    """A folder tree {folder id: [items]} and file bytes {file id: bytes}."""

    def __init__(self, tree, data=None):
        self.tree, self.data = tree, data or {}

    def children(self, folder_id):
        return list(self.tree.get(folder_id, []))

    def meta(self, file_id):
        for items in self.tree.values():
            for i in items:
                if i["id"] == file_id:
                    return {**i, "trashed": False}
        raise RuntimeError("404")

    def download(self, file_id, mime):
        body = self.data.get(file_id, b"%PDF fake")
        resp = mock.Mock()
        resp.iter_content = lambda size: iter([body])
        resp.headers = {"Content-Type": "application/pdf"}
        return resp, (".pdf" if mime in drive.EXPORTS else "")


def ara_tree():
    return {
        "ARAFOLDER01": [item("LOIF", "LOI", FOLDER), item("PDAF", "PDA", FOLDER), item("ARCH", "_Archive", FOLDER),
                item("SUM", "ACMI_Properties_Site_Summary_ARA_2026.06.15.docx", "application/msword"),
                item("INI", "desktop.ini", "text/plain"),
                item("NOTES", "ARA_response_to_redline_INTERNAL.docx", "application/msword")],
        "LOIF": [item("LOI_DOCX", "ARA LOI - MEIH - 2026.03.23.docx", "application/msword"),
                 item("LOI_PDF", "ARA LOI - MEIH - 2026.03.23.pdf"),
                 item("LOI_OLD", "ARA LOI - MEIH - 2026.02.10.pdf")],
        "PDAF": [item("PDA_FINAL", "ACMI Preliminary Development Agreement_FINAL_5_15_26 (002).pdf"),
                 item("LOCK", "~$ARA_Architect_Tracker.xlsx")],
        "ARCH": [item("ANCIENT", "ARA LOI - MEIH - 2025.12.01.pdf")],
    }


class VersionTests(SimpleTestCase):
    def test_dates_in_the_teams_names(self):
        self.assertEqual(drive.name_date("ARA LOI - MEIH - 2026.03.23.pdf"), date(2026, 3, 23))
        self.assertEqual(drive.name_date("ACMI_HoarMBI_MeetingNotes_20260910.docx"), date(2026, 9, 10))
        self.assertEqual(drive.name_date("Agreement_FINAL_5_15_26 (002).pdf"), date(2026, 5, 15))
        self.assertEqual(drive.name_date("2025 07 03 Livingston Cover Letter.pdf"), date(2025, 7, 3))
        self.assertIsNone(drive.name_date("Overview.pptx"))

    def test_versions_group_and_the_newest_pdf_wins(self):
        files = [drive.DriveFile(i["id"], i["name"], i["mimeType"], None, "LOI") for i in ara_tree()["LOIF"]]
        docs = drive.current_documents(files)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].current.id, "LOI_PDF")
        self.assertEqual({v.id for v in docs[0].older}, {"LOI_DOCX", "LOI_OLD"})

    def test_internal_names(self):
        self.assertTrue(drive.is_internal("ACMI_LOI_response_to_Fortus_redline_INTERNAL.docx"))
        self.assertTrue(drive.is_internal("PDA draft v2.docx"))
        self.assertFalse(drive.is_internal("International Overview.pdf"))

    def test_links(self):
        self.assertEqual(drive.folder_id_from("https://drive.google.com/drive/folders/1AbCdEfGhIjK?usp=sharing"), "1AbCdEfGhIjK")
        self.assertEqual(drive.file_id_from("https://docs.google.com/document/d/1XyZ0123456789/edit"), "1XyZ0123456789")
        self.assertEqual(drive.file_id_from("https://example.com/x.pdf"), "")


@override_settings(DRIVE_SA_FILE="fake.json", DRIVE_TENANCY_FOLDER="TENANCY")
class DrivePanelTests(TestCase):
    def setUp(self):
        cache.clear()
        self.fake = FakeDrive({**ara_tree(), "TENANCY": [item("ARAFOLDER01", "Applied Research Associates", FOLDER),
                                                         item("CAS", "Castelion (Ranger)", FOLDER)]})
        patcher = mock.patch("crm.drive.client", return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.kellen = make_manager(User.objects.create_user("Kellen", "k@acmigroup.com", "x"))
        self.client.force_login(self.kellen)
        self.meih = Campus.objects.create(code="MEIH", name="MEIH")
        self.ara = Company.objects.create(name="Applied Research Associates")
        self.deal = Opportunity.objects.create(name="ARA · MEIH", company=self.ara, campus=self.meih, stage="loi_signed",
                                               investor_report=True)
        self.room = DataRoom.objects.create(name="Fortress")

    def panel(self, refresh=False):
        return self.client.get(reverse("crm:drive_panel", args=["company", self.ara.pk]) + ("?refresh=1" if refresh else ""))

    def test_suggests_the_tenancy_folder_by_name_and_maps_it(self):
        self.assertContains(self.panel(), "Applied Research Associates")
        self.client.post(reverse("crm:drive_folder_set", args=["company", self.ara.pk]), {"folder": "https://drive.google.com/drive/folders/ARAFOLDER01?usp=sharing"})
        self.ara.refresh_from_db()
        self.assertEqual(self.ara.drive_folder, "ARAFOLDER01")
        self.assertEqual(drive.suggest_folder("Castelion"), ("Castelion (Ranger)", "CAS"))

    def test_panel_lists_current_versions_and_skips_archive_and_junk(self):
        self.ara.drive_folder = "ARAFOLDER01"
        self.ara.save()
        r = self.panel()
        self.assertContains(r, "ARA LOI - MEIH - 2026.03.23.pdf")
        self.assertContains(r, "2 older")
        self.assertNotContains(r, "2025.12.01")  # in _Archive
        self.assertNotContains(r, "desktop.ini")
        self.assertNotContains(r, "~$ARA")
        self.assertContains(r, "Internal")
        self.assertContains(r, "Share in Fortress room")

    def test_file_becomes_the_deals_loi_and_feeds_roc_on_card_and_report(self):
        from decimal import Decimal

        from crm.reports import cell_value, pipeline_workbook

        self.ara.drive_folder = "ARAFOLDER01"
        self.ara.save()
        self.assertContains(self.panel(), "Use as lease / LOI")
        r = self.client.post(reverse("crm:drive_set_lease", args=[self.ara.pk]),
                             {"file": "LOI_PDF", "opportunity": self.deal.pk})
        self.assertRedirects(r, self.ara.get_absolute_url() + "?tab=opportunities", fetch_redirect_response=False)
        self.deal.refresh_from_db()
        self.assertEqual((self.deal.lease_doc_kind, self.deal.lease_doc_name), ("loi", "ARA LOI - MEIH - 2026.03.23.pdf"))
        self.assertEqual(self.deal.lease_doc_url, "https://drive.google.com/file/d/LOI_PDF/view")
        # An old version isn't offered.
        self.client.post(reverse("crm:drive_set_lease", args=[self.ara.pk]), {"file": "LOI_OLD", "opportunity": self.deal.pk})
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.lease_doc_name, "ARA LOI - MEIH - 2026.03.23.pdf")

        self.deal.value, self.deal.annual_rent = Decimal("20000000"), Decimal("1700000")
        self.deal.save()
        self.assertEqual(cell_value(self.deal, "roc"), 0.085)
        card = self.client.get(self.ara.get_absolute_url() + "?tab=opportunities")
        self.assertContains(card, "8.5%")
        self.assertContains(card, "https://drive.google.com/file/d/LOI_PDF/view")
        report = self.client.get(reverse("crm:pipeline_report"))
        self.assertContains(report, "8.5%")
        self.assertContains(report, ">LOI</a>")
        from crm.reports import chosen_columns
        from openpyxl import load_workbook
        from io import BytesIO
        cols = chosen_columns(["tenant", "cost", "rent", "roc", "lease_doc"])
        wb = load_workbook(BytesIO(pipeline_workbook({"as_of": date(2026, 11, 4), "sites": [], "sections": [
            {"title": "LOI signed", "rows": [self.deal]}]}, cols)))
        ws = wb.active
        values = [c.value for row in ws.iter_rows() for c in row if c.value]
        self.assertIn("=IFERROR(C7/B7,\"\")", values)
        self.assertTrue(any(c.hyperlink for row in ws.iter_rows() for c in row))

    def test_sharing_only_from_the_folder_internal_needs_confirming(self):
        self.ara.drive_folder = "ARAFOLDER01"
        self.ara.save()
        url = reverse("crm:drive_share", args=["company", self.ara.pk])
        self.client.post(url, {"file": "SOMEONE_ELSES_FILE", "room": self.room.pk})
        self.client.post(url, {"file": "LOI_OLD", "room": self.room.pk})  # not the current version
        self.client.post(url, {"file": "NOTES", "room": self.room.pk})  # internal, unconfirmed
        self.assertFalse(DataRoomFile.objects.exists())
        self.client.post(url, {"file": "LOI_PDF", "room": self.room.pk})
        shared = DataRoomFile.objects.get()
        self.assertEqual((shared.drive_id, shared.opportunity, shared.drive_folder), ("LOI_PDF", self.deal, "LOI"))
        self.client.post(url, {"file": "NOTES", "room": self.room.pk, "confirm_internal": "1"})
        self.assertEqual(DataRoomFile.objects.count(), 2)

    def test_only_managers_share(self):
        self.ara.drive_folder = "ARAFOLDER01"
        self.ara.save()
        self.client.force_login(User.objects.create_user("Simon", password="x"))
        self.client.post(reverse("crm:drive_share", args=["company", self.ara.pk]), {"file": "LOI_PDF", "room": self.room.pk})
        self.assertFalse(DataRoomFile.objects.exists())

    def test_a_newer_version_is_flagged_and_swapped_only_when_asked(self):
        self.ara.drive_folder = "ARAFOLDER01"
        self.ara.save()
        self.client.post(reverse("crm:drive_share", args=["company", self.ara.pk]), {"file": "LOI_PDF", "room": self.room.pk})
        shared = DataRoomFile.objects.get()
        # A new dated LOI arrives; the old ones move to _Archive.
        self.fake.tree["LOIF"] = [item("LOI_NEW", "ARA LOI - MEIH - 2026.10.20.pdf")]
        self.fake.tree["ARCH"] += [item("LOI_PDF", "ARA LOI - MEIH - 2026.03.23.pdf")]
        r = self.panel(refresh=True)
        self.assertContains(r, "Newer version.")
        shared.refresh_from_db()
        self.assertEqual(shared.drive_id, "LOI_PDF")  # nothing changes until a Manager swaps
        page = self.client.get(reverse("crm:data_room_manage", args=[self.room.pk]))
        self.assertContains(page, "Newer version in Drive")
        self.client.post(reverse("crm:drive_swap", args=[shared.pk]), {"next": "https://evil.example/"})
        shared.refresh_from_db()
        self.assertEqual((shared.drive_id, shared.drive_name), ("LOI_NEW", "ARA LOI - MEIH - 2026.10.20.pdf"))


@override_settings(DRIVE_SA_FILE="fake.json")
class PartnerDriveDownloadTests(TestCase):
    def setUp(self):
        cache.clear()
        self.fake = FakeDrive(ara_tree(), {"LOI_PDF": b"%PDF the LOI"})
        patcher = mock.patch("crm.drive.client", return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        kellen = make_manager(User.objects.create_user("Kellen", "k@acmigroup.com", "x"))
        meih = Campus.objects.create(code="MEIH", name="MEIH")
        self.deal = Opportunity.objects.create(name="ARA · MEIH", company=Company.objects.create(name="ARAFOLDER01"), campus=meih,
                                               stage="loi_signed", investor_report=True)
        self.room = DataRoom.objects.create(name="Fortress")
        self.shared = DataRoomFile.objects.create(room=self.room, opportunity=self.deal, drive_id="LOI_PDF",
                                                  drive_name="ARA LOI - MEIH - 2026.03.23.pdf", drive_folder="LOI",
                                                  title="ARA LOI - MEIH - 2026.03.23.pdf", approved_by=kellen)
        member = DataRoomMember.objects.create(room=self.room, name="Pat", email="pat@fortress.com")
        link = logic.issue_invite(member, kellen)
        pw = "harbor-lantern-quietly-71"
        self.client.post(link, {"first_name": "Pat", "last_name": "I", "password1": pw, "password2": pw})
        self.member = DataRoomMember.objects.get(pk=member.pk)

    def test_the_partner_downloads_through_the_hub_and_never_sees_drive(self):
        page = self.client.get(reverse("crm:room_deal", args=[self.deal.pk]))
        self.assertContains(page, "ARA LOI - MEIH - 2026.03.23.pdf")
        self.assertNotContains(page, "drive.google.com")
        self.assertNotContains(page, "LOI_PDF")
        r = self.client.get(reverse("crm:room_file", args=[self.shared.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"%PDF the LOI")
        self.assertIn("ARA LOI - MEIH - 2026.03.23.pdf", r["Content-Disposition"])
        self.assertTrue(DataRoomView.objects.filter(member=self.member, page="file").exists())

    def test_a_deal_documents_drive_link_is_served_too(self):
        doc = Document.objects.create(opportunity=self.deal, title="LOI", url="https://drive.google.com/file/d/LOI_PDF_X01/view")
        self.fake.tree["LOIF"].append(item("LOI_PDF_X01", "LOI copy.pdf"))
        f = DataRoomFile.objects.create(room=self.room, opportunity=self.deal, document=doc, title="LOI")
        r = self.client.get(reverse("crm:room_file", args=[f.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.has_header("Location"))

    def test_drive_trouble_shows_a_plain_message(self):
        with mock.patch.object(self.fake, "download", side_effect=RuntimeError("quota")):
            r = self.client.get(reverse("crm:room_file", args=[self.shared.pk]))
        self.assertEqual(r.status_code, 502)
        self.assertNotIn(b"quota", r.content)
