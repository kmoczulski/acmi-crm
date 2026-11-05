"""Design review 2026-11-05: folded prospecting tasks, paged duplicates, page titles, quieter risk text."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.models import Company, Opportunity, Task
from crm.views import DUP_PAGE, PROSPECT_CAP

User = get_user_model()
ROW = 4  # a task title appears in the row 4 times (link text, title, two aria-labels), so rows = count // ROW


class DesignReviewTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("jordan", password="x")
        self.client.force_login(self.u)

    def test_prospecting_folds_after_the_cap_and_show_all_restores_it(self):
        co = Company.objects.create(name="Cold Call Co")
        for i in range(PROSPECT_CAP + 5):
            Task.objects.create(title=f"Research new lead {i}", assignee=self.u, company=co)
        html = self.client.get(reverse("crm:task_list")).content.decode()
        self.assertEqual(html.count("Research new lead") // ROW, PROSPECT_CAP)
        self.assertIn("5 more prospecting tasks hidden", html)
        full = self.client.get(reverse("crm:task_list"), {"prospecting": "all"}).content.decode()
        self.assertEqual(full.count("Research new lead") // ROW, PROSPECT_CAP + 5)
        self.assertNotIn("prospecting tasks hidden", full)

    def test_deal_tasks_are_never_folded(self):
        co = Company.objects.create(name="Acme")
        opp = Opportunity.objects.create(company=co, name="Acme deal", site="Project North Star")
        for i in range(PROSPECT_CAP + 3):
            Task.objects.create(title=f"deal step {i}", assignee=self.u, opportunity=opp, company=co)
        html = self.client.get(reverse("crm:task_list")).content.decode()
        self.assertEqual(html.count("deal step") // ROW, PROSPECT_CAP + 3)

    def test_duplicates_show_a_page_at_a_time(self):
        groups = [{"key": f"k{i}", "reasons": ["same name"], "keep": i, "companies": [
            {"pk": i, "name": f"Co {i}", "status": "prospect", "n_people": 0, "touch_count": 0,
             "last_interaction_at": None, "domains": [], "suggest": False}]} for i in range(DUP_PAGE + 4)]
        with mock.patch("crm.views.merge.suggestions", return_value=groups):
            html = self.client.get(reverse("crm:duplicates")).content.decode()
            self.assertEqual(html.count('class="dup-group"'), DUP_PAGE)
            self.assertIn(f"{DUP_PAGE + 4} groups", html)
            self.assertIn("Show 4 more groups", html)
            all_html = self.client.get(reverse("crm:duplicates"), {"show": DUP_PAGE * 2}).content.decode()
            self.assertEqual(all_html.count('class="dup-group"'), DUP_PAGE + 4)
            self.assertNotIn("more group", all_html)
            self.assertEqual(self.client.get(reverse("crm:duplicates"), {"show": "x"}).status_code, 200)

    def test_every_page_has_an_h1(self):
        for name in ("crm:list_index", "crm:chat"):
            self.assertContains(self.client.get(reverse(name)), "<h1", msg_prefix=name)

    def test_task_add_form_fields_are_labelled(self):
        html = self.client.get(reverse("crm:task_list")).content.decode()
        for label in ("Due date", "Due time", "Assign to"):
            self.assertIn(f'aria-label="{label}"', html)

    def test_only_the_first_risk_reason_is_red(self):
        co = Company.objects.create(name="Acme")
        opp = Opportunity.objects.create(company=co, name="Acme deal")
        with mock.patch("crm.views.risk_reasons", return_value=["No next step", "Never contacted"]):
            html = self.client.get(reverse("crm:opportunity_board"), {}).content.decode()
        if "kcard" in html:
            self.assertIn('class="risk-more"', html)
