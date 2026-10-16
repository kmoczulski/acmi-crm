"""Fixes from the October 2026 audit (docs/AUDIT_REPORT.md): one test class per finding."""
import json
import logging
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from shapely.geometry import box, mapping

from crm.land import briefs, lookup, parcels, score, tracker, trim
from crm.land import views as land_views
from crm.models import (
    Assignment, AssignmentSite, EmailTemplate, LandCounty, LandSite, Mailbox, OutreachSettings, Parcel, Person,
    SendBatch, SendItem,
)
from crm.news.drafts import sanitize_html
from crm.tests.test_land import make_parcel
from crm.tests.test_outreach import FakeSmtp

User = get_user_model()


def writes(queries):
    return [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]


class PasswordSignInSwitchTests(TestCase):
    """S1: ACMI_LOCAL_LOGIN=false switches password sign-in off everywhere, not just the form."""

    def setUp(self):
        self.staff = User.objects.create_user("kellen", password="a-long-test-password-1", is_staff=True,
                                              is_superuser=True)

    def signed_in(self, client):
        return "_auth_user_id" in client.session

    def test_password_works_while_local_login_is_on(self):
        self.client.post("/accounts/login/", {"login": "kellen", "password": "a-long-test-password-1"})
        self.assertTrue(self.signed_in(self.client))

    @override_settings(LOCAL_LOGIN_ENABLED=False)
    def test_sign_in_page_refuses_passwords_when_off(self):
        self.client.post("/accounts/login/", {"login": "kellen", "password": "a-long-test-password-1"})
        self.assertFalse(self.signed_in(self.client))

    @override_settings(LOCAL_LOGIN_ENABLED=False)
    def test_admin_login_hands_over_to_google_sign_in(self):
        r = self.client.post("/admin/login/?next=/admin/", {"username": "kellen", "password": "a-long-test-password-1"})
        self.assertFalse(self.signed_in(self.client))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/accounts/login/"))

    @override_settings(LOCAL_LOGIN_ENABLED=False)
    def test_password_reset_and_signup_pages_are_gone(self):
        self.assertEqual(self.client.get("/accounts/password/reset/").status_code, 404)
        self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)

    @override_settings(LOCAL_LOGIN_ENABLED=False)
    def test_signed_in_non_staff_on_admin_login_gets_403_not_a_loop(self):
        self.client.force_login(User.objects.create_user("simon"))
        self.assertEqual(self.client.get("/admin/login/?next=/admin/").status_code, 403)

    def test_login_redirect_keeps_the_query_string(self):
        r = self.client.get("/land/briefs/?ids=1,2")
        self.assertEqual(r.status_code, 302)
        self.assertIn("next=/land/briefs/%3Fids%3D1%2C2", r["Location"])


class SecurityHeaderAndRedirectTests(TestCase):
    """S5 / S6: CSP header, pinned CDN files, safe ?next=, tokens out of admin, error logging."""

    def setUp(self):
        self.user = User.objects.create_user("kellen")
        self.client.force_login(self.user)

    def test_csp_header_and_integrity_hashes(self):
        r = self.client.get("/land/")
        self.assertIn("object-src 'none'", r["Content-Security-Policy"])
        self.assertIn("script-src 'self' 'unsafe-inline' https://unpkg.com", r["Content-Security-Policy"])
        html = r.content.decode()
        self.assertEqual(html.count("https://unpkg.com/"), html.count('integrity="sha256-'))

    def test_next_must_stay_on_this_site(self):
        for bad in ("/\\evil.com", "//evil.com", "https://evil.com/"):
            r = self.client.post("/notifications/read-all/", {"next": bad})
            self.assertEqual(r["Location"], "/notifications/", bad)
        r = self.client.post("/notifications/read-all/", {"next": "/companies/?page=2"})
        self.assertEqual(r["Location"], "/companies/?page=2")

    def test_google_tokens_are_not_in_admin(self):
        from allauth.socialaccount.models import SocialToken

        self.assertNotIn(SocialToken, admin.site._registry)

    def test_request_errors_reach_the_console_with_debug_off(self):
        handlers = logging.getLogger("django.request").handlers
        self.assertTrue(handlers)
        self.assertFalse(any(type(f).__name__ == "RequireDebugTrue" for h in handlers for f in h.filters))


