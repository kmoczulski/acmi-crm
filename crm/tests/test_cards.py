"""Record cards: a company, deal or person at a glance over the page you were on (crm/cards.py, nav.js "peek").
Kellen, 2026-10-27: "Should it not be their card? ... It should be much more fluid and easy to navigate."
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.models import Activity, Campus, Comment, Company, Contract, LandSite, Opportunity, OpportunityContact, Person, Task

User = get_user_model()


class RecordCardTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.client.force_login(self.kellen)
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.co = Company.objects.create(name="Acme Energetics", sector="Defense", hq_location="Huntsville, AL",
                                         description="Employee-owned since 1979.\n\n# What they do\n- **Energetics** research",
                                         owner=self.kellen)
        self.ann = Person.objects.create(first_name="Ann", last_name="Smith", email="ann@acme.test", title="VP Real Estate",
                                         company=self.co, phones=["256-555-0100"])
        self.deal = Opportunity.objects.create(name="Acme · MEIH", company=self.co, campus=self.campus, stage="loi_neg",
                                               value=25_000_000, sq_ft_need=50_000, next_step="Send the PDA draft",
                                               next_step_due=timezone.localdate() - timedelta(days=3), investor_report=True)
        OpportunityContact.objects.create(opportunity=self.deal, person=self.ann, role="decision_maker")
        self.site = LandSite.objects.create(name="Wamsley 240 ac", status="loi", campus=self.campus)
        self.deal.land_sites.add(self.site)
        Contract.objects.create(title="Acme LOI", kind="loi", status="negotiating", company=self.co, opportunity=self.deal,
                                created_by=self.kellen)
        Task.objects.create(title="Book the site tour", assignee=self.kellen, company=self.co, opportunity=self.deal,
                            due_date=timezone.localdate() + timedelta(days=2))
        Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=timezone.now() - timedelta(days=1), company=self.co,
                                person=self.ann, opportunity=self.deal, summary="Re: PDA draft", outcome="Positive")
        Comment.objects.create(body="Ann wants the arcs on the brief", author=self.kellen, company=self.co, opportunity=self.deal)

    def test_the_deal_card_shows_the_essentials_and_links_the_people_and_records_around_it(self):
        r = self.client.get(f"/opportunities/{self.deal.pk}/card/")
        self.assertEqual(r.status_code, 200)
        for text in ("Acme · MEIH", "$25M", "50,000", "Send the PDA draft", "Ann Smith", "Decision maker", "Acme LOI",
                     "Wamsley 240 ac", "Book the site tour", "Re: PDA draft", "Ann wants the arcs", "On the investor report",
                     "Needs attention"):
            self.assertContains(r, text)
        self.assertContains(r, f'href="{self.co.get_absolute_url()}"')  # the company, as a card
        self.assertContains(r, f'href="{self.ann.get_absolute_url()}"')
        self.assertContains(r, f'href="{self.deal.get_absolute_url()}" class="btn btn-small btn-primary" data-page')
        self.assertContains(r, "data-card-back")
        self.assertContains(r, "data-card-close")

    def test_the_company_card_and_the_person_card(self):
        r = self.client.get(f"/companies/{self.co.pk}/card/")
        for text in ("Acme Energetics", "Defense", "Huntsville, AL", "Employee-owned since 1979. Energetics research.",
                     "Acme · MEIH", "Ann Smith", "Book the site tour", "Re: PDA draft", "Open company page"):
            self.assertContains(r, text)
        self.assertNotContains(r, "# What they do")  # the description reads as one plain line on the card
        r = self.client.get(f"/people/{self.ann.pk}/card/")
        for text in ("Ann Smith", "VP Real Estate", "Acme Energetics", "ann@acme.test", "256-555-0100", "Acme · MEIH",
                     "Open person page"):
            self.assertContains(r, text)
        self.assertContains(r, f'href="{self.co.get_absolute_url()}"')

    def test_open_page_keeps_the_list_position_the_link_carried(self):
        r = self.client.get(f"/companies/{self.co.pk}/card/?from=scope%3Dactive")
        self.assertContains(r, f'href="/companies/{self.co.pk}/?from=scope%3Dactive" class="btn btn-small btn-primary" data-page')
        self.assertEqual(self.client.get("/companies/999/card/").status_code, 404)

    def test_every_page_carries_the_card_dialog_and_record_links_stay_real_links(self):
        r = self.client.get(reverse("crm:pipeline_report"))
        self.assertContains(r, 'id="peek"')
        self.assertContains(r, f'href="{self.deal.get_absolute_url()}"')  # a plain link: middle-click still opens the page
        r = self.client.get(self.co.get_absolute_url())
        self.assertContains(r, f'href="{reverse("crm:campus_summary", args=["MEIH"])}"')  # the deal's campus is a link now

    def test_investor_report_links_skip_the_card_and_open_the_page_in_one_click(self):
        r = self.client.get(reverse("crm:pipeline_report"))
        self.assertContains(r, 'pipeline-table rp-sections" data-no-peek')  # nav.js leaves links inside it alone

    def test_the_deal_page_links_its_campus_and_land_sites(self):
        r = self.client.get(self.deal.get_absolute_url())
        self.assertContains(r, f'href="{reverse("crm:campus_summary", args=["MEIH"])}"')
        self.assertContains(r, f'href="{reverse("crm:land_site_detail", args=[self.site.pk])}"')
