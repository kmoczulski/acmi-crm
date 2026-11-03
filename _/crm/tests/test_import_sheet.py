"""Import from a spreadsheet (crm/sheet_import/): column guesses, matching companies by name and domain and people by
email, fill-blanks vs update, free-mail and directory-firm domains, in-file duplicates, the pages, and Undo."""
import io
from datetime import timedelta

import openpyxl
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Activity, Company, FieldHistory, Firm, ImportBatch, List, MailFilter, Person
from crm.permissions import PARTNERS
from crm.sheet_import import logic

from .utils import make_manager

User = get_user_model()

HEADER = ["Company", "Website", "Industry", "First Name", "Last Name", "Email", "Job Title", "Phone"]


def xlsx(rows, header=HEADER, extra_sheets=()):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Contacts"
    ws.append(header)
    for r in rows:
        ws.append(r)
    for name, data in extra_sheets:
        other = wb.create_sheet(name)
        for r in data:
            other.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def sheet_of(rows, header=HEADER):
    return logic.read_upload("people.xlsx", xlsx(rows, header))[0]


def plan_of(rows, header=HEADER, **options):
    s = sheet_of(rows, header)
    return logic.build_plan(s["header"], s["rows"], logic.guess_mapping(s["header"]), options, s["numbers"])


class ImportTestCase(TestCase):
    def setUp(self):
        self.kellen = make_manager(User.objects.create_user("kellen", "kmoczulski@acmigroup.com", "x",
                                                            first_name="Kellen", last_name="Moczulski"))
        self.simon = User.objects.create_user("simon", "simon@acmigroup.com", "x", first_name="Simon")
        self.acme = Company.objects.create(name="Acme Rockets", domains=["acmerockets.com"], sector="")
        self.jane = Person.objects.create(first_name="Jane", last_name="Doe", email="jane@acmerockets.com",
                                          company=self.acme, title="")

    def run_import(self, rows, header=HEADER, **options):
        s = sheet_of(rows, header)
        return logic.run(s, logic.guess_mapping(s["header"]), options, self.kellen, "people.xlsx")


class MappingTests(ImportTestCase):
    def test_guesses_columns_from_headers(self):
        guessed = logic.guess_mapping(["Company Name", "Website URL", "Sector", "City", "ST", "Priority", "Status",
                                       "Account Owner", "First", "Last", "Full Name", "E-mail Address", "Title",
                                       "Mobile Phone", "LinkedIn Profile", "Notes", "Favourite colour", "Email 2"])
        self.assertEqual(guessed, [
            "company_name", "company_domain", "company_sector", "company_city", "company_state", "company_priority",
            "company_status", "company_owner", "first_name", "last_name", "full_name", "email", "title", "phone",
            "linkedin", "notes", "", "",  # unknown -> ignore; a second email column isn't used twice
        ])

    def test_reads_csv_and_picks_sheets(self):
        csv_data = "Name;Email;Organization\nBob Smith;bob@zeta.com;Zeta Corp\n;;\n".encode()
        sheets = logic.read_upload("list.csv", csv_data)
        self.assertEqual(sheets[0]["header"], ["Name", "Email", "Organization"])
        self.assertEqual(sheets[0]["rows"], [["Bob Smith", "bob@zeta.com", "Zeta Corp"]])
        book = logic.read_upload("x.xlsx", xlsx([["A Co"]], extra_sheets=[("Second", [["Company"], ["B Co"]])]))
        self.assertEqual([s["name"] for s in book], ["Contacts", "Second"])

    def test_refuses_big_and_unreadable_files(self):
        with self.assertRaisesMessage(logic.SheetError, "limit is 5 MB"):
            logic.read_upload("big.csv", b"x" * (logic.MAX_BYTES + 1))
        with self.assertRaisesMessage(logic.SheetError, "save it as .xlsx"):
            logic.read_upload("old.xls", b"\xd0\xcf\x11\xe0 binary")
        rows = "\n".join(f"Co {i}" for i in range(logic.MAX_ROWS + 1))
        sheet = logic.read_upload("many.csv", f"Company\n{rows}".encode())[0]
        self.assertTrue(sheet["too_big"])
        self.assertEqual(sheet["rows"], [])


