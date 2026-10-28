"""Land sales: last transfers from county records, sale comps, the team-only value range, off-market signals and owner
groups (portfolios). No network calls: every county service is mocked."""
from datetime import date, timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from crm.land import briefs, comps, letters, portfolio, sales, signals, tracker
from crm.models import LandCounty, LandSite, Parcel, SaleComp
from crm.tests.test_land import make_parcel

User = get_user_model()
TODAY = date.today()


def feats(*records):
    return [{"properties": r} for r in records]


class SaleRecordTests(TestCase):
    """Each county's fields -> last-transfer fields."""

    def test_charles_statewide_record(self):
        f = sales._charles({"ACCTID": "0901007033", "OWNADD1": "8745 PORT TOBACCO", "OWNADD2": None, "OWNCITY": "LA PLATA",
                            "OWNSTATE": "MD", "OWNERZIP": "20646", "NFMLNDVL": 19000, "NFMIMPVL": None,
                            "NFMTTLVL": 19000, "TRADATE": "20090714", "CONSIDR1": 200000, "CONVEY1": 2,
                            "GR1LIBR1": "06497", "GR1FOLO1": "0077"})
        self.assertEqual((f["last_sale_date"], f["last_sale_price"], f["last_sale_arms_length"]),
                         (date(2009, 7, 14), 200000, True))
        self.assertEqual(f["last_sale_type"], "Arms-length, vacant land")
        self.assertEqual(f["last_sale_deed"], "Liber 6497 folio 77")
        self.assertEqual((f["mail_address"], f["mail_city"], f["mail_state"], f["mail_zip"]),
                         ("8745 PORT TOBACCO", "LA PLATA", "MD", "20646"))
        self.assertEqual((f["land_value"], f["improvement_value"], f["market_value"]), (19000, None, 19000))
        gift = sales._charles({"TRADATE": "19870520", "CONSIDR1": 0, "CONVEY1": 4, "OWNADD1": " "})
        self.assertEqual((gift["last_sale_price"], gift["last_sale_arms_length"]), (None, False))
        self.assertNotIn("mail_address", gift)  # no whole mailing address: nothing to fill

    def test_hays_madison_morgan_nc_hoke(self):
        self.assertEqual(sales._hays({"Deed_Date": "01/30/2008", "Volume": "3324", "Page": "452"}),
                         {"last_sale_date": date(2008, 1, 30), "last_sale_deed": "Vol 3324 pg 452"})
        self.assertEqual(sales._hays({"Deed_Date": "02/14/2024", "Number": "24005393"})["last_sale_deed"], "Doc 24005393")
        m = sales._madison({"DeedType": "PREP", "DeedBook": "2009", "DeedPage": "707590", "DeedDate": 1235001600000})
        self.assertEqual((m["last_sale_date"], m["last_sale_arms_length"]), (date(2009, 2, 19), False))
        self.assertIn("estate", m["last_sale_type"])
        self.assertIsNone(sales._madison({"DeedType": "WARR"})["last_sale_arms_length"])
        mo = sales._morgan({"SoldTotalPrice": 450000.0, "LastSalesDate": 305442000000, "UseOnSales": 1, "LastDeed": "2021-2382-5"})
        self.assertEqual((mo["last_sale_price"], mo["last_sale_arms_length"]), (450000, True))
        self.assertIsNone(sales._morgan({"SoldTotalPrice": 0.0, "LastSalesDate": -928436400000, "UseOnSales": 0})["last_sale_price"])
        harnett = sales._nc({"saledatetx": "8/2006", "saledate": None, "sourceref": "Deed Book/Page 4279/0696"})
        self.assertEqual((harnett["last_sale_date"], harnett["last_sale_deed"]), (date(2006, 8, 1), "Book 4279 page 696"))
        moore_fill = sales._nc({"saledate": -2208970800000, "saledatetx": ""})  # the 1900-01-01 fill value
        self.assertIsNone(moore_fill["last_sale_date"])
        self.assertEqual(sales._hoke({"DEED_DATE": 20160214.0, "DEED_BOOK": "1141", "DEED_PAGE": "593"})["last_sale_date"],
                         date(2016, 2, 14))

    def test_arizona_sales(self):
        same = sales._maricopa({"DEED_DATE": 959817600000, "SALE_DATE": "05/01/2000", "SALE_PRICE": "2317392",
                                "DEED_NUMBER": "20000417526"})
        self.assertEqual((same["last_sale_date"], same["last_sale_price"]), (date(2000, 5, 1), 2317392))
        later_deed = sales._maricopa({"DEED_DATE": 1701734400000, "SALE_DATE": "05/01/2000", "SALE_PRICE": "500000"})
        self.assertEqual((later_deed["last_sale_date"], later_deed["last_sale_price"]), (date(2023, 12, 5), None))
        used = sales._pinal({"SALEDATE": "2024-06-01", "SALEPRICE": 375000.0, "REASONTOEXCLUDE": None})
        self.assertEqual((used["last_sale_price"], used["last_sale_arms_length"]), (375000, True))
        excluded = sales._pinal({"SALEDATE": "2026-01-01", "SALEPRICE": 265000.0, "REASONTOEXCLUDE": "R14"})
        self.assertIsNone(excluded["last_sale_arms_length"])  # left out by the assessor, reason not published
        self.assertIn("R14", excluded["last_sale_type"])
        self.assertIsNone(sales.price("10"))  # a nominal consideration isn't a price


