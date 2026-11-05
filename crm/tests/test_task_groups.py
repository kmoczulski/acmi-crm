"""The Tasks page groups tasks by what they are about: deals, land sites, prospecting, general."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.models import Campus, Company, LandSite, Opportunity, Person, Task

User = get_user_model()


class TaskGroupTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("jordan", password="x")
        self.client.force_login(self.u)

    def page(self, **params):
        return self.client.get(reverse("crm:task_list"), params).content.decode()

    def test_sections_in_order(self):
        co = Company.objects.create(name="Acme")
        cold = Company.objects.create(name="Cold Call Co")  # no deal: stays under Prospecting
        opp = Opportunity.objects.create(company=co, name="Acme deal", site="Project North Star")
        site = LandSite.objects.create(name="Parcel A")
        for title, kw in [("general one", {}), ("prospect one", {"company": cold}), ("site one", {"site": site}),
                          ("deal one", {"opportunity": opp, "company": co})]:
            Task.objects.create(title=title, assignee=self.u, **kw)
        html = self.page()
        order = [html.index(x) for x in ("Deals · Project North Star", "deal one", "Land sites · Parcel A", "site one",
                                         "Prospecting", "prospect one", ">General<", "general one")]
        self.assertEqual(order, sorted(order))

    def test_a_company_task_follows_the_companys_deal_not_prospecting(self):
        """Kellen, 2026-11-05: when a company has a project (ARA Facility), its task is not under Prospecting."""
        campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        ara = Company.objects.create(name="Applied Research Associates")
        Opportunity.objects.create(company=ara, name="ARA Facility", campus=campus, stage="signed")
        person = Person.objects.create(first_name="Ann", last_name="Lee", company=ara)
        Task.objects.create(title="Send ARA the PDA", assignee=self.u, company=ara)
        Task.objects.create(title="Call Ann about design", assignee=self.u, person=person)
        html = self.page()
        self.assertIn("Deals · MEIH", html)
        self.assertNotIn("Prospecting", html)
        self.assertLess(html.index("Deals · MEIH"), html.index("Send ARA the PDA"))
        self.assertLess(html.index("Deals · MEIH"), html.index("Call Ann about design"))

    def test_a_lost_deal_does_not_pull_tasks_out_of_prospecting(self):
        co = Company.objects.create(name="Gone Co")
        Opportunity.objects.create(company=co, name="Old deal", stage="lost")
        Task.objects.create(title="Try again", assignee=self.u, company=co)
        html = self.page()
        self.assertIn("Prospecting", html)
        self.assertNotIn("Deals ·", html)

    def test_a_task_on_the_deal_itself_still_groups_by_that_deal(self):
        co = Company.objects.create(name="Acme")
        Opportunity.objects.create(company=co, name="Newer deal", site="Elsewhere")
        older = Opportunity.objects.create(company=co, name="Pinned deal", site="Project North Star")
        Opportunity.objects.filter(pk=older.pk).update(name="Pinned deal")
        Task.objects.create(title="On the pinned deal", assignee=self.u, company=co, opportunity=older)
        self.assertIn("Deals · Project North Star", self.page())


class TaskQuadrantAndBulkTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("jordan", password="x")
        self.client.force_login(self.u)
        co = Company.objects.create(name="Acme")
        cold = Company.objects.create(name="Cold Call Co")
        opp = Opportunity.objects.create(company=co, name="Acme deal", site="Project North Star")
        site = LandSite.objects.create(name="Parcel A")
        self.t_general = Task.objects.create(title="general one", assignee=self.u)
        self.t_prospect = Task.objects.create(title="prospect one", assignee=self.u, company=cold)
        self.t_site = Task.objects.create(title="site one", assignee=self.u, site=site)
        self.t_deal = Task.objects.create(title="deal one", assignee=self.u, company=co, opportunity=opp)

    def test_quadrants_put_each_task_type_in_its_own_box(self):
        r = self.client.get(reverse("crm:task_list"), {"view": "quadrants"})
        boxes = {q["title"]: [t.title for t in q["tasks"]] for q in r.context["quads"]}
        self.assertEqual(boxes, {"Deals": ["deal one"], "Land sites": ["site one"], "Prospecting": ["prospect one"],
                                 "General": ["general one"]})
        self.assertContains(r, "quad-grid")
        self.assertContains(r, 'form="bulk-tasks"')  # every row can be selected
        self.assertEqual(self.client.get(reverse("crm:task_list")).context["view"], "list")

    def test_bulk_mark_done_ticks_all_selected_and_logs_them(self):
        from crm.models import Activity
        ids = [self.t_general.pk, self.t_prospect.pk, self.t_deal.pk]
        r = self.client.post(reverse("crm:task_bulk"), {"ids": ids, "action": "done", "next": "/tasks/?view=quadrants"})
        self.assertRedirects(r, "/tasks/?view=quadrants", fetch_redirect_response=False)
        done = set(Task.objects.filter(done=True).values_list("pk", flat=True))
        self.assertEqual(done, set(ids))
        self.assertFalse(Task.objects.get(pk=self.t_site.pk).done)  # not selected: untouched
        self.assertEqual(Activity.objects.filter(kind=Activity.Kind.TASK, summary__startswith="Completed task").count(), 3)
        self.assertTrue(all(Task.objects.get(pk=i).done_at for i in ids))
        self.assertContains(self.client.get("/tasks/"), "Marked 3 tasks done.")

    def test_bulk_done_makes_the_next_one_for_a_repeating_task(self):
        from datetime import date
        weekly = Task.objects.create(title="weekly", assignee=self.u, repeat="weekly", due_date=date(2026, 11, 2))
        self.client.post(reverse("crm:task_bulk"), {"ids": [weekly.pk], "action": "done"})
        self.assertEqual(Task.objects.filter(title="weekly", done=False).count(), 1)  # the next occurrence

    def test_bulk_reopen_and_ignores_junk(self):
        self.client.post(reverse("crm:task_bulk"), {"ids": [self.t_general.pk], "action": "done"})
        self.client.post(reverse("crm:task_bulk"), {"ids": [self.t_general.pk, "x", "9999"], "action": "reopen"})
        self.assertFalse(Task.objects.get(pk=self.t_general.pk).done)
        self.assertEqual(self.client.post(reverse("crm:task_bulk"), {}).status_code, 302)  # nothing selected: no error
        self.assertEqual(self.client.get(reverse("crm:task_bulk")).status_code, 405)
