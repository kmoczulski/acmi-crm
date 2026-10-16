"""Client land-search assignments: report editor, ranking rule, and the private guest share link."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from crm.land import assignments as A
from crm.models import Assignment, AssignmentSite, LandCounty, LandSite
from crm.tests.test_land import make_parcel


def post_for(ratings, **extra):
    """Editor POST data: category -> rating, plus any extra fields."""
    data = {f"cat_{k}_rating": ratings.get(k, "") for k, _ in A.CATEGORIES}
    data.update(extra)
    return data


class AssignmentTests(TestCase):
    def setUp(self):
        from crm.tests.utils import make_manager

        self.user = make_manager(get_user_model().objects.create_user("kellen", password="x"))
        self.client.force_login(self.user)
        self.county = LandCounty.objects.create(fips="40031", name="Comanche", state="OK")
        self.a = Assignment.objects.create(name="Elbit land search", client="Elbit", acreage_floor=500)
        self.sites = {}
        for i, name in enumerate(["Lawton", "Benton", "Battle Creek"]):
            site = LandSite.objects.create(name=name, notes="INTERNAL ONLY note")
            p = make_parcel(self.county, f"P{i}", -98.5 + i * 0.05, 34.5, acres=700.0 + i * 10, owner_name="SECRET OWNER LLC")
            site.parcels.add(p)
            self.sites[name] = AssignmentSite.objects.create(assignment=self.a, site=site)

    def save_report(self, name, ratings, **extra):
        e = self.sites[name]
        r = self.client.post(f"/land/assignments/{self.a.pk}/sites/{e.pk}/edit/", post_for(ratings, **extra))
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        return e

    def test_ranking_follows_the_rule(self):
        self.save_report("Lawton", {"land": "moderate", "zoning": "low", "electric": "moderate"})
        self.save_report("Benton", {"land": "low", "zoning": "low", "electric": "elevated"})
        self.save_report("Battle Creek", {"land": "moderate", "zoning": "low"})
        order = [c["site"].name for _, c, _ in A.rank(self.a)]
        self.assertEqual(order, ["Battle Creek", "Lawton", "Benton"])  # fewest moderates first, elevated last

    def test_deadline_critical_moderate_ranks_below_cost_moderate(self):
        self.save_report("Lawton", {"land": "moderate"}, cat_land_critical="on")
        self.save_report("Benton", {"land": "moderate"})
        self.save_report("Battle Creek", {"zoning": "low"})
        order = [c["site"].name for _, c, _ in A.rank(self.a)]
        self.assertEqual(order, ["Battle Creek", "Benton", "Lawton"])

    def test_rom_nets_out_incentive_eligible_and_breaks_ties(self):
        extra = {"rom_0_category": "Grading", "rom_0_amount": "$2,000,000", "rom_0_incentive": "No",
                 "rom_1_category": "Electric", "rom_1_amount": "1,500,000", "rom_1_incentive": "Yes"}
        self.save_report("Lawton", {"land": "moderate"}, **extra)
        e = self.save_report("Benton", {"land": "moderate"}, rom_0_category="Grading", rom_0_amount="900000", rom_0_incentive="No")
        c = A.build(self.sites["Lawton"], self.a)
        self.assertEqual((c["rom_total"], c["rom_net"]), (3500000, 2000000))
        order = [c["site"].name for _, c, _ in A.rank(self.a)]
        self.assertEqual(order[:2], ["Benton", "Lawton"])  # 900k net beats 2.0M net
        self.assertIsNotNone(e)

    def test_pinned_position_overrides_rule(self):
        self.save_report("Lawton", {"land": "low"})
        self.save_report("Benton", {"land": "moderate"})
        self.client.post(self.a.get_absolute_url(), {"action": "entry", "entry": self.sites["Benton"].pk, "rank_override": "1", "show_to_guest": "on"})
        ranked = A.rank(self.a)
        self.assertEqual(ranked[0][1]["site"].name, "Benton")
        self.assertTrue(ranked[0][2])

    def test_headroom_uses_net_acres_and_program_floor(self):
        self.save_report("Lawton", {}, net_wetlands="50", net_qd="100")
        c = A.build(self.sites["Lawton"], self.a)
        self.assertEqual(c["gross"], 700.0)
        self.assertEqual((c["net_acres"], c["headroom"]), (550.0, 50.0))

    def test_report_prefills_from_parcels(self):
        c = A.build(self.sites["Lawton"], self.a)
        overview = dict(c["overview"])
        self.assertEqual(overview["Location"], "Comanche, OK")
        self.assertIn("P0", overview["Parcel identifiers"])
        self.assertIn("700.0 acres", overview["Total area"])

    def test_checklist_counts(self):
        self.save_report("Lawton", {}, **{"ck_A-1_status": "Complete", "ck_A-2_status": "N/A"})
        c = A.build(self.sites["Lawton"], self.a)
        self.assertEqual((c["checklist_done"], c["checklist_active"]), (1, len(A.CHECKLIST) - 1))

    def test_internal_pages_render(self):
        self.save_report("Lawton", {"land": "low"}, summary="A strong site.")
        self.assertEqual(self.client.get("/land/assignments/").status_code, 200)
        self.assertEqual(self.client.get(self.a.get_absolute_url()).status_code, 200)
        e = self.sites["Lawton"]
        for tail in ("", "edit/"):
            self.assertEqual(self.client.get(f"/land/assignments/{self.a.pk}/sites/{e.pk}/{tail}").status_code, 200)

    def test_guest_link_lifecycle_and_redaction(self):
        self.save_report("Lawton", {"land": "low"}, summary="A strong site.")
        guest = Client()
        self.assertEqual(guest.get("/g/not-a-real-token-xxxxxxxxxxxx/").status_code, 404)

        self.client.post(self.a.get_absolute_url(), {"action": "share_on"})
        self.a.refresh_from_db()
        token = self.a.share_token
        self.assertGreaterEqual(len(token), 30)

        r = guest.get(f"/g/{token}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("noindex", r["X-Robots-Tag"])
        e = self.sites["Lawton"]
        r = guest.get(f"/g/{token}/{e.pk}/")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("A strong site.", body)
        for secret in ("SECRET OWNER", "INTERNAL ONLY", "Score"):
            self.assertNotIn(secret, body)
        self.assertNotIn("SECRET OWNER", guest.get(f"/g/{token}/").content.decode())
        self.a.refresh_from_db()
        self.assertEqual(self.a.guest_views, 2)

        # hidden site is not reachable
        self.client.post(self.a.get_absolute_url(), {"action": "entry", "entry": e.pk})  # show_to_guest unchecked
        self.assertEqual(guest.get(f"/g/{token}/{e.pk}/").status_code, 404)
        self.assertNotIn("Lawton", guest.get(f"/g/{token}/").content.decode())

        # switching off kills the link; a regenerated link replaces the old one
        self.client.post(self.a.get_absolute_url(), {"action": "share_off"})
        self.assertEqual(guest.get(f"/g/{token}/").status_code, 404)
        self.client.post(self.a.get_absolute_url(), {"action": "share_regen"})
        self.a.refresh_from_db()
        self.assertNotEqual(self.a.share_token, token)
        self.assertEqual(guest.get(f"/g/{token}/").status_code, 404)
        self.assertEqual(guest.get(f"/g/{self.a.share_token}/").status_code, 200)

    def test_expired_link_is_dead(self):
        self.client.post(self.a.get_absolute_url(), {"action": "share_on", "share_expires": "2020-01-01"})
        self.a.refresh_from_db()
        self.assertEqual(Client().get(f"/g/{self.a.share_token}/").status_code, 404)

    def test_other_assignments_entries_not_reachable_through_a_link(self):
        other = Assignment.objects.create(name="Other client", share_token=A.new_token(), share_enabled=True)
        e = self.sites["Lawton"]  # belongs to self.a, not `other`
        self.assertEqual(Client().get(f"/g/{other.share_token}/{e.pk}/").status_code, 404)
