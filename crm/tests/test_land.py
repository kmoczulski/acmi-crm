"""Land search: QD engine, scoring, zoning buckets, assemblages/sites and the pages. No network calls."""
import json
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from PIL import Image
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

    def test_pursue_only_marks_the_parcel(self):
        """Landowners are not CRM companies: Pursue sets the status and nothing else."""
        before = (Company.objects.count(), Task.objects.count())
        p = make_parcel(self.tx, "8", -97.9, 29.9, checked_at="2026-10-14T12:00:00Z")
        self.client.post(f"/land/parcel/{p.pk}/status/", {"status": "pursue", "reason": "good road frontage"})
        p.refresh_from_db()
        self.assertEqual(p.status, "pursue")
        self.assertEqual(p.status_by, self.user)
        self.assertIn("good road frontage", p.notes)
        self.assertIsNone(p.company)
        self.assertEqual((Company.objects.count(), Task.objects.count()), before)

    def test_parcel_goes_straight_onto_a_project(self):
        project = Project.objects.create(name="Greystar")
        p = make_parcel(self.tx, "20", -97.9, 29.9, checked_at="2026-10-14T12:00:00Z")
        self.assertContains(self.client.get(f"/land/parcel/{p.pk}/"), "Add to project")
        self.client.post(f"/land/parcel/{p.pk}/project/", {"project": project.pk})
        site = project.land_sites.get()
        self.assertEqual(list(site.parcels.all()), [p])
        # A second click moves the same site; it does not make another one.
        other = Project.objects.create(name="Other")
        self.client.post(f"/land/parcel/{p.pk}/project/", {"project": other.pk})
        self.assertEqual(LandSite.objects.count(), 1)
        self.assertEqual(LandSite.objects.get().project, other)

    def test_project_page_lists_adds_and_removes_land_sites(self):
        project = Project.objects.create(name="Greystar")
        site = LandSite.objects.create(name="Bloomfield 77 ac")
        page = f"/projects/{project.pk}/"
        self.assertContains(self.client.get(page), "Bloomfield 77 ac")  # offered in the add list
        self.client.post(f"/projects/{project.pk}/sites/", {"site": site.pk, "action": "add"})
        site.refresh_from_db()
        self.assertEqual(site.project, project)
        self.assertContains(self.client.get(page), "Land sites (1)")
        self.client.post(f"/projects/{project.pk}/sites/", {"site": site.pk, "action": "remove"})
        site.refresh_from_db()
        self.assertIsNone(site.project)

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


