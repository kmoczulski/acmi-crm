"""The October 2026 UX overhaul: top navigation, search, the customizable dashboard, saved views on every list,
the Companies scopes, the merged Tenant pipeline and keyed alerts sent once."""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm import notify
from crm.models import Company, LandSite, Notification, Opportunity, OpportunityStage, Person, SavedView, Task, UserPrefs
from crm.prefs import DEFAULT_DASHBOARD, clean_dashboard

User = get_user_model()


class NavTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("jordan", password="x", first_name="Jordan")
        self.client.force_login(self.user)


class ShellTests(NavTestCase):
    def test_every_page_has_top_menus_search_and_new(self):
        r = self.client.get(reverse("crm:task_list"))
        self.assertContains(r, 'class="appbar')
        self.assertContains(r, ">Tenant pipeline</strong>")
        self.assertContains(r, "data-search-open")
        self.assertContains(r, ">+ New</summary>")
        self.assertContains(r, 'id="nav-pages"')
        self.assertNotContains(r, 'class="sidebar"')

    def test_investor_report_is_a_tab_of_the_tenant_pipeline(self):
        board = self.client.get(reverse("crm:opportunity_board"))
        self.assertContains(board, "<h1>Tenant pipeline</h1>")
        self.assertContains(board, f'href="{reverse("crm:pipeline_report")}"')
        report = self.client.get(reverse("crm:pipeline_report"))
        self.assertContains(report, "<h1>Tenant pipeline</h1>")
        self.assertContains(report, 'class="on" title="The investor-ready report')
        self.assertContains(report, "ACMI Properties pipeline")  # the printed title stays outside-facing
        self.assertContains(report, "logo_blue.png")


class SearchTests(NavTestCase):
    def test_finds_companies_deals_and_people_with_deals_first(self):
        quiet = Company.objects.create(name="Vector Quiet Labs")
        live = Company.objects.create(name="Vector Energetics")
        Opportunity.objects.create(name="Vector Energetics plant", company=live, stage=OpportunityStage.QUALIFIED)
        Person.objects.create(full_name="Pat Vector", email="pat@vector.com")
        groups = self.client.get(reverse("crm:search"), {"q": "vector"}).json()["groups"]
        by_label = {g["label"]: g["items"] for g in groups}
        self.assertEqual([c["title"] for c in by_label["Companies"]], ["Vector Energetics", quiet.name])
        self.assertEqual(by_label["Companies"][0]["badge"], "In a deal")
        self.assertEqual(by_label["Tenant deals"][0]["tone"], "stage-qualified")
        self.assertEqual(by_label["People"][0]["title"], "Pat Vector")

    def test_short_query_returns_nothing(self):
        self.assertEqual(self.client.get(reverse("crm:search"), {"q": "v"}).json(), {"groups": []})


class DashboardTests(NavTestCase):
    def test_default_layout_and_search_box(self):
        r = self.client.get(reverse("crm:home"))
        self.assertContains(r, "data-hero-search")
        self.assertEqual([w["id"] for w in r.context["layout"]], [w["id"] for w in DEFAULT_DASHBOARD])

    def test_saved_layout_is_used_and_only_those_widgets_are_built(self):
        self.client.post(reverse("crm:prefs_save"), {"key": "dashboard", "value": json.dumps(
            [{"id": "week", "w": 12, "h": 480}, {"id": "my_sites", "w": 4}, {"id": "bogus", "w": 6}, {"id": "week", "w": 3}])})
        self.assertEqual(UserPrefs.objects.get(user=self.user).data["dashboard"],
                         [{"id": "week", "w": 12, "h": 480}, {"id": "my_sites", "w": 4, "h": 0}])
        LandSite.objects.create(name="Bastrop 400", owner=self.user)
        r = self.client.get(reverse("crm:home"))
        self.assertContains(r, 'data-widget="week" data-w="12" data-h="480"')
        self.assertContains(r, "Bastrop 400")
        self.assertNotIn("summary", r.context)  # the KPI and stage widgets are off, so the pipeline isn't summed

    def test_reset_goes_back_to_default(self):
        UserPrefs.objects.create(user=self.user, data={"dashboard": [{"id": "funnel", "w": 6, "h": 0}]})
        self.client.post(reverse("crm:prefs_save"), {"key": "dashboard", "value": "null"})
        self.assertEqual(len(self.client.get(reverse("crm:home")).context["layout"]), len(DEFAULT_DASHBOARD))

    def test_clean_dashboard_bounds_sizes(self):
        self.assertEqual(clean_dashboard([{"id": "kpis", "w": 5, "h": 99999}]), [{"id": "kpis", "w": 12, "h": 2000}])
        self.assertEqual(clean_dashboard("nope"), [])

    def test_unknown_pref_key_is_refused(self):
        r = self.client.post(reverse("crm:prefs_save"), {"key": "is_staff", "value": "true"})
        self.assertEqual(r.status_code, 400)