class RefreshTests(TestCase):
    def setUp(self):
        self.charles = LandCounty.objects.create(fips="24017", name="Charles", state="MD", min_acres=20)
        self.vacant = make_parcel(self.charles, "0901007033", -77.0, 38.5, owner_name="SMITH JOHN ET AL", acres=38.0,
                                  situs_zip="20646")
        self.mailed = make_parcel(self.charles, "0901006967", -77.02, 38.5, owner_name="JONES FARM LLC", acres=114.0,
                                  mail_address="1 OLD RD", mail_city="WALDORF", mail_zip="20601", mail_state="MD",
                                  situs_zip="20646")
        self.multi = make_parcel(self.charles, "0901000001", -77.04, 38.5, owner_name="BIG BUYER LLC", acres=60.0)
        self.small = make_parcel(self.charles, "0901000002", -77.06, 38.5, owner_name="LATE ADD", acres=3.0)
        self.records = {
            "0901007033": {"ACCTID": "0901007033", "OWNADD1": "8745 PORT TOBACCO RD", "OWNCITY": "LA PLATA",
                           "OWNSTATE": "MD", "OWNERZIP": "20646", "NFMLNDVL": 19000, "NFMTTLVL": 19000,
                           "TRADATE": "19870520", "CONSIDR1": None, "CONVEY1": 4, "GR1LIBR1": "1195", "GR1FOLO1": "359"},
            "0901006967": {"ACCTID": "0901006967", "OWNADD1": None, "NFMLNDVL": 400600, "NFMIMPVL": 0,
                           "NFMTTLVL": 400600, "TRADATE": "20240528", "CONSIDR1": 1600000, "CONVEY1": 2,
                           "GR1LIBR1": "1037", "GR1FOLO1": "218"},
            "0901000001": {"ACCTID": "0901000001", "TRADATE": "20230101", "CONSIDR1": 900000, "CONVEY1": 3},
            "0901000002": {"ACCTID": "0901000002", "TRADATE": "20250101", "CONSIDR1": 50000, "CONVEY1": 2},
        }
        self.calls = []

    def fake_query(self, url, *, where="1=1", **kw):
        self.calls.append(where)
        self.assertIn("mdgeodata.md.gov", url)
        self.assertFalse(kw.get("return_geometry", True))
        if "ACCTID IN" in where:  # the small parcel isn't in the big-parcel pass
            return feats(self.records["0901000002"])
        self.assertIn("JURSCODE = 'CHAR'", where)
        return feats(*(r for k, r in self.records.items() if k != "0901000002"))

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(sales, "arcgis_query", side_effect=self.fake_query):
            stats = sales.refresh_county(self.charles, commit=False, log=lambda *_: None)
        self.assertEqual((stats["matched"], stats["changed"]), (4, 4))
        self.assertEqual(stats["fields"]["mail_address"], 1)
        self.vacant.refresh_from_db()
        self.assertEqual((self.vacant.mail_address, self.vacant.last_sale_date), ("", None))
        self.assertFalse(SaleComp.objects.exists())

    def test_refresh_fills_mailing_values_sales_signals_and_comps(self):
        with mock.patch.object(sales, "arcgis_query", side_effect=self.fake_query):
            stats = sales.refresh_county(self.charles, log=lambda *_: None)
        self.assertTrue(any("ACCTID IN" in w for w in self.calls))
        v, m = Parcel.objects.get(pk=self.vacant.pk), Parcel.objects.get(pk=self.mailed.pk)
        self.assertEqual((v.mail_address, v.mail_city, v.mail_zip, v.land_value), ("8745 PORT TOBACCO RD", "LA PLATA", "20646", 19000))
        self.assertEqual(m.mail_address, "1 OLD RD")  # the statewide layer had none: a blank never overwrites
        self.assertEqual((m.last_sale_price, m.last_sale_arms_length, m.last_sale_deed), (1600000, True, "Liber 1037 folio 218"))
        self.assertEqual(m.last_sale_source, "MD iMAP statewide parcels (SDAT)")
        # Signals: held since 1987, not arms-length, ET AL
        self.assertIn(" long_hold ", v.signals)
        self.assertIn(" non_arms ", v.signals)
        self.assertIn(" family ", v.signals)
        self.assertGreater(v.motivation, m.motivation)
        # Comps: only the arms-length single-account sales with a price
        keys = set(SaleComp.objects.values_list("parcel__key", flat=True))
        self.assertEqual(keys, {"0901006967", "0901000002"})
        comp = SaleComp.objects.get(parcel=self.mailed)
        self.assertEqual((comp.source, comp.price_per_acre, comp.buyer), ("county", round(1600000 / 114.0, 2), "JONES FARM LLC"))
        self.assertEqual(stats["comps"], 2)
        # A second refresh keeps the same comps (no duplicates) and drops ones that no longer qualify
        self.records["0901000002"]["CONVEY1"] = 4
        with mock.patch.object(sales, "arcgis_query", side_effect=self.fake_query):
            sales.refresh_county(self.charles, log=lambda *_: None)
        self.assertEqual(SaleComp.objects.count(), 1)

    def test_a_newer_transfer_clears_the_old_price(self):
        self.mailed.last_sale_date, self.mailed.last_sale_price, self.mailed.last_sale_type = date(2001, 1, 1), 5000, "x"
        self.mailed.save()
        diff = sales.changes_for(self.mailed, {"last_sale_date": date(2024, 1, 1)}, sales.SOURCES["48209"])
        self.assertEqual((diff["last_sale_price"], diff["last_sale_type"]), (None, ""))

    def test_hays_keeps_the_latest_deed_and_never_a_price(self):
        hays = LandCounty.objects.create(fips="48209", name="Hays", state="TX", min_acres=20)
        p = make_parcel(hays, "10821", -97.9, 30.0, owner_name="CARSON FAMILY PROPERTIES LTD")

        def fake(url, *, where="1=1", **kw):
            self.assertIn("Hays_County_Parcels", url)
            return feats({"prop_id": 10821, "Deed_Date": "01/30/2008", "Volume": "3324", "Page": "452"},
                         {"prop_id": 10821, "Deed_Date": "05/01/1999", "Volume": "1500", "Page": "1"})

        with mock.patch.object(sales, "arcgis_query", side_effect=fake):
            sales.refresh_county(hays, log=lambda *_: None)
        p.refresh_from_db()
        self.assertEqual((p.last_sale_date, p.last_sale_price, p.last_sale_deed), (date(2008, 1, 30), None, "Vol 3324 pg 452"))
        self.assertFalse(SaleComp.objects.filter(county=hays).exists())

    def test_command_dry_run_and_commit(self):
        out = StringIO()
        with mock.patch.object(sales, "arcgis_query", side_effect=self.fake_query):
            call_command("refresh_sales", "Charles", stdout=out)
        self.assertIn("Dry run", out.getvalue())
        self.assertFalse(Parcel.objects.exclude(last_sale_date=None).exists())
        with mock.patch.object(sales, "arcgis_query", side_effect=self.fake_query):
            call_command("refresh_sales", "Charles", "--commit", stdout=out)
        self.assertEqual(Parcel.objects.exclude(last_sale_date=None).count(), 4)
        self.assertIn("Owner groups", out.getvalue())

    def test_a_failing_sale_source_does_not_fail_the_load(self):
        from crm.land import parcels

        msgs = []
        with mock.patch.object(sales, "arcgis_query", side_effect=RuntimeError("down")):
            parcels.after_load(self.charles, log=msgs.append)
        self.assertTrue(any("refresh_sales" in m for m in msgs))


