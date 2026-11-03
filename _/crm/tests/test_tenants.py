"""Tenant site fit (acres, explosives arcs, power, rail, gas, control) and lease terms with the rent roll."""
import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from crm.land import tracker
from crm.models import (
    Campus, Company, Contract, ContractDate, ContractDocument, LandCounty, LandSite, LeaseTerms, Opportunity,
    TenantNeeds,
)
from crm.tenants import fit, leases
from crm.tests.test_contracts import fake_claude, text_block
from crm.tests.test_land import make_parcel
from crm.tests.utils import make_manager

User = get_user_model()

NEAR = {"transmission": {"miles": 0.8, "VOLT_CLASS": "230"}, "substation": {"miles": 1.5, "MAX_VOLTAG": 115,
                                                                             "name": "Benning"},
        "rail": {"miles": 0.5, "name": "CSXT"}, "gas_pipeline": {"miles": 4.0, "name": "Columbia Gas"}}


class SiteFitTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("john"))
        self.client.force_login(self.user)
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        # About 1.1 x 0.87 km: room for a 1,250 ft IBD arc. The small one (330 x 260 m) has none.
        self.big = LandSite.objects.create(name="Lake Acton", status="contract", campus=self.campus)
        self.big.parcels.add(make_parcel(county, "1", -77.10, 38.40, size=0.01, acres=237.0, near=NEAR,
                                         checks={"net_usable": {"acres": 200}}))
        self.small = LandSite.objects.create(name="Corner lot", status="prospect", campus=self.campus)
        self.small.parcels.add(make_parcel(county, "2", -77.00, 38.40, size=0.003, acres=21.0, near=NEAR))
        self.elsewhere = LandSite.objects.create(name="Far away", status="acquired")
        self.elsewhere.parcels.add(make_parcel(county, "3", -76.90, 38.40, size=0.01, acres=240.0, near=NEAR))
        for s in (self.big, self.small, self.elsewhere):
            tracker.ensure_deals(s)
        company = Company.objects.create(name="Shine")
        self.opp = Opportunity.objects.create(name="Shine energetics plant", company=company, campus=self.campus,
                                              acreage_need=Decimal("150"), power_need_mw=Decimal("25"))
        self.needs = TenantNeeds.objects.create(opportunity=self.opp, needs_rail=True, needs_gas=True, qd_cases=[
            {"name": "Mixing building", "hd": "HD 1.1", "newqd": 10000, "kind": "operating", "sqft": 0}])

    def test_rank_checks_each_need(self):
        rows = fit.rank(self.opp, self.needs)
        self.assertEqual([r["site"].name for r in rows], ["Lake Acton", "Corner lot"])  # the deal's campus only
        best = {c["key"]: c for c in rows[0]["checks"]}
        self.assertEqual(best["acres"]["detail"], "200 net usable of 150 needed")
        self.assertTrue(best["acres"]["ok"])
        self.assertTrue(best["qd"]["ok"], best["qd"]["detail"])
        self.assertIn("arcs close on the site (widest IBD 1,250 ft)", best["qd"]["detail"])
        self.assertTrue(best["power"]["ok"])
        self.assertEqual(best["power"]["detail"], "0.8 mi to a 230 kV line; 1.5 mi to Benning substation (115 kV)")
        self.assertTrue(best["rail"]["ok"])
        self.assertFalse(best["gas"]["ok"])  # 4 mi to the pipeline
        self.assertEqual(rows[0]["fails"], 1)
        worst = {c["key"]: c for c in rows[1]["checks"]}
        self.assertFalse(worst["acres"]["ok"])
        self.assertFalse(worst["qd"]["ok"])
        self.assertIn("off the site", worst["qd"]["detail"])
        self.assertIsNone(worst["ready"]["ok"])  # a prospect isn't a failure, just not under control yet
        self.assertGreater(rows[0]["score"], rows[1]["score"])
        LandSite.objects.create(name="No parcels yet", campus=self.campus)
        self.assertNotIn("No parcels yet", [r["site"].name for r in fit.rank(self.opp, self.needs)])
        everywhere = fit.rank(self.opp, self.needs, everywhere=True)
        self.assertIn("Far away", [r["site"].name for r in everywhere])

    def test_fit_page_saves_needs(self):
        page = self.client.get(f"/opportunities/{self.opp.pk}/fit/")
        self.assertContains(page, "Lake Acton")
        self.assertContains(page, "Mixing building: HD 1.1, 10,000 lb, IBD 1,250 ft")
        r = self.client.post(f"/opportunities/{self.opp.pk}/fit/", {
            "name_0": "Press building", "hd_0": "HD 1.3", "newqd_0": "2,000", "kind_0": "operating", "sqft_0": "15000",
            "name_1": "", "hd_1": "HD 1.1", "newqd_1": "", "kind_1": "magazine", "sqft_1": "",
            "needs_rail": "1", "notes": "Process water 50k gpd"})
        self.assertRedirects(r, f"/opportunities/{self.opp.pk}/fit/")
        self.needs.refresh_from_db()
        self.assertEqual(self.needs.qd_cases, [{"name": "Press building", "hd": "HD 1.3", "newqd": 2000.0,
                                                "sqft": 15000.0, "kind": "operating"}])
        self.assertEqual((self.needs.needs_rail, self.needs.needs_gas, self.needs.notes),
                         (True, False, "Process water 50k gpd"))
        deal = self.client.get(self.opp.get_absolute_url())
        self.assertContains(deal, "Site fit")
        self.assertContains(deal, "Press building: HD 1.3, 2,000 lb")


class LeaseTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen"))
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.campus = Campus.objects.create(code="NSIH", name="Naval Support Innovation Hub")
        company = Company.objects.create(name="Thales")
        self.opp = Opportunity.objects.create(name="Thales building", company=company, campus=self.campus)
        self.lease = Contract.objects.create(title="Thales lease", kind="lease", status="signed", campus=self.campus,
                                             opportunity=self.opp, owner=self.user)

    def test_rent_math(self):
        start = leases.add_months(self.today, -30)
        t = LeaseTerms(base_rent=Decimal("1200000"), escalation_pct=Decimal("3"), rent_start=start)
        self.assertEqual(leases.rent_now(t, self.today), Decimal("1273080"))  # two full years of 3% bumps
        self.assertEqual(leases.rent_now(LeaseTerms(base_rent=Decimal("100"), rent_start=self.today + timedelta(days=9)),
                                         self.today), Decimal("100"))
        self.assertEqual(leases.add_months(date(2026, 3, 31), -1), date(2026, 2, 28))
        t = LeaseTerms(expiration=date(2042, 2, 28), renewal_notice_months=12)
        self.assertEqual(leases.notice_deadline(t), date(2041, 2, 28))

    def test_terms_dates_and_rent_roll(self):
        url = self.lease.get_absolute_url()
        self.assertContains(self.client.get(url), "Enter the terms")
        expires = self.today + timedelta(days=3650)
        self.client.post(url, {"action": "lease_terms", "premises_sf": "120000", "rent_start": str(self.today - timedelta(days=40)),
                               "expiration": str(expires), "base_rent": "1500000", "escalation_pct": "2.5",
                               "renewals": "2", "renewal_years": "5", "renewal_notice_months": "12", "guarantor": ""})
        terms = LeaseTerms.objects.get(contract=self.lease)
        self.assertEqual((terms.premises_sf, terms.base_rent), (120000, Decimal("1500000")))
        auto = {d.kind: d for d in self.lease.dates.filter(rule=leases.AUTO)}
        self.assertEqual(set(auto), {"expiration", "notice", "payment"})
        self.assertEqual(auto["notice"].date, leases.add_months(expires, -12))
        self.assertTrue(auto["payment"].done)  # rent already started
        self.client.post(url, {"action": "lease_terms", "premises_sf": "120000", "rent_start": str(self.today - timedelta(days=40)),
                               "expiration": str(expires + timedelta(days=365)), "base_rent": "1500000",
                               "escalation_pct": "2.5", "renewals": "2", "renewal_years": "5",
                               "renewal_notice_months": "12"})
        self.assertEqual(self.lease.dates.get(kind="expiration", rule=leases.AUTO).date, expires + timedelta(days=365))

        Contract.objects.create(title="ERG lease", kind="lease", status="signed", campus=self.campus,
                                counterparty="ERG")  # no terms yet
        Contract.objects.create(title="Shine lease (draft)", kind="lease", status="negotiating", campus=self.campus)
        roll = leases.rent_roll(today=self.today)
        self.assertEqual(roll["totals"]["count"], 2)
        self.assertEqual((roll["totals"]["sf"], roll["totals"]["rent"]), (120000, Decimal("1500000")))
        self.assertAlmostEqual(roll["totals"]["walt"], 11.0, delta=0.1)
        page = self.client.get("/leases/")
        self.assertContains(page, "Thales")
        self.assertContains(page, "$1,500,000")
        self.assertContains(page, "No terms yet")
        self.assertContains(page, "report-logo")
        self.assertNotContains(page, "Shine lease")

    def test_a_kept_date_is_left_alone(self):
        ContractDate.objects.create(contract=self.lease, kind="expiration", label="Lease ends", date=date(2040, 1, 1))
        t = LeaseTerms.objects.create(contract=self.lease, expiration=date(2041, 6, 30))
        leases.sync_dates(self.lease, t)
        self.assertEqual(list(self.lease.dates.filter(kind="expiration").values_list("label", flat=True)),
                         ["Lease ends"])

    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"})
    def test_claude_reads_the_terms_with_sources(self):
        text = ("LEASE AGREEMENT\nThe Premises contain approximately 120,000 rentable square feet.\n"
                "Annual Base Rent for the first Lease Year shall be $1,500,000, increasing by two and one-half "
                "percent (2.5%) each Lease Year.\nTenant may renew for two (2) periods of five (5) years each by "
                "notice given at least twelve (12) months before the Expiration Date.")
        doc = ContractDocument.objects.create(contract=self.lease, kind="original", title="Thales lease", text=text,
                                              page_starts=[0], pages=1, text_status="ok")
        LeaseTerms.objects.create(contract=self.lease, premises_sf=118000)  # typed by hand: kept
        found = {"terms": [
            {"field": "premises_sf", "value": "120,000", "document": 1, "note": "",
             "quote": "The Premises contain approximately 120,000 rentable square feet."},
            {"field": "base_rent", "value": "$1,500,000", "document": 1, "note": "first Lease Year",
             "quote": "Annual Base Rent for the first Lease Year shall be $1,500,000"},
            {"field": "escalation_pct", "value": "2.5", "document": 1, "note": "",
             "quote": "increasing by two and one-half percent (2.5%) each Lease Year."},
            {"field": "renewal_notice_months", "value": "12", "document": 1, "note": "", "quote": "Not in the lease"},
        ]}
        patcher, stream = fake_claude([text_block(json.dumps(found))])
        with patcher:
            r = self.client.post(self.lease.get_absolute_url(), {"action": "lease_read"}, follow=True)
        kwargs = stream.call_args.kwargs
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("citations", kwargs["messages"][0]["content"][0])
        t = LeaseTerms.objects.get(contract=self.lease)
        self.assertEqual((t.premises_sf, t.base_rent, t.escalation_pct, t.renewal_notice_months),
                         (118000, Decimal("1500000"), Decimal("2.5"), 12))
        self.assertTrue(text[t.sources["base_rent"]["start"]:].startswith("Annual Base Rent"))
        self.assertIsNone(t.sources["renewal_notice_months"]["start"])  # quote not found: no source link
        self.assertNotIn("premises_sf", t.sources)
        self.assertContains(r, "Claude filled in annual base rent ($), annual escalation (%), renewal notice")
        self.assertContains(r, f"/contracts/{self.lease.pk}/read/{doc.pk}/?hl=")
