"""A deal lives in its company card, the pipeline leads with the company, and every page has the back slot."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from crm.models import Company, Opportunity
from crm.tests.utils import make_manager

User = get_user_model()


class CompanyCardTests(TestCase):
    def setUp(self):
        self.client.force_login(make_manager(User.objects.create_user("kellen")))
        self.rtx = Company.objects.create(name="RTX")
        self.opp = Opportunity.objects.create(name="MEIH - Energetics plant", company=self.rtx)
        self.other = Opportunity.objects.create(name="NSIH - Rocket motors", company=self.rtx)

    def test_pipeline_cards_lead_with_company(self):
        for view in ("board", "table"):
            page = self.client.get(f"/opportunities/?view={view}").content.decode()
            self.assertIn("RTX", page)
            # The company name comes before the deal name, and links to the deal inside the company card.
            self.assertLess(page.index(">RTX<") if ">RTX<" in page else page.index("<b>RTX</b>"),
                            page.index("MEIH - Energetics plant"))

    def test_deal_page_is_the_company_card(self):
        page = self.client.get(self.opp.get_absolute_url())
        self.assertContains(page, '<nav class="tabs co-tabs"')
        self.assertContains(page, f'href="{self.rtx.get_absolute_url()}?tab=opportunities" class="active"')
        self.assertContains(page, "NSIH - Rocket motors")  # the company's other deal
        self.assertContains(page, "<title>RTX · MEIH - Energetics plant · ACMI CRM</title>", html=False)

    def test_back_slot_on_pages(self):
        self.assertContains(self.client.get("/opportunities/"), 'id="hub-back"')
        self.assertContains(self.client.get(self.rtx.get_absolute_url()), "crm/back.js")
