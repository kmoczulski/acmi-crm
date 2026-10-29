"""The company tenant overview PDF: facts pulled from the records, Open where nothing is entered, the Chrome fallback."""
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from crm import company_pdf
from crm.land import briefs
from crm.models import Company, Contract, Document, LeaseTerms, Opportunity, Person, TenantNeeds


class CompanyPdfTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("kellen", password="pw")
        self.client.force_login(self.user)
        self.co = Company.objects.create(name="American Rheinmetall", sector="defense", hq_location="Vienna, VA",
                                         description="U.S. arm of Rheinmetall AG.\n\n## Ownership\n- **Parent:** Rheinmetall AG")
        self.deal = Opportunity.objects.create(name="American Rheinmetall · NSIH", company=self.co, stage="qualified",
                                               acreage_need=200, power_need_mw=12, next_step="Call Terry")
        self.url = reverse("crm:company_overview_pdf", args=[self.co.pk])

    def facts(self):
        return {item: (value, source) for item, value, source in company_pdf.build(self.co)["tenant"]}

    def test_nothing_entered_is_open_and_listed_as_still_needed(self):
        built = company_pdf.build(self.co)
        self.assertEqual(self.facts()["Signing entity"], (None, ""))
        self.assertIn("Signing entity", built["missing"])
        self.assertIn("Financials on file", built["missing"])
        self.assertIn("American Rheinmetall · NSIH: size, delivery date", built["missing"])

    def test_a_draft_agreement_is_not_the_signing_entity(self):
        # Starting an LOI from a deal fills in the company's CRM name; that isn't a confirmed legal entity.
        Contract.objects.create(title="LOI", kind="loi", status="drafting", counterparty="American Rheinmetall",
                                opportunity=self.deal)
        lease = Contract.objects.create(title="Lease", kind="lease", status="negotiating", company=self.co,
                                        counterparty="American Rheinmetall Munition Inc.")
        LeaseTerms.objects.create(contract=lease, guarantor="Rheinmetall AG")
        facts = self.facts()
        self.assertEqual(facts["Signing entity"],
                         (None, "Lease, negotiating: American Rheinmetall Munition Inc. (not signed)"))
        self.assertEqual(facts["Guarantor"], (None, ""))
        self.assertIn("Signing entity", company_pdf.build(self.co)["missing"])

    def test_signed_and_on_hold_deals_are_listed_lost_ones_are_not(self):
        Opportunity.objects.create(name="Signed deal", company=self.co, stage="signed", sq_ft_need=250000)
        Opportunity.objects.create(name="Paused deal", company=self.co, stage="on_hold")
        Opportunity.objects.create(name="Dead deal", company=self.co, stage="lost")
        names = {d["deal"].name for d in company_pdf.build(self.co)["deals"]}
        self.assertEqual(names, {"American Rheinmetall · NSIH", "Signed deal", "Paused deal"})

    def test_signing_entity_comes_from_the_strongest_agreement(self):
        Contract.objects.create(title="LOI", kind="loi", status="signed", counterparty="American Rheinmetall Defense, Inc.",
                                opportunity=self.deal, effective_date=date(2026, 5, 1))
        self.assertEqual(self.facts()["Signing entity"],
                         ("American Rheinmetall Defense, Inc.", "Letter of intent, signed May 1, 2026"))
        # An unsigned lease doesn't beat a signed LOI; a signed one does, and its guarantor fills that row.
        lease = Contract.objects.create(title="Lease", kind="lease", status="negotiating", company=self.co,
                                        counterparty="American Rheinmetall Munition Inc.")
        self.assertEqual(self.facts()["Signing entity"][0], "American Rheinmetall Defense, Inc.")
        lease.status, lease.effective_date = "signed", date(2026, 9, 1)
        lease.save()
        LeaseTerms.objects.create(contract=lease, guarantor="Rheinmetall AG", base_rent=1200000)
        facts = self.facts()
        self.assertEqual(facts["Signing entity"], ("American Rheinmetall Munition Inc.", "Lease, signed Sep 1, 2026"))
        self.assertEqual(facts["Guarantor"][0], "Rheinmetall AG")
        # Terminated agreements and other companies' agreements are ignored.
        lease.status = "terminated"
        lease.save()
        Contract.objects.create(title="Other", kind="lease", status="signed", counterparty="Someone Else LLC",
                                company=Company.objects.create(name="Someone Else"))
        self.assertEqual(self.facts()["Signing entity"][0], "American Rheinmetall Defense, Inc.")

    def test_nda_and_financials_from_contracts_and_documents(self):
        Document.objects.create(company=self.co, title="FY2025 audited financials", category="Financials")
        Document.objects.create(opportunity=self.deal, title="Mutual NDA", category="NDA")
        facts = self.facts()
        self.assertEqual(facts["Financials on file"], ("FY2025 audited financials", "1 document"))
        self.assertEqual(facts["NDA"], ("On file", "Mutual NDA"))
        Contract.objects.create(title="NDA", kind="nda", status="signed", company=self.co, effective_date=date(2026, 2, 3))
        self.assertEqual(self.facts()["NDA"], ("Signed", "NDA, signed Feb 3, 2026"))

    def test_pdf_download_has_logo_overview_deals_and_lease(self):
        TenantNeeds.objects.create(opportunity=self.deal, qd_cases=[{"name": "1.1"}], needs_rail=True)
        lease = Contract.objects.create(title="NSIH lease", kind="lease", status="signed", company=self.co,
                                        counterparty="American Rheinmetall Munition Inc.")
        LeaseTerms.objects.create(contract=lease, base_rent=1200000, escalation_pct=3)
        Person.objects.create(full_name="Terry Russell", title="VP of Business Development", company=self.co)
        with mock.patch.object(briefs, "html_to_pdf", return_value=b"%PDF-co") as pdf:
            resp = self.client.get(self.url)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn('filename="ACMI_Tenant_Overview_American_Rheinmetall_', resp["Content-Disposition"])
        html = pdf.call_args.args[0]
        self.assertIn("data:image/png;base64,", html)  # the ACMI logo, inline
        self.assertIn("<h4>Ownership</h4>", html)  # the description's light markdown
        self.assertIn("1 explosives (QD) case, rail", html)
        self.assertIn("$1,200,000", html)
        self.assertIn("Terry Russell", html)
        self.assertIn("Call Terry", html)
        self.assertNotIn("window.print()", html)  # no on-screen toolbar in the PDF

    def test_without_chrome_the_page_comes_back_to_print(self):
        with mock.patch.object(briefs, "html_to_pdf", side_effect=briefs.PdfUnavailable("no Chrome")):
            resp = self.client.get(self.url)
        self.assertContains(resp, escape("Couldn't make the PDF (no Chrome)"))
        self.assertContains(resp, "Tenant overview")
        printed = self.client.get(self.url + "?print=1")
        self.assertContains(printed, "window.print()")

    def test_contacts_are_named_and_listed_once(self):
        Person.objects.create(full_name="", email="contactus@example.com", company=self.co)
        Person.objects.create(full_name="Jeb Brough", email="jeb@example.com", company=self.co)
        Person.objects.create(full_name="jeb  brough", email="jeb2@example.com", company=self.co)
        Person.objects.create(full_name="Terry Russell", company=self.co)
        self.assertEqual(sorted(p.full_name for p in company_pdf.build(self.co)["people"]), ["Jeb Brough", "Terry Russell"])
        with mock.patch.object(briefs, "html_to_pdf", return_value=b"%PDF") as pdf:
            self.client.get(self.url)
        self.assertNotIn("contactus@example.com", pdf.call_args.args[0])

    def test_details_panel_renders_the_overview_markdown(self):
        html = render_to_string("crm/partials/company_details.html", {"company": self.co, "grid": False})
        self.assertIn("<strong>Parent:</strong>", html)
        self.assertNotIn("**", html)

    def test_company_page_links_to_it(self):
        page = self.client.get(reverse("crm:company_detail", args=[self.co.pk]))
        self.assertContains(page, self.url)

    def test_company_with_nothing_still_renders(self):
        bare = Company.objects.create(name="Bare Co")
        with mock.patch.object(briefs, "html_to_pdf", return_value=b"%PDF") as pdf:
            self.client.get(reverse("crm:company_overview_pdf", args=[bare.pk]))
        self.assertIn("No overview written yet.", pdf.call_args.args[0])