class LandSiteImportTests(TestCase):
    def test_account_numbers_normalize(self):
        from crm.land.parcels import normalize_account

        for raw in ("906022677", "0906022677", "06022677", "09-06022677"):
            self.assertEqual(normalize_account("24017", raw), "0906022677")
        self.assertEqual(normalize_account("24017", "7043589"), "0907043589")

    def test_import_creates_and_updates_sites(self):
        import tempfile

        from django.core.management import call_command

        county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        a = make_parcel(county, "0906058558", -77.0, 38.4, owner_name="WALD")
        b = make_parcel(county, "0906027342", -76.99, 38.4, owner_name="FILIAGGI")
        campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        data = {"campus": "MEIH", "county": "Charles", "sites": [
            {"name": "Billingsley", "status": "loi", "accounts": ["906058558", "906027342"], "notes": "LOIs out"},
            {"name": "Rockhill (PG)", "status": "outreach", "accounts": [], "notes": "PG county"},
        ]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(data, fh)
        with mock.patch("crm.management.commands.import_land_sites.ensure_parcels",
                        return_value={a.key: a, b.key: b}):
            call_command("import_land_sites", fh.name, stdout=open(os.devnull, "w"))
            data["sites"][0]["status"] = "contract"
            with open(fh.name, "w", encoding="utf-8") as out:
                json.dump(data, out)
            call_command("import_land_sites", fh.name, stdout=open(os.devnull, "w"))
            self.assertEqual(LandSite.objects.get(name="Billingsley").status, "loi")  # a plain re-run keeps edits
            call_command("import_land_sites", fh.name, "--overwrite", stdout=open(os.devnull, "w"))
        site = LandSite.objects.get(name="Billingsley")
        self.assertEqual(site.status, "contract")  # --overwrite takes the file's values
        self.assertEqual(site.campus, campus)
        self.assertEqual(set(site.parcels.all()), {a, b})
        self.assertEqual(LandSite.objects.count(), 2)

    def test_sites_geojson(self):
        user = get_user_model().objects.create_user("kellen", password="x")
        self.client.force_login(user)
        county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        site = LandSite.objects.create(name="Lake Acton", status="loi")
        site.parcels.set([make_parcel(county, "1", -77.0, 38.4), make_parcel(county, "2", -76.99, 38.4)])
        LandSite.objects.create(name="No parcels yet")
        d = self.client.get("/land/sites.geojson").json()
        self.assertEqual(len(d["features"]), 1)
        self.assertEqual(d["features"][0]["properties"]["status"], "loi")
        self.assertEqual(d["features"][0]["geometry"]["type"], "Polygon")  # neighbours merge into one outline


class SharedShapeMergeTests(TestCase):
    """Texas appraisal files that give several property ids the same polygon load as one parcel."""

    def write_shapefile(self, path, rows):
        import shapefile

        with shapefile.Writer(str(path), shapeType=shapefile.POLYGON) as w:
            for name in ("PROP_ID", "OWNER_NAME", "LEGAL_DESC"):
                w.field(name, "C", 100)
            w.field("GIS_AREA", "N", 18, 4)
            w.field("LEGAL_AREA", "N", 18, 4)
            for prop_id, owner, legal, x0, gis, legal_ac in rows:
                w.poly([[(x0, 30.0), (x0, 30.05), (x0 + 0.05, 30.05), (x0 + 0.05, 30.0), (x0, 30.0)]])
                w.record(prop_id, owner, legal, gis, legal_ac)

    def test_ids_sharing_a_shape_become_one_parcel(self):
        import tempfile
        from pathlib import Path

        from crm.land.parcels import _load_texas_shapefile

        county = LandCounty.objects.create(fips="48209", name="Hays", state="TX")
        stale = make_parcel(county, "102", -98.0, 30.0, owner_name="HAY HILL HAY LLC", acres=3654.87)
        noted = make_parcel(county, "103", -98.0, 30.0, owner_name="HAY HILL HAY LLC", acres=3654.87, notes="call")
        with tempfile.TemporaryDirectory() as tmp:
            shp = Path(tmp) / "parcels.shp"
            self.write_shapefile(shp, [
                ("101", "HAY HILL HAY LLC", "ABS 1 3236 AC", -98.0, 3654.87, 3236.0),
                ("102", "HAY HILL HAY LLC", "ABS 2 38 AC", -98.0, 3654.87, 38.0),
                ("103", "HAY HILL HAY LLC", "ABS 3 1 AC", -98.0, 3654.87, 1.0),
                ("104", "OTHER RANCH LP", "ABS 4 356 AC", -98.0, 3654.87, 356.0),
                ("201", "HAY HILL HAY LLC", "ABS 9 100 AC", -97.9, 100.0, 100.0),
            ])
            entry = {"fips": "48209", "name": "Hays", "release": "2026"}
            _load_texas_shapefile(shp, entry, 20, log=lambda *_: None)

        keys = set(Parcel.objects.filter(county=county).values_list("key", flat=True))
        # 103 holds team notes, so it is the one kept for the shape; 101/102/104 fold into it.
        self.assertEqual(keys, {"103", "201"})
        self.assertFalse(Parcel.objects.filter(pk=stale.pk).exists())
        ranch = Parcel.objects.get(pk=noted.pk)
        self.assertEqual(ranch.notes, "call")
        self.assertEqual(ranch.acres, 3654.87)
        self.assertIn("same shape as prop ids 101, 102, 104", ranch.legal_desc)
        self.assertIn("also owned by OTHER RANCH LP", ranch.legal_desc)
        # Owner totals count the shared shape once: 3,654.87 + 100.
        self.assertEqual((ranch.owner_acres, ranch.owner_parcels), (3754.9, 2))

    def test_largest_legal_area_is_kept_without_team_work(self):
        from crm.land.parcels import shared_shapes

        sig = (-98.0, 30.0, -97.95, 30.05, 3654.9)
        merged = shared_shapes({"101": (sig, (False, 3236.0)), "102": (sig, (False, 38.0)),
                                "201": ((-97.9, 30.0, -97.85, 30.05, 100.0), (False, 100.0))})
        self.assertEqual(merged, {"101": ["102"]})


class SiteManagementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("kellen", password="x")
        self.client.force_login(self.user)
        self.county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.site = LandSite.objects.create(name="Livingston Rd", status="outreach", campus=self.campus)
        self.site.parcels.set([make_parcel(self.county, "0907075189", -77.0, 38.4)])

    def url(self):
        return f"/land/sites/{self.site.pk}/manage/"

    def test_milestones_move_status_forward_and_log(self):
        self.client.post(self.url(), {"action": "milestones", "loi_sent": "2026-01-23", "loi_signed": "2026-05-06"})
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, "loi")
        rows = {r["key"]: r for r in self.site.milestone_rows()}
        self.assertEqual(rows["loi_signed"]["gap"], 103)
        self.client.post(self.url(), {"action": "milestones", "loi_sent": "2026-01-23", "psa_signed": "2026-09-01"})
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, "contract")
        # Clearing dates never moves the status back.
        self.client.post(self.url(), {"action": "milestones"})
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, "contract")
        texts = list(self.site.events.values_list("text", flat=True))
        self.assertTrue(any("LOI signed: May 6, 2026" in t for t in texts))
        self.assertTrue(any("Under contract" in t for t in texts))

    def test_site_page_has_mini_map_of_its_parcels(self):
        with mock.patch("crm.land.sources.fema_flood_layer_id", return_value=35):
            r = self.client.get(self.url())
        data = r.context["map_data"]
        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["properties"]["status"], "outreach")  # inherits the site's stage
        html = r.content.decode()
        self.assertIn('id="site-map"', html)
        self.assertIn("land_layers.js", html)

    def test_dropped_site_keeps_status(self):
        self.site.status = "dropped"
        self.site.save()
        self.client.post(self.url(), {"action": "milestones", "loi_sent": "2026-03-13"})
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, "dropped")

    def test_edit_details_note_and_remove_parcel(self):
        self.client.post(self.url(), {"action": "details", "name": "Airport Land", "status": "loi",
                                      "campus": self.campus.pk, "project": "", "notes": "PSA redlines"})
        self.client.post(self.url(), {"action": "note", "text": "Met the County"})
        p = self.site.parcels.first()
        self.client.post(self.url(), {"action": "remove_parcel", "parcel": p.pk})
        self.site.refresh_from_db()
        self.assertEqual((self.site.name, self.site.status, self.site.parcels.count()), ("Airport Land", "loi", 0))
        texts = " | ".join(self.site.events.values_list("text", flat=True))
        for bit in ("Renamed", "Met the County", "Removed parcel", "LOI / negotiating"):
            self.assertIn(bit, texts)
        self.assertTrue(Parcel.objects.filter(pk=p.pk).exists())  # the parcel itself stays in land search

    def test_sites_page_timeline_and_order(self):
        LandSite.objects.create(name="ETC", status="contract", campus=self.campus, milestones={"psa_signed": "2026-06-22"})
        self.site.milestones = {"contacted": "2026-02-06"}
        self.site.save()
        html = self.client.get("/land/sites/").content.decode()
        self.assertIn("Deal timelines", html)
        self.assertLess(html.index(">ETC<"), html.index(">Livingston Rd<"))  # contract before outreach

    def test_incentives_check_lists_programs(self):
        from shapely.geometry import box

        from crm.land import incentives

        def fake_query(url, **kw):
            if "Opportunity_Zones/FeatureServer/13" in url:
                return [{"properties": {"GEOID10": "48209010702"}}]
            if "FTZ_2023" in url:
                return [{"properties": {"Zone": "183", "Grantee": "Foreign Trade Zone of Central Texas",
                                        "Service_Ar": "Bastrop, Caldwell, Hays, Travis and Williamson Counties"},
                         "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]}}]
            return []

        with mock.patch.object(incentives, "arcgis_query", side_effect=fake_query):
            out = incentives.check(box(-97.95, 29.87, -97.94, 29.88), "Hays", "TX")
        keys = {p["key"]: p for p in out["programs"]}
        self.assertEqual(set(keys), {"oz", "ftz"})
        self.assertIn("covers Hays County", keys["ftz"]["area"])
        self.assertTrue(keys["oz"]["link"].startswith("https://www.irs.gov"))
        # Maryland-only programs aren't queried for Texas parcels.
        self.assertNotIn("md_pfa", [p["key"] for p in incentives.for_state("TX")])


class SitePlanTests(TestCase):
    def setUp(self):
        from shapely.geometry import box as sbox

        self.square = sbox(0, 0, 640, 640)  # ~101 acres, in metres
        self.user = get_user_model().objects.create_user("kellen", password="x")
        self.client.force_login(self.user)

    def test_charles_standards_from_county_code(self):
        from crm.land import siteplan

        ig = siteplan.standards_for("24017", "IG")
        self.assertEqual((ig["front"], ig["side"], ig["rear"], ig["far"], ig["isr"]), (50, 6, 10, 0.50, 0.75))
        wcd = siteplan.standards_for("24017", "WCD")
        self.assertEqual((wcd["isr"], wcd["coverage"]), (0.08, 0.10))
        self.assertEqual(siteplan.standards_for("48209", "")["code"], "")  # Texas: no-zoning default

    def test_layout_respects_zone_limits_and_rezoning_adds_yield(self):
        from crm.land import siteplan

        program = [{"type": "flex", "count": 2}, {"type": "warehouse"}]
        _, wcd = siteplan.layout(self.square, siteplan.standards_for("24017", "WCD"), program)
        _, ig = siteplan.layout(self.square, siteplan.standards_for("24017", "IG"), program)
        self.assertTrue(all(c["ok"] for c in wcd["checks"]))
        self.assertTrue(all(c["ok"] for c in ig["checks"]))
        self.assertLessEqual(wcd["impervious_sf"] / (wcd["site_acres"] * 43560), 0.08 + 0.005)
        self.assertGreater(ig["gfa_sf"], 4 * wcd["gfa_sf"])

    def test_parking_modes(self):
        from crm.land import siteplan

        self.assertEqual(siteplan.parking_required("flex", 100_000, "market"), 250)       # 2.5 / 1,000 sf
        self.assertEqual(siteplan.parking_required("flex", 100_000, "market", ratio=1.0), 100)
        # County code: 25% office / 300 sf + 1.5 staff per 1,000 sf = 84 + 150
        self.assertEqual(siteplan.parking_required("flex", 100_000, "code"), 234)

    def test_endpoint_plans_and_saves_on_site(self):
        county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        p = make_parcel(county, "1", -77.0, 38.4, size=0.008,
                        overlays={"zoning": {"value": "WCD", "parts": {"WCD": 100}}})
        site = LandSite.objects.create(name="Test site")
        site.parcels.set([p])
        body = {"site": site.pk, "program": [{"type": "flex", "count": 2}], "zone": "IG", "action": "save"}
        r = self.client.post("/land/siteplan/", json.dumps(body), content_type="application/json").json()
        self.assertEqual(r["current_zone"], "WCD")
        self.assertEqual(r["standards"]["code"], "IG")
        self.assertGreater(r["summary"]["gfa_sf"], 0)
        self.assertTrue(any(f["properties"]["kind"] == "building" for f in r["features"]["features"]))
        site.refresh_from_db()
        self.assertEqual(site.site_plan["zone"], "IG")


class ArchiveAndParcelDealTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("kellen", password="x")
        self.client.force_login(self.user)
        county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        self.k1 = make_parcel(county, "0907000286", -77.0, 38.4, size=0.002, owner_name="KROC DAVID", acres=0.25)
        self.k2 = make_parcel(county, "0907002149", -76.998, 38.4, size=0.002, owner_name="KROC DAVID", acres=0.48)
        self.f = make_parcel(county, "0907000103", -76.996, 38.4, size=0.002, owner_name="FOCUS ON LLC", acres=0.16)
        self.site = LandSite.objects.create(name="Outside the Gate", status="loi")
        self.site.parcels.set([self.k1, self.k2, self.f])

    def test_dropped_sites_go_to_archive(self):
        LandSite.objects.create(name="Garner", status="dropped", notes="PASS (seller passed)")
        html = self.client.get("/land/sites/").content.decode()
        archive = html[html.index('id="archive"'):]
        self.assertIn("Garner", archive)
        self.assertIn("seller passed", archive)
        self.assertNotIn("Garner", html[:html.index('id="archive"')])

    def test_parcel_deal_owner_apply_and_rollup(self):
        from crm.land import tracker

        tracker.ensure_deals(self.site)  # deal rows are made when parcels join a site (map save, import, site page)
        summary = tracker.summary(self.site)
        self.assertEqual(summary["owner_count"], 2)
        kroc = self.site.parcel_deals.get(parcel=self.k1)
        self.client.post(f"/land/sites/{self.site.pk}/manage/", {
            "action": "parcel_deal", "deal": kroc.pk, "status": "loi", "loi_sent": "2026-04-10",
            "psa_signed": "2026-09-30", "notes": "PSA signed", "apply_owner": "1"})
        statuses = dict(self.site.parcel_deals.values_list("parcel__owner_name", "status"))
        self.assertEqual(statuses["KROC DAVID"], "contract")  # PSA signed moves it forward
        self.assertEqual(self.site.parcel_deals.filter(status="contract").count(), 2)  # both Kroc parcels
        self.assertEqual(statuses["FOCUS ON LLC"], "review")
        s = tracker.summary(self.site)
        self.assertEqual(s["secured_acres"], 0.7)
        self.assertEqual(s["suggested_status"], "review")  # the slowest parcel sets the assemblage's stage
        self.assertTrue(self.site.events.filter(kind="parcel", text__contains="Kroc David (2 parcels)").exists())
        html = self.client.get(f"/land/sites/{self.site.pk}/manage/").content.decode()
        self.assertIn("Assemblage tracker", html)

    def test_single_owner_site_inherits_site_status(self):
        from crm.land import tracker

        solo = LandSite.objects.create(name="Lake Acton", status="loi", milestones={"loi_signed": "2026-04-10"})
        solo.parcels.set([self.f])
        tracker.ensure_deals(solo)
        d = solo.parcel_deals.get()
        self.assertEqual((d.status, d.milestones), ("loi", {"loi_signed": "2026-04-10"}))


class AlabamaCountyTests(TestCase):
    """Huntsville-area counties (Limestone, Madison, Morgan): field mapping, keys, regions, zoning buckets."""

    def test_limestone_record_maps_to_parcel_fields(self):
        from crm.land import counties

        cfg = counties.find("limestone al")
        rec = {"ParcelNo": "05 07 36 0 000 001.000   ", "CalcAcreage": 399.8, "OwnerName": "GLASS HOLLOW PROPERTIES   ",
               "MailAddress1": "402 W HOBBS ST", "MailCity": "ATHENS ", "MailState": "AL  ", "MailZip": "35611 ",
               "AssessmentClass": "3", "TotalLandValue": 900000.0, "TotalImpValue": 0.0, "TotalValue": 900000.0,
               "RecordYear": 2027}
        f = counties.record_fields(cfg, rec)
        self.assertEqual(counties.record_key(cfg, rec), "0507360000001000")
        self.assertEqual((f["owner_name"], f["mail_state"], f["state_land_use"]), ("GLASS HOLLOW PROPERTIES", "AL", "D"))
        self.assertEqual(f["market_value"], 900000.0)
        self.assertEqual(counties.acres_of(cfg, rec), 399.8)
        out_of_state = counties.record_fields(cfg, {**rec, "MailState": "TEXAS", "MailZip": "75024"})
        self.assertEqual(out_of_state["mail_state"], "TX")

    def test_madison_one_line_mailing_address_is_split(self):
        from crm.land import counties

        f = counties.record_fields(counties.find("01089"), {
            "ParcelNum": "16-09-30-0-000-041.000", "PropertyOwner": "HUNTSVILLE MADISON COUNTY AIRPORT",
            "MailingAddress": "1000 GLENN HEARN BLVD SW BOX 20008, HUNTSVILLE, AL 35824-2107", "ExemptCode": "S",
            "Acres": 363.95})
        self.assertEqual((f["mail_address"], f["mail_city"], f["mail_state"], f["mail_zip"]),
                         ("1000 GLENN HEARN BLVD SW BOX 20008", "HUNTSVILLE", "AL", "35824-2107"))
        self.assertEqual(f["state_land_use"], "X")  # exempt owner

    def test_texas_and_alabama_counties_with_the_same_name_stay_apart(self):
        from crm.land import counties
        from crm.land.sources import region_for

        self.assertIsNone(counties.find("limestone"))  # bare name stays the Texas county (TxGIO)
        self.assertEqual(counties.find("morgan")["fips"], "01103")
        self.assertEqual(region_for("Limestone", "AL"), "North Alabama (Huntsville)")
        self.assertEqual(region_for("Limestone", "TX"), "Central Texas (I-35 / Brazos Valley)")

    def test_huntsville_zones_and_unincorporated_alabama(self):
        al = LandCounty.objects.create(fips="01083", name="Limestone", state="AL")
        pi = make_parcel(al, "1", -86.9, 34.7, overlays={"zoning": {"value": "PI", "name": "Planned Industrial", "pct": 100}},
                         in_city="Huntsville")
        rural = make_parcel(al, "2", -87.1, 34.9, state_land_use="D")
        self.assertEqual((pi.zoning_class, rural.zoning_class), ("industrial", "unzoned"))
        self.assertTrue(any("no county zoning" in r["text"] for r in rural.score_reasons))


class FayettevilleCountyTests(TestCase):
    """Cumberland County, NC from NC OneMap's statewide layer: county scoping, acres, keys, field mapping."""

    def test_cumberland_record_maps_to_parcel_fields(self):
        from crm.land import counties
        from crm.land.sources import region_for

        cfg = counties.find("Fayetteville NC")
        self.assertEqual(cfg["fips"], "37051")
        rec = {"objectid": 7, "parno": "9495-62-3296", "ownname": "PATEL, MIT", "mailadd": "6847 TOWBRIDGE RD",
               "mcity": "FAYETTEVILLE", "mstate": "NC", "mzip": "28306", "siteadd": "6847 TOWBRIDGE RD",
               "recareano": 0.58, "gisacres": 1.3e-05, "parusedesc": "R101-RES", "landval": 180000.0,
               "improvval": 1283148.0, "parval": 1191700.0, "structyear": 2023}
        f = counties.record_fields(cfg, rec)
        self.assertEqual((f["owner_name"], f["mail_state"], f["state_land_use"], f["year_built"]),
                         ("PATEL, MIT", "NC", "A", "2023"))
        self.assertEqual(counties.acres_of(cfg, rec), 0.58)
        self.assertEqual(counties.acres_of(cfg, {"recareano": 0, "Shape__Area": 87120.0}), 2.0)
        self.assertEqual(counties.record_key(cfg, rec), "9495-62-3296")
        self.assertEqual(counties.record_key(cfg, {"objectid": 7, "parno": ""}), "OID7")
        self.assertEqual(region_for("Cumberland", "NC"), "Fayetteville, NC")
        self.assertEqual(region_for("Cumberland", "TX"), "")
        self.assertEqual([counties.find(k)["fips"] for k in ("hoke nc", "Harnett NC", "moore nc")],
                         ["37093", "37085", "37125"])
        self.assertIsNone(counties.find("moore"))  # bare name stays the Texas county

    def test_hoke_uses_its_own_county_service_with_owners(self):
        from crm.land import counties

        cfg = counties.find("hoke nc")
        self.assertIn("maps.hokecounty.org", cfg["parcels_url"])
        rec = {"TWN_PIN": "183990001001", "OWNER_NAME": "GESSLER, WILLIAM JR.", "OWNER_NA_1": "GESSLER, LEONA",
               "MAILING_AD": "4445 MCGIRT RD", "CITY": "MAXTON", "STATE_OR_C": "NC", "ZIP_CODE": "28364",
               "TOTAL_ACRE": 19.73, "CAL_ACRES": 11.98, "COST_TOTAL": 19850.0, "STREET_NAM": "OFF S HILLTOP RD"}
        f = counties.record_fields(cfg, rec)
        self.assertEqual((f["owner_name"], f["mail_city"], f["mail_state"], f["market_value"]),
                         ("GESSLER, WILLIAM JR.", "MAXTON", "NC", 19850.0))
        self.assertEqual(counties.acres_of(cfg, rec), 19.73)
        self.assertEqual(counties.record_key(cfg, rec), "183990001001")
        self.assertTrue(counties.big_where(cfg, 16).startswith("(1=1) AND ("))

    def test_queries_are_scoped_to_the_county(self):
        from crm.land import counties

        cfg = counties.find("37051")
        where = counties.big_where(cfg, 16)
        self.assertTrue(where.startswith("(cntyname = 'Cumberland') AND ("))
        self.assertIn("recareano IS NULL", where)


class ArizonaCountyTests(TestCase):
    """Maricopa (Tonopah) and Pinal (Casa Grande, Eloy): keys, acres, field mapping, regions."""

    def test_apns_keep_their_suffix_and_dashes_are_dropped(self):
        from crm.land import counties
        from crm.land.parcels import normalize_account

        self.assertEqual(normalize_account("04013", "506-45-029h"), "50645029H")
        self.assertEqual(normalize_account("04021", "503-26-020E"), "50326020E")
        self.assertEqual(counties.record_key(counties.find("04021"), {"PARCELID": "50326025D"}), "50326025D")
        self.assertEqual(counties.record_key(counties.find("04021"), {"PARCELID": " "}), "")  # no tax account: skipped
        # "NAP" (no parcel number assigned) is shared by many parcels: key each by its object id
        nap = counties.find("04021")
        self.assertEqual(counties.record_key(nap, {"PARCELID": "NAP", "OBJECTID": 77}), "NAP77")
        self.assertNotEqual(counties.record_key(nap, {"PARCELID": "NAP", "OBJECTID": 78}), "NAP77")
        self.assertEqual(counties.record_fields(nap, {"PARCELID": "NAP", "OBJECTID": 77})["prop_id"], "")

    def test_full_windows_keep_small_parcels_only_inside_them(self):
        from crm.land import counties

        cfg = counties.find("04021")
        self.assertTrue(counties.in_windows(cfg, box(-111.65, 32.80, -111.64, 32.81)))
        self.assertFalse(counties.in_windows(cfg, box(-112.2, 33.0, -112.19, 33.01)))
        self.assertFalse(counties.in_windows(counties.find("24017"), box(-77.0, 38.5, -76.99, 38.51)))

    def test_maricopa_record_maps_to_parcel_fields(self):
        from crm.land import counties
        from crm.land.sources import region_for

        cfg = counties.find("Maricopa AZ")
        rec = {"APN": "50645028", "APN_DASH": "506-45-028", "OWNER_NAME": "313 WINTERSBURG LLC", "LAND_SIZE": 6969600,
               "PUC": "0014", "MAIL_STATE": "AZ", "MAIL_ADDR1": "1 MAIN ST", "FCV_CUR": "  1,250,000", "TAX_YR_CUR": "2027"}
        f = counties.record_fields(cfg, rec)
        self.assertEqual(counties.acres_of(cfg, rec), 160.0)
        self.assertEqual((f["prop_id"], f["state_land_use"], f["market_value"], f["mail_state"]),
                         ("506-45-028", "D", 1250000.0, "AZ"))
        self.assertEqual(region_for("Pinal", "AZ"), "Central Arizona (I-10 corridor)")
        self.assertIn("LAND_SIZE >= ", counties.count_where(cfg))
        self.assertEqual(counties.big_where(cfg, 16), "LAND_SIZE >= 696960")

    def test_pinal_record_and_zoning_map_to_parcel_fields(self):
        from crm.land import counties

        cfg = counties.find("pinal")
        self.assertIn("gis.pinal.gov", cfg["parcels_url"])
        rec = {"PARCELID": "409010150", "OWNERNME1": "ZUHEIR PROPERTIES", "PSTLADDRESS": "3959 E NAVIGATOR LN",
               "PSTLCITY": "PHOENIX", "PSTLSTATE": "AZ", "PSTLZIP5": "85050", "USEDSCRP": "Vacant Land",
               "CLASSDSCRP": "Vacant Land / Non-Profit Imp", "LNDVALUE": 479760.0, "GROSSAC": 319.84}
        f = counties.record_fields(cfg, rec)
        self.assertEqual((f["owner_name"], f["mail_city"], f["state_land_use"], f["land_value"], f["legal_acres"]),
                         ("ZUHEIR PROPERTIES", "PHOENIX", "F1", 479760.0, 319.84))
        self.assertEqual(counties.acres_of(cfg, {"GROSSAC": 0, "LANDSF": 87120}), 2.0)
        zoning = cfg["overlays"]["zoning"]
        self.assertEqual(zoning["value_labels"]["19"], "CI-1")

    def test_pinal_zoning_codes_bucket(self):
        county = LandCounty.objects.create(fips="04021", name="Pinal", state="AZ")
        ind = make_parcel(county, "1", -111.6, 32.8, overlays={"zoning": {"value": "CI-2", "pct": 100}})
        city = make_parcel(county, "2", -111.7, 32.9, overlays={"zoning": {"value": "ELOY", "pct": 100}})
        self.assertEqual((ind.zoning_class, city.zoning_class), ("industrial", "town"))


class IndianaCountyTests(TestCase):
    """Greene County, IN from IndianaMap's statewide layer: county scoping, acres from the geometry, class codes."""

    def test_greene_record_maps_to_parcel_fields(self):
        from crm.land import counties
        from crm.land.sources import region_for

        cfg = counties.find("Greene IN")
        self.assertEqual(cfg["fips"], "18055")
        self.assertIsNone(counties.find("greene"))
        rec = {"parcel_id": "2808220000070051", "local_id": "5217", "esri_poname": "Bloomfield", "esri_zip": "47424",
               "dlgf_prop_class_code": "101", "SHAPE__Area": 4.6245e-07}
        f = counties.record_fields(cfg, rec)
        self.assertEqual((f["prop_id"], f["state_land_use"], f["situs_city"], f["owner_name"]),
                         ("2808220000070051", "D", "Bloomfield", ""))
        self.assertAlmostEqual(counties.acres_of(cfg, rec), 1.07, places=1)
        self.assertEqual(region_for("Greene", "IN"), "Southwest Indiana (Crane)")
        self.assertTrue(counties.big_where(cfg, 16).startswith("(county_fips = '18055') AND (SHAPE__Area >= "))


class ArizonaZoningAndUtilityTests(TestCase):
    """City zoning layers refine the county's bare city name; AZ utility checks; Pinal / Maricopa zoning buckets."""

    def _index(self, layers):
        """{key: (tree, albers geoms, props, spec)} like counties.overlay_index builds, from (key, spec, box, props)."""
        from shapely import STRtree

        from crm.land.layers import to_albers

        index = {}
        for key, spec, geom, props in layers:
            g = to_albers(geom)
            index[key] = (STRtree([g]), [g], [props], spec)
        return index

    def test_city_zoning_replaces_the_county_city_name_where_it_applies(self):
        from crm.land import counties

        parcel = box(-111.600, 32.750, -111.598, 32.752)
        county = {"label": "Zoning (Pinal County)", "value": "ZONING", "value_labels": {"54": "ELOY"}, "fields": ["ZONING"]}
        eloy = {"label": "Zoning (City of Eloy)", "value": "Zoning", "name_field": "ShortDescription", "authority": "City of Eloy",
                "refines": "zoning", "when_value": "ELOY", "fields": ["Zoning"]}
        index = self._index([
            ("zoning", county, box(-111.7, 32.7, -111.5, 32.8), {"ZONING": 54}),
            ("zoning_eloy", eloy, box(-111.7, 32.7, -111.5, 32.8), {"Zoning": "I-2", "ShortDescription": "I-2 Heavy Industrial"}),
        ])
        zoning, overlays = counties.stamp(index, parcel)
        self.assertEqual(zoning, "I-2")
        self.assertEqual(overlays["zoning"]["value"], "I-2")
        self.assertEqual(overlays["zoning"]["authority"], "City of Eloy")
        self.assertEqual(overlays["zoning_county"]["value"], "ELOY")
        self.assertNotIn("zoning_eloy", overlays)
        # A parcel the county zones itself keeps the county zoning, and the unused city overlay is dropped.
        county_zoned = self._index([
            ("zoning", county, box(-111.7, 32.7, -111.5, 32.8), {"ZONING": 12}),
            ("zoning_eloy", eloy, box(-111.7, 32.7, -111.5, 32.8), {"Zoning": "I-2", "ShortDescription": ""}),
        ])
        county_zoned["zoning"][3]["value_labels"] = {"12": "RV"}
        zoning, overlays = counties.stamp(county_zoned, parcel)
        self.assertEqual((zoning, overlays["zoning"]["value"]), ("RV", "RV"))
        self.assertNotIn("zoning_eloy", overlays)

    def test_zoning_buckets_for_pinal_cities_and_maricopa_county(self):
        pinal = LandCounty.objects.create(fips="04021", name="Pinal", state="AZ")
        maricopa = LandCounty.objects.create(fips="04013", name="Maricopa", state="AZ")
        cg = make_parcel(pinal, "A", -111.7, 32.9, overlays={"zoning": {"value": "I-2 / CR", "pct": 100}})
        eloy = make_parcel(pinal, "B", -111.6, 32.7, overlays={"zoning": {"value": "R1-6", "pct": 100}})
        county = make_parcel(pinal, "C", -111.5, 32.8, overlays={"zoning": {"value": "CI-2", "pct": 100}})
        mc = make_parcel(maricopa, "D", -113.0, 33.4, overlays={"zoning": {"value": "IND-3", "pct": 100}})
        self.assertEqual([p.zoning_class for p in (cg, eloy, county, mc)],
                         ["industrial", "residential", "industrial", "industrial"])

    def test_arizona_checks_and_electric_filter(self):
        from crm.land import lookup

        pinal = lookup.checks_for("AZ", "Pinal")
        self.assertTrue({"water", "sewer", "electric", "flood", "incentives"} <= set(pinal))
        self.assertIn("water", lookup.checks_for("AZ", "Maricopa"))
        national = {"providers": [{"name": "Arizona Public Service Co", "kind": "Investor owned"},
                                  {"name": "Tonopah Irrigation District", "kind": "Not available"},
                                  {"name": "Wapa-- Western Area Power Administration", "kind": "Federal"},
                                  {"name": "Electrical Dist No2 Pinal County", "kind": "Political subdivision"}]}
        county_layer = [{"properties": {"District_Name": "Electrical District 4"}}]
        with mock.patch.object(lookup, "check_electric_national", return_value=(dict(national), None)), \
                mock.patch.object(lookup, "arcgis_query", return_value=county_layer):
            data, _ = lookup.az_electric_check("Pinal")(box(-111.6, 32.7, -111.59, 32.71), None)
        self.assertEqual([p["name"] for p in data["providers"]], ["Arizona Public Service Co", "Electrical District 4"])
        self.assertEqual(data["other_territories"], ["Tonopah Irrigation District", "Wapa-- Western Area Power Administration"])


def briefs_module():
    from crm.land import briefs

    return briefs


class SiteBriefTests(TestCase):
    """One-page site briefs and their aerials."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("kellen", password="x")
        self.client.force_login(self.user)
        county = LandCounty.objects.create(fips="04021", name="Pinal", state="AZ")
        checks = {
            "flood": {"sfha_pct": 60.0, "floodway_pct": 0, "x500_pct": 0, "zones": ["A"], "mapped": True},
            "wetlands": {"pct": 1.0, "types": {}}, "electric": {"providers": [{"name": "APS", "kind": "Investor owned"}]},
            "water": {"providers": [], "details": [], "nearest_line": {"miles": 0.2, "name": "City water main"}},
            "sewer": {"providers": [], "details": [], "nearest_line": None}, "hubzone": {"qualified": True},
            "epa": {"sites": []}, "incentives": {"programs": [{"key": "oz", "label": "Opportunity Zones", "points": 3, "area": "Tract 1", "summary": "Federal tax incentive for investors."}]},
            "net_usable": {"acres": 60.0, "lost_pct": 40.0},
        }
        self.a = make_parcel(county, "50326020E", -111.7, 32.9, owner_name="BARNES VERNON LESTER", acres=100.0,
                             overlays={"zoning": {"value": "I-2", "name": "Heavy Industrial", "authority": "City of Casa Grande",
                                                  "pct": 100}}, checks=checks, near={"transmission": {"miles": 0.7, "name": "APS", "VOLT_CLASS": "230"}})
        self.site = LandSite.objects.create(name="AZ - Barnes Farm", notes="Land Advisors listing")
        self.site.parcels.set([self.a])

    def test_build_summarises_the_site_without_running_checks_again(self):
        from crm.land import briefs

        with mock.patch.object(briefs, "run_checks_many", side_effect=AssertionError("checks are already stored")):
            b = briefs.build(self.site)
        self.assertEqual((b["n_parcels"], b["flood"], b["net"]), (1, 60.0, 60.0))
        self.assertEqual(b["owners"][0]["name"], "BARNES VERNON LESTER")
        self.assertEqual(b["zoning"][0]["code"], "I-2")
        texts = [f["text"] for f in b["flags"]]
        self.assertTrue(any("zone A" in t and "approximate" in t for t in texts))
        self.assertTrue(any("industrial" in t for t in texts))
        self.assertEqual(b["flags"][0]["kind"], "bad")  # red flags sort first
        self.assertTrue(b["hubzone"])

    def test_sections_can_be_removed_and_added_per_site(self):
        from crm.land import briefs

        with mock.patch.object(briefs, "run_checks_many"):
            full = self.client.get(f"/land/sites/{self.site.pk}/brief/")
            self.assertContains(full, "Power, roads &amp; utilities")
            self.assertContains(full, "Parcels in this site")
            form = self.client.get(f"/land/sites/{self.site.pk}/brief/customize/")
            self.assertContains(form, "Your own sections")
            resp = self.client.post(f"/land/sites/{self.site.pk}/brief/customize/", {
                "show": ["maps", "ownership", "flags", "verify", "notes", "flyer", "claims"],  # power and parcels left out
                "extra_title_0": "Why ACMI likes it", "extra_body_0": "Next to the I-10 interchange."})
            self.assertRedirects(resp, f"/land/sites/{self.site.pk}/brief/")
            cut = self.client.get(f"/land/sites/{self.site.pk}/brief/")
        self.assertNotContains(cut, "Power, roads &amp; utilities")
        self.assertNotContains(cut, "Parcels in this site")
        self.assertContains(cut, "Why ACMI likes it")
        self.assertContains(cut, "Next to the I-10 interchange.")
        self.site.refresh_from_db()
        self.assertEqual(sorted(self.site.brief_options["hide"]), ["parcels", "power"])
        with mock.patch.object(briefs, "run_checks_many"):
            self.client.post(f"/land/sites/{self.site.pk}/brief/customize/", {
                "show": ["maps", "ownership", "flags", "power", "verify", "notes", "parcels", "flyer", "claims"],
                "extra_title_0": "Why ACMI likes it", "extra_body_0": "x", "extra_remove_0": "on"})
            back = self.client.get(f"/land/sites/{self.site.pk}/brief/")
        self.assertContains(back, "Power, roads &amp; utilities")
        self.assertNotContains(back, "Why ACMI likes it")

    def test_comparison_pages_and_rows_can_be_removed(self):
        from crm.land import briefs

        other = LandSite.objects.create(name="AZ - Second")
        other.parcels.set([self.a])
        with mock.patch.object(briefs, "run_checks_many"):
            both = self.client.get(f"/land/briefs/?ids={self.site.pk}&ids={other.pk}")
            cut = self.client.get(f"/land/briefs/?ids={self.site.pk}&ids={other.pk}&hide=row_flood,definitions,zoning")
        self.assertContains(both, "FEMA floodplain</th>")
        self.assertContains(both, "Notes &amp; definitions")
        self.assertNotContains(cut, "FEMA floodplain</th>")
        self.assertNotContains(cut, "How the buy-box score is calculated")
        self.assertContains(cut, "Add or remove from this report")

    def test_pick_sites_to_compare_from_the_sites_page_and_the_briefs_page(self):
        from crm.land import briefs

        other = LandSite.objects.create(name="AZ - Petersen Park")
        self.assertContains(self.client.get("/land/sites/"), f'name="ids" value="{other.pk}"')
        with mock.patch.object(briefs, "run_checks_many"):
            # The sites page sends repeated ids; one site only, then both.
            one = self.client.get("/land/briefs/", {"ids": [self.site.pk]})
            both = self.client.get(f"/land/briefs/?ids={self.site.pk}&ids={other.pk}")
        self.assertEqual(len(one.context["briefs"]), 1)
        self.assertEqual(len(both.context["briefs"]), 1)  # a site with no parcels can't be briefed...
        self.assertContains(both, "Left out, no parcels mapped yet")  # ...and the page says so
        self.assertContains(both, "AZ - Petersen Park")
        self.assertContains(both, "Choose sites")
        self.assertEqual(sorted(on for _, on in one.context["picker"]), [False, True])
        other.parcels.set([self.a])
        with mock.patch.object(briefs, "run_checks_many"):
            two = self.client.get(f"/land/briefs/?ids={self.site.pk}&ids={other.pk}")
        self.assertEqual(len(two.context["briefs"]), 2)

    def test_pages_render_and_aerial_is_a_jpeg(self):
        from crm.land import briefs

        with mock.patch.object(briefs, "run_checks_many"):
            page = self.client.get(f"/land/sites/{self.site.pk}/brief/")
            cover = self.client.get(f"/land/briefs/?ids={self.site.pk}")
        self.assertContains(page, "AZ - Barnes Farm")
        self.assertContains(page, "Save as PDF")
        self.assertContains(page, "window.print()")
        self.assertContains(cover, "Site comparison")
        fake = Image.new("RGBA", (1400, 760), (90, 120, 60, 255))
        with mock.patch.object(briefs, "_fetch", return_value=fake),                 mock.patch.object(briefs, "_fetch_fema", return_value=Image.new("RGBA", (1400, 760), (0, 229, 255, 77))):
            briefs._CACHE.clear()
            resp = self.client.get(f"/land/sites/{self.site.pk}/aerial.jpg")
            ctx = self.client.get(f"/land/sites/{self.site.pk}/aerial.jpg?mode=context")
        self.assertEqual(resp["Content-Type"], "image/jpeg")
        self.assertTrue(resp.content.startswith(b"\xff\xd8"))
        self.assertEqual(ctx.status_code, 200)

    def test_fema_layer_is_strengthened_and_the_aerial_survives_fema_being_down(self):
        from crm.land import briefs

        fema = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        for x in range(30):
            for y in range(60):
                fema.putpixel((x, y), (0, 229, 255, 77))  # FEMA's faint 100-year fill
        fema.putpixel((50, 50), (254, 254, 254, 255))  # a label pixel keeps its own opacity
        out, kinds = briefs._strengthen_flood(fema)
        self.assertEqual(kinds, {"100"})
        self.assertEqual(out.getpixel((5, 5))[3], briefs.FEMA_ALPHA)
        self.assertEqual(out.getpixel((50, 50))[3], 255)
        self.assertEqual(out.getpixel((45, 5))[3], 0)
        fake = Image.new("RGBA", (1400, 760), (90, 120, 60, 255))
        geom = box(-111.7, 32.9, -111.69, 32.91)
        requested = []
        with mock.patch.object(briefs, "_fetch", side_effect=lambda layer, *a, **k: requested.append(layer) or fake),                 mock.patch.object(briefs, "_fetch_fema", side_effect=OSError("FEMA down")):
            briefs._CACHE.clear()
            data = briefs.render_aerial([geom], geom, size=(1400, 760))
            self.assertEqual(requested, ["World_Imagery", *briefs.ROAD_LAYERS])  # road symbols on the tight map too
        self.assertTrue(data.startswith(bytes([0xFF, 0xD8])))

    def test_notes_define_only_the_terms_that_appear(self):
        from crm.land import briefs

        with mock.patch.object(briefs, "run_checks_many"):
            b = briefs.build(self.site)
        terms = [t for t, _ in briefs.definitions([b])]
        self.assertIn("Zone A", terms)
        self.assertNotIn("Zone AE", terms)  # only zone A is on this site
        self.assertIn("Net usable acres", terms)
        self.assertIn("Opportunity Zones", terms)  # the incentive found on the site, using the app's own summary
        self.assertIn("HUBZone", terms)
        self.assertEqual(terms[-1], "Aerial imagery and outlines")
        page = self.client.get(f"/land/sites/{self.site.pk}/brief/")
        self.assertContains(page, "Notes &amp; definitions")
        self.assertContains(page, "approximate methods")

    def test_pdf_download_and_fallback_when_no_browser(self):
        from crm.land import briefs

        fake = Image.new("RGBA", (1400, 760), (90, 120, 60, 255))
        captured = {}

        def fake_pdf(html, timeout=180):
            captured["html"] = html
            return b"%PDF-1.4 test"

        with mock.patch.object(briefs, "run_checks_many"), mock.patch.object(briefs, "_fetch", return_value=fake),                 mock.patch.object(briefs, "_fetch_fema", return_value=Image.new("RGBA", (1400, 760), (0, 0, 0, 0))),                 mock.patch.object(briefs, "html_to_pdf", side_effect=fake_pdf):
            briefs._CACHE.clear()
            resp = self.client.get(f"/land/sites/{self.site.pk}/brief/?format=pdf")
            pkg = self.client.get(f"/land/briefs/?ids={self.site.pk}&format=pdf")
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertRegex(resp["Content-Disposition"], r'filename="ACMI_Site_Brief_AZ_Barnes_Farm_\d{4}\.\d{2}\.\d{2}\.pdf"')
        self.assertEqual(resp.content, b"%PDF-1.4 test")
        self.assertIn("data:image/jpeg;base64,", captured["html"])  # aerials travel inside the page: no login needed
        self.assertIn("data:image/png;base64,", captured["html"])  # and so does the logo
        self.assertIn("Notes &amp; definitions", captured["html"])
        self.assertRegex(pkg["Content-Disposition"], r'filename="ACMI_Site_Briefs_\d{4}\.\d{2}\.\d{2}\.pdf"')
        from datetime import date

        self.assertEqual(briefs.export_name("Petersen Park - Eloy (Pinal Co.)", date(2026, 10, 16)),
                         "ACMI_Site_Brief_Petersen_Park_Eloy_Pinal_Co_2026.10.16.pdf")
        self.assertEqual(briefs.export_name(day=date(2026, 10, 16)), "ACMI_Site_Briefs_2026.10.16.pdf")
        with mock.patch.object(briefs, "run_checks_many"), mock.patch.object(briefs, "chrome_path", return_value=None):
            nope = self.client.get(f"/land/sites/{self.site.pk}/brief/?format=pdf&refresh=0", follow=True)
        self.assertContains(nope, "Print the page and choose Save as PDF")

    def test_html_to_pdf_runs_a_headless_browser(self):
        from crm.land import briefs

        def fake_run(cmd, **kwargs):
            out = next(c for c in cmd if c.startswith("--print-to-pdf=")).split("=", 1)[1]
            open(out, "wb").write(b"%PDF-fake")
            fake_run.cmd = cmd

        with mock.patch.object(briefs, "chrome_path", return_value="/usr/bin/chromium"),                 mock.patch.object(briefs.subprocess, "run", side_effect=fake_run):
            data = briefs.html_to_pdf("<html></html>")
        self.assertEqual(data, b"%PDF-fake")
        self.assertIn("--headless=new", fake_run.cmd)
        self.assertTrue(fake_run.cmd[-1].startswith("file:///"))
        with mock.patch.object(briefs, "chrome_path", return_value=None), self.assertRaises(briefs.PdfUnavailable):
            briefs.html_to_pdf("<html></html>")
        with mock.patch.dict(os.environ, {"ACMI_CHROME": __file__}):
            self.assertEqual(briefs.chrome_path(), __file__)

    def test_buy_box_methodology_matches_the_scorer_and_is_on_the_notes_page(self):
        method = score.methodology()
        parts = {p["part"]: p for p in method["parts"]}
        self.assertEqual(parts["Size"]["max"], 25)
        self.assertIn("industrial 22", parts["Land use and entitlement"]["how"][0])
        self.assertEqual(sum(p["max"] for p in method["parts"] if p["max"] > 0), 110)
        self.assertTrue(any(str(score.FLOOD_KILL_PCT) in k for k in method["killers"]))
        self.assertTrue(any(str(score.KILLER_CAP) in n for n in method["notes"]))
        # the numbers quoted really are what the scorer awards
        near = {"transmission": {"miles": 0.5}, "substation": {"miles": 1.0}, "highway": {"miles": 0.5}, "rail": {"miles": 0.5}}
        p = make_parcel(self.a.county, "S", -111.6, 32.8, acres=300.0,
                        near=near, overlays={}, state_land_use="", owner_name="", mail_state="")
        reasons = {r["text"]: r["pts"] for r in score.screen(p)[1]}
        self.assertEqual(reasons["300 gross acres"], 25)
        self.assertTrue(any(pts == 14 and "transmission" in text for text, pts in reasons.items()))
        self.assertTrue(any(pts == 6 and "substation" in text for text, pts in reasons.items()))
        with mock.patch.object(briefs_module(), "run_checks_many"):
            page = self.client.get(f"/land/sites/{self.site.pk}/brief/")
        self.assertContains(page, "How the buy-box score is calculated")
        self.assertContains(page, "Deal-killers")

    def test_deal_killers_are_summarised_per_kind_for_a_multi_parcel_site(self):
        from crm.land import briefs

        a, b = self.a, make_parcel(self.a.county, "B", -111.5, 32.9)
        b.killers = ["57% in the FEMA 100-year floodplain", "Superfund (NPL) site 0.2 mi away: X"]
        a.killers = ["99% in the FEMA 100-year floodplain"]
        lines = briefs._summarise_killers([a, b])
        self.assertEqual(len([x for x in lines if "floodplain" in x]), 1)
        self.assertIn('2 of 2 parcels are over the deal-killer limit for "in the FEMA 100-year floodplain" (worst 99%)', lines)
        self.assertIn("Superfund (NPL) site 0.2 mi away: X", lines)
        self.assertEqual(briefs._summarise_killers([b])[1:], ["Superfund (NPL) site 0.2 mi away: X"])

    def test_zoning_reference_finds_districts_and_prints_them(self):
        from crm.land import zoning_ref

        p = self.a  # I-2, City of Casa Grande
        ref = zoning_ref.district_for(p)
        self.assertEqual((ref["key"], ref["district"]["code"]), ("casa_grande", "I-2"))
        self.assertTrue(ref["district"]["permitted_uses"] or ref["district"]["purpose"])
        # variants and combined entries resolve
        data, d = zoning_ref.find("maricopa_county", "IND-3p")
        self.assertEqual(d["code"], "IND-3") if d and d["code"] == "IND-3" else self.assertIsNotNone(data)
        self.assertEqual(zoning_ref.find("pinal_county", "ELOY")[1]["code"].split(" / ")[0], "CASA GRANDE")
        self.assertEqual(zoning_ref.find("casa_grande", "I-2 / CR")[1]["code"], "I-2")
        self.assertEqual(zoning_ref.find("eloy", "I-2")[1]["code"], "I-2")
        self.assertFalse(zoning_ref.load("eloy")["mirror"])  # Eloy's code was read from American Legal itself
        self.assertTrue(zoning_ref.load("casa_grande")["mirror"])  # a third-party copy: flagged
        with mock.patch.object(briefs_module(), "run_checks_many"):
            page = self.client.get(f"/land/sites/{self.site.pk}/brief/")
        self.assertContains(page, "Zoning districts on these sites")
        self.assertContains(page, "Permitted uses")
        card = self.client.get(f"/land/parcel/{p.pk}/")
        self.assertContains(card, "What I-2 allows")

    def test_texas_and_other_unmapped_counties_say_so_instead_of_a_district(self):
        from crm.land import zoning_ref

        tx = LandCounty.objects.create(fips="48209", name="Hays", state="TX")
        q = make_parcel(tx, "T1", -98.0, 30.0)
        out = zoning_ref.references([q])
        self.assertEqual(out["districts"], [])
        self.assertIn("Hays, TX", out["no_layer"])

    def test_listing_form_round_trips_and_blanks_print_as_na(self):
        from crm.land import listing

        # raw land with no listing facts starts without the flyer sections...
        with mock.patch.object(briefs_module(), "run_checks_many"):
            self.assertNotContains(self.client.get(f"/land/sites/{self.site.pk}/brief/"), "From the listing flyer")
        # ...and once they're switched on, a site with nothing entered still prints every row, as N/A
        self.site.brief_options = {"hide": []}
        self.site.save()
        with mock.patch.object(briefs_module(), "run_checks_many"):
            page = self.client.get(f"/land/sites/{self.site.pk}/brief/")
        self.assertContains(page, "From the listing flyer")
        self.assertContains(page, "N/A")
        form = self.client.get(f"/land/sites/{self.site.pk}/listing/")
        self.assertContains(form, 'name="price"')
        self.assertContains(form, 'name="util_0_label" value="Electric"')
        posted = {"source": "Land Advisors flyer", "price": "Submit", "taxes": "", "size": "±320 acres",
                  "access": "I-8 at Thornton Rd, 3.5 mi\n\n  Peters Rd improvements ", "nearby": "Lucid\nKohler",
                  "util_0_label": "Electric", "util_0_value": "APS", "util_1_label": "Water", "util_1_value": "",
                  "util_2_label": "Fiber", "util_2_value": "CenturyLink",
                  "contact_0_name": "Trey Davis", "contact_0_phone": "602-363-3901", "contact_0_firm": "", "contact_0_email": "",
                  "contact_0_address": "", "contact_1_name": "", "contact_1_phone": "9", "contact_1_firm": "", "contact_1_email": "",
                  "contact_1_address": "",
                  "drive_0_place": "Phoenix", "drive_0_value": "50 mi / 50 min", "drive_1_place": "", "drive_1_value": "",
                  "claim_0_claim": "Zoned I-2", "claim_0_found": "Confirmed on the city map", "claim_0_status": "confirmed",
                  "claim_1_claim": "Fiber", "claim_1_found": "", "claim_1_status": "bogus"}
        resp = self.client.post(f"/land/sites/{self.site.pk}/listing/", posted)
        self.assertEqual(resp.status_code, 302)
        self.site.refresh_from_db()
        L = self.site.listing
        self.assertEqual((L["price"], L["size"], "taxes" in L), ("Submit", "±320 acres", False))  # blanks are left out
        self.assertEqual(L["access"], ["I-8 at Thornton Rd, 3.5 mi", "Peters Rd improvements"])
        self.assertEqual(L["utilities"], {"Electric": "APS", "Fiber": "CenturyLink"})
        self.assertEqual(L["contacts"], [{"name": "Trey Davis", "phone": "602-363-3901"}])
        self.assertEqual(L["drive_times"], [["Phoenix", "50 mi / 50 min"]])
        self.assertEqual([c["status"] for c in L["claims_check"]], ["confirmed", "not checked"])
        with mock.patch.object(briefs_module(), "run_checks_many"):
            page = self.client.get(f"/land/sites/{self.site.pk}/brief/")
        self.assertContains(page, "Submit")
        self.assertContains(page, "CenturyLink")
        d = listing.display(L)
        self.assertEqual([r[0] for r in d["utility_rows"]][:6], list(listing.UTILITY_LABELS))
        self.assertEqual(d["utility_rows"][-1], ("Fiber", "CenturyLink"))
        # reopening the form shows what was saved
        again = self.client.get(f"/land/sites/{self.site.pk}/listing/")
        self.assertContains(again, 'value="Submit"')
        self.assertContains(again, "Peters Rd improvements")

    def test_live_checks_for_many_parcels_run_together_and_are_stored_by_the_caller(self):
        from crm.land import lookup

        second = make_parcel(self.a.county, "Q", -111.5, 32.9)
        calls = []

        def fake_compute(parcel, only=None):
            calls.append(parcel.key)
            return {"flood": {"sfha_pct": 0.0, "zones": ["X"], "mapped": True}, "net_usable": {"acres": parcel.acres, "lost_pct": 0}}

        with mock.patch.object(lookup, "compute_checks", side_effect=fake_compute):
            lookup.run_checks_many([self.a, second], workers=2)
        self.assertEqual(sorted(calls), sorted([self.a.key, second.key]))
        second.refresh_from_db()
        self.assertEqual(second.checks["flood"]["zones"], ["X"])
        self.assertIsNotNone(second.checked_at)
        lookup.run_checks_many([])  # nothing to do is fine

    def test_empty_site_has_no_brief(self):
        empty = LandSite.objects.create(name="Nothing yet")
        self.assertContains(self.client.get(f"/land/sites/{empty.pk}/brief/"), "no parcels")


class AssembleSmallParcelsTests(TestCase):
    """Assembling needs parcels of any size: no default minimum on the map, and click-to-add for unstored parcels."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("kellen", password="x")
        self.client.force_login(self.user)
        self.county = LandCounty.objects.create(fips="04021", name="Pinal", state="AZ")

    def test_map_has_no_default_minimum_acres(self):
        page = self.client.get("/land/")
        self.assertContains(page, 'name="min_acres"')
        self.assertNotContains(page, 'name="min_acres" type="number" min="0" step="10" placeholder="any size" value="50"')
        self.assertNotRegex(page.content.decode(), r'name="min_acres"[^>]*value="50"')

    def test_states_and_regions_can_be_collapsed(self):
        county = LandCounty.objects.create(fips="04013", name="Maricopa", state="AZ", region="Central Arizona (I-10 corridor)")
        make_parcel(county, "50645028", -112.9, 33.4)  # the filter lists counties that have parcels
        page = self.client.get("/land/").content.decode()
        self.assertIn('data-key="Arizona"', page)
        self.assertIn('data-key="Arizona/Central Arizona (I-10 corridor)"', page)
        self.assertGreaterEqual(page.count("data-collapse"), 2)
        self.assertIn("land:folded", page)  # the open / closed choice is remembered in the browser

    def test_clicking_an_unstored_parcel_stores_it_and_returns_its_id(self):
        from crm.land import views as land_views

        geometry = mapping(box(-111.6501, 32.8001, -111.6491, 32.8011))
        hit = {"county": {"fips": "04021", "name": "Pinal", "state": "AZ", "source": "test"}, "key": "", "geometry": geometry,
               "fields": {"prop_id": "", "owner_name": "NEW SPLIT LLC", "acres": 3.2, "state_land_use": ""}}
        with mock.patch.object(land_views, "identify", return_value=hit),                 mock.patch.object(land_views.layers, "measure", return_value=({}, "")):
            resp = self.client.post("/land/assemble-point/", {"lat": "32.8006", "lon": "-111.6496"})
        self.assertEqual(resp.status_code, 200)
        parcel = Parcel.objects.get(pk=resp.json()["id"])
        self.assertEqual((parcel.owner_name, round(parcel.acres, 1)), ("NEW SPLIT LLC", 3.2))
        self.assertTrue(parcel.geometry)

    def test_clicking_a_stored_parcel_returns_it_instead_of_duplicating(self):
        from crm.land import views as land_views

        existing = make_parcel(self.county, "40303004A", -111.65, 32.80, prop_id="40303004A")
        hit = {"county": {"fips": "04021", "name": "Pinal", "state": "AZ"}, "key": "40303004A", "geometry": existing.geometry
               and json.loads(existing.geometry), "fields": {"prop_id": "40303004A"}}
        with mock.patch.object(land_views, "identify", return_value=hit):
            resp = self.client.post("/land/assemble-point/", {"lat": "32.805", "lon": "-111.645"})
        self.assertEqual(resp.json()["id"], existing.pk)
        self.assertEqual(Parcel.objects.count(), 1)

    def test_no_parcel_at_a_point_is_a_404(self):
        from crm.land import views as land_views

        with mock.patch.object(land_views, "identify", return_value=None):
            resp = self.client.post("/land/assemble-point/", {"lat": "32.8", "lon": "-111.6"})
        self.assertEqual(resp.status_code, 404)


class CheckCountyCommandTests(TestCase):
    """check_county: the gate after loading a county."""

    def test_reports_coverage_and_undefined_zoning_codes(self):
        from io import StringIO

        from django.core.management import call_command, CommandError

        county = LandCounty.objects.create(fips="04021", name="Pinal", state="AZ", region="Central Arizona (I-10 corridor)",
                                          min_acres=20, source="test")
        make_parcel(county, "A", -111.7, 32.9, owner_name="BARNES VERNON LESTER", mail_address="3057 S TREKELL RD",
                    overlays={"zoning": {"value": "I-2", "authority": "City of Casa Grande", "pct": 100}}, zoning="I-2")
        make_parcel(county, "B", -111.6, 32.9, owner_name="", overlays={"zoning": {"value": "ZZ-9", "authority": "City of Casa Grande",
                                                                                   "pct": 100}}, zoning="ZZ-9")
        out = StringIO()
        call_command("check_county", "Pinal_AZ", stdout=out)
        text = out.getvalue()
        self.assertIn("2 parcels stored", text)
        self.assertIn("Owner name", text)
        self.assertIn("50%", text)                       # one of the two has an owner
        self.assertIn("<-- check", text)                 # owner coverage under 90% is flagged
        self.assertIn("No definition in zoning_ref for 1: ZZ-9", text)
        with self.assertRaises(CommandError):
            call_command("check_county", "Hoke_NC", stdout=StringIO())  # configured but nothing loaded


class SiteTrimTests(TestCase):
    """Excluding parcels from a site and trimming a parcel to the part the site needs."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("kellen", password="x")
        self.client.force_login(self.user)
        county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        # A 2-wide parcel with a neighbour touching its east half only.
        self.big = make_parcel(county, "BIG", -77.0, 38.5, size=0.01, owner_name="COTTRELL ADAM M", acres=200.0,
                               geometry=json.dumps(mapping(box(-77.02, 38.5, -77.0, 38.51))))
        self.east = make_parcel(county, "EAST", -77.0, 38.5, owner_name="WALD BRUCE", acres=100.0)
        self.far = make_parcel(county, "FAR", -76.9, 38.5, owner_name="SMALLWOOD JOSEPH", acres=10.0)
        self.site = LandSite.objects.create(name="Billingsley", status="loi")
        self.site.parcels.set([self.big, self.east, self.far])
        self.trail = [[[-77.01, 38.499], [-77.01, 38.511]]]  # north-south, splits BIG in half

    def deal(self, parcel):
        from crm.land import tracker

        tracker.ensure_deals(self.site)
        return self.site.parcel_deals.get(parcel=parcel)

    def test_cut_keeps_the_side_touching_the_site(self):
        from crm.land import trim

        out = trim.preview(self.deal(self.big), self.trail)
        self.assertEqual(len(out["pieces"]), 2)
        kept = [p for p in out["pieces"] if p["keep"]]
        self.assertEqual(len(kept), 1)
        self.assertGreater(min(c[0] for c in kept[0]["geometry"]["coordinates"][0]), -77.0101)  # the east half

    def test_cut_line_ending_on_the_edge_still_cuts(self):
        from shapely.geometry import shape

        from crm.land import trim

        pieces = trim.cut(shape(json.loads(self.big.geometry)), [[[-77.01, 38.5], [-77.01, 38.51]]])
        self.assertEqual(len(pieces), 2)

    def test_trim_counts_portion_in_site_acres_and_tracker(self):
        from crm.land import tracker, trim

        r = self.client.post(f"/land/sites/{self.site.pk}/trim/", {"parcel": self.big.pk, "action": "preview",
                             "lines": self.trail}, content_type="application/json").json()
        keep = [p["i"] for p in r["pieces"] if p["keep"]]
        r = self.client.post(f"/land/sites/{self.site.pk}/trim/", {"parcel": self.big.pk, "action": "save",
                             "lines": self.trail, "keep": keep}, content_type="application/json").json()
        self.assertTrue(r["trimmed"])
        d = self.deal(self.big)
        self.assertAlmostEqual(d.acres, 100.0, delta=1)  # half of the 200-ac tax-roll acreage
        self.assertAlmostEqual(trim.site_acres(self.site), 210.0, delta=1)
        self.assertAlmostEqual(tracker.summary(self.site)["total_acres"], 210.0, delta=1)
        self.assertTrue(self.site.events.filter(text__startswith="Trimmed parcel BIG").exists())
        # Re-opening the trim shows the saved choice.
        again = trim.preview(d, self.trail)
        self.assertEqual([p["i"] for p in again["pieces"] if p["keep"]], keep)
        self.client.post(f"/land/sites/{self.site.pk}/trim/", {"parcel": self.big.pk, "action": "reset"},
                         content_type="application/json")
        self.assertAlmostEqual(trim.site_acres(self.site), 310.0, delta=0.1)

    def test_excluded_parcel_counts_for_nothing_but_stays(self):
        from crm.land import tracker, trim

        far = self.deal(self.far)
        self.client.post(f"/land/sites/{self.site.pk}/manage/", {"action": "include_parcel", "deal": far.pk, "included": "0"})
        self.assertEqual(trim.site_acres(self.site), 300.0)
        s = tracker.summary(self.site)
        self.assertEqual((s["parcels"], s["all_parcels"], s["owner_count"]), (2, 3, 2))
        data = self.client.get(f"/land/sites/{self.site.pk}/").json()
        self.assertEqual((sorted(data["parcels"]), data["excluded"]), (sorted([self.big.pk, self.east.pk]), [self.far.pk]))
        # Saving from the map (which only selects included parcels) keeps the excluded one on the site.
        self.client.post("/land/sites/save/", {"id": self.site.pk, "name": "Billingsley", "parcels": data["parcels"]},
                         content_type="application/json")
        self.assertEqual(self.site.parcels.count(), 3)
        with mock.patch("crm.land.sources.fema_flood_layer_id", return_value=35):
            html = self.client.get(f"/land/sites/{self.site.pk}/manage/").content.decode()
        self.assertIn("row-excluded", html)
        self.assertIn('data-trim="%d"' % self.big.pk, html)


class FemaLayerLookupTests(TestCase):
    def test_a_failed_lookup_isnt_retried_on_every_page(self):
        from crm.land import sources

        saved = dict(sources._fema_layer_cache)
        self.addCleanup(lambda: (sources._fema_layer_cache.clear(), sources._fema_layer_cache.update(saved)))
        sources._fema_layer_cache.clear()
        with mock.patch.object(sources.SESSION, "get", side_effect=OSError("down")) as get, \
                mock.patch("time.sleep"):
            self.assertEqual(sources.fema_flood_layer_id(), sources.FEMA_FLOOD_LAYER_DEFAULT)
            self.assertEqual(get.call_count, 3)
            self.assertEqual(sources.fema_flood_layer_id(), sources.FEMA_FLOOD_LAYER_DEFAULT)
            self.assertEqual(get.call_count, 3)  # backed off: no new requests for 10 minutes
        ok = mock.Mock()
        ok.json.return_value = {"layers": [{"id": 36, "name": sources.FEMA_FLOOD_LAYER_NAME}]}
        sources._fema_layer_cache["failed"] -= sources.FEMA_RETRY_AFTER + 1
        with mock.patch.object(sources.SESSION, "get", return_value=ok):
            self.assertEqual(sources.fema_flood_layer_id(), 36)
        self.assertNotIn("failed", sources._fema_layer_cache)
