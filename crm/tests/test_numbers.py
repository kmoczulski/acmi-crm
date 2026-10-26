"""Amounts with thousands separators (Kellen, 2026-10-26: "the lack of commas for amounts... 10,000 shows 10000").

Number fields on every form take "25,000,000" as typed and render as text fields marked data-commas, which nav.js
shows with separators (a type=number field can't). The QD planner and the site plan use the same marker and
acmiNum.parse in their own scripts.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.build.views import LineForm
from crm.contracts.forms import DateForm
from crm.forms import OpportunityForm, plain_number
from crm.models import CapitalSource, Company, Contract, Opportunity, Project

User = get_user_model()


class CommaNumberTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", email="kmoczulski@acmigroup.com")
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="Firehawk Aerospace")

    def test_plain_number(self):
        self.assertEqual(plain_number("25,000,000"), "25000000")
        self.assertEqual(plain_number("$1,250,000.50 "), "1250000.50")
        self.assertEqual(plain_number(""), "")
        self.assertEqual(plain_number(None), None)

    def test_a_styled_form_takes_separators_and_renders_text_fields(self):
        form = OpportunityForm({"name": "ARA · MEIH", "company": self.company.pk, "stage": "prospect", "probability": "80",
                                "priority": "High", "value": "25,000,000", "sq_ft_need": "50,000", "acreage_need": "1,200.5"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["value"], Decimal("25000000"))
        self.assertEqual(form.cleaned_data["sq_ft_need"], 50000)
        self.assertEqual(form.cleaned_data["acreage_need"], Decimal("1200.5"))
        html = str(OpportunityForm()["value"])
        self.assertIn('data-commas="1"', html)
        self.assertIn('inputmode="decimal"', html)
        self.assertNotIn('type="number"', html)

    def test_plain_model_forms_with_the_mixin(self):
        contract = Contract.objects.create(title="Wamsley PSA", kind="psa", status="negotiating", created_by=self.user)
        form = DateForm({"kind": "deposit", "label": "Deposit", "date": "2026-12-01", "amount": "1,500,000"})
        form.instance.contract = contract
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["amount"], Decimal("1500000"))
        form = LineForm({"group": "hard", "code": "03", "name": "Concrete", "original": "$2,000,000.50", "committed": "1,000,000"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.cleaned_data["original"], form.cleaned_data["committed"]),
                         (Decimal("2000000.50"), Decimal("1000000")))

    def test_the_capital_quick_add_row_takes_a_formatted_amount(self):
        project = Project.objects.create(name="The Forge")
        r = self.client.post(reverse("crm:capital_quick_add", args=[project.pk]),
                             {"provider_name": "Shore United Bank", "kind": "construction", "status": "identified",
                              "amount_sought": "2,000,000"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CapitalSource.objects.get().amount_sought, Decimal("2000000"))

    def test_the_opportunity_form_page_marks_its_number_fields(self):
        opp = Opportunity.objects.create(name="ARA · MEIH", company=self.company, stage="prospect", value=25000000)
        r = self.client.get(opp.get_absolute_url())
        self.assertContains(r, 'name="value"')
        self.assertContains(r, 'data-commas="1"')