class SignalTests(TestCase):
    def setUp(self):
        self.county = LandCounty.objects.create(fips="48209", name="Hays", state="TX")
        signals.reset_cache()

    def codes(self, **kw):
        p = Parcel(county=self.county, **{"owner_name": "X", **kw})
        return signals.compute(p, today=date(2026, 10, 26), zip3s={"786"})

    def test_names(self):
        self.assertIn("estate", self.codes(owner_name="WHITT J B (HEIRS OF)"))
        self.assertIn("estate", self.codes(owner_name="LACY GLADYS J ESTATE OF"))
        self.assertIn("estate", self.codes(owner_name="SMITH MARY", care_of="LIFE ESTATE"))
        self.assertNotIn("estate", self.codes(owner_name="TEXAS REAL ESTATE PARTNERS LLC"))
        self.assertNotIn("estate", self.codes(owner_name="OAK ESTATES LLC"))
        self.assertIn("family", self.codes(owner_name="SMITH JOHN ET AL"))
        self.assertIn("trust", self.codes(owner_name="NORD LINDA K TRUSTEE"))
        self.assertIn("trust", self.codes(owner_name="ULAND KIMBERLY J REVOCABLE TRUST"))
        self.assertNotIn("trust", self.codes(owner_name="FIRST TRUST BANK"))
        self.assertEqual(self.codes(owner_name="UNITED STATES OF AMERICA", mail_state="DC"), [])

    def test_absentee_long_hold_and_non_arms(self):
        self.assertEqual(self.codes(mail_state="CA"), ["out_of_state"])
        self.assertEqual(self.codes(mail_state="TX", mail_zip="78701"), ["absentee"])  # Austin, not a Hays ZIP area
        self.assertEqual(self.codes(mail_state="TX", mail_zip="78666"), [])
        self.assertEqual(self.codes(last_sale_date=date(2005, 1, 1)), ["long_hold"])
        self.assertEqual(self.codes(last_sale_date=date(2007, 1, 1)), [])
        self.assertEqual(self.codes(last_sale_arms_length=False), ["non_arms"])
        p = Parcel(county=self.county, owner_name="X", last_sale_date=date(1987, 5, 20), mail_state="VA")
        signals.apply(p, today=date(2026, 10, 26), zip3s=set())
        labels = [label for _, label, _ in signals.chips(p, today=date(2026, 10, 26))]
        self.assertEqual(labels, ["Owned since 1987", "Out-of-state owner (VA)"])
        self.assertEqual(p.motivation, signals.WEIGHTS["long_hold"] + signals.WEIGHTS["out_of_state"])

    def test_a_first_load_without_a_sale_source_gets_absentee(self):
        # The review found this: signals were scored before the parcels were saved, when the county had no land
        # ZIPs yet, and only counties with a sale source were rescored afterwards.
        from crm.land import parcels
        from crm.tests.test_land import make_parcel

        blanco = LandCounty.objects.create(fips="48031", name="Blanco", state="TX")
        signals.reset_cache()
        signals.area_zip3s(blanco.pk)  # cached empty, as during the load
        make_parcel(blanco, "1", -98.4, 30.1, situs_zip="78606", mail_state="TX", mail_zip="78606")
        far = make_parcel(blanco, "2", -98.5, 30.1, situs_zip="78636", mail_state="TX", mail_zip="77002")
        with mock.patch("crm.land.portfolio.rebuild"):
            parcels.after_load(blanco, log=lambda *a: None)
        far.refresh_from_db()
        self.assertIn("absentee", far.signals)

    def test_score_is_unchanged_by_signals_and_land_search_filters(self):
        user = User.objects.create_user("kellen")
        self.client.force_login(user)
        plain = make_parcel(self.county, "1", -97.9, 30.0, owner_name="PLAIN LLC", mail_state="TX", mail_zip="78666")
        estate = make_parcel(self.county, "2", -97.88, 30.0, owner_name="PLAIN LLC HEIRS", mail_state="TX", mail_zip="78666")
        self.assertEqual(plain.score, estate.score)
        self.assertEqual((plain.motivation, estate.signals), (0, " estate "))
        bbox = "-98.5,29.5,-97.0,30.5"
        ids = lambda q: {f["properties"]["id"] for f in self.client.get(f"/land/parcels.json?bbox={bbox}&{q}").json()["features"]}  # noqa: E731
        self.assertEqual(ids("signal=estate"), {estate.pk})
        self.assertEqual(ids("signal=long_hold"), set())
        self.assertEqual(self.client.get(f"/land/parcels.json?bbox={bbox}&sort=motivation").json()["list"][0]["id"], estate.pk)
        self.assertContains(self.client.get("/land/"), "Off-market signals")


