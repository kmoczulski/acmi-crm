"""Worklist 2026-10-28, group 5: W7 (Excel formula injection), W12 (copy page title), W13 (deal breadcrumb)."""
from datetime import date
from io import BytesIO

import openpyxl
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.models import Company, ContractTemplate, Opportunity, OpportunityStage
from crm.reports import _input, pipeline_workbook
from crm.tests.utils import make_manager

User = get_user_model()


class ExcelFormulaTextTests(TestCase):
    """W7: text a person typed that starts like a formula stays text in the Excel export."""

    def setUp(self):
        self.client.force_login(make_manager(User.objects.create_user("kellen")))
        self.company = Company.objects.create(name="=HYPERLINK(\"http://x.test\",\"Click\")")
        self.opp = Opportunity.objects.create(
            name="Deal", company=self.company, stage=OpportunityStage.LOI_SIGNED, value=1_000_000,
            sq_ft_need=10000, next_step="+1 call", lead_source="@broker", investor_report=True,
        )

    def tenant_cells(self):
        wb = openpyxl.load_workbook(BytesIO(self.client.get(reverse("crm:pipeline_report_xlsx")).content))
        return {c.value: c for row in wb["Tenants"].iter_rows() for c in row if isinstance(c.value, str)}

    def test_formula_like_text_is_a_text_cell(self):
        cells = self.tenant_cells()
        for text in ('=HYPERLINK("http://x.test","Click")', "+1 call", "@broker"):
            self.assertIn(text, cells)
            self.assertEqual(cells[text].data_type, "s", text)
            self.assertTrue(cells[text].quotePrefix, text)

    def test_site_sheet_and_tab_or_return_starts(self):
        site = {"site": "=1+1", "campus": None, "acres": 0, "tenants": 1, "sf": 0, "cost": 0, "weighted": 0,
                "psf": None}
        data = {"as_of": date(2026, 10, 28), "sections": [], "sites": [site]}
        ws = openpyxl.load_workbook(BytesIO(pipeline_workbook(data)))["Sites"]
        self.assertEqual((ws["A6"].value, ws["A6"].data_type), ("=1+1", "s"))
        self.assertTrue(ws["A6"].quotePrefix)
        wb = openpyxl.Workbook()
        for i, text in enumerate(("\tTabbed", "\r=1+1"), start=1):
            self.assertTrue(_input(wb.active, i, 1, text).quotePrefix, repr(text))

    def test_real_formulas_and_numbers_are_left_alone(self):
        wb = openpyxl.load_workbook(BytesIO(self.client.get(reverse("crm:pipeline_report_xlsx")).content))
        ws = wb["Tenants"]
        self.assertEqual(ws["I7"].value, '=IFERROR(H7/G7,"")')
        self.assertEqual(ws["I7"].data_type, "f")
        self.assertEqual(ws["G7"].value, 10000)
        self.assertFalse(ws["G7"].quotePrefix)


class TemplateCopyTitleTests(TestCase):
    """W12: copying a built-in template titles the page the way its heading reads."""

    def test_copy_page_title(self):
        self.client.force_login(make_manager(User.objects.create_user("kellen")))
        builtin = ContractTemplate.objects.create(key="lease", name="Lease abstract")
        r = self.client.get(reverse("crm:contract_template_edit", args=[builtin.pk]))
        self.assertContains(r, "<title>Copy of Lease abstract · ACMI CRM</title>")
        self.assertContains(r, "<h1>Copy of Lease abstract</h1>")
        # A new one is still "New template".
        self.assertContains(self.client.get(reverse("crm:contract_template_new")), "<title>New template · ACMI CRM</title>")


class DealBreadcrumbTests(TestCase):
    """W13: a deal named after its company doesn't repeat the name at the end of the breadcrumb."""

    def setUp(self):
        self.client.force_login(make_manager(User.objects.create_user("kellen")))
        self.company = Company.objects.create(name="Nova Optics")

    def crumbs(self, opp):
        page = self.client.get(opp.get_absolute_url()).content.decode()
        start = page.index('<nav class="crumbs')
        return page[start:page.index("</nav>", start)]

    def test_deal_named_after_company(self):
        crumbs = self.crumbs(Opportunity.objects.create(name="Nova Optics", company=self.company))
        self.assertTrue(crumbs.endswith(">Deals</a>"), crumbs)

    def test_other_deal_names_still_show(self):
        crumbs = self.crumbs(Opportunity.objects.create(name="Nova Optics · MEIH", company=self.company))
        self.assertIn("<span>Nova Optics · MEIH</span>", crumbs)
