"""Land search: QD engine, scoring, zoning buckets, assemblages/sites and the pages. No network calls."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from shapely.geometry import box, mapping

from crm.land import drive, qd, score
from crm.models import Campus, Company, LandCounty, LandSite, Parcel, Project, Task


def make_parcel(county, key, x0, y0, size=0.01, **kw):
    geom = box(x0, y0, x0 + size, y0 + size)
    defaults = dict(owner_name="SMITH RANCH LLC", acres=150.0, state_land_use="D1", lat=y0 + size / 2, lon=x0 + size / 2,
                    min_lon=x0, min_lat=y0, max_lon=x0 + size, max_lat=y0 + size, geometry=json.dumps(mapping(geom)),
                    prop_id=key)
    defaults.update(kw)
    p = Parcel(county=county, key=key, **defaults)
    score.apply(p)
    p.save()
    return p


class QDEngineTests(TestCase):
    """Distances from DoD 6055.09-M (QD CALCS v20), checked against published values and the Pomfret site plan."""

    def test_hd11_open_storage_matches_published_ibd(self):
        self.assertAlmostEqual(qd.distances("HD 1.1", 50000, "Aboveground structure")["ibd"], 1474, delta=1)
        self.assertAlmostEqual(qd.distances("HD 1.1", 100000, "Aboveground structure")["ibd"], 1857, delta=1)
        self.assertAlmostEqual(qd.distances("HD 1.1", 500000, "Aboveground structure")["ibd"], 3969, delta=1)

    def test_fragment_floor_governs_mid_quantities(self):
        d = qd.distances("HD 1.1", 30000, "Aboveground structure")
        self.assertEqual(d["ibd"], 1250)
        self.assertIn("Fragment", d["governing"])

    def test_pomfret_site_plan_arcs(self):
        # Site Plan 2026.09.30: ARA 02 (HD 1.3, 100,000 lb) 300 ft; ERG A (HD 1.1, 500 lb) 1,250 ft; ERG B (5 lb) 419 ft.
        self.assertAlmostEqual(qd.distances("HD 1.3", 100000)["ibd"], 300, delta=1)
        self.assertAlmostEqual(qd.distances("HD 1.1", 500, "Open (no structure)")["ibd"], 1250, delta=1)
        self.assertAlmostEqual(qd.distances("HD 1.1", 5, "Open (no structure)")["ibd"], 419, delta=1)
        # The site-screen sheet's single row used the Structure column: 200 ft at 5 lb.
        self.assertEqual(qd.distances("HD 1.1", 5, "Aboveground or Open")["ibd"], 200)

    def test_ild_barricaded_and_hd14(self):
        self.assertAlmostEqual(qd.distances("HD 1.1", 100000, "Aboveground structure", barricaded=True)["ild"], 418, delta=1)
        self.assertEqual(qd.distances("HD 1.4", 5000)["ibd"], 100)
        self.assertEqual(qd.distances("HD 1.4", 100)["ibd"], 75)

    def test_zero_weight_has_no_arcs(self):
        self.assertEqual(qd.distances("HD 1.1", 0)["ibd"], 0)


class LandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("kellen", password="x")
        self.client.force_login(self.user)
        self.tx = LandCounty.objects.create(fips="48209", name="Hays", state="TX", region="Austin–San Antonio")
        self.md = LandCounty.objects.create(fips="24017", name="Charles", state="MD", region="Southern Maryland")

    def test_texas_unincorporated_scores_and_buckets(self):
        p = make_parcel(self.tx, "1", -97.9, 29.9, near={
            "transmission": {"miles": 0.4, "VOLT_CLASS": "345"}, "highway": {"miles": 0.5, "name": "I35"},
            "rail": {"miles": 0.8, "name": "UP"}, "military": {"miles": 20, "name": "Joint Base San Antonio"}})
        self.assertEqual(p.zoning_class, "unzoned")
        self.assertGreaterEqual(p.score, 60)
        self.assertTrue(any("I-35" in r["text"] for r in p.score_reasons))

    def test_maryland_zoning_codes_map_to_buckets(self):
        bp = make_parcel(self.md, "2", -77.0, 38.4, overlays={"zoning": {"value": "BP", "name": "Business Park",
                                                                           "general": "Mixed Use", "parts": {"BP": 100}}})
        res = make_parcel(self.md, "3", -77.1, 38.4, overlays={"zoning": {"value": "RM", "general": "Medium Density Residential",
                                                                            "parts": {"RM": 100}}})
        self.assertEqual(bp.zoning_class, "business_park")  # the county files BP under "Mixed Use"
        self.assertEqual(res.zoning_class, "residential")
        self.assertGreater(bp.score, res.score)

    def test_floodplain_is_a_deal_killer(self):
        p = make_parcel(self.tx, "4", -97.9, 29.9, checks={"flood": {"sfha_pct": 45, "floodway_pct": 0, "mapped": True}})
        self.assertTrue(p.killers)
        self.assertLessEqual(p.score, score.KILLER_CAP)

    def test_map_json_filters_by_zoning_and_county(self):
        make_parcel(self.md, "5", -77.0, 38.4, overlays={"zoning": {"value": "IG", "parts": {"IG": 100}}})
        make_parcel(self.tx, "6", -97.9, 29.9)
        r = self.client.get("/land/parcels.json", {"bbox": "-100,25,-70,40", "zoom": 10, "zoning": "industrial",
                                                   "status": "all"}).json()
        self.assertEqual(r["total"], 1)
        r = self.client.get("/land/parcels.json", {"bbox": "-100,25,-70,40", "zoom": 14, "county": "48209",
                                                   "status": "all"}).json()
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["features"][0]["geometry"]["type"], "Polygon")

    def test_pages_render(self):
        p = make_parcel(self.tx, "7", -97.9, 29.9, checked_at="2026-10-14T12:00:00Z")
        self.assertContains(self.client.get("/land/"), "Austin–San Antonio")
        self.assertContains(self.client.get(f"/land/parcel/{p.pk}/"), "Buy-box screen")
        self.assertContains(self.client.get("/land/export.csv", {"status": "all"}), "SMITH RANCH LLC")

    def test_pursue_creates_landowner_company_and_task(self):
        p = make_parcel(self.tx, "8", -97.9, 29.9, checked_at="2026-10-14T12:00:00Z")
        self.client.post(f"/land/parcel/{p.pk}/status/", {"status": "pursue"})
        p.refresh_from_db()
        self.assertEqual(p.status, "pursue")
        self.assertEqual(p.company.name, "Smith Ranch Llc")
        self.assertIn("Landowner", p.company.categories)
        self.assertTrue(Task.objects.filter(company=p.company, assignee=self.user).exists())
        # Pursuing again does not make a second company.
        p2 = make_parcel(self.tx, "9", -97.8, 29.9, checked_at="2026-10-14T12:00:00Z")
        self.client.post(f"/land/parcel/{p2.pk}/status/", {"status": "pursue"})
        self.assertEqual(Company.objects.filter(name="Smith Ranch Llc").count(), 1)

    def test_assemblage_site_and_qd_fit(self):
        a = make_parcel(self.md, "10", -77.000, 38.400, size=0.01, owner_name="WALD")
        b = make_parcel(self.md, "11", -76.990, 38.400, size=0.01, owner_name="COTTRELL")
        info = self.client.get("/land/assemblage.json", {"ids": f"{a.pk},{b.pk}"}).json()
        self.assertTrue(info["contiguous"])
        self.assertEqual(len(info["owners"]), 2)
        campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        project = Project.objects.create(name="ARA Facility", campus=campus)
        site = self.client.post("/land/sites/save/", json.dumps({"name": "Pomfret assemblage", "project": project.pk,
                                                                  "parcels": [a.pk, b.pk]}),
                                content_type="application/json").json()
        self.assertEqual(site["acres"], 300.0)
        saved = LandSite.objects.get(pk=site["id"])
        self.assertEqual(saved.campus, campus)  # taken from the project
        buildings = [{"name": "ERG A", "kind": "operating", "sqft": 1000, "hd": "HD 1.1", "newqd": 500,
                      "pes": "Open (no structure)"},
                     {"name": "ARA 02", "kind": "operating", "sqft": 21600, "hd": "HD 1.3", "newqd": 100000}]
        r = self.client.post("/land/qd/", json.dumps({"site": saved.pk, "buildings": buildings, "action": "fit"}),
                             content_type="application/json").json()
        self.assertTrue(r["summary"]["all_clear"], r)
        self.assertEqual(r["buildings"][0]["dist"]["ibd"], 1250)
        self.assertIn("ibd", r["buildings"][0]["shapes"])
        saved.refresh_from_db()
        self.assertEqual(len(saved.qd_design["buildings"]), 2)
        self.assertContains(self.client.get("/projects/"), "Pomfret assemblage")

    def test_qd_flags_arcs_off_a_small_parcel(self):
        p = make_parcel(self.md, "12", -77.0, 38.4, size=0.004)  # about 350 m square
        buildings = [{"name": "Mag", "kind": "magazine", "hd": "HD 1.1", "newqd": 100000, "pes": "Aboveground structure"}]
        r = self.client.post("/land/qd/", json.dumps({"parcel": p.pk, "buildings": buildings, "action": "fit"}),
                             content_type="application/json").json()
        self.assertFalse(r["summary"]["all_clear"])
        self.assertGreater(r["buildings"][0]["result"]["off_site_acres"], 100)


class DriveTimeTests(TestCase):
    def test_polyline6_decoding(self):
        # Two points encoded at precision 6: (38.5, -120.2), (40.7, -120.95)
        coords = drive._decode_polyline6("_izlhA~rlgdF_{geC~ywl@")
        self.assertAlmostEqual(coords[0][1], 38.5, places=4)
        self.assertAlmostEqual(coords[0][0], -120.2, places=4)
        self.assertAlmostEqual(coords[1][1], 40.7, places=4)

    def test_isochrones_batches_more_than_four_bands(self):
        calls = []

        class Resp:
            def __init__(self, contours):
                self._c = contours

            def json(self):
                return {"features": [{"geometry": {"type": "Polygon", "coordinates": []},
                                      "properties": {"contour": c["time"]}} for c in self._c]}

        def fake_post(url, json=None, timeout=None):
            calls.append(json["contours"])
            return Resp(json["contours"])

        with mock.patch.object(drive.SESSION, "post", side_effect=fake_post):
            out = drive.isochrones((38.4, -77.0), [10, 20, 30, 45, 60])
        self.assertEqual(len(calls), 2)
        self.assertEqual([f["properties"]["minutes"] for f in out["features"]], [60, 45, 30, 20, 10])


class SavedMapViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.me = User.objects.create_user("kellen", password="x")
        self.simon = User.objects.create_user("simon", password="x")
        self.client.force_login(self.me)

    def save(self, **body):
        return self.client.post("/land/views/save/", json.dumps(body), content_type="application/json").json()

    def test_save_default_share_and_delete(self):
        state = {"center": [38.4, -77.0], "zoom": 12, "base": "streets", "layers": ["rail"], "tools": {"drive": False},
                 "filters": "zoning=industrial", "side": False, "qd": True}
        a = self.save(name="Charles industrial", state=state, is_default=True)
        b = self.save(name="Texas rural", state={"zoom": 9}, is_default=True)
        views = {v["name"]: v for v in self.client.get("/land/views.json").json()["views"]}
        self.assertFalse(views["Charles industrial"]["is_default"])  # only one default per person
        self.assertTrue(views["Texas rural"]["is_default"])
        self.assertEqual(views["Charles industrial"]["state"]["tools"], {"drive": False})
        # Saving the same name again updates rather than duplicating.
        self.save(name="Texas rural", state={"zoom": 10})
        self.assertEqual(len(self.client.get("/land/views.json").json()["views"]), 2)
        # Shared views show for teammates, who can't change them.
        self.save(id=a["id"], shared=True)
        self.client.force_login(self.simon)
        names = [v["name"] for v in self.client.get("/land/views.json").json()["views"]]
        self.assertEqual(names, ["Charles industrial"])
        self.assertEqual(self.client.post(f"/land/views/{a['id']}/delete/").status_code, 404)
        self.client.force_login(self.me)
        self.client.post(f"/land/views/{b['id']}/delete/")
        self.assertEqual(len(self.client.get("/land/views.json").json()["views"]), 1)

    def test_page_has_toolbar_and_tool_switches(self):
        html = self.client.get("/land/").content.decode()
        for marker in ('id="toolbar"', 'data-show="qd"', 'id="views-menu"', 'id="tb-measure"', 'id="tb-drive"'):
            self.assertIn(marker, html)

    def test_list_form_does_not_offer_land_type(self):
        from crm.forms import ListForm

        self.assertNotIn("land", [c[0] for c in ListForm().fields["object_type"].choices])
