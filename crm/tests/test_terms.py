"""The LOI / PSA terms tracker's schedule engine (crm/contracts/terms.py). The three rounds of the Lewis Place "LOI
Tracker" workbook are the fixtures, and the expected numbers are the ones the workbook itself shows: its dates,
aggregate refundable / non-refundable columns, closing date and cash to close."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from crm.contracts import terms

D = Decimal

# ACMI initial offer, sent 2026-09-01: $81,972.05 an acre, 60 days to a PSA, 48 months to final zoning approval.
INITIAL_OFFER = {
    "psa_days": 60, "zoning_months": 48, "subdivision": "no",
    "phases": [{"acres": 182.99, "price_per_acre": 81972.05, "feasibility_days": 60, "feas_ext_days": 0,
                "feas_ext_options": 0, "feas_ext_used": 0, "initial_deposit": 25000, "initial_nonref": 0,
                "go_hard": 0, "go_hard_nonref": 0, "close_days": 60}],
}
# Seller redline, received 2026-10-23: $100,000 an acre, $50,000 down (all non-refundable), $250,000 more after
# feasibility ($100,000 of it non-refundable), 16 months to zoning, outside date 18 months out.
SELLER_REDLINE = {
    "psa_days": 14, "zoning_months": 16, "outside_months": 18, "subdivision": "no",
    "phases": [{"acres": 182.99, "price_per_acre": 100000, "feasibility_days": 60, "feas_ext_days": 0,
                "feas_ext_options": 2, "feas_ext_used": 2, "initial_deposit": 50000, "initial_nonref": 50000,
                "go_hard": 250000, "go_hard_nonref": 100000, "close_days": 30}],
}
# ACMI counter, sent 2026-10-30: $90,000 an acre, 120 days of feasibility plus one 60-day extension ($25,000), $100,000
# after feasibility ($50,000 non-refundable), and two 60-day closing extensions of $25,000, non-refundable.
ACMI_COUNTER = {
    "psa_days": 14, "zoning_months": 16, "outside_months": 24, "subdivision": "no",
    "phases": [{"acres": 182.99, "price_per_acre": 90000, "feasibility_days": 120, "feas_ext_days": 60,
                "feas_ext_options": 1, "feas_ext_used": 1, "feas_ext_deposit": 25000, "initial_deposit": 50000,
                "initial_nonref": 25000, "go_hard": 100000, "go_hard_nonref": 50000, "close_days": 30,
                "close_ext_days": 60, "close_ext_options": 2, "close_ext_used": 2, "close_ext_deposit": 25000,
                "close_ext_nonref": 25000}],
}


def event(result, key, phase=0):
    return next(e for e in result["phases"][phase]["events"] if e["base"] == key)


class WorkbookNumbers(SimpleTestCase):
    """What the workbook's LOI TRACKER block shows for each round."""

    def test_initial_offer(self):
        r = terms.schedule(INITIAL_OFFER, date(2026, 9, 1))
        ph = r["phases"][0]
        self.assertEqual(r["psa"], date(2026, 10, 31))  # C16
        self.assertEqual(ph["feasibility_end"], date(2026, 12, 30))  # C82
        self.assertEqual(ph["zoning"], date(2030, 10, 31))  # C84
        self.assertEqual(ph["closing"], date(2030, 12, 30))  # C90
        self.assertEqual(ph["price"], D("15000065.43"))  # C26 (the workbook keeps 15000065.4295)
        self.assertEqual(ph["deposits"], D(25000))
        self.assertEqual(ph["nonrefundable"], D(0))
        self.assertEqual(ph["cash_to_close"], D("14975065.43"))  # F90
        self.assertIsNone(ph["outside"])

    def test_seller_redline(self):
        r = terms.schedule(SELLER_REDLINE, date(2026, 10, 23))
        ph = r["phases"][0]
        self.assertEqual(r["psa"], date(2026, 11, 6))  # H16
        self.assertEqual(ph["feasibility_end"], date(2027, 1, 5))  # H82 and H83: a 0-day extension moves nothing
        self.assertEqual(ph["zoning"], date(2028, 3, 31))  # H84
        self.assertEqual(ph["closing"], date(2028, 4, 30))  # H90
        self.assertEqual(ph["outside"], date(2028, 5, 31))  # H88
        self.assertEqual(ph["price"], D("18299000.00"))  # H26
        self.assertEqual(ph["deposits"], D(300000))  # I89
        self.assertEqual(ph["refundable"], D(150000))  # J83
        self.assertEqual(ph["nonrefundable"], D(150000))  # K83
        self.assertEqual(ph["cash_to_close"], D(17999000))  # K90
        self.assertEqual(r["warnings"], [])

    def test_acmi_counter(self):
        r = terms.schedule(ACMI_COUNTER, date(2026, 10, 30))
        ph = r["phases"][0]
        self.assertEqual(r["psa"], date(2026, 11, 13))  # M16
        self.assertEqual(event(r, "feas_end0")["date"], date(2027, 3, 13))  # M82
        self.assertEqual(ph["feasibility_end"], date(2027, 5, 12))  # M83
        self.assertEqual(ph["zoning"], date(2028, 3, 31))  # M84
        self.assertEqual(event(r, "close0")["date"], date(2028, 4, 30))  # M85
        self.assertEqual(ph["outside"], date(2028, 11, 30))  # M88
        self.assertEqual(ph["closing"], date(2028, 8, 28))  # M90: the 2nd extension, inside the outside date
        self.assertEqual(ph["price"], D("16469100.00"))  # M26
        self.assertEqual(ph["deposits"], D(225000))  # N89
        self.assertEqual(ph["refundable"], D(75000))  # O89
        self.assertEqual(ph["nonrefundable"], D(150000))  # P89
        self.assertEqual(ph["cash_to_close"], D(16244100))  # P90
        # deposits + cash to close = price: the workbook's "Deposits Check"
        self.assertEqual(ph["deposits"] + ph["cash_to_close"], ph["price"])

    def test_counter_running_totals_match_the_aggregate_columns(self):
        r = terms.schedule(ACMI_COUNTER, date(2026, 10, 30))
        got = {e["base"]: (e["cum_ref"], e["cum_nonref"]) for e in r["phases"][0]["events"] if e["money"]}
        self.assertEqual(got["initial"], (D(25000), D(25000)))  # O78, P78
        self.assertEqual(got["feas_ext1"], (D(25000), D(50000)))  # O82, P82
        self.assertEqual(got["go_hard"], (D(75000), D(100000)))  # O83, P83
        self.assertEqual(got["close_ext1"], (D(75000), D(125000)))  # O86, P86
        self.assertEqual(got["close_ext2"], (D(75000), D(150000)))  # O87, P87

    def test_counter_deposits_fall_due_when_each_extension_is_exercised(self):
        r = terms.schedule(ACMI_COUNTER, date(2026, 10, 30))
        # The workbook dates a closing extension's deposit on the extended closing date; here it is paid by the day
        # the closing it extends falls due, and the row says where closing moves to.
        self.assertEqual(event(r, "feas_ext1")["date"], date(2027, 3, 13))
        self.assertEqual(event(r, "feas_ext1")["to"], date(2027, 5, 12))
        self.assertEqual(event(r, "close_ext1")["date"], date(2028, 4, 30))
        self.assertEqual(event(r, "close_ext1")["to"], date(2028, 6, 29))  # M86
        self.assertEqual(event(r, "close_ext2")["date"], date(2028, 6, 29))
        self.assertEqual(event(r, "close_ext2")["to"], date(2028, 8, 28))  # M87

    def test_money_at_risk_when_feasibility_ends(self):
        head = terms.schedule(ACMI_COUNTER, date(2026, 10, 30))["head"]
        self.assertEqual(head["committed_feas"], D(175000))  # 50,000 + 25,000 + 100,000
        self.assertEqual(head["at_risk_feas"], D(100000))  # P83
        head = terms.schedule(SELLER_REDLINE, date(2026, 10, 23))["head"]
        self.assertEqual((head["committed_feas"], head["at_risk_feas"]), (D(300000), D(150000)))

    def test_closing_is_the_last_row_and_carries_cash(self):
        r = terms.schedule(ACMI_COUNTER, date(2026, 10, 30))
        last = r["phases"][0]["events"][-1]
        self.assertEqual(last["base"], "closing")
        self.assertEqual(last["cash"], D(16244100))
        self.assertEqual((last["cum_ref"], last["cum_nonref"]), (D(75000), D(150000)))


