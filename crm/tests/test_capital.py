"""Capitalization: sources per project and the stack, status moves and the log, term sheets (upload, Claude reading
the terms with their quotes, versions side by side), touches from emails and Granola meetings, the portfolio page,
and the dashboard / calendar / reminders / campus summary hooks."""
import json
import tempfile
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import campus as campus_mod
from crm import notify
from crm.capital import logic
from crm.contracts import dates
from crm.models import (
    Activity, CapitalLog, CapitalSource, Campus, Company, Contract, Meeting, Notification, Project, TermSheet,
)
from crm.tests.test_contracts import fake_claude, make_docx, text_block
from crm.tests.utils import make_manager

User = get_user_model()

TERM_SHEET = ("SUMMARY OF TERMS\nBorrower: ACMI MEIH Owner LLC\nLoan Amount: up to $42,000,000\n"
              "Interest Rate: 1-month Term SOFR plus 3.25% per annum, with a SOFR floor of 3.00%\n"
              "Term: 36 months, with two 12-month extension options\nLoan-to-Cost: 60%\n"
              "Origination Fee: 1.00% of the Loan Amount\nRecourse: 25% repayment guaranty; completion guaranty\n"
              "This term sheet expires if not accepted by November 14, 2026.")


def at(day, hour=10):
    return timezone.make_aware(datetime.combine(day, time(hour)))


