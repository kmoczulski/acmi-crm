"""The contract list is grouped by type and, inside each type, sorted by project (Kellen, 2026-11-05)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.contracts.views import NO_PROJECT, _contract_sections, project_label
from crm.models import Campus, Company, Contract, LandSite, Opportunity, Project

User = get_user_model()


class ContractProjectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x")
        self.client.force_login(self.user)
        self.meih = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.nsih = Campus.objects.create(code="NSIH", name="National Security Innovation Hub")
        self.ara = Project.objects.create(name="ARA Facility", campus=self.meih)
        self.ara_site = LandSite.objects.create(name="Parcel A", project=self.ara)
        self.plain_site = LandSite.objects.create(name="Wamsley 240 ac")
        self.n = 0

    def contract(self, title, kind="psa", **kw):
        self.n += 1
        return Contract.objects.create(title=title, kind=kind, status="negotiating", created_by=self.user, **kw)

    def test_the_project_comes_from_the_site_project_then_site_then_campus_then_deal(self):
        deal_campus = Opportunity.objects.create(company=Company.objects.create(name="A"), name="d1", campus=self.nsih)
        deal_site = Opportunity.objects.create(company=Company.objects.create(name="B"), name="d2", site="Project North Star")
        cases = {
            "ARA Facility": self.contract("c1", site=self.ara_site, campus=self.meih),  # the site's project wins
            "Wamsley 240 ac": self.contract("c2", site=self.plain_site),  # a site with no project: the site itself
            "MEIH": self.contract("c3", campus=self.meih),
            "NSIH": self.contract("c4", opportunity=deal_campus),
            "Project North Star": self.contract("c5", opportunity=deal_site),
            NO_PROJECT: self.contract("c6"),
        }
        for label, c in cases.items():
            self.assertEqual(project_label(c), label, c.title)

    def test_inside_each_type_the_projects_run_a_to_z_with_no_project_last(self):
        self.contract("p-none", "psa")
        self.contract("p-meih", "psa", campus=self.meih)
        self.contract("p-ara", "psa", site=self.ara_site)
        self.contract("p-ara-2", "psa", site=self.ara_site)
        self.contract("lease-meih", "lease", campus=self.meih)
        sections = _contract_sections(list(Contract.objects.select_related("site__project", "campus", "opportunity__campus")))
        self.assertEqual([s["title"] for s in sections], ["Land", "Leases"])
        land = [(g["project"], sorted(c.title for c in g["contracts"])) for g in sections[0]["groups"]]
        self.assertEqual(land, [("ARA Facility", ["p-ara", "p-ara-2"]), ("MEIH", ["p-meih"]), (NO_PROJECT, ["p-none"])])
        self.assertEqual([g["project"] for g in sections[1]["groups"]], ["MEIH"])

    def test_the_page_shows_type_sections_with_project_headings(self):
        self.contract("p-ara", "psa", site=self.ara_site)
        self.contract("p-meih", "psa", campus=self.meih)
        html = self.client.get(reverse("crm:contract_list")).content.decode()
        self.assertLess(html.index(">Land<") if ">Land<" in html else html.index("Land "), html.index("ct-project-row"))
        self.assertIn("ARA Facility", html)
        self.assertLess(html.index("ARA Facility"), html.index("p-ara"))
        self.assertIn("Group by type, then project", html)

    def test_group_by_project_makes_a_section_per_project_with_types_inside(self):
        self.contract("lease-ara", "lease", site=self.ara_site)
        self.contract("psa-ara", "psa", site=self.ara_site)
        self.contract("pda-meih", "pda", campus=self.meih)
        r = self.client.get(reverse("crm:contract_list"), {"group": "project"})
        self.assertEqual(r.context["group"], "project")
        sections = r.context["sections"]
        self.assertEqual([s["title"] for s in sections], ["ARA Facility", "MEIH"])
        inside = [c.title for c in sections[0]["groups"][0]["contracts"]]
        self.assertEqual(inside, ["psa-ara", "lease-ara"])  # land before leases
        self.assertContains(r, "Group by project, then type")
        self.assertNotContains(r, "ct-project-row")  # the section heading already is the project

    def test_filters_and_junk_group_values_still_work(self):
        self.contract("psa-ara", "psa", site=self.ara_site)
        self.contract("lease-ara", "lease", site=self.ara_site)
        r = self.client.get(reverse("crm:contract_list"), {"kind": "lease", "group": "nonsense"})
        self.assertEqual(r.context["group"], "type")
        self.assertEqual([c.title for c in r.context["contracts"]], ["lease-ara"])