class Dates(SimpleTestCase):
    def test_eomonth_matches_excel(self):
        self.assertEqual(terms.eomonth(date(2026, 11, 6), 0), date(2026, 11, 30))
        self.assertEqual(terms.eomonth(date(2026, 11, 6), 1), date(2026, 12, 31))
        self.assertEqual(terms.eomonth(date(2026, 12, 15), 1), date(2027, 1, 31))
        self.assertEqual(terms.eomonth(date(2027, 1, 31), 1), date(2027, 2, 28))
        self.assertEqual(terms.eomonth(date(2027, 12, 31), 12), date(2028, 12, 31))
        self.assertEqual(terms.eomonth(date(2027, 1, 31), 13), date(2028, 2, 29))
        self.assertIsNone(terms.eomonth(None, 3))
        self.assertIsNone(terms.eomonth(date(2027, 1, 31), None))

    def test_a_fixed_psa_date_beats_the_days(self):
        raw = {**ACMI_COUNTER, "psa_date": "2026-12-01"}
        self.assertEqual(terms.schedule(raw, date(2026, 10, 30))["psa"], date(2026, 12, 1))

    def test_without_a_date_the_dates_are_blank_and_the_money_still_adds_up(self):
        r = terms.schedule(ACMI_COUNTER)
        self.assertIsNone(r["psa"])
        self.assertTrue(r["warnings"][0].startswith("Add this round's date"))
        self.assertEqual(r["phases"][0]["deposits"], D(225000))
        self.assertTrue(all(e["date"] is None for e in r["phases"][0]["events"]))