class PlanTests(ImportTestCase):
    def test_matches_company_by_name_and_by_domain_and_person_by_email(self):
        plan = plan_of([
            ["acme rockets", "", "", "Jane", "Doe", "JANE@acmerockets.com", "", ""],
            ["Acme Rocket Co Intl", "https://www.acmerockets.com/about", "", "Raj", "Patel", "raj@acmerockets.com", "",
             ""],
            ["", "", "", "Lee", "Wong", "lee@acmerockets.com", "", ""],
            ["Brand New Inc", "brandnew.io", "", "Ann", "Lee", "ann@brandnew.io", "", ""],
        ])
        r1, r2, r3, r4 = plan.rows
        self.assertEqual((r1.company.existing, r1.company.how), (self.acme, "by name"))
        self.assertEqual((r1.person.existing, r1.person.how), (self.jane, "by email"))
        self.assertIs(r2.company, r1.company)  # the website finds Acme too: one company for both rows
        self.assertTrue(any("The sheet calls it" in f for f in r2.flags))
        alone = plan_of([["Acme Rocket Co Intl", "acmerockets.com", "", "", "", "", "", ""]]).rows[0]
        self.assertEqual((alone.company.existing, alone.company.how), (self.acme, "by website"))
        self.assertEqual(r3.company.existing, self.acme)  # no company column: the email domain finds it
        self.assertTrue(r4.company.is_new and r4.person.is_new)
        self.assertEqual(r4.company.new_extras, ["brandnew.io"])
        c = plan.counts
        self.assertEqual((c["companies_new"], c["companies_matched"], c["people_new"], c["people_matched"]),
                         (1, 1, 3, 1))

    def test_free_mail_is_never_a_company_domain(self):
        plan = plan_of([["Solo Works", "", "", "Sam", "Solo", "sam.solo@gmail.com", "", ""],
                        ["", "", "", "Pat", "Free", "pat@yahoo.com", "", ""]])
        self.assertTrue(plan.rows[0].company.is_new)
        self.assertEqual(plan.rows[0].company.new_extras, [])
        self.assertIsNone(plan.rows[1].company)
        self.assertTrue(plan.rows[1].person.is_new)

    def test_directory_firm_and_protected_rows_are_skipped(self):
        Firm.objects.create(name="Truist", kind="bank", domains=["truist.com"])
        MailFilter.objects.create(pattern="secret.com", kind=MailFilter.Kind.PROTECT)
        MailFilter.objects.create(pattern="vendor.com", kind=MailFilter.Kind.IGNORE)
        plan = plan_of([["Truist Bank", "", "", "Tom", "Banker", "tom@truist.com", "", ""],
                        ["Secret Co", "", "", "Sue", "S", "sue@secret.com", "", ""],
                        ["Vendor Co", "vendor.com", "", "Val", "V", "val@vendor.com", "", ""],
                        ["Mine", "", "", "Me", "Too", "me@acmigroup.com", "", ""]])
        truist, secret, vendor, ours = plan.rows
        self.assertIn("belongs to Truist in the directory", truist.skipped)
        self.assertIsNone(truist.company)
        self.assertIn("protected", secret.skipped)
        self.assertFalse(vendor.skipped)
        self.assertEqual(vendor.company.new_extras, [])  # ignored domain: not added
        self.assertTrue(any("ignore list" in f for f in vendor.flags))
        self.assertIn("ACMI address", ours.skipped)
        self.assertEqual(plan.counts["skipped"], 3)

    def test_in_file_duplicates_close_names_and_invalid_emails(self):
        Company.objects.create(name="Zenith Technologies, Inc.")
        plan = plan_of([["Zenith", "", "", "Al", "One", "al@zenith.com", "", ""],
                        ["Zenith", "", "", "Bo", "Two", "bo@zenith.com", "", ""],
                        ["Zenith", "", "", "Al", "Again", "AL@zenith.com", "", ""],
                        ["Other Co", "", "", "Cy", "Bad", "cy at other", "", ""]])
        r1, r2, r3, r4 = plan.rows
        self.assertIs(r1.company, r2.company)  # one company for the three rows
        self.assertEqual([c.name for c in r1.company.close], ["Zenith Technologies, Inc."])
        self.assertIn("Same email as row 2", r3.person_skipped)
        self.assertIsNone(r3.person)
        self.assertTrue(any("Invalid email" in f for f in r4.flags))
        self.assertTrue(r4.person.is_new)  # still a contact, by name
        self.assertEqual(plan.counts["companies_new"], 2)

    def test_unknown_choices_and_owners_are_flagged(self):
        plan = plan_of([["Gamma", "Urgent", "Simon", "Nobody Here"]],
                       header=["Company", "Priority", "Owner", "Account Owner"])
        r = plan.rows[0]
        self.assertTrue(any("Priority “Urgent”" in f for f in r.flags))
        self.assertEqual(r.company.values["owner_id"], self.simon.pk)