class DraftHtmlCleanerTests(TestCase):
    """S6: News-review draft HTML is rebuilt from a parse; the old regex let several payloads through."""

    def test_script_and_handlers_are_removed(self):
        for payload in ['<svg/onload=alert(1)>', '<img src=x onerror=alert(1)>', '<a href=javascript:alert(1)>x</a>',
                        '<a href="jav&#x61;script:alert(1)">x</a>', '<a href="java\tscript:alert(1)">x</a>',
                        '<script>alert(1)</script>', '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
                        '<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>',
                        '<div style="background:url(javascript:alert(1))">x</div>', '<form action="/x"><button>go']:
            out = sanitize_html(payload).lower()
            for bad in ("onerror", "onload", "javascript:", "<script", "<iframe", "<svg", "<form", "<style"):
                self.assertNotIn(bad, out, payload)

    def test_formatting_survives(self):
        html = ('<div dir="ltr">Hi <b>Ben</b>,<br><a href="https://acmi.com" target="_blank">site</a>'
                '<span style="color:#104C88">blue</span><img src="cid:logo" alt="logo"></div>')
        out = sanitize_html(html)
        for keep in ("<b>Ben</b>", 'href="https://acmi.com"', 'style="color:#104C88"', 'src="cid:logo"', "<br>"):
            self.assertIn(keep, out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_unclosed_tags_are_closed(self):
        self.assertEqual(sanitize_html("<div><b>bold"), "<div><b>bold</b></div>")


class OutreachDoubleSendTests(TestCase):
    """S3 / finding 1: a batch and each email are claimed once, so nothing goes out twice."""

    def setUp(self):
        self.user = User.objects.create(username="kellen", email="k@acmigroup.com")
        Mailbox.objects.create(user=self.user, email="k@acmigroup.com")
        settings = OutreachSettings.load()
        settings.include_attachments = False
        settings.save()
        t = EmailTemplate.objects.create(template_id="T1", name="t", campaign="Fall", subject="Hi", body_html="<p>x</p>")
        self.people = [Person.objects.create(first_name=f"P{i}", email=f"p{i}@firehawk.com") for i in range(3)]
        self.batch = SendBatch.objects.create(created_by=self.user, campaign="Fall")
        SendItem.objects.bulk_create([SendItem(batch=self.batch, person=p, template=t, email=p.email)
                                      for p in self.people])
        FakeSmtp.sent = []

    def run_it(self, **kw):
        with mock.patch("crm.outreach.sender.Smtp", FakeSmtp):
            from crm.outreach.sender import run_batch

            return run_batch(self.batch.pk, sleep=lambda s: None, **kw)

    def test_a_batch_already_sending_is_left_to_its_sender(self):
        SendBatch.objects.filter(pk=self.batch.pk).update(status=SendBatch.Status.SENDING)
        self.run_it()
        self.assertEqual(FakeSmtp.sent, [])
        self.run_it(resume=True)  # an operator taking over a crashed batch
        self.assertEqual(len(FakeSmtp.sent), 3)

    def test_an_email_another_sender_claimed_is_skipped(self):
        first = self.batch.items.first()
        SendItem.objects.filter(pk=first.pk).update(status=SendItem.Status.SENDING)
        self.run_it()
        self.assertEqual(sorted(m["To"] for m in FakeSmtp.sent), ["p1@firehawk.com", "p2@firehawk.com"])
        self.run_it()  # finished batch: nothing more
        self.assertEqual(len(FakeSmtp.sent), 2)

    def test_stop_closes_a_batch_whose_process_died(self):
        from crm.outreach.sender import stop_batch

        SendBatch.objects.filter(pk=self.batch.pk).update(status=SendBatch.Status.SENDING,
                                                          started_at=timezone.now() - timedelta(hours=2))
        self.batch.refresh_from_db()
        self.assertTrue(stop_batch(self.batch))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, SendBatch.Status.STOPPED)

    def test_stop_on_a_live_batch_only_asks_it_to_stop(self):
        from crm.outreach.sender import stop_batch

        SendBatch.objects.filter(pk=self.batch.pk).update(status=SendBatch.Status.SENDING, started_at=timezone.now())
        self.batch.refresh_from_db()
        self.assertFalse(stop_batch(self.batch))
        self.batch.refresh_from_db()
        self.assertEqual((self.batch.status, self.batch.stop_requested), (SendBatch.Status.SENDING, True))