class SignedDate(SimpleTestCase):
    def test_a_real_signing_date_replaces_the_anticipated_one(self):
        r = terms.schedule(ACMI_COUNTER, date(2026, 10, 30), signed_on=date(2026, 12, 1))
        self.assertEqual(r["psa"], date(2026, 12, 1))
        self.assertEqual(event(r, "start")["label"], "PSA signed")
        self.assertEqual(r["phases"][0]["feasibility_end"], date(2027, 5, 30))  # 120 + 60 days after Dec 1
        self.assertEqual(r["phases"][0]["zoning"], date(2028, 4, 30))  # EOMONTH(Dec 1, 16)

    def test_without_it_the_label_says_anticipated(self):
        r = terms.schedule(ACMI_COUNTER, date(2026, 10, 30))
        self.assertEqual(event(r, "start")["label"], "PSA signed (anticipated)")

    def test_a_date_fixed_on_the_round_still_wins(self):
        raw = {**ACMI_COUNTER, "psa_date": "2026-11-20"}
        r = terms.schedule(raw, date(2026, 10, 30), signed_on=date(2026, 12, 1))
        self.assertEqual(r["psa"], date(2026, 11, 20))
        self.assertEqual(event(r, "start")["label"], "PSA signed (anticipated)")


class Axis(SimpleTestCase):
    def ticks(self, months, start=date(2026, 11, 1)):
        out, d = [], start
        for _ in range(months):
            out.append({"pos": 1, "label": f"{d:%b} ’{d:%y}" if d.month == 1 else f"{d:%b}"})
            d = terms.eomonth(d, 0) + timedelta(days=1)  # the first of the next month
        return out + [{"pos": 0, "label": "Today", "today": True}]

    def test_a_short_span_keeps_every_month(self):
        ticks = self.ticks(14)
        self.assertEqual(terms.thin_ticks(ticks), ticks)

    def test_a_long_span_keeps_quarters_then_half_years_then_years(self):
        quarters = [t["label"] for t in terms.thin_ticks(self.ticks(30)) if not t.get("today")]
        self.assertTrue(all(label[:3] in ("Jan", "Apr", "Jul", "Oct") for label in quarters))
        halves = [t["label"] for t in terms.thin_ticks(self.ticks(60)) if not t.get("today")]
        self.assertTrue(all(label[:3] in ("Jan", "Jul") for label in halves))
        years = [t["label"] for t in terms.thin_ticks(self.ticks(100)) if not t.get("today")]
        self.assertTrue(all(label.startswith("Jan") for label in years))

    def test_today_always_stays(self):
        self.assertIn("Today", [t["label"] for t in terms.thin_ticks(self.ticks(100))])