class RunTests(ImportTestCase):
    def test_fill_blanks_never_overwrites(self):
        self.acme.sector = "Space"
        self.acme.save()
        batch = self.run_import([["Acme Rockets", "", "Defense", "Jane", "Doe", "jane@acmerockets.com", "CEO",
                                  "555-1234"]])
        self.acme.refresh_from_db()
        self.jane.refresh_from_db()
        self.assertEqual(self.acme.sector, "Space")
        self.assertEqual(self.jane.title, "CEO")  # was blank: filled
        self.assertEqual(self.jane.phones, ["555-1234"])
        self.assertEqual(batch.counts["people_changed"], 1)
        self.assertTrue(Activity.objects.filter(person=self.jane, summary__startswith="Updated from people.xlsx")
                        .exists())

    def test_update_mode_writes_the_sheets_values(self):
        self.acme.sector = "Space"
        self.acme.save()
        self.run_import([["Acme Rockets", "", "Defense", "Jane", "Doe", "jane@acmerockets.com", "CEO", ""]],
                        mode="update")
        self.acme.refresh_from_db()
        self.assertEqual(self.acme.sector, "Defense")

    def test_creates_companies_and_linked_contacts_with_history_and_lists(self):
        batch = self.run_import(
            [["Brand New Inc", "brandnew.io", "Energetics", "Ann", "Lee", "ann@brandnew.io", "VP Ops", "555"],
             ["Brand New Inc", "", "", "Ben", "Ray", "ben@brandnew.io", "", ""]],
            owner=str(self.simon.pk), status="contacted", priority="High",
            company_list={"new": "Sheet prospects"}, person_list=None,
        )
        co = Company.objects.get(name="Brand New Inc")
        self.assertEqual((co.domains, co.sector, co.owner, co.status, co.priority, co.website),
                         (["brandnew.io"], "Energetics", self.simon, "contacted", "High", "https://brandnew.io"))
        self.assertEqual(co.created_by, self.kellen)
        self.assertEqual(set(co.people.values_list("email", flat=True)), {"ann@brandnew.io", "ben@brandnew.io"})
        self.assertTrue(Activity.objects.filter(company=co, person=None, kind=Activity.Kind.RECORD_CREATED,
                                                summary="Imported from people.xlsx", actor=self.kellen).exists())
        self.assertEqual(Activity.objects.filter(company=co, kind=Activity.Kind.RECORD_CREATED).count(), 3)
        self.assertFalse(FieldHistory.objects.exists())
        self.assertEqual(List.objects.get(name="Sheet prospects").entries.count(), 1)
        self.assertEqual(batch.counts["companies_new"], 1)
        self.assertEqual(batch.results[0]["company"]["pk"], co.pk)

    def test_close_name_can_be_matched_on_the_preview(self):
        zen = Company.objects.create(name="Zenith Technologies, Inc.")
        self.run_import([["Zenith", "", "", "Al", "One", "al@zenith.com", "", ""]], use={"zenith": str(zen.pk)})
        self.assertFalse(Company.objects.filter(name="Zenith").exists())
        self.assertEqual(Person.objects.get(email="al@zenith.com").company, zen)

    def test_undo_deletes_what_it_created_and_puts_back_what_it_filled(self):
        batch = self.run_import([["Acme Rockets", "", "Defense", "Jane", "Doe", "jane@acmerockets.com", "CEO", "555"],
                                 ["Brand New Inc", "brandnew.io", "", "Ann", "Lee", "ann@brandnew.io", "", ""]],
                                company_list={"new": "From the sheet"})
        Person.objects.filter(pk=self.jane.pk).update(title="Chief Executive")  # edited since: kept
        out = logic.undo(batch, self.kellen)
        self.assertEqual((out["companies"], out["people"]), (1, 1))
        self.assertFalse(Company.objects.filter(name="Brand New Inc").exists())
        self.assertFalse(Person.objects.filter(email="ann@brandnew.io").exists())
        self.acme.refresh_from_db()
        self.jane.refresh_from_db()
        self.assertEqual(self.acme.sector, "")
        self.assertEqual(self.jane.title, "Chief Executive")
        self.assertEqual(self.jane.phones, [])
        self.assertFalse(List.objects.filter(name="From the sheet").exists())
        self.assertFalse(Activity.objects.filter(summary__startswith="Updated from").exists())
        batch.refresh_from_db()
        self.assertFalse(logic.can_undo(batch))