class LandFixtures(TestCase):
    def setUp(self):
        from crm.tests.utils import make_manager

        self.user = make_manager(User.objects.create_user("kellen"))
        self.client.force_login(self.user)
        self.county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")


class LiveCheckRefreshTests(LandFixtures):
    """Finding 2: a failed refresh keeps the last good flood result (and its deal-killer)."""

    def test_failed_flood_refresh_keeps_the_floodplain(self):
        p = make_parcel(self.county, "F1", -77.0, 38.4, checks={
            "flood": {"sfha_pct": 60.0, "floodway_pct": 0, "zones": ["AE"], "mapped": True},
            "wetlands": {"pct": 2.0}, "net_usable": {"acres": 58.0, "lost_pct": 61.3}})
        score.apply(p)
        self.assertTrue(p.killers)

        def fema_down(geom, parcel_a):
            raise RuntimeError("FEMA timed out")

        with mock.patch.object(lookup, "checks_for", return_value={"flood": fema_down,
                                                                   "wetlands": lambda g, a: ({"pct": 2.0}, None)}):
            lookup.run_checks(p)
        p.refresh_from_db()
        self.assertEqual(p.checks["flood"]["sfha_pct"], 60.0)
        self.assertIn("FEMA timed out", p.checks["flood"]["refresh_error"])
        self.assertEqual(p.checks["net_usable"]["acres"], 58.0)  # not recomputed without a fresh flood outline
        self.assertTrue(any("floodplain" in k for k in p.killers))
        self.assertLessEqual(p.score, score.KILLER_CAP)

    def test_partial_check_keeps_net_usable(self):
        p = make_parcel(self.county, "F2", -77.0, 38.4, checks={"net_usable": {"acres": 80.0, "lost_pct": 46.7}})
        with mock.patch.object(lookup, "checks_for", return_value={"hubzone": lambda g, a: ({"qualified": False}, None)}):
            lookup.run_checks(p, only=["hubzone"])
        p.refresh_from_db()
        self.assertEqual(p.checks["net_usable"]["acres"], 80.0)


class BriefFloodWordingTests(LandFixtures):
    """Finding 6 and the coverage warning: Zone X isn't the 100-year floodplain; missing checks are said out loud."""

    def make_site(self, *checks_list):
        site = LandSite.objects.create(name="Test site")
        for i, checks in enumerate(checks_list):
            site.parcels.add(make_parcel(self.county, f"B{i}", -77.0 + i * 0.02, 38.4, checks=checks, acres=100.0))
        return site

    def build(self, site):
        with mock.patch.object(briefs, "run_checks_many"):
            return briefs.build(site)

    def test_hundred_year_sentence_lists_only_sfha_zones(self):
        site = self.make_site({"flood": {"sfha_pct": 52.0, "zones": ["A", "AE", "X"]}, "wetlands": {"pct": 1.0}})
        text = " ".join(f["text"] for f in self.build(site)["flags"])
        self.assertIn("100-year floodplain (zone A, AE)", text)
        self.assertNotIn("AE, X", text)

    def test_missing_flood_data_is_flagged_not_called_good(self):
        site = self.make_site({"flood": {"sfha_pct": 0.0, "zones": []}, "wetlands": {"pct": 0.0}},
                              {"flood": {"error": "FEMA down"}, "wetlands": {"pct": 0.0}})
        flags = self.build(site)["flags"]
        self.assertFalse(any("Little or no FEMA flood exposure" in f["text"] for f in flags))
        self.assertTrue(any("Live checks incomplete" in f["text"] and "1 of 2 parcels" in f["text"] for f in flags))

    def test_rerun_is_a_post(self):
        complete = {name: {} for name in lookup.checks_for("MD", "Charles")}  # nothing left for a GET to run
        site = self.make_site({**complete, "flood": {"sfha_pct": 0.0, "zones": []}})
        with mock.patch.object(briefs, "run_checks_many") as run:
            self.client.get(f"/land/sites/{site.pk}/brief/?refresh=1")
            self.assertEqual(run.call_args[0][0], [])  # a GET no longer forces the agency calls
            r = self.client.post(f"/land/sites/{site.pk}/brief/")
            self.assertEqual(r.status_code, 302)
            self.assertEqual(len(run.call_args[0][0]), 1)