class Edges(SimpleTestCase):
    def test_closing_is_held_at_the_outside_date(self):
        raw = {**ACMI_COUNTER, "outside_months": 17}  # EOMONTH(2026-11-13, 17) = 2028-04-30
        r = terms.schedule(raw, date(2026, 10, 30))
        self.assertEqual(r["phases"][0]["closing"], date(2028, 4, 30))
        self.assertTrue(any("after the outside date" in w for w in r["warnings"]))

    def test_more_extensions_used_than_available_are_capped_and_flagged(self):
        phase = {**ACMI_COUNTER["phases"][0], "close_ext_used": 5}
        r = terms.schedule({**ACMI_COUNTER, "phases": [phase]}, date(2026, 10, 30))
        self.assertEqual(r["phases"][0]["close_ext_used"], 2)
        self.assertTrue(any("closing extensions used but only 2" in w for w in r["warnings"]))

    def test_non_refundable_part_cannot_exceed_the_deposit(self):
        phase = {**INITIAL_OFFER["phases"][0], "initial_nonref": 99999}
        r = terms.schedule({**INITIAL_OFFER, "phases": [phase]}, date(2026, 9, 1))
        ph = r["phases"][0]
        self.assertEqual((ph["refundable"], ph["nonrefundable"]), (D(0), D(25000)))
        self.assertTrue(any("non-refundable part of the initial deposit" in w for w in r["warnings"]))

    def test_deposits_over_the_price_are_flagged(self):
        phase = {**INITIAL_OFFER["phases"][0], "initial_deposit": 20000000}
        r = terms.schedule({**INITIAL_OFFER, "phases": [phase]}, date(2026, 9, 1))
        self.assertTrue(any("more than the price" in w for w in r["warnings"]))

    def test_closing_from_zoning_with_no_zoning_months_says_so(self):
        raw = {**INITIAL_OFFER, "zoning_months": None}
        phase = {**INITIAL_OFFER["phases"][0], "close_basis": "zoning"}
        r = terms.schedule({**raw, "phases": [phase]}, date(2026, 9, 1))
        self.assertIsNone(r["phases"][0]["closing"])
        self.assertTrue(any("no months to approval" in w for w in r["warnings"]))

    def test_a_blank_phase_is_skipped_and_nothing_breaks_on_empty_terms(self):
        self.assertEqual(terms.schedule({}, date(2026, 9, 1))["phases"], [])
        r = terms.schedule({"phases": [{}, {"name": "Phase II"}]}, date(2026, 9, 1))
        self.assertEqual(r["phases"], [])
        self.assertEqual(r["head"]["price"], D(0))

    def test_zero_extensions_leave_no_extension_rows(self):
        r = terms.schedule(SELLER_REDLINE, date(2026, 10, 23))  # 2 options "used" but 0 days and no deposit
        bases = [e["base"] for e in r["phases"][0]["events"]]
        self.assertNotIn("feas_ext1", bases)
        self.assertNotIn("feas_end0", bases)
        self.assertNotIn("close0", bases)