class CapitalBase(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.meih = Project.objects.create(name="MEIH", campus=self.campus)
        self.ara = Project.objects.create(name="ARA Facility", campus=self.campus, parent=self.meih)
        self.fortress = Company.objects.create(name="Fortress Investment Group LLC", domains=["fortress.com"])
        self.bank = Company.objects.create(name="Truist Bank", domains=["truist.com"])

    def source(self, **kw):
        kw.setdefault("project", self.ara)
        kw.setdefault("owner", self.user)
        return CapitalSource.objects.create(**kw)


class SourceTests(CapitalBase):
    def test_quick_add_links_a_known_company_and_logs(self):
        r = self.client.post(f"/projects/{self.ara.pk}/capital/add/",
                             {"provider": "truist bank", "kind": "construction", "status": "outreach",
                              "amount_sought": "42000000"})
        s = CapitalSource.objects.get()
        self.assertRedirects(r, s.get_absolute_url())
        self.assertEqual(s.company, self.bank)
        self.assertEqual(s.status_since, self.today)
        self.assertTrue(s.log.filter(kind="status").exists())

    def test_unknown_provider_is_kept_by_name_then_added_as_company(self):
        self.client.post(f"/projects/{self.ara.pk}/capital/add/", {"provider": "Bank OZK", "kind": "construction",
                                                                   "status": "identified"})
        s = CapitalSource.objects.get()
        self.assertIsNone(s.company)
        self.assertEqual(s.provider, "Bank OZK")
        self.assertContains(self.client.get(s.get_absolute_url()), "isn't a company in the CRM")
        self.client.post(s.get_absolute_url(), {"action": "add_company"})
        s.refresh_from_db()
        self.assertEqual(s.company.name, "Bank OZK")
        self.assertEqual(s.provider_name, "")

    def test_status_change_is_dated_and_logged_and_advance_never_goes_back(self):
        s = self.source(company=self.bank, kind="construction", status="discussion")
        self.client.post(s.get_absolute_url(), {"action": "status", "status": "approved"})
        s.refresh_from_db()
        self.assertEqual(s.status, "approved")
        self.assertIn("In discussion → Committee / approved", s.log.get(kind="status").text)
        self.assertFalse(logic.advance(s, "term_sheet"))
        s.status = "passed"
        self.assertFalse(logic.advance(s, "closing"))

    def test_stack_counts_one_quote_per_loan_and_skips_passed(self):
        self.source(company=self.fortress, kind="equity", status="closed", amount_sought=Decimal("30000000"),
                    amount_committed=Decimal("28000000"))
        self.source(company=self.bank, kind="construction", amount_sought=Decimal("42000000"))
        self.source(provider_name="Bank OZK", kind="construction", amount_sought=Decimal("40000000"))
        self.source(provider_name="Mezz Co", kind="mezz", status="passed", amount_sought=Decimal("8000000"))
        st = logic.stack(self.ara)
        self.assertEqual([g["name"] for g in st["groups"]], ["Equity", "Debt"])
        self.assertEqual(st["total"]["sought"], Decimal("72000000"))  # 30M equity + the larger construction quote
        self.assertEqual(st["total"]["committed"], Decimal("28000000"))
        self.assertEqual(st["groups"][1]["rows"][0].kind, "mezz")  # mezz sits above the senior loan

    def test_start_loan_agreement_makes_a_contract(self):
        s = self.source(company=self.bank, kind="construction", amount_sought=Decimal("42000000"))
        self.client.post(s.get_absolute_url(), {"action": "start_contract"})
        s.refresh_from_db()
        self.assertEqual(s.contract.kind, Contract.Kind.LOAN)
        self.assertEqual(s.contract.company, self.bank)
        self.assertEqual(s.contract.campus, self.campus)
        eq = self.source(company=self.fortress, kind="equity")
        self.client.post(eq.get_absolute_url(), {"action": "start_contract"})
        eq.refresh_from_db()
        self.assertEqual(eq.contract.kind, Contract.Kind.JV)

    def test_pages_render(self):
        s = self.source(company=self.bank, kind="construction", waiting_on="them", waiting_since=self.today)
        TermSheet.objects.create(source=s, amount=Decimal("42000000"), rate="SOFR + 3.25%",
                                 expires_on=self.today + timedelta(days=5))
        for url in ("/capital/", f"/projects/{self.ara.pk}/capital/", s.get_absolute_url(), "/capital/?show=all&kind=debt",
                    f"/capital/?campus=MEIH", f"/projects/{self.ara.pk}/", "/projects/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get("/capital/"), "Truist Bank")
        self.assertContains(self.client.get(f"/projects/{self.ara.pk}/"), "Capital")

    def test_only_managers_delete(self):
        s = self.source(company=self.bank)
        other = User.objects.create_user("dave")
        self.client.force_login(other)
        self.client.post(s.get_absolute_url(), {"action": "delete"})
        self.assertTrue(CapitalSource.objects.filter(pk=s.pk).exists())


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class TermSheetTests(CapitalBase):
    def setUp(self):
        super().setUp()
        self.s = self.source(company=self.bank, kind="construction", status="discussion")

    def upload(self, **extra):
        data = {"action": "ts_add", "new-kind": "term_sheet", "new-status": "received",
                "new-dated": str(self.today),
                "new-file": SimpleUploadedFile("Truist_term_sheet.docx", make_docx(TERM_SHEET.split("\n")))}
        data.update(extra)
        return self.client.post(self.s.get_absolute_url(), data)

    def test_upload_reads_text_versions_and_advances(self):
        self.upload()
        ts = self.s.term_sheets.get()
        self.assertEqual(ts.version, 1)
        self.assertEqual(ts.text_status, "ok")
        self.assertIn("Loan-to-Cost", ts.text)
        self.assertEqual(ts.title, "Truist term sheet")
        self.s.refresh_from_db()
        self.assertEqual(self.s.status, "term_sheet")
        self.upload()
        self.assertEqual(sorted(self.s.term_sheets.values_list("version", flat=True)), [1, 2])

    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"})
    def test_claude_fills_blank_terms_with_quotes(self):
        found = {"terms": [
            {"field": "amount", "value": "42000000", "quote": "Loan Amount: up to $42,000,000", "note": "the maximum"},
            {"field": "rate", "value": "SOFR + 3.25%", "quote": "1-month Term SOFR plus 3.25% per annum", "note": ""},
            {"field": "spread_bps", "value": "325", "quote": "plus 3.25% per annum", "note": ""},
            {"field": "ltc_pct", "value": "60", "quote": "Loan-to-Cost: 60%", "note": ""},
            {"field": "term_months", "value": "36", "quote": "Term: 36 months", "note": ""},
            {"field": "expires_on", "value": "2026-11-14", "quote": "expires if not accepted by November 14, 2026",
             "note": ""},
            {"field": "origination_fee_pct", "value": "1.00", "quote": "Origination Fee: 1.00%", "note": ""},
        ]}
        patch, stream = fake_claude([text_block(json.dumps(found))])
        with patch:
            self.upload(read="1")
        ts = self.s.term_sheets.get()
        self.assertEqual(ts.amount, Decimal("42000000"))
        self.assertEqual(ts.spread_bps, Decimal("325"))
        self.assertEqual(ts.ltc_pct, Decimal("60"))
        self.assertEqual(ts.term_months, 36)
        self.assertEqual(str(ts.expires_on), "2026-11-14")
        src = ts.sources["ltc_pct"]
        self.assertEqual(ts.text[src["start"]:src["start"] + 10], "Loan-to-Co")
        self.assertEqual(src["page"], 1)
        kwargs = stream.call_args.kwargs
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("citations", kwargs["messages"][0]["content"][0])
        # A second read leaves typed (or already read) terms alone
        ts.amount = Decimal("40000000")
        ts.save()
        patch, _ = fake_claude([text_block(json.dumps(found))])
        with patch:
            self.client.post(self.s.get_absolute_url(), {"action": "ts_read", "ts": ts.pk})
        ts.refresh_from_db()
        self.assertEqual(ts.amount, Decimal("40000000"))

    def test_editing_a_term_drops_its_quote(self):
        ts = TermSheet.objects.create(source=self.s, amount=Decimal("42000000"), rate="SOFR + 3.25%",
                                      sources={"rate": {"quote": "SOFR plus 3.25%"}})
        data = {f"ts{ts.pk}-{k}": v for k, v in {"kind": "term_sheet", "status": "received",
                                                  "amount": "42000000", "rate": "SOFR + 3.00%"}.items()}
        data.update(action="ts_edit", ts=ts.pk)
        self.client.post(self.s.get_absolute_url(), data)
        ts.refresh_from_db()
        self.assertEqual(ts.rate, "SOFR + 3.00%")
        self.assertNotIn("rate", ts.sources)

    def test_compare_marks_changes(self):
        v1 = TermSheet.objects.create(source=self.s, version=1, amount=Decimal("42000000"), rate="SOFR + 3.50%",
                                      ltc_pct=Decimal("60"))
        v2 = TermSheet.objects.create(source=self.s, version=2, amount=Decimal("42000000"), rate="SOFR + 3.25%",
                                      ltc_pct=Decimal("60"))
        rows = {r["field"]: r for r in logic.compare([v1, v2])}
        self.assertTrue(rows["rate"]["changed"])
        self.assertFalse(rows["amount"]["changed"])
        self.assertNotIn("pref_return_pct", rows)
        r = self.client.get(f"/capital/compare/?ts={v1.pk}&ts={v2.pk}")
        self.assertContains(r, "SOFR + 3.25%")
        self.assertContains(r, "report-logo")
        self.assertContains(self.client.get(self.s.get_absolute_url()), "Versions side by side")


class TouchTests(CapitalBase):
    def test_emails_and_meetings_reach_the_source(self):
        s = self.source(company=self.fortress, kind="equity", status="closed")
        yesterday = self.today - timedelta(days=1)
        Activity.objects.create(occurred_at=at(yesterday), kind=Activity.Kind.EMAIL_RECEIVED, company=self.fortress,
                                summary="Re: MEIH capital call schedule")
        other = Project.objects.create(name="NSIH")
        elsewhere = Activity.objects.create(occurred_at=at(yesterday), kind=Activity.Kind.EMAIL_RECEIVED,
                                            company=self.fortress, summary="NSIH only")
        elsewhere.projects.add(other)
        Meeting.objects.create(source="granola", external_id="g1", title="Fortress / ACMI weekly",
                               start=at(self.today - timedelta(days=3)))
        Meeting.objects.create(source="granola", external_id="g2", title="Lender call",
                               start=at(self.today - timedelta(days=4)),
                               attendees=[{"name": "Pat", "email": "pat@fortress.com"}])
        Meeting.objects.create(source="granola", external_id="g3", title="Fortressville zoning",
                               start=at(self.today - timedelta(days=2)))
        texts = [t.text for t in logic.touches(s, self.today)]
        self.assertIn("Re: MEIH capital call schedule", texts)
        self.assertNotIn("NSIH only", texts)
        self.assertIn("Fortress / ACMI weekly", texts)
        self.assertIn("Lender call", texts)
        self.assertNotIn("Fortressville zoning", texts)
        self.assertEqual(logic.last_touch(s), (yesterday, "email received"))

    def test_short_name(self):
        self.assertEqual(logic.short_name("Fortress Investment Group LLC"), "Fortress")
        self.assertEqual(logic.short_name("The Capital Group"), "")

    def test_log_entry(self):
        s = self.source(company=self.bank)
        self.client.post(s.get_absolute_url(), {"action": "log", "kind": "call", "text": "Wants the rent roll",
                                                "happened_on": str(self.today)})
        self.assertEqual(s.log.get(kind="call").text, "Wants the rent roll")
        s.refresh_from_db()
        logic.annotate([s], self.today)
        self.assertEqual(s.touch_on, self.today)
        self.assertFalse(s.quiet)


class HookTests(CapitalBase):
    def setUp(self):
        super().setUp()
        self.s = self.source(company=self.bank, kind="construction", status="term_sheet",
                             next_step="Send the appraisal", next_step_on=self.today + timedelta(days=1))
        self.v1 = TermSheet.objects.create(source=self.s, version=1, expires_on=self.today + timedelta(days=3))
        self.v2 = TermSheet.objects.create(source=self.s, version=2, expires_on=self.today + timedelta(days=7),
                                           close_by=self.today + timedelta(days=40))

    def test_deadlines_show_the_current_version_and_next_step(self):
        rows = [r for r in dates.upcoming(14, self.today)["soon"] if r.tag == "Capital"]
        labels = sorted(r.label for r in rows)
        self.assertEqual(labels, ["Send the appraisal", "Term sheet v2 expires"])

    def test_calendar_only_takes_accepted_term_sheets(self):
        first, last = self.today - timedelta(days=5), self.today + timedelta(days=60)
        self.assertFalse([r for r in dates.critical_between(first, last, self.today) if r.tag == "Loan"])
        self.v2.status = TermSheet.Status.ACCEPTED
        self.v2.save()
        rows = [r for r in dates.critical_between(first, last, self.today) if r.tag == "Loan"]
        self.assertEqual(sorted(r.label for r in rows), ["Close by", "Term sheet expires"])

    def test_reminders(self):
        notify.due_sweep(self.user)
        texts = list(Notification.objects.values_list("text", flat=True))
        self.assertTrue(any("Truist Bank's term sheet expires in 7 days" in t for t in texts), texts)
        self.assertTrue(any("Send the appraisal in 1 day" in t for t in texts), texts)
        notify.due_sweep(self.user)
        self.assertEqual(Notification.objects.count(), len(texts))  # keyed, so nothing repeats

    def test_campus_summary_lines(self):
        TermSheet.objects.filter(pk=self.v2.pk).update(rate="SOFR + 3.25%", amount=Decimal("42000000"))
        item = next(p for p in campus_mod.summary(self.campus, self.today)["projects"] if p["obj"] == self.ara)
        self.assertTrue(any("Construction loan with Truist Bank: term sheet" in f and "SOFR + 3.25%" in f
                            for f in item["facts"]), item["facts"])
        self.assertTrue(any("expires in 7 days" in f for f in item["flags"]), item["flags"])
        self.assertTrue(any("no touch recorded" in f for f in item["flags"]), item["flags"])

    def test_closed_or_passed_sources_drop_off(self):
        self.s.status = "passed"
        self.s.save()
        self.assertFalse(logic.due_rows(30, self.today))