class ReadsDontWriteTests(LandFixtures):
    """Finding 8: building a brief or opening a guest report writes nothing to the land tables."""

    def test_brief_and_guest_page_are_read_only(self):
        site = LandSite.objects.create(name="Lawton")
        site.parcels.add(make_parcel(self.county, "R1", -77.0, 38.4, checks={"flood": {"sfha_pct": 0.0, "zones": []}}))
        a = Assignment.objects.create(name="Elbit", share_token="t" * 32, share_enabled=True)
        e = AssignmentSite.objects.create(assignment=a, site=site, show_to_guest=True)
        with mock.patch.object(briefs, "run_checks_many"), CaptureQueriesContext(connection) as q:
            briefs.build(site)
            trim.site_acres(site)
            self.assertEqual(Client().get(f"/g/{a.share_token}/").status_code, 200)
            self.assertEqual(Client().get(f"/g/{a.share_token}/").status_code, 200)
            self.assertEqual(Client().get(f"/g/{a.share_token}/{e.pk}/").status_code, 200)
        # The only write is the guest view counter (the ranking page counts visits).
        self.assertEqual([w for w in writes(q.captured_queries) if '"crm_assignment"' not in w], [])
        a.refresh_from_db()
        self.assertEqual(a.guest_views, 2)

    def test_site_page_and_map_save_create_the_deal_rows(self):
        site = LandSite.objects.create(name="Lawton")
        site.parcels.add(make_parcel(self.county, "R1", -77.0, 38.4))
        self.assertTrue(tracker.missing_deals(site))
        self.client.get(f"/land/sites/{site.pk}/manage/")
        self.assertFalse(tracker.missing_deals(site))
        p2 = make_parcel(self.county, "R2", -76.98, 38.4)
        self.client.post("/land/sites/save/", json.dumps({"id": site.pk, "name": "Lawton", "parcels": [p2.pk]}),
                         content_type="application/json")
        self.assertEqual(list(site.parcel_deals.values_list("parcel_id", flat=True)), [p2.pk])


class MapClickTests(LandFixtures):
    """Findings 3, 4 and 9: county scoping, matching stored parcels by outline, stable keys for id-less land."""

    def test_statewide_layer_query_is_scoped_to_the_county(self):
        from crm.land import counties

        calls = []

        def fake_query(url, **kw):
            calls.append((url, kw.get("where")))
            return []

        lat, lon = 35.2, -79.0  # inside Cumberland's box, which overlaps Harnett / Hoke / Moore
        with mock.patch.object(lookup, "arcgis_query", side_effect=fake_query):
            lookup.identify(lat, lon)
        statewide = [where for url, where in calls if url == counties.NC_PARCELS]
        self.assertIn(counties.in_county(counties.COUNTIES["37051"], "1=1"), statewide)
        self.assertTrue(statewide and all("cntyname" in w for w in statewide))

    def test_click_on_a_merged_shape_finds_the_stored_parcel(self):
        tx = LandCounty.objects.create(fips="48209", name="Hays", state="TX")
        stored = make_parcel(tx, "13247", -98.0, 30.0, size=0.01)
        hit = {"county": {"fips": "48209", "name": "Hays", "state": "TX"},
               "fields": {"prop_id": "13248", "owner_name": "HAY HILL HAY LLC", "acres": 150.0},
               "geometry": mapping(box(-98.0, 30.0, -97.99, 30.01))}
        with mock.patch.object(land_views, "identify", return_value=hit):
            r = self.client.get("/land/identify/?lat=30.005&lon=-97.995")
            self.assertContains(r, f'data-parcel="{stored.pk}"')
            r = self.client.post("/land/assemble-point/", {"lat": 30.005, "lon": -97.995})
        self.assertEqual(r.json()["id"], stored.pk)
        self.assertEqual(Parcel.objects.filter(county=tx).count(), 1)

    def test_id_less_land_gets_one_stable_key(self):
        hit = {"county": {"fips": "24017", "name": "Charles", "state": "MD"},
               "fields": {"owner_name": "NO ID OWNER", "acres": 3.0},
               "geometry": mapping(box(-77.2, 38.5, -77.19, 38.51))}
        with mock.patch.object(land_views, "identify", return_value=hit):
            a = land_views._store_parcel_at(38.505, -77.195)
            b = land_views._store_parcel_at(38.501, -77.199)
        self.assertEqual(a.pk, b.pk)
        self.assertTrue(a.key.startswith("shp-"))

    def test_saving_from_a_click_keeps_file_loaded_owner_data(self):
        p = Parcel(county=self.county, key="K1", owner_name="")
        p.pk = 1  # an existing parcel
        parcels.assign_fields(p, {"owner_name": "OWNER FROM FILE"})
        parcels.assign_fields(p, {"owner_name": ""})
        self.assertEqual(p.owner_name, "OWNER FROM FILE")


