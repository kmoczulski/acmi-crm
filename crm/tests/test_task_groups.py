"""The Tasks page groups tasks by what they are about: deals, land sites, prospecting, general."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.models import Company, LandSite, Opportunity, Task

User = get_user_model()


class TaskGroupTests(TestCase):
    def test_sections_in_order(self):
        u = User.objects.create_user("jordan", password="x")
        self.client.force_login(u)
        co = Company.objects.create(name="Acme")
        opp = Opportunity.objects.create(company=co, name="Acme deal", site="Project North Star")
        site = LandSite.objects.create(name="Parcel A")
        for title, kw in [("general one", {}), ("prospect one", {"company": co}), ("site one", {"site": site}),
                          ("deal one", {"opportunity": opp, "company": co})]:
            Task.objects.create(title=title, assignee=u, **kw)
        html = self.client.get(reverse("crm:task_list")).content.decode()
        order = [html.index(x) for x in ("Deals · Project North Star", "deal one", "Land sites · Parcel A", "site one",
                                         "Prospecting", "prospect one", ">General<", "general one")]
        self.assertEqual(order, sorted(order))