class LaterPhases(SimpleTestCase):
    """Phase II starts after Phase I closes and runs its own feasibility, deposits and closing."""

    RAW = {
        "psa_days": 14, "zoning_months": 16, "outside_months": 24,
        "phases": [
            {**ACMI_COUNTER["phases"][0]},
            {"name": "Phase II", "acres": 50, "price_per_acre": 100000, "start_days": 30, "feasibility_days": 90,
             "feas_ext_days": 30, "feas_ext_options": 2, "feas_ext_used": 2, "feas_ext_deposit": 10000,
             "initial_deposit": 40000, "initial_nonref": 20000, "close_days": 45},
        ],
    }

    def test_phase_two_runs_from_phase_one_closing(self):
        r = terms.schedule(self.RAW, date(2026, 10, 30))
        one, two = r["phases"]
        self.assertEqual(one["closing"], date(2028, 8, 28))
        self.assertEqual(two["start"], date(2028, 9, 27))  # + 30 days
        self.assertEqual(event(r, "initial", 1)["date"], date(2028, 9, 27))
        self.assertEqual(event(r, "feas_end0", 1)["date"], date(2028, 12, 26))  # + 90
        self.assertEqual(event(r, "feas_ext1", 1)["date"], date(2028, 12, 26))
        self.assertEqual(event(r, "feas_ext2", 1)["date"], date(2029, 1, 25))
        self.assertEqual(two["feasibility_end"], date(2029, 2, 24))
        self.assertEqual(two["closing"], date(2029, 4, 10))  # feasibility end + 45
        self.assertEqual(two["price"], D("5000000.00"))
        self.assertEqual((two["deposits"], two["nonrefundable"]), (D(60000), D(40000)))
        self.assertEqual(two["cash_to_close"], D(4940000))

    def test_totals_add_up_across_phases(self):
        r = terms.schedule(self.RAW, date(2026, 10, 30))
        t = r["totals"]
        self.assertEqual(t["price"], D("21469100.00"))
        self.assertEqual(t["deposits"], D(285000))
        self.assertEqual(t["deposits"] + t["cash_to_close"], t["price"])
        self.assertEqual(t["acres"], D("232.99"))
        self.assertEqual(r["head"]["closing"], date(2029, 4, 10))

    def test_phase_two_does_not_date_until_phase_one_closes(self):
        raw = {**self.RAW, "zoning_months": None}
        phase = {**ACMI_COUNTER["phases"][0], "close_basis": "zoning"}
        r = terms.schedule({**raw, "phases": [phase, self.RAW["phases"][1]]}, date(2026, 10, 30))
        self.assertIsNone(r["phases"][1]["start"])
        self.assertIsNone(r["phases"][1]["closing"])


class StoredTerms(SimpleTestCase):
    def test_numbers_as_typed_in_a_form(self):
        self.assertEqual(terms.num("$1,250,000.50"), D("1250000.50"))
        self.assertEqual(terms.num("25,000"), D(25000))
        self.assertIsNone(terms.num("N/A"))
        self.assertIsNone(terms.num(""))
        self.assertIsNone(terms.num(None))
        self.assertIsNone(terms.num(True))
        self.assertIsNone(terms.num("about ten"))
        with self.assertRaises(ValueError):
            terms.num("about ten", strict=True)

    def test_round_trip_keeps_values_exact(self):
        t = terms.typed(ACMI_COUNTER)
        again = terms.typed(terms.dump(t))
        self.assertEqual(t, again)
        self.assertEqual(terms.dump(t)["phases"][0]["acres"], 182.99)
        self.assertEqual(terms.typed({"phases": [{"acres": 182.99, "price_per_acre": 81972.05}]})["phases"][0]["acres"],
                         D("182.99"))

    def test_dump_drops_blanks_and_empty_phases(self):
        t = terms.typed({"psa_days": 14, "zoning_months": "", "phases": [{"name": "Phase I"}, {"acres": "10"}]})
        out = terms.dump(t)
        self.assertEqual(out, {"psa_days": 14, "phases": [{"acres": 10}]})

    def test_default_days_to_psa_apply_only_when_the_key_is_missing(self):
        self.assertEqual(terms.typed({})["psa_days"], 14)
        self.assertIsNone(terms.typed({"psa_days": ""})["psa_days"])

    def test_damaged_stored_terms_read_as_empty_instead_of_crashing(self):
        for junk in (None, [], "text", 5, {"phases": "x"}, {"phases": [None, "x", 3]}):
            r = terms.schedule(junk, date(2026, 9, 1))
            self.assertEqual(r["phases"], [], junk)

    def test_phase_names(self):
        self.assertEqual(terms.phase_name(0), "Phase I")
        self.assertEqual(terms.phase_name(2, {"name": ""}), "Phase III")
        self.assertEqual(terms.phase_name(1, {"name": "East 40"}), "East 40")