class ViewBarTests(NavTestCase):
    def test_save_a_view_as_default_on_any_list_page(self):
        url = reverse("crm:task_list")
        r = self.client.post(reverse("crm:saved_view_save"), {
            "object_type": "tasks", "name": "Team open", "query": "scope=all&page=3", "next": url, "is_default": "on"})
        self.assertRedirects(r, url + "?scope=all", fetch_redirect_response=False)
        view = SavedView.objects.get(owner=self.user, object_type="tasks")
        self.assertTrue(view.is_default)
        page = self.client.get(url)
        self.assertContains(page, 'data-default="scope=all"')
        self.assertContains(page, "★ Team open")

    def test_next_must_stay_on_site_and_page_must_be_known(self):
        r = self.client.post(reverse("crm:saved_view_save"), {
            "object_type": "tasks", "name": "x", "query": "", "next": "https://evil.example/"})
        self.assertEqual(r["Location"], reverse("crm:task_list") + "?")
        self.assertEqual(self.client.post(reverse("crm:saved_view_save"), {"object_type": "admin", "name": "x"}).status_code, 400)

    def test_column_and_density_prefs(self):
        self.client.post(reverse("crm:prefs_save"), {"key": "cols:contracts", "value": json.dumps(["Counterparty"])})
        self.client.post(reverse("crm:prefs_save"), {"key": "density:contracts", "value": json.dumps("compact")})
        r = self.client.get(reverse("crm:contract_list"))
        self.assertContains(r, 'data-hidden-cols="Counterparty"')
        self.assertContains(r, "data-compact")


class CompanyScopeTests(NavTestCase):
    def setUp(self):
        super().setUp()
        self.deal = Company.objects.create(name="In A Deal Co")
        Opportunity.objects.create(name="HQ", company=self.deal, stage=OpportunityStage.LOI_NEGOTIATION)
        self.recent = Company.objects.create(name="Recently Emailed Co")
        Company.objects.filter(pk=self.recent.pk).update(last_interaction_at=timezone.now() - timedelta(days=3))
        self.cold = Company.objects.create(name="Cold Prospect Co")

    def names(self, **params):
        return {c.name for c in self.client.get(reverse("crm:company_list"), params).context["page"].object_list}

    def test_companies_opens_on_active(self):
        self.assertRedirects(self.client.get(reverse("crm:company_list")), reverse("crm:company_list") + "?scope=active",
                             fetch_redirect_response=False)
        self.assertEqual(self.names(scope="active"), {"In A Deal Co", "Recently Emailed Co"})
        self.assertEqual(self.names(scope="deal"), {"In A Deal Co"})
        self.assertEqual(self.names(_v="all"), {"In A Deal Co", "Recently Emailed Co", "Cold Prospect Co"})

    def test_a_default_view_wins(self):
        SavedView.objects.create(owner=self.user, object_type="company", name="Mine", query="owner=me", is_default=True)
        r = self.client.get(reverse("crm:company_list"))
        self.assertEqual(r["Location"], reverse("crm:company_list") + "?owner=me")


class ColourTests(NavTestCase):
    def test_stage_pills_and_due_dates_carry_their_colour(self):
        co = Company.objects.create(name="Acme")
        Opportunity.objects.create(name="Acme HQ", company=co, stage=OpportunityStage.SIGNED)
        Task.objects.create(title="Call Acme", assignee=self.user, due_date=timezone.localdate())
        self.assertContains(self.client.get(reverse("crm:opportunity_board") + "?view=table"), "stage stage-signed")
        r = self.client.get(reverse("crm:task_list"))
        self.assertContains(r, 'class="due-today">Due today')


class AlertOnceTests(NavTestCase):
    def test_a_keyed_alert_is_stored_once_even_if_the_check_races(self):
        notify.send(self.user, Notification.Kind.DUE, "Due today: x", key="due:1")
        Notification.objects.create(user=self.user, kind=Notification.Kind.DUE, text="other", key="")  # unkeyed: fine
        # A second request whose check ran before the first one's row existed hits the unique rule and gives up quietly.
        from unittest import mock
        with mock.patch.object(Notification.objects, "filter") as f:
            f.return_value.exists.return_value = False
            self.assertIsNone(notify.send(self.user, Notification.Kind.DUE, "Due today: x", key="due:1"))
        self.assertEqual(Notification.objects.filter(user=self.user, key="due:1").count(), 1)
