from django.test import TestCase

from crm import choices
from crm.models import Campus, ChoiceOption, LandSite, Task
from crm.tests.utils import make_manager


class DropdownListTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        choices.clear()
        self.addCleanup(choices.clear)
        self.user = make_manager(get_user_model().objects.create_user("kellen", first_name="Kellen", password="x"))
        self.client.force_login(self.user)

    def post(self, key, data):
        return self.client.post(f"/settings/dropdowns/{key}/", data)

    def rows(self, key):
        return {v: (l, h) for v, l, h, _ in choices.options(key)}

    def test_settings_lists_every_dropdown_but_the_system_ones(self):
        page = self.client.get("/settings/dropdowns/")
        for text in ("Site stage", "Tenant deal stage", "Task label", "Critical date type", "logic"):
            self.assertContains(page, text)
        self.assertNotContains(page, "Activity")
        self.assertEqual(self.client.get("/settings/dropdowns/Activity.Kind/").status_code, 404)

    def test_rename_reorder_and_hide_apply_everywhere(self):
        order = [v for v, _ in LandSite.Status.choices]
        order = [order[1], order[0]] + order[2:]  # Under review first
        data = {"order": order, **{f"label_{v}": l for v, l in LandSite.Status.choices}}
        data["label_review"] = "In review"
        data["hidden_outreach"] = "on"
        self.post("LandSite.Status", data)
        self.assertEqual(LandSite.Status.choices[0], ("review", "In review"))
        self.assertEqual(LandSite.Status.REVIEW.label, "In review")
        site = LandSite.objects.create(name="Lake Acton", status="review")
        self.assertEqual(site.get_status_display(), "In review")
        page = self.client.get(f"/land/sites/{site.pk}/manage/")
        self.assertContains(page, ">In review<")
        self.assertNotContains(page, '<option value="outreach"')  # hidden: not offered...
        site.status = "outreach"
        site.save()
        self.assertContains(self.client.get(f"/land/sites/{site.pk}/manage/"), '<option value="outreach" selected')  # ...unless it's the current value
        self.assertFalse(self.rows("LandSite.Status").get("dropped")[1])
        # the built-in name is never stored as an override
        self.assertEqual(ChoiceOption.objects.get(list_key="LandSite.Status", value="prospect").label, "")
        self.client.post("/settings/dropdowns/LandSite.Status/", {"action": "reset"})
        self.assertEqual(LandSite.Status.REVIEW.label, "Under review")

    def test_add_and_remove_options_on_open_lists_only(self):
        self.post("Task.Label", {"new_label": "Environmental"})
        value = dict((l, v) for v, l in Task.Label.choices)["Environmental"]
        self.assertEqual(value, "environmenta")  # fits the field's 12 characters
        t = Task(title="Phase I", label=value)
        t.full_clean(exclude=["assignee"])  # an added option is a valid value
        t.save()
        self.assertEqual(t.get_label_display(), "Environmental")
        self.post("Task.Label", {f"remove_{value}": "on"})
        self.assertIn(value, self.rows("Task.Label"))  # in use: kept
        self.assertContains(self.client.get("/settings/dropdowns/Task.Label/"), "In use")
        self.post("LandSite.Status", {"new_label": "Fast track"})
        self.assertNotIn("fast_track", self.rows("LandSite.Status"))  # a logic list needs "works like"
        t.delete()
        self.post("Task.Label", {f"remove_{value}": "on"})
        self.assertNotIn(value, self.rows("Task.Label"))

    def test_added_stage_works_like_the_one_chosen(self):
        from crm.models import OPEN_STAGES, STAGE_PROBABILITY, Company, Opportunity

        self.post("LandSite.Status", {"new_label": "Option signed", "new_like": "contract"})
        self.assertIn(("option_sig", "Option signed"), list(LandSite.Status.choices))  # 10-character field
        values = [v for v, _ in LandSite.Status.choices]
        self.assertEqual(values.index("option_sig"), values.index("contract") + 1)  # sits after its look-alike
        a = LandSite.objects.create(name="A", status="option_sig")
        b = LandSite.objects.create(name="B", status="prospect")
        self.assertEqual([s.name for s in LandSite.by_status([b, a])], ["A", "B"])
        self.assertEqual(LandSite.STATUS_ORDER.index("option_sig"), LandSite.STATUS_ORDER.index("contract"))
        self.assertContains(self.client.get("/land/sites/?view=board"), 'data-status="option_sig"')
        self.assertContains(self.client.get(f"/land/sites/{a.pk}/manage/"), "Option signed")
        self.post("OpportunityStage", {"new_label": "Term sheet", "new_like": "loi_neg"})
        opp = Opportunity.objects.create(name="Plant", company=Company.objects.create(name="Firehawk"), stage="term_sheet")
        self.assertIn("term_sheet", OPEN_STAGES)
        self.assertEqual(STAGE_PROBABILITY["term_sheet"], 50)
        self.assertIn(opp, Opportunity.objects.filter(stage__in=OPEN_STAGES))
        self.assertEqual(self.client.get("/opportunities/").status_code, 200)

    def test_model_forms_offer_visible_options(self):
        from crm.forms import TaskForm

        ChoiceOption.objects.create(list_key="Task.Label", value="civil", hidden=True)
        ChoiceOption.objects.create(list_key="Task.Label", value="env", label="Environmental", added=True)
        choices.clear()
        values = [v for v, _ in TaskForm(full=True).fields["label"].choices]
        self.assertIn("env", values)
        self.assertNotIn("civil", values)
        t = Task.objects.create(title="Grading", label="civil")
        self.assertIn("civil", [v for v, _ in TaskForm(instance=t, full=True).fields["label"].choices])

    def test_only_managers_edit_lists(self):
        from django.contrib.auth import get_user_model

        other = get_user_model().objects.create_user("intern", password="x")
        self.client.force_login(other)
        self.assertNotEqual(self.client.get("/settings/dropdowns/").status_code, 200)
        Campus.objects.create(code="MEIH", name="x")  # pages still render for everyone
        self.assertEqual(self.client.get("/land/sites/").status_code, 200)