class PageTests(ImportTestCase):
    def upload(self, data, name="people.xlsx"):
        return self.client.post(reverse("crm:sheet_import"), {"file": SimpleUploadedFile(name, data)})

    def test_upload_map_preview_import_and_undo(self):
        self.client.force_login(self.simon)
        self.assertContains(self.client.get(reverse("crm:company_list") + "?scope=active"),
                            reverse("crm:sheet_import"))
        self.assertContains(self.client.get(reverse("crm:sheet_import")), "Drop a spreadsheet here")
        r = self.upload(xlsx([["Brand New Inc", "brandnew.io", "", "Ann", "Lee", "ann@brandnew.io", "", ""]]))
        self.assertRedirects(r, reverse("crm:sheet_import_map"))
        r = self.client.get(reverse("crm:sheet_import_map"))
        self.assertContains(r, 'value="company_name" selected')
        post = {f"col_{i}": k for i, k in enumerate(logic.guess_mapping(HEADER))}
        r = self.client.post(reverse("crm:sheet_import_map"), {**post, "mode": "fill", "company_list": ""})
        self.assertRedirects(r, reverse("crm:sheet_import_preview"))
        r = self.client.get(reverse("crm:sheet_import_preview"))
        self.assertContains(r, "New company")
        self.assertFalse(Company.objects.filter(name="Brand New Inc").exists())  # the preview writes nothing
        r = self.client.post(reverse("crm:sheet_import_preview"))
        batch = ImportBatch.objects.get()
        self.assertRedirects(r, batch.get_absolute_url())
        self.assertNotIn("sheet_import", self.client.session)  # the file's cells are gone
        r = self.client.get(batch.get_absolute_url())
        self.assertContains(r, "Created")
        self.assertNotContains(r, "Undo this import</button>")  # Simon isn't a manager
        self.client.post(reverse("crm:sheet_import_undo", args=[batch.pk]))
        self.assertTrue(Company.objects.filter(name="Brand New Inc").exists())
        self.client.force_login(self.kellen)
        self.assertContains(self.client.get(batch.get_absolute_url()), "Undo this import</button>")
        self.client.post(reverse("crm:sheet_import_undo", args=[batch.pk]))
        self.assertFalse(Company.objects.filter(name="Brand New Inc").exists())

    def test_friendly_errors_and_old_imports_cant_be_undone(self):
        self.client.force_login(self.kellen)
        r = self.upload(b"\xd0\xcf\x11\xe0", name="old.xls")
        self.assertRedirects(r, reverse("crm:sheet_import"), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("crm:sheet_import")), "save it as .xlsx")
        batch = self.run_import([["Brand New Inc", "", "", "", "", "", "", ""]])
        ImportBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(days=8))
        self.client.post(reverse("crm:sheet_import_undo", args=[batch.pk]))
        self.assertTrue(Company.objects.filter(name="Brand New Inc").exists())

    def test_partners_never_reach_it(self):
        pat = User.objects.create_user("pat@fortress.com", "pat@fortress.com", "x")
        pat.groups.add(Group.objects.get_or_create(name=PARTNERS)[0])
        self.client.force_login(pat)
        r = self.client.get(reverse("crm:sheet_import"))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/room"))