class PruneTests(LandFixtures):
    """Finding 5: reload prune keeps site plans, and a short reload removes nothing."""

    def item(self, key, x):
        return key, {"owner_name": "OWNER LLC", "acres": 30.0}, [box(x, 38.4, x + 0.01, 38.41)]

    def save(self, keys, **kw):
        items = [self.item(k, -77.0 + i * 0.02) for i, k in enumerate(keys)]
        parcels._save(self.county, iter(items), {}, len(items), lambda *a: None, **kw)

    def test_site_plan_survives_and_short_reloads_remove_nothing(self):
        self.save([f"K{i}" for i in range(10)])
        Parcel.objects.filter(key="K9").update(site_plan={"zone": "IG"})
        self.save(["K0", "K1"])  # 2 of 10 came back: looks like a partial download
        self.assertEqual(Parcel.objects.filter(county=self.county).count(), 10)
        self.save(["K0", "K1"], force_prune=True)
        self.assertEqual(sorted(Parcel.objects.filter(county=self.county).values_list("key", flat=True)),
                         ["K0", "K1", "K9"])


class CheckCountyTexasTests(TestCase):
    """Finding 7: check_county works for counties loaded from files (Texas)."""

    def test_texas_county_by_name_and_state(self):
        hays = LandCounty.objects.create(fips="48209", name="Hays", state="TX")
        make_parcel(hays, "1", -98.0, 30.0)
        out = StringIO()
        call_command("check_county", "Hays_TX", stdout=out)
        self.assertIn("Hays County, TX", out.getvalue())


class ShareLinkTests(LandFixtures):
    """Finding 10: a bad expiry date changes nothing (it used to make the link permanent)."""

    def test_bad_expiry_leaves_the_link_off(self):
        a = Assignment.objects.create(name="Elbit")
        r = self.client.post(f"/land/assignments/{a.pk}/", {"action": "share_on", "share_expires": "12/31/2026"},
                             follow=True)
        a.refresh_from_db()
        self.assertFalse(a.share_enabled)
        self.assertContains(r, "a date (use YYYY-MM-DD)")
        self.client.post(f"/land/assignments/{a.pk}/", {"action": "share_on", "share_expires": "2026-12-31"})
        a.refresh_from_db()
        self.assertTrue(a.share_enabled)
        self.assertEqual(str(a.share_expires), "2026-12-31")


class PublicOwnerScoreTests(LandFixtures):
    """Public land no longer ranks as top buy-box land."""

    def test_owner_classes(self):
        cases = {"UNITED STATES OF AMERICA": "federal", "REDSTONE ARSENAL": "federal", "T V A": "federal",
                 "USA BUREAU OF RECLAMATION": "federal", "STATE OF ARIZONA": "public", "HARNETT COUNTY": "public",
                 "CITY OF ELOY": "public", "PHOENIX CITY OF": "public", "FIRST BAPTIST CHURCH": "public",
                 "UNITED STATES STEEL CORP": "", "USA FARMS LLC": "", "CHURCH JOHN W": "", "SALVATION ARMY": "",
                 "ESTATE OF JOHN SMITH": "", "SMITH RANCH LLC": ""}
        for name, want in cases.items():
            self.assertEqual(score.public_owner(name), want, name)

    def test_federal_is_capped_and_public_docked(self):
        near = {"transmission": {"miles": 0.5}, "substation": {"miles": 1.0}, "highway": {"miles": 0.5},
                "rail": {"miles": 0.5}, "military": {"miles": 5}}
        private = make_parcel(self.county, "P", -77.0, 38.4, acres=900.0, near=near, owner_name="SMITH RANCH LLC")
        federal = make_parcel(self.county, "F", -77.0, 38.4, acres=900.0, near=near, owner_name="REDSTONE ARSENAL")
        state = make_parcel(self.county, "S", -77.0, 38.4, acres=900.0, near=near, owner_name="STATE OF MARYLAND")
        self.assertGreater(private.score, score.FEDERAL_CAP)
        self.assertLessEqual(federal.score, score.FEDERAL_CAP)
        self.assertEqual(state.score, private.score - score.PUBLIC_PENALTY - 2)  # also loses the entity +2
        self.assertTrue(any(r["kind"] == "bad" and "Federal owner" in r["text"] for r in federal.score_reasons))