class SideBySide(SimpleTestCase):
    def results(self):
        return [terms.schedule(INITIAL_OFFER, date(2026, 9, 1)), terms.schedule(SELLER_REDLINE, date(2026, 10, 23)),
                terms.schedule(ACMI_COUNTER, date(2026, 10, 30))]

    def test_gap_between_the_two_sides(self):
        a, b = self.results()[2]["head"], self.results()[1]["head"]
        rows = {r["key"]: r for r in terms.gap(a, b)}
        self.assertEqual(rows["ppa"]["delta"], D("10000.00"))  # seller 100,000 vs our 90,000
        self.assertEqual(rows["price"]["delta"], D("1829900.00"))
        self.assertEqual(rows["at_risk_feas"]["delta"], D(50000))
        self.assertEqual(rows["closing"]["delta"], (date(2028, 4, 30) - date(2028, 8, 28)).days)
        self.assertTrue(rows["zoning"]["same"])

    def test_event_rows_flag_what_changed(self):
        rows = {r["key"]: r for r in terms.compare_events(self.results())}
        self.assertTrue(rows["p1.initial"]["changed"])  # 25,000 / 50,000 / 50,000
        self.assertTrue(rows["p1.close_ext1"]["changed"])  # only the counter has it
        self.assertEqual([c is not None for c in rows["p1.close_ext1"]["cells"]], [False, False, True])
        self.assertEqual([c is not None for c in rows["p1.start"]["cells"]], [True, True, True])

    def test_event_rows_run_in_the_order_things_happen(self):
        keys = [r["key"] for r in terms.compare_events(self.results())]
        self.assertEqual(keys[0], "p1.start")
        self.assertEqual(keys[-1], "p1.closing")
        self.assertLess(keys.index("p1.initial"), keys.index("p1.feas_end"))
        self.assertLess(keys.index("p1.feas_end"), keys.index("p1.go_hard"))
        self.assertLess(keys.index("p1.go_hard"), keys.index("p1.zoning"))
        self.assertLess(keys.index("p1.close_ext1"), keys.index("p1.close_ext2"))

    def test_terms_grid_leaves_out_what_nobody_filled_in_and_marks_changes(self):
        sections = terms.compare_terms([INITIAL_OFFER, SELLER_REDLINE, ACMI_COUNTER])
        titles = [s["title"] for s in sections]
        self.assertEqual(titles[0], "The deal")
        self.assertIn("Phase I", titles)
        rows = {r["label"]: r for s in sections for r in s["rows"]}
        self.assertEqual(rows["Price per acre"]["cells"], ["$81,972.05", "$100,000", "$90,000"])
        self.assertTrue(rows["Price per acre"]["changed"])
        self.assertFalse(rows["Purchase size (acres)"]["changed"])
        self.assertNotIn("30-day deposit (non-refundable)", rows)

    def test_lane_rows_are_what_the_timeline_lanes_draw(self):
        rows = terms.lane_rows(terms.schedule(ACMI_COUNTER, date(2026, 10, 30)))
        self.assertTrue(all(r["when"] for r in rows))
        self.assertEqual({r["kind"] for r in rows} - {"other", "deposit", "extension", "feasibility", "hard", "approval",
                                                      "closing", "expiration"}, set())
        self.assertEqual(rows[0]["label"], "PSA signed (anticipated)")
