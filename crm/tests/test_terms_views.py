"""The LOI / PSA terms tracker's pages: the rounds side by side, adding and editing a round, the live preview, agreeing a
round, who may delete, the contract page's hook, and loading a team LOI Tracker workbook."""
import io
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

import openpyxl
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from crm.contracts import terms, terms_import
from crm.models import Campus, Contract, ContractDocument, LandSite, TermsRound
from crm.tests.test_terms import ACMI_COUNTER, INITIAL_OFFER, SELLER_REDLINE
from crm.tests.utils import make_manager

User = get_user_model()
D = Decimal


def lewis_workbook(drop_block=False):
    """A workbook laid out like the Lewis Place "LOI Tracker": the same labels, spelling slips and all, the phase blocks,
    the closing-extension rows after the last phase, and values (not formulas, which openpyxl can't cache)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MD - LEWIS PLACE LOI"
    ws["B2"], ws["B3"], ws["C3"] = "DEAL INFORMATION", "Deal Name", "Lewis Place"
    if drop_block:
        ws["B14"] = "SOMETHING ELSE"
        return wb
    ws["B14"], ws["C14"], ws["H14"], ws["M14"] = "LOI INFO", "ACMI INITIAL OFFER", "SELLER REDLINE", "ACMI COUNTER"
    dt = datetime
    rows = [
        ("Date Sent/Received", [dt(2026, 9, 1), dt(2026, 10, 23), dt(2026, 10, 30)]),
        ("Ancitipated Signed PSA", [dt(2026, 10, 31), dt(2026, 11, 6), dt(2026, 11, 13)]),
        ("Subdivision?", ["No", "No", "No"]),
        ("Months to Final Zoning Approval (from PSA)", [48, 16, 16]),
        ("Initial Purchase Size (acres)", [182.99, 182.99, 182.99]),
        ("Feasibility Period (days)", [60, 60, 120]),
        ("Extention Period (days)", [0, 0, 60]),
        ("# of Extention Options Available", [0, 2, 1]),
        ("# of Extention Options Exercised", [0, 2, 1]),
        ("Price/Acre", [81972.05, 100000, 90000]),
        ("Price", [15000065.4295, 18299000, 16469100]),
        ("Intitial Deposit", [25000, 50000, 50000]),
        ("Non-Refundable Portion", [0, 50000, 25000]),
        ("Feasability Period Extenstion Deposit", ["N/A", "N/A", 25000]),
        ("30-Day Deposit - Non-Refundable", ["N/A", None, None]),
        ("Non-Refundable Deposit per Extention", [0, None, None]),  # a later, blank row must not wipe the 25,000
        ("Deposit after Expiration of Feasilbity Period", [0, 250000, 100000]),
        ("Non-Redunable Portion", [0, 100000, 50000]),
        ("Days to Close Following Final Zoning Approval", [60, 30, 30]),
        ("Phase II", None),
        ("Purchase Size (acres)", [None, None, None]),
        ("Period to Commece Feasability (days)", [None, None, None]),
        ("Days to Close Post-Dilligence Period", [0, None, None]),
        ("Phase III", None),
        ("Purchase Size (acres)", [0, None, None]),
        ("Extention Period (days)", [0, None, None]),
        ("# of Extention Options Available", [0, None, None]),
        ("# of Extention Options Exercised", [0, None, None]),
        ("Extenstion Period ", ["N/A", "N/A", 60]),  # after Phase III: the closing extension
        ("# of Extention Options Available", ["N/A", "N/A", 2]),
        ("# of Extention Options Exercised", ["N/A", "N/A", 2]),
        ("Deposit Amount", ["N/A", "N/A", 25000]),
        ("Non-Refundable Portion", ["N/A", "N/A", 25000]),
        ("Outside Date to Close ", ["N/A", 18, 24]),
        ("LOI TRACKER", None),
        ("Intitial Deposit - Phase I", [999, 999, 999]),  # the computed block below the title is never read
    ]
    for i, (label, values) in enumerate(rows):
        r = 15 + i
        ws.cell(r, 2, label)
        for col, v in zip((3, 8, 13), values or []):
            if v is not None:
                ws.cell(r, col, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TermsTestCase(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.site = LandSite.objects.create(name="Lewis Place", status="loi", campus=self.campus)
        self.c = Contract.objects.create(title="Lewis Place LOI", kind="loi", site=self.site, campus=self.campus,
                                         owner=self.user, counterparty="Randall Williams")
        self.url = f"/contracts/{self.c.pk}/terms/"

    def make_round(self, raw, label, sent_by, dated, **kw):
        return TermsRound.objects.create(contract=self.c, label=label, sent_by=sent_by, dated=dated, terms=raw,
                                         created_by=self.user, **kw)

    def make_three(self):
        return [self.make_round(INITIAL_OFFER, "ACMI initial offer", "us", date(2026, 9, 1)),
                self.make_round(SELLER_REDLINE, "Seller redline", "them", date(2026, 10, 23)),
                self.make_round(ACMI_COUNTER, "ACMI counter", "us", date(2026, 10, 30))]

    def post_data(self, **extra):
        """A filled-in new-round form: ACMI's counter, as the boxes would send it."""
        data = {
            "label": "ACMI counter", "sent_by": "us", "dated": "2026-10-30", "notes": "", "slots": "2",
            "r_psa_days": "14", "r_zoning_months": "16", "r_outside_months": "24", "r_subdivision": "no",
            "p1_acres": "182.99", "p1_price_per_acre": "90,000", "p1_feasibility_days": "120",
            "p1_feas_ext_days": "60", "p1_feas_ext_options": "1", "p1_feas_ext_used": "1",
            "p1_feas_ext_deposit": "25,000", "p1_close_days": "30", "p1_initial_deposit": "50,000",
            "p1_initial_nonref": "25,000", "p1_go_hard": "100,000", "p1_go_hard_nonref": "50,000",
            "p1_close_ext_days": "60", "p1_close_ext_options": "2", "p1_close_ext_used": "2",
            "p1_close_ext_deposit": "25,000", "p1_close_ext_nonref": "25,000",
        }
        data.update(extra)
        return data