class ManagersRoleTests(TestCase):
    """S2: sending, deleting, merging, site deletion and client links need the Managers group."""

    def setUp(self):
        from django.contrib.auth.models import Group

        self.managers = Group.objects.get(name="Managers")  # made by migration 0032
        self.member = User.objects.create_user("simon")
        self.boss = User.objects.create_user("kellen")
        self.boss.groups.add(self.managers)

    def test_migration_puts_existing_accounts_in_managers(self):
        from importlib import import_module

        from django.apps import apps

        later = User.objects.create_user("joe")
        import_module("crm.migrations.0032_managers_group").add_managers(apps, None)
        self.assertTrue(later.groups.filter(name="Managers").exists())

    def test_member_is_refused_and_manager_is_not(self):
        from crm.models import Company

        co = Company.objects.create(name="Firehawk")
        self.client.force_login(self.member)
        r = self.client.post(f"/companies/{co.pk}/delete/", HTTP_REFERER=f"/companies/{co.pk}/")
        self.assertEqual(r["Location"], f"/companies/{co.pk}/")
        self.assertTrue(Company.objects.filter(pk=co.pk).exists())
        self.assertNotContains(self.client.get(f"/companies/{co.pk}/"), "Delete company")
        self.client.force_login(self.boss)
        self.assertContains(self.client.get(f"/companies/{co.pk}/"), "Delete company")
        self.client.post(f"/companies/{co.pk}/delete/")
        self.assertFalse(Company.objects.filter(pk=co.pk).exists())

    def test_member_cannot_start_a_send_or_share_a_client_link(self):
        self.client.force_login(self.member)
        with mock.patch("crm.outreach.views.start_in_background") as start:
            self.client.post("/outreach/start/", {"confirm": "SEND"})
        start.assert_not_called()
        a = Assignment.objects.create(name="Elbit")
        self.client.post(f"/land/assignments/{a.pk}/", {"action": "share_on"})
        a.refresh_from_db()
        self.assertFalse(a.share_enabled)
        r = self.client.post(f"/land/sites/{LandSite.objects.create(name='X').pk}/delete/",
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_superuser_counts_as_manager(self):
        from crm.permissions import is_manager

        self.assertTrue(is_manager(User.objects.create_superuser("admin", password="x")))
        self.assertFalse(is_manager(self.member))


class AttachmentAndDocumentTests(TestCase):
    """Outreach never sends without a missing brochure; uploads are served through a signed-in view."""

    def setUp(self):
        import tempfile

        self.user = User.objects.create(username="kellen", email="k@acmigroup.com")
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)

    def test_missing_attachment_fails_the_batch_before_sending(self):
        from django.core.files.base import ContentFile

        from crm.models import OutreachFile

        Mailbox.objects.create(user=self.user, email="k@acmigroup.com")
        settings = OutreachSettings.load()
        settings.include_attachments = True
        settings.save()
        f = OutreachFile(active=True)
        f.file.save("brochure.pdf", ContentFile(b"%PDF-1.4"), save=True)
        f.file.storage.delete(f.file.name)  # the server's disk lost it
        t = EmailTemplate.objects.create(template_id="T1", name="t", campaign="Fall", subject="Hi", body_html="x")
        p = Person.objects.create(first_name="Ben", email="ben@firehawk.com")
        batch = SendBatch.objects.create(created_by=self.user, campaign="Fall")
        SendItem.objects.create(batch=batch, person=p, template=t, email=p.email)
        FakeSmtp.sent = []
        with mock.patch("crm.outreach.sender.Smtp", FakeSmtp):
            from crm.outreach.sender import run_batch

            batch = run_batch(batch.pk, sleep=lambda s: None)
        self.assertEqual(FakeSmtp.sent, [])
        self.assertEqual(batch.status, SendBatch.Status.FAILED)
        self.assertIn("brochure", batch.error)

    def test_uploaded_document_needs_sign_in_and_web_pages_download(self):
        from django.core.files.base import ContentFile

        from crm.models import Company, Document

        co = Company.objects.create(name="Firehawk")
        doc = Document(company=co, title="Page")
        doc.file.save("page.html", ContentFile(b"<script>alert(1)</script>"), save=True)
        pdf = Document(company=co, title="Brochure")
        pdf.file.save("brochure.pdf", ContentFile(b"%PDF-1.4"), save=True)
        self.assertEqual(Client().get(doc.href).status_code, 302)  # to sign-in
        self.client.force_login(self.user)
        r = self.client.get(doc.href)
        r.close()  # FileResponse holds the file open (Windows won't delete an open file)
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])
        r = self.client.get(pdf.href)
        r.close()
        self.assertIn("inline", r["Content-Disposition"])