class PortfolioTests(TestCase):
    def setUp(self):
        self.hays = LandCounty.objects.create(fips="48209", name="Hays", state="TX")
        self.caldwell = LandCounty.objects.create(fips="48055", name="Caldwell", state="TX")

    def test_address_key(self):
        self.assertEqual(portfolio.address_key("1200 N. Main Street, Suite 400", "78666-1234"), "1200 N MAIN ST|78666")
        self.assertEqual(portfolio.address_key("1200 NORTH MAIN ST #400", "78666"), "1200 N MAIN ST|78666")
        self.assertEqual(portfolio.address_key("P.O. Box 12", "78666"), "PO BOX 12|78666")
        self.assertEqual(portfolio.address_key("", "78666"), "")
        self.assertEqual(portfolio.address_key("1 MAIN ST", ""), "")

    def test_groups_by_name_in_county_and_address_anywhere(self):
        a = make_parcel(self.hays, "A", -97.9, 30.0, owner_name="RANCH ONE LLC", mail_address="10 Oak Lane", mail_zip="78701", acres=100)
        b = make_parcel(self.caldwell, "B", -97.6, 29.9, owner_name="RANCH TWO LLC", mail_address="10 OAK LN.", mail_zip="78701", acres=50)
        c = make_parcel(self.hays, "C", -97.92, 30.0, owner_name="RANCH ONE LLC", mail_address="", acres=30)
        d = make_parcel(self.caldwell, "D", -97.62, 29.9, owner_name="RANCH ONE LLC", acres=20)  # same name, other county
        loner = make_parcel(self.hays, "E", -97.94, 30.0, owner_name="SOMEONE ELSE")
        portfolio.rebuild()
        a, b, c, d, loner = (Parcel.objects.get(pk=x.pk) for x in (a, b, c, d, loner))
        self.assertTrue(a.portfolio_key)
        self.assertEqual({a.portfolio_key, b.portfolio_key, c.portfolio_key}, {a.portfolio_key})
        self.assertEqual((a.portfolio_parcels, a.portfolio_acres), (3, 180.0))
        self.assertEqual((d.portfolio_key, loner.portfolio_key), ("", ""))
        s = portfolio.summary(a)
        self.assertEqual(s["counties"], ["Caldwell TX", "Hays TX"])
        self.assertEqual(s["names"], ["RANCH TWO LLC"])
        user = User.objects.create_user("kellen")
        self.client.force_login(user)
        self.assertContains(self.client.get(f"/land/parcel/{a.pk}/"), "Same owner group:")
        bbox = "-98.5,29.5,-97.0,30.5"
        got = {f["properties"]["id"] for f in self.client.get(f"/land/parcels.json?bbox={bbox}&portfolio=1").json()["features"]}
        self.assertEqual(got, {a.pk, b.pk, c.pk})

    def test_generic_addresses_do_not_link_owners(self):
        made = [make_parcel(self.hays, f"T{i}", -97.9 + i / 100, 30.0, owner_name=f"OWNER {i}",
                            mail_address="PO BOX 999", mail_zip="78666") for i in range(4)]
        made += [make_parcel(self.hays, f"L{i}", -97.5 + i / 100, 30.1, owner_name=f"FIRM CLIENT {i}",
                             mail_address="100 CONGRESS AVE", mail_zip="78701") for i in range(6)]
        tax = [make_parcel(self.hays, f"X{i}", -97.3 + i / 100, 30.2, owner_name=f"TAXED {i}", care_of="C/O CORELOGIC TAX SERVICE",
                           mail_address="1 TAX WAY", mail_zip="75000") for i in range(2)]
        pair = [make_parcel(self.hays, f"P{i}", -97.1 + i / 100, 30.3, owner_name=f"COUPLE {i}", mail_address="PO BOX 5",
                            mail_zip="78666") for i in range(2)]
        portfolio.rebuild()
        keys = lambda ps: {Parcel.objects.get(pk=p.pk).portfolio_key for p in ps}  # noqa: E731
        self.assertEqual(keys(made), {""})  # 4 names on a PO box and 6 at one office: generic
        self.assertEqual(keys(tax), {""})
        self.assertEqual(len(keys(pair) - {""}), 1)  # two owners at one PO box: linked


class CompTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen")
        self.client.force_login(self.user)
        self.pinal = LandCounty.objects.create(fips="04021", name="Pinal", state="AZ")
        self.hays = LandCounty.objects.create(fips="48209", name="Hays", state="TX")
        self.subject = make_parcel(self.pinal, "S", -111.7, 32.9, acres=100.0)

    def comp(self, ppa, acres=100.0, years_ago=1, **kw):
        defaults = dict(source="county", county=self.pinal, sale_date=TODAY - timedelta(days=365 * years_ago),
                        price=int(ppa * acres), acres=acres, lat=32.9 + 0.01, lon=-111.7, arms_length=True,
                        record_key=f"{ppa}-{acres}-{years_ago}-{kw.get('improved', False)}")
        defaults.update(kw)
        return SaleComp.objects.create(**defaults)

    def test_summary_and_value_range(self):
        self.assertFalse(comps.parcel_value_range(self.subject)["enough"])
        for ppa in (4000, 5000, 6000, 7000, 8000):
            self.comp(ppa)
        self.comp(90000, improved=True)  # buildings: not land
        self.comp(1000, arms_length=False, record_key="gift")  # not arms-length
        self.comp(2000, acres=5.0)  # outside the acreage band
        self.comp(3000, lat=34.5, record_key="far")  # 110 miles away
        vr = comps.parcel_value_range(self.subject)
        self.assertTrue(vr["enough"])
        self.assertEqual((vr["n"], vr["low"], vr["base"], vr["high"]), (5, 5000, 6000, 7000))
        self.assertIn("parcel=", vr["query"])
        self.assertEqual(vr["total_base"], 600000)
        s = comps.summarize(comps.find(county=self.pinal, arms="all"))
        self.assertEqual(s["n"], 9)

    def test_old_sales_widen_the_window(self):
        for ppa in (4000, 5000, 6000):
            self.comp(ppa, years_ago=8)
        vr = comps.parcel_value_range(self.subject)
        self.assertEqual((vr["enough"], vr["years"]), (True, comps.WIDER_YEARS))

    def test_card_shows_comps_and_range_and_texas_says_why(self):
        for ppa in (4000, 5000, 6000):
            self.comp(ppa)
        card = self.client.get(f"/land/parcel/{self.subject.pk}/")
        self.assertContains(card, "Sale comps nearby")
        self.assertContains(card, "team only")
        self.assertContains(card, "$5,000")
        tx = make_parcel(self.hays, "T", -97.9, 30.0)
        tx_card = self.client.get(f"/land/parcel/{tx.pk}/")
        self.assertContains(tx_card, "Not enough comps")
        self.assertContains(tx_card, "non-disclosure state")

    def test_comps_page_csv_and_team_entry(self):
        self.comp(5000)
        page = self.client.get(f"/land/comps/?parcel={self.subject.pk}&radius=15")
        self.assertContains(page, "Median $/acre")
        self.assertContains(page, "comp-pins")
        resp = self.client.get(f"/land/comps/?county=04021&format=csv")
        body = resp.content.decode()
        self.assertIn("Price per acre", body.splitlines()[0])
        self.assertIn("5000", body)
        tx = self.client.get("/land/comps/?county=48209")
        self.assertContains(tx, "non-disclosure state")
        # Team entry for Texas
        resp = self.client.post("/land/comps/new/", {"sale_date": "2025-03-01", "price": "2400000", "acres": "120",
                                                     "county": self.hays.pk, "buyer": "ACME", "seller": "RANCH",
                                                     "arms_length": "true", "parcels_count": "1", "notes": "broker call",
                                                     "lat": "30.0", "lon": "-97.9"})
        self.assertEqual(resp.status_code, 302)
        c = SaleComp.objects.get(source="team")
        self.assertEqual((c.price_per_acre, c.created_by, c.arms_length), (20000.0, self.user, True))
        bad = self.client.post("/land/comps/new/", {"sale_date": "2025-03-01", "price": "0", "acres": "10",
                                                    "parcels_count": "1"})
        self.assertContains(bad, "Enter the sale price.")
        self.client.post(f"/land/comps/{c.pk}/delete/")
        self.assertFalse(SaleComp.objects.filter(pk=c.pk).exists())
        county_comp = SaleComp.objects.get(source="county")
        self.client.post(f"/land/comps/{county_comp.pk}/delete/")
        self.assertTrue(SaleComp.objects.filter(pk=county_comp.pk).exists())  # county comps come from the records
        self.client.post(f"/land/comps/{county_comp.pk}/", {"notes": "Checked the deed", "price": "1"})
        county_comp.refresh_from_db()
        self.assertEqual((county_comp.notes, county_comp.price), ("Checked the deed", 500000))

    def test_site_page_and_brief(self):
        for ppa in (4000, 5000, 6000):
            self.comp(ppa)
        self.comp(7777, internal_only=True, notes="NDA broker intel", record_key="nda", source="team")
        self.subject.owner_name = "LACY GLADYS ESTATE OF"
        self.subject.save()
        signals.apply(self.subject)
        self.subject.save()
        site = LandSite.objects.create(name="Casa Grande 100")
        site.parcels.set([self.subject])
        tracker.ensure_deals(site)
        page = self.client.get(f"/land/sites/{site.pk}/manage/")
        self.assertContains(page, "Sale comps nearby")
        self.assertContains(page, "Value range")
        with mock.patch.object(briefs, "run_checks_many"):
            web = self.client.get(f"/land/sites/{site.pk}/brief/")
            captured = {}

            def fake_pdf(html, timeout=180):
                captured["html"] = html
                return b"%PDF-1.4"

            with mock.patch.object(briefs, "html_to_pdf", side_effect=fake_pdf):
                pdf = self.client.get(f"/land/sites/{site.pk}/brief/?format=pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertContains(web, "Recorded land sales nearby")
        self.assertContains(web, "Off-market signals")  # the team's view in the app
        self.assertNotContains(web, "Value range")  # never on a brief
        self.assertNotContains(web, "7,777")  # comps marked internal stay off
        self.assertNotContains(web, "NDA broker intel")
        html = captured["html"]
        self.assertIn("Recorded land sales nearby", html)
        self.assertIn('class="report-logo"', html)  # every report keeps the ACMI logo
        self.assertNotIn("Off-market signals", html)
        self.assertNotIn("Estate / heirs", html)
        self.assertNotIn("7,777", html)


class GroupLetterTests(TestCase):
    def setUp(self):
        county = LandCounty.objects.create(fips="48209", name="Hays", state="TX")
        self.a = make_parcel(county, "A", -97.9, 30.0, owner_name="RANCH ONE LLC", mail_address="10 OAK LN",
                             mail_city="AUSTIN", mail_state="TX", mail_zip="78701", acres=200.0)
        self.b = make_parcel(county, "B", -97.89, 30.0, owner_name="RANCH TWO LLC", mail_address="10 Oak Lane",
                             mail_city="AUSTIN", mail_state="TX", mail_zip="78701", acres=50.0)
        self.site = LandSite.objects.create(name="Kyle 250")
        self.site.parcels.set([self.a, self.b])
        tracker.ensure_deals(self.site)
        portfolio.rebuild()

    def test_one_letter_per_owner_group(self):
        self.assertEqual(len(letters.recipients(self.site)), 2)
        rows = letters.recipients(self.site, by_group=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["owner"], rows[0]["others"]), ("RANCH ONE LLC", ["Ranch Two LLC"]))
        self.assertEqual(rows[0]["acres"], 250.0)
        body = "Dear {owner},\n\nAbout your land, {property} ({acres} acres).\n\nThanks."
        text = letters.build(self.site, rows, body, {}, TODAY)[0]["text"]
        self.assertIn("250 acres).\n\nSome of this land is held as Ranch Two LLC.\n\nThanks.", text)
        user = User.objects.create_user("kellen")
        self.client.force_login(user)
        page = self.client.get(f"/land/sites/{self.site.pk}/letters/")
        self.assertContains(page, "One letter per owner group instead")
        grouped = self.client.get(f"/land/sites/{self.site.pk}/letters/?group=1")
        self.assertContains(grouped, "also for Ranch Two LLC")