class TrackerPageTests(TermsTestCase):
    def test_empty_page_invites_the_first_round_and_the_import(self):
        r = self.client.get(self.url)
        self.assertContains(r, "No rounds yet")
        self.assertContains(r, "Add the first round")
        self.assertContains(r, "Import rounds from a team LOI Tracker workbook")

    def test_three_rounds_side_by_side(self):
        self.make_three()
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        for text in ("ACMI initial offer", "Seller redline", "ACMI counter",
                     "$16,244,100",   # cash to close, counter
                     "$17,999,000",   # cash to close, redline
                     "$14,975,065",   # cash to close, first offer
                     "Aug 28, 2028",  # the counter's closing, after two 60-day extensions
                     "Where ACMI and the seller stand", "Aligned", "Differs", "Walk away here",
                     "Closing extension 1", "closing moves to"):
            self.assertContains(r, text)
        self.assertContains(r, "tl-overview")  # the lanes
        self.assertContains(r, 'id="tl-p1.closing"')

    def test_the_gap_panel_compares_the_latest_round_from_each_side(self):
        self.make_three()
        r = self.client.get(self.url)
        gap = {g["key"]: g for g in r.context["gap"]}
        self.assertEqual(gap["ppa"]["a_text"], "$90,000")      # our counter
        self.assertEqual(gap["ppa"]["b_text"], "$100,000")     # their redline
        self.assertEqual(gap["ppa"]["delta_text"], "+$10,000")
        self.assertTrue(gap["zoning"]["same"])
        self.assertEqual(gap["closing"]["delta_text"], "−120 days")  # Apr 30 against Aug 28, 2028

    def test_no_gap_panel_until_both_sides_have_made_a_round(self):
        self.make_round(INITIAL_OFFER, "ACMI initial offer", "us", date(2026, 9, 1))
        r = self.client.get(self.url)
        self.assertEqual(r.context["gap"], [])
        self.assertNotContains(r, "Where ACMI and the seller stand")

    def test_pick_which_rounds_to_compare(self):
        a, b, c = self.make_three()
        r = self.client.get(self.url + f"?r={a.pk}&r={c.pk}")
        self.assertEqual([x["round"].pk for x in r.context["cols"]], [a.pk, c.pk])
        self.assertContains(r, 'name="r"')

    def test_only_the_last_four_rounds_show_by_default(self):
        for i in range(6):
            self.make_round(INITIAL_OFFER, f"Round {i}", "us" if i % 2 == 0 else "them", date(2026, 9, 1 + i))
        r = self.client.get(self.url)
        self.assertEqual([x["round"].label for x in r.context["cols"]], ["Round 2", "Round 3", "Round 4", "Round 5"])

    def test_warnings_show_above_the_cards(self):
        raw = {**ACMI_COUNTER, "outside_months": 17}
        self.make_round(raw, "ACMI counter", "us", date(2026, 10, 30))
        self.assertContains(self.client.get(self.url), "after the outside date")

    def test_a_phase_gets_its_own_heading_when_there_are_two(self):
        raw = {**ACMI_COUNTER, "phases": [ACMI_COUNTER["phases"][0], {"name": "East 40", "acres": 40,
                                                                      "price_per_acre": 90000, "start_days": 30,
                                                                      "feasibility_days": 60, "close_days": 30,
                                                                      "initial_deposit": 10000}]}
        self.make_round(raw, "ACMI counter", "us", date(2026, 10, 30))
        r = self.client.get(self.url)
        self.assertContains(r, "East 40")
        self.assertContains(r, 'id="tl-p2.initial"')

    def test_a_round_links_to_the_draft_it_summarizes(self):
        readable = ContractDocument.objects.create(contract=self.c, title="LOI draft 1", kind="draft", text="x",
                                                   text_status="ok")
        linked = ContractDocument.objects.create(contract=self.c, title="Seller markup", kind="draft",
                                                 url="https://drive.example.com/loi")
        bare = ContractDocument.objects.create(contract=self.c, title="Phone call notes", kind="draft")
        self.make_round(INITIAL_OFFER, "ACMI initial offer", "us", date(2026, 9, 1), document=readable)
        self.make_round(SELLER_REDLINE, "Seller redline", "them", date(2026, 10, 23), document=linked)
        self.make_round(ACMI_COUNTER, "ACMI counter", "us", date(2026, 10, 30), document=bare)
        r = self.client.get(self.url)
        self.assertContains(r, f"/contracts/{self.c.pk}/read/{readable.pk}/")
        self.assertContains(r, 'href="https://drive.example.com/loi"')
        self.assertContains(r, "Phone call notes")
        self.assertNotContains(r, f"/contracts/{self.c.pk}/read/{bare.pk}/")

    def test_the_page_needs_a_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class AddAndEditTests(TermsTestCase):
    def test_the_first_round_is_suggested_as_ACMIs_offer(self):
        r = self.client.get(self.url + "new/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["form"].initial["label"], "ACMI initial offer")
        self.assertEqual(r.context["form"].initial["sent_by"], "us")
        self.assertContains(r, 'value="14"')  # days to PSA, prefilled

    def test_the_next_round_starts_from_the_last_and_is_named_for_the_other_side(self):
        self.make_round(INITIAL_OFFER, "ACMI initial offer", "us", date(2026, 9, 1))
        r = self.client.get(self.url + "new/")
        form = r.context["form"]
        self.assertEqual((form.initial["label"], form.initial["sent_by"]), ("Seller redline", "them"))
        self.assertEqual(form.initial["p1_acres"], "182.99")
        self.assertEqual(form.initial["p1_price_per_acre"], "81,972.05")
        self.assertContains(r, "Started from the terms of ACMI initial offer")

    def test_next_round_names_alternate_and_repeat_with_a_number(self):
        self.make_three()
        self.assertEqual(self.client.get(self.url + "new/").context["form"].initial["label"], "Seller redline 2")

    def test_start_from_a_chosen_round(self):
        a, b, c = self.make_three()
        r = self.client.get(self.url + f"new/?from={a.pk}")
        self.assertEqual(r.context["form"].initial["p1_price_per_acre"], "81,972.05")

    def test_add_a_round(self):
        r = self.client.post(self.url + "new/", self.post_data())
        self.assertRedirects(r, self.url)
        round_ = TermsRound.objects.get()
        self.assertEqual((round_.contract, round_.label, round_.sent_by, round_.dated, round_.created_by),
                         (self.c, "ACMI counter", "us", date(2026, 10, 30), self.user))
        self.assertEqual(round_.terms["phases"][0]["price_per_acre"], 90000)
        self.assertEqual(round_.terms["phases"][0]["acres"], 182.99)
        self.assertEqual(len(round_.terms["phases"]), 1)  # the spare, empty phase block isn't saved
        head = round_.schedule()["head"]
        self.assertEqual((head["cash_to_close"], head["closing"]), (D(16244100), date(2028, 8, 28)))  # the workbook's numbers

    def test_a_second_phase_is_saved_when_filled_in(self):
        data = self.post_data(**{"p2_name": "East 40", "p2_acres": "40", "p2_price_per_acre": "95,000",
                                 "p2_start_days": "30", "p2_feasibility_days": "60", "p2_close_days": "30"})
        self.client.post(self.url + "new/", data)
        phases = TermsRound.objects.get().terms["phases"]
        self.assertEqual([p.get("name") for p in phases], [None, "East 40"])
        self.assertEqual(phases[1]["start_days"], 30)

    def test_bad_boxes_are_marked_and_nothing_is_saved(self):
        data = self.post_data(**{"p1_initial_deposit": "lots", "p1_go_hard_nonref": "150,000",
                                 "p1_feas_ext_used": "3", "p1_acres": "-4", "label": ""})
        r = self.client.post(self.url + "new/", data)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(TermsRound.objects.exists())
        for text in ("Enter a number, like 25,000", "More than the deposit it is part of", "Only 1 available",
                     "Can&#x27;t be negative"):
            self.assertContains(r, text)

    def test_using_extensions_without_saying_how_many_exist_means_that_many_exist(self):
        data = self.post_data(**{"p1_close_ext_options": ""})
        self.client.post(self.url + "new/", data)
        self.assertEqual(TermsRound.objects.get().terms["phases"][0]["close_ext_options"], 2)

    def test_edit_a_round(self):
        c = self.make_round(ACMI_COUNTER, "ACMI counter", "us", date(2026, 10, 30))
        page = self.client.get(self.url + f"{c.pk}/")
        self.assertContains(page, 'value="90,000"')
        self.assertContains(page, "Edit ACMI counter")
        r = self.client.post(self.url + f"{c.pk}/", self.post_data(p1_price_per_acre="95,000", label="ACMI counter v2"))
        self.assertRedirects(r, self.url)
        c.refresh_from_db()
        self.assertEqual((c.label, c.terms["phases"][0]["price_per_acre"]), ("ACMI counter v2", 95000))
        self.assertEqual(TermsRound.objects.count(), 1)

    def test_a_round_of_another_contract_is_not_reachable(self):
        other = Contract.objects.create(title="Other PSA", kind="psa")
        r = TermsRound.objects.create(contract=other, label="x", terms={})
        self.assertEqual(self.client.get(self.url + f"{r.pk}/").status_code, 404)

    def test_the_draft_must_belong_to_the_contract(self):
        mine = ContractDocument.objects.create(contract=self.c, title="LOI draft 1", kind="draft")
        elsewhere = ContractDocument.objects.create(contract=Contract.objects.create(title="Other"), title="Other draft",
                                                    kind="draft")
        bad = self.client.post(self.url + "new/", self.post_data(document=str(elsewhere.pk)))
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(TermsRound.objects.exists())
        self.client.post(self.url + "new/", self.post_data(document=str(mine.pk)))
        self.assertEqual(TermsRound.objects.get().document, mine)

    def test_clearing_every_phase_box_removes_the_phase(self):
        c = self.make_round(ACMI_COUNTER, "ACMI counter", "us", date(2026, 10, 30))
        blank = {k: "" for k in self.post_data() if k.startswith("p1_")}
        self.client.post(self.url + f"{c.pk}/", self.post_data(**blank))
        c.refresh_from_db()
        self.assertNotIn("phases", c.terms)


class PreviewTests(TermsTestCase):
    def test_preview_works_the_schedule_out_from_the_boxes(self):
        r = self.client.post(self.url + "preview/", self.post_data())
        self.assertEqual(r.status_code, 200)
        for text in ("$16,244,100", "Closing and cash to close", "Aug 28, 2028", "Closing extension 2"):
            self.assertContains(r, text)

    def test_preview_names_the_problem_instead_of_guessing(self):
        r = self.client.post(self.url + "preview/", self.post_data(p1_initial_deposit="lots"))
        self.assertContains(r, "Fix these to see the schedule")
        self.assertContains(r, "Initial deposit")

    def test_preview_does_not_need_a_label_yet(self):
        r = self.client.post(self.url + "preview/", self.post_data(label=""))
        self.assertContains(r, "Closing and cash to close")

    def test_preview_saves_nothing_and_wants_a_post(self):
        self.client.post(self.url + "preview/", self.post_data())
        self.assertFalse(TermsRound.objects.exists())
        self.assertEqual(self.client.get(self.url + "preview/").status_code, 405)

    def test_preview_with_nothing_filled_in_says_what_to_fill_in(self):
        r = self.client.post(self.url + "preview/", {"label": "x", "slots": "1"})
        self.assertContains(r, "Fill in the acres")


class AgreeAndDeleteTests(TermsTestCase):
    def test_marking_a_round_agreed_unmarks_the_others(self):
        a, b, c = self.make_three()
        self.client.post(self.url + f"{b.pk}/agree/")
        self.client.post(self.url + f"{c.pk}/agree/")
        a.refresh_from_db(), b.refresh_from_db(), c.refresh_from_db()
        self.assertEqual((a.agreed, b.agreed, c.agreed), (False, False, True))
        self.client.post(self.url + f"{c.pk}/agree/")
        c.refresh_from_db()
        self.assertFalse(c.agreed)

    def test_the_agreed_round_is_marked_on_the_page(self):
        a, b, c = self.make_three()
        self.client.post(self.url + f"{c.pk}/agree/")
        r = self.client.get(self.url)
        self.assertContains(r, "lt-agreed")
        self.assertContains(r, "✓ Agreed")

    def test_only_managers_and_the_round_s_creator_can_delete(self):
        a = self.make_round(INITIAL_OFFER, "ACMI initial offer", "us", date(2026, 9, 1))
        joe = User.objects.create_user("joe")
        self.client.force_login(joe)
        self.assertEqual(self.client.post(self.url + f"{a.pk}/delete/").status_code, 302)
        self.assertTrue(TermsRound.objects.filter(pk=a.pk).exists())
        a.created_by = joe
        a.save()
        self.client.post(self.url + f"{a.pk}/delete/")
        self.assertFalse(TermsRound.objects.filter(pk=a.pk).exists())

    def test_a_manager_can_delete_anyones_round(self):
        a = self.make_round(INITIAL_OFFER, "ACMI initial offer", "us", date(2026, 9, 1))
        a.created_by = User.objects.create_user("joe")
        a.save()
        self.client.post(self.url + f"{a.pk}/delete/")
        self.assertFalse(TermsRound.objects.exists())

    def test_deleting_wants_a_post(self):
        a = self.make_round(INITIAL_OFFER, "ACMI initial offer", "us", date(2026, 9, 1))
        self.assertEqual(self.client.get(self.url + f"{a.pk}/delete/").status_code, 405)

    def test_rounds_go_with_their_contract(self):
        self.make_three()
        self.c.delete()
        self.assertFalse(TermsRound.objects.exists())


class ContractPageTests(TermsTestCase):
    def test_a_land_contract_offers_the_tracker(self):
        r = self.client.get(self.c.get_absolute_url())
        self.assertContains(r, "Start the tracker")
        self.assertContains(r, f'href="{self.url}"')

    def test_the_contract_page_shows_the_agreed_round(self):
        a, b, c = self.make_three()
        self.client.post(self.url + f"{b.pk}/agree/")
        r = self.client.get(self.c.get_absolute_url())
        self.assertContains(r, "Open the tracker (3 rounds)")
        self.assertContains(r, "Seller redline")
        self.assertContains(r, "✓ Agreed")
        self.assertContains(r, "$18,299,000")

    def test_without_an_agreed_round_it_shows_the_latest(self):
        self.make_three()
        r = self.client.get(self.c.get_absolute_url())
        self.assertContains(r, "ACMI counter")
        self.assertContains(r, "latest round")

    def test_the_land_site_page_links_to_the_tracker_once_there_are_rounds(self):
        site_url = f"/land/sites/{self.site.pk}/manage/"
        self.assertNotContains(self.client.get(site_url), "Terms (")
        self.make_three()
        r = self.client.get(site_url)
        self.assertContains(r, "Terms (3)")
        self.assertContains(r, f'href="{self.url}"')

    def test_a_lease_without_rounds_has_no_terms_panel(self):
        lease = Contract.objects.create(title="Acme lease", kind="lease")
        r = self.client.get(lease.get_absolute_url())
        self.assertNotContains(r, "LOI / PSA terms")
        self.assertIsNone(r.context["terms"])

    def test_a_contract_that_already_has_rounds_keeps_the_panel_whatever_its_kind(self):
        lease = Contract.objects.create(title="Acme lease", kind="other")
        TermsRound.objects.create(contract=lease, label="x", terms=INITIAL_OFFER, dated=date(2026, 9, 1))
        self.assertContains(self.client.get(lease.get_absolute_url()), "LOI / PSA terms")


class SignedContractTests(TermsTestCase):
    def test_the_agreed_round_counts_from_the_real_signing_date_once_signed(self):
        a, b, c = self.make_three()
        c.agreed = True
        c.save()
        self.c.status, self.c.effective_date = "signed", date(2026, 12, 1)
        self.c.save()
        c = TermsRound.objects.get(pk=c.pk)
        res = c.schedule()
        self.assertEqual(res["psa"], date(2026, 12, 1))
        self.assertEqual(res["phases"][0]["events"][0]["label"], "PSA signed")
        page = self.client.get(self.url)
        self.assertContains(page, "Dec 1, 2026")
        self.assertEqual(TermsRound.objects.get(pk=b.pk).schedule()["psa"], date(2026, 11, 6))  # not agreed: unchanged

    def test_an_unsigned_contract_keeps_the_anticipated_date(self):
        a, b, c = self.make_three()
        c.agreed = True
        c.save()
        self.c.effective_date = date(2026, 12, 1)  # a date but still negotiating
        self.c.save()
        self.assertEqual(TermsRound.objects.get(pk=c.pk).schedule()["psa"], date(2026, 11, 13))


class WorkbookImportTests(TermsTestCase):
    def test_reads_the_rounds_by_their_wording(self):
        found = terms_import.read_workbook(io.BytesIO(lewis_workbook()))
        self.assertEqual(found["skipped"], [])
        self.assertEqual([(r["label"], r["sent_by"], r["dated"]) for r in found["rounds"]],
                         [("ACMI initial offer", "us", date(2026, 9, 1)), ("Seller redline", "them", date(2026, 10, 23)),
                          ("ACMI counter", "us", date(2026, 10, 30))])
        first, redline, counter = found["rounds"]
        self.assertEqual(first["terms"]["psa_days"], 60)  # the anticipated PSA date less the round's date
        self.assertEqual(redline["terms"]["psa_days"], 14)
        self.assertEqual(len(counter["terms"]["phases"]), 1)  # Phase II is empty, Phase III only zeros
        phase = counter["terms"]["phases"][0]
        self.assertEqual((phase["close_basis"], phase["close_days"]), ("zoning", 30))
        self.assertEqual(phase["feas_ext_deposit"], 25000)  # a later blank row didn't wipe it
        self.assertEqual((phase["close_ext_days"], phase["close_ext_used"], phase["close_ext_deposit"],
                          phase["close_ext_nonref"]), (60, 2, 25000, 25000))  # the rows after Phase III
        self.assertEqual((phase["feas_ext_days"], phase["feas_ext_options"]), (60, 1))  # not overwritten by them
        self.assertEqual(counter["terms"]["outside_months"], 24)
        self.assertEqual((phase["go_hard"], phase["go_hard_nonref"]), (100000, 50000))
        self.assertEqual((phase["initial_deposit"], phase["initial_nonref"]), (50000, 25000))

    def test_the_imported_rounds_work_out_to_the_workbook_s_numbers(self):
        rounds = terms_import.read_workbook(io.BytesIO(lewis_workbook()))["rounds"]
        heads = [terms.schedule(r["terms"], r["dated"])["head"] for r in rounds]
        self.assertEqual([h["cash_to_close"] for h in heads], [D("14975065.43"), D(17999000), D(16244100)])
        self.assertEqual([h["closing"] for h in heads], [date(2030, 12, 30), date(2028, 4, 30), date(2028, 8, 28)])
        self.assertEqual([h["deposits"] for h in heads], [D(25000), D(300000), D(225000)])

    def test_import_through_the_page(self):
        upload = SimpleUploadedFile("LOI Tracker - MD - Lewis Place.xlsx", lewis_workbook())
        r = self.client.post(self.url + "import/", {"file": upload}, follow=True)
        self.assertEqual(TermsRound.objects.count(), 3)
        self.assertContains(r, "Added 3 rounds from LOI Tracker - MD - Lewis Place.xlsx")
        counter = TermsRound.objects.get(label="ACMI counter")
        self.assertEqual((counter.contract, counter.created_by), (self.c, self.user))
        self.assertIn("Imported from", counter.notes)
        self.assertContains(r, "$16,244,100")
        positions = list(TermsRound.objects.order_by("position").values_list("label", flat=True))
        self.assertEqual(positions, ["ACMI initial offer", "Seller redline", "ACMI counter"])

    def test_rounds_are_added_after_the_ones_already_there(self):
        self.make_round(INITIAL_OFFER, "Earlier", "us", date(2026, 8, 1), position=5)
        self.client.post(self.url + "import/", {"file": SimpleUploadedFile("t.xlsx", lewis_workbook())})
        self.assertEqual(TermsRound.objects.count(), 4)
        self.assertEqual(TermsRound.objects.order_by("position").last().label, "ACMI counter")

    def test_a_workbook_without_the_block_is_refused_plainly(self):
        buf = io.BytesIO()
        lewis_workbook(drop_block=True).save(buf)
        r = self.client.post(self.url + "import/", {"file": SimpleUploadedFile("other.xlsx", buf.getvalue())},
                             follow=True)
        self.assertContains(r, "Didn&#x27;t find an &quot;LOI INFO&quot; block")
        self.assertFalse(TermsRound.objects.exists())

    def test_a_file_that_is_not_a_workbook_is_refused(self):
        r = self.client.post(self.url + "import/", {"file": SimpleUploadedFile("notes.txt", b"hello")}, follow=True)
        self.assertContains(r, "Choose an Excel workbook")
        r = self.client.post(self.url + "import/", {"file": SimpleUploadedFile("fake.xlsx", b"not a zip")}, follow=True)
        self.assertContains(r, "couldn&#x27;t be opened as an Excel workbook")
        self.assertFalse(TermsRound.objects.exists())

    def test_unrecognised_rows_with_numbers_are_reported(self):
        wb = openpyxl.load_workbook(io.BytesIO(lewis_workbook()))
        ws = wb.active
        ws["B16"], ws["M16"] = "Broker commission (%)", 3
        buf = io.BytesIO()
        wb.save(buf)
        found = terms_import.read_workbook(io.BytesIO(buf.getvalue()))
        self.assertIn("Broker commission (%)", found["skipped"])
        r = self.client.post(self.url + "import/", {"file": SimpleUploadedFile("t.xlsx", buf.getvalue())}, follow=True)
        self.assertContains(r, "Not read: Broker commission (%)")

    def test_a_workbook_that_unpacks_absurdly_large_is_refused(self):
        with mock.patch.object(terms_import, "MAX_UNPACKED", 1000):
            r = self.client.post(self.url + "import/", {"file": SimpleUploadedFile("big.xlsx", lewis_workbook())},
                                 follow=True)
        self.assertContains(r, "too large to read")
        self.assertFalse(TermsRound.objects.exists())

    def test_import_wants_a_post(self):
        self.assertEqual(self.client.get(self.url + "import/").status_code, 405)