class LoaderTidyTests(LandFixtures):
    """Loads move re-keyed parcels instead of re-creating them; clean_parcels tidies what is already stored."""

    def items(self, rows):
        return iter([(k, {"owner_name": owner, "acres": acres}, [box(x, 38.4, x + size, 38.4 + size)])
                     for k, owner, acres, x, size in rows])

    def test_rekeyed_parcel_keeps_its_record(self):
        parcels._save(self.county, self.items([("NAP101", "STATE", 5.0, -77.0, 0.005)]), {}, 1, lambda *a: None)
        p = Parcel.objects.get(key="NAP101")
        Parcel.objects.filter(pk=p.pk).update(notes="Call the land office")
        parcels._save(self.county, self.items([("NAP202", "STATE", 5.0, -77.0, 0.005)]), {}, 1, lambda *a: None)
        p.refresh_from_db()
        self.assertEqual((p.key, p.notes), ("NAP202", "Call the land office"))
        self.assertEqual(Parcel.objects.filter(county=self.county).count(), 1)

    def test_clean_stored_removes_slivers_and_duplicate_outlines(self):
        a = make_parcel(self.county, "OID1", -77.0, 38.4, owner_name="SMITH")
        b = make_parcel(self.county, "OID2", -77.0, 38.4, owner_name="JONES")  # same outline
        c = make_parcel(self.county, "1234", -77.0, 38.4, owner_name="SMITH")  # same outline, real parcel number
        sliver = make_parcel(self.county, "S1", -76.9, 38.4, acres=0.0)
        _, removed = parcels.clean_stored()
        self.assertEqual(removed, 3)
        self.assertEqual(Parcel.objects.count(), 4)  # dry run
        parcels.clean_stored(commit=True)
        self.assertEqual(list(Parcel.objects.values_list("key", flat=True)), ["1234"])
        c.refresh_from_db()
        self.assertIn("[same shape as parcel ids", c.legal_desc)
        self.assertIn("JONES", c.legal_desc)
        self.assertFalse(Parcel.objects.filter(pk__in=[a.pk, b.pk, sliver.pk]).exists())

    def test_rescore_walks_parcels_by_id(self):
        # Scores change during the run. On the real (file) database, walking the score index (the default order)
        # visited ~1,500 parcels twice; an in-memory test database sorts up front and hides that, so check the order.
        geom = json.dumps(mapping(box(-77.0, 38.4, -76.99, 38.41)))
        Parcel.objects.bulk_create([
            Parcel(county=self.county, key=f"K{i}", owner_name="UNITED STATES OF AMERICA", acres=100.0, score=90,
                   lat=38.405, lon=-76.995, min_lon=-77.0, min_lat=38.4, max_lon=-76.99, max_lat=38.41, geometry=geom)
            for i in range(1500)])
        with CaptureQueriesContext(connection) as q:
            self.assertEqual(parcels.rescore(log=lambda *a: None), 1500)
        select = next(x["sql"] for x in q.captured_queries if x["sql"].startswith("SELECT") and "crm_parcel" in x["sql"])
        self.assertIn('ORDER BY "crm_parcel"."id" ASC', select)
        self.assertFalse(Parcel.objects.filter(score__gt=score.FEDERAL_CAP).exists())

    def test_clean_stored_keeps_the_record_with_team_work(self):
        make_parcel(self.county, "1234", -77.0, 38.4)
        make_parcel(self.county, "OID9", -77.0, 38.4, notes="Owner called back")
        _, removed = parcels.clean_stored(commit=True)
        self.assertEqual(removed, 1)
        self.assertEqual(list(Parcel.objects.values_list("key", flat=True)), ["OID9"])
        make_parcel(self.county, "OID8", -77.0, 38.4, status="watch")  # two with team work: both stay
        self.assertEqual(parcels.clean_stored(commit=True)[1], 0)
