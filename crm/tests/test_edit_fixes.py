"""Campus rename, the add-to-investor-report toggle on a company's deals, and the report's shared column grid."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.models import Campus, Company, Opportunity, Project
from crm.tests.utils import make_manager

User = get_user_model()

CAMPUS = {"location": "", "state": "", "acreage": "", "status": ""}


class EditFixesTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)

    def test_campus_can_be_renamed_and_its_project_follows(self):
        campus = Campus.objects.create(code="Greystar", name="Manufactured Housin")
        home = Project.objects.create(name="Greystar", campus=campus, keywords=["Greystar", "Manufactured Housin"])
        url = reverse("crm:campus_edit", args=[campus.pk])
        r = self.client.post(url, {"code": "Greystar", "name": "Manufactured Housing", **CAMPUS})
        self.assertRedirects(r, reverse("crm:project_list"))
        campus.refresh_from_db()
        self.assertEqual(campus.name, "Manufactured Housing")
        self.client.post(url, {"code": "Greyster", "name": "Manufactured Housing", **CAMPUS})
        home.refresh_from_db()
        self.assertEqual(home.name, "Greyster")
        self.assertIn("Greyster", home.keywords)
        self.assertContains(self.client.get(reverse("crm:project_list")), url)

    def test_toggle_deal_on_investor_report_from_company_page(self):
        co = Company.objects.create(name="Greystar")
        opp = Opportunity.objects.create(name="Greystar MH", company=co)
        url = reverse("crm:company_detail", args=[co.pk]) + "?tab=opportunities"
        self.assertContains(self.client.get(url), "Add to report")
        r = self.client.post(reverse("crm:opportunity_toggle_report", args=[opp.pk]), {"next": url})
        self.assertRedirects(r, url)
        opp.refresh_from_db()
        self.assertTrue(opp.investor_report)
        self.assertContains(self.client.get(url), "On report")
        self.client.post(reverse("crm:opportunity_toggle_report", args=[opp.pk]))
        opp.refresh_from_db()
        self.assertFalse(opp.investor_report)

    def test_report_tables_share_one_column_grid(self):
        co = Company.objects.create(name="Greystar")
        Opportunity.objects.create(name="Greystar MH", company=co, investor_report=True, stage="signed")
        html = self.client.get(reverse("crm:pipeline_report")).content.decode()
        self.assertEqual(html.count("<colgroup>"), 4)  # three sections and the development sites
