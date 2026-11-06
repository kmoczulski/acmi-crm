"""Legal (the old Contracts page): every agreement is filed to a tenant, a deal and a project, and reachable from the
company card, the company page, the project page and the deal (Kellen, 2026-11-06: "I just executed a lease and it
wasn't filed to the tenant or project. I want to be able to do that so I can navigate from a company card quickly")."""
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from crm.contracts import filing
from crm.contracts.forms import ContractForm
from crm.contracts.views import project_label
from crm.models import Campus, Company, Contract, LandSite, Opportunity, Project
from crm.tenants import leases

User = get_user_model()


class LegalBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x")
        self.client.force_login(self.user)
        self.meih = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.nsih = Campus.objects.create(code="NSIH", name="National Security Innovation Hub")
        self.meih_project = Project.objects.create(name="MEIH", campus=self.meih)
        self.ara = Project.objects.create(name="ARA Facility", parent=self.meih_project, campus=self.meih)
        self.acme = Company.objects.create(name="Acme Aerospace")
        self.deal = Opportunity.objects.create(company=self.acme, name="Hangar 1", campus=self.meih)

    def lease(self, title="Acme lease", **kw):
        return Contract.objects.create(title=title, kind=Contract.Kind.LEASE, status=Contract.Status.SIGNED,
                                       created_by=self.user, **kw)

    def agreement(self, title, kind=Contract.Kind.PSA, **kw):
        return Contract.objects.create(title=title, kind=kind, status=Contract.Status.NEGOTIATING,
                                       created_by=self.user, **kw)


class FileToTests(LegalBase):
    def test_a_deal_files_the_agreement_to_its_company_and_campus(self):
        c = self.lease()
        changed = filing.file_to(c, opportunity=self.deal)
        c.refresh_from_db()
        self.assertEqual((c.opportunity, c.company, c.campus), (self.deal, self.acme, self.meih))
        self.assertEqual(changed, ["campus", "company", "opportunity"])

    def test_another_company_takes_the_old_deal_off(self):
        other = Company.objects.create(name="Other Co")
        c = self.lease(opportunity=self.deal, company=self.acme)
        filing.file_to(c, company=other)
        c.refresh_from_db()
        self.assertEqual((c.company, c.opportunity), (other, None))

    def test_the_same_company_keeps_its_deal(self):
        c = self.lease(opportunity=self.deal, company=self.acme)
        self.assertEqual(filing.file_to(c, company=self.acme), [])
        c.refresh_from_db()
        self.assertEqual(c.opportunity, self.deal)

    def test_a_site_brings_its_campus_and_project_but_only_into_blanks(self):
        site = LandSite.objects.create(name="Parcel A", campus=self.meih, project=self.ara)
        c = self.lease(campus=self.nsih)
        filing.file_to(c, site=site)
        c.refresh_from_db()
        self.assertEqual((c.site, c.campus, c.project), (site, self.nsih, self.ara))  # NSIH was already chosen

    def test_a_project_brings_its_campus_from_itself_or_its_parent(self):
        site_project = Project.objects.create(name="Wamsley", parent=self.meih_project)  # no campus of its own
        c = self.lease()
        filing.file_to(c, project=site_project)
        c.refresh_from_db()
        self.assertEqual((c.project, c.campus), (site_project, self.meih))

    def test_what_is_picked_beats_what_is_implied(self):
        c = self.lease()
        filing.file_to(c, opportunity=self.deal, campus=self.nsih)
        c.refresh_from_db()
        self.assertEqual(c.campus, self.nsih)

    def test_clearing_the_company_takes_its_deal_off_too(self):
        c = self.lease(opportunity=self.deal, company=self.acme, project=self.ara)
        filing.file_to(c, clear=["company"])
        c.refresh_from_db()
        self.assertEqual((c.company, c.opportunity, c.project), (None, None, self.ara))

    def test_clearing_a_place_that_is_empty_changes_nothing(self):
        c = self.lease()
        self.assertEqual(filing.file_to(c, clear=["project", "site"]), [])


class MissingTests(LegalBase):
    def test_a_lease_needs_a_tenant_and_a_project(self):
        self.assertEqual(filing.missing(self.lease()), ["tenant", "project"])

    def test_a_lease_with_both_is_filed(self):
        self.assertEqual(filing.missing(self.lease(company=self.acme, campus=self.meih)), [])

    def test_a_lease_through_a_deal_is_filed(self):
        self.assertEqual(filing.missing(self.lease(opportunity=self.deal)), [])

    def test_land_agreements_need_only_a_project(self):
        self.assertEqual(filing.missing(self.agreement("Wamsley PSA")), ["project"])
        site = LandSite.objects.create(name="Wamsley 240 ac")
        self.assertEqual(filing.missing(self.agreement("Wamsley PSA 2", site=site)), [])

    def test_an_nda_is_never_unfiled_for_lack_of_a_project_or_a_tenant(self):
        nda = self.agreement("Acme NDA", Contract.Kind.NDA, counterparty="Acme Aerospace")
        self.assertEqual(filing.missing(nda), [])

    def test_the_project_it_was_filed_to_is_its_project_label(self):
        c = self.lease(project=self.ara, campus=self.nsih)
        self.assertEqual(project_label(c), "ARA Facility")
        self.assertEqual(filing.places(c)["project_url"], self.ara.get_absolute_url())

    def test_summary_names_the_tenant_and_the_project(self):
        c = self.lease(company=self.acme, project=self.ara)
        self.assertEqual(filing.summary(c), "Acme Aerospace · ARA Facility")
        self.assertEqual(filing.summary(self.lease("Nowhere")), "")


class AutoFileTests(LegalBase):
    def test_words_ignore_legal_suffixes_and_punctuation(self):
        self.assertEqual(filing.words("The Acme Aerospace, Inc."), ["acme", "aerospace"])
        self.assertEqual(filing.words("Smith & Sons LLC"), ["smith", "and", "sons"])

    def test_a_company_matches_only_on_one_exact_name(self):
        Company.objects.create(name="Acme Aerospace Holdings")
        self.assertEqual(filing.match_company("ACME AEROSPACE, INC."), self.acme)
        Company.objects.create(name="Acme Aerospace LLC")  # two companies read the same: no guess
        self.assertIsNone(filing.match_company("Acme Aerospace Inc"))

    def test_a_lease_that_names_the_tenant_is_filed_to_it_its_deal_and_the_campus_project(self):
        c = self.lease(counterparty="Acme Aerospace, Inc.")
        filing.auto_file(c)
        c.refresh_from_db()
        self.assertEqual((c.company, c.opportunity, c.campus, c.project),
                         (self.acme, self.deal, self.meih, self.meih_project))

    def test_a_company_with_the_in_front_still_matches(self):
        the = Company.objects.create(name="The Gamma Works")
        self.assertEqual(filing.match_company("Gamma Works, LLC"), the)

    def test_an_nda_is_filed_to_the_company_it_names(self):
        nda = self.agreement("Acme NDA", Contract.Kind.NDA, counterparty="Acme Aerospace, Inc.")
        filing.auto_file(nda)
        nda.refresh_from_db()
        self.assertEqual(nda.company, self.acme)

    def test_a_purchase_agreement_is_never_filed_to_a_company_by_name(self):
        c = self.agreement("Seller PSA", counterparty="Acme Aerospace")
        self.assertEqual(filing.auto_file(c), [])
        c.refresh_from_db()
        self.assertIsNone(c.company)

    def test_two_deals_are_not_guessed_between(self):
        Opportunity.objects.create(company=self.acme, name="Hangar 2", campus=self.nsih)
        c = self.lease(counterparty="Acme Aerospace")
        filing.auto_file(c)
        c.refresh_from_db()
        self.assertEqual((c.company, c.opportunity), (self.acme, None))

    def test_what_is_filed_already_is_not_moved(self):
        other = Company.objects.create(name="Other Co")
        c = self.lease(counterparty="Acme Aerospace", company=other)
        filing.auto_file(c)
        c.refresh_from_db()
        self.assertEqual(c.company, other)


class FilingViewTests(LegalBase):
    def url(self, c):
        return reverse("crm:contract_file_to", args=[c.pk])

    def test_picks_file_the_agreement_and_say_where(self):
        c = self.lease()
        r = self.client.post(self.url(c), {"opportunity": self.deal.pk, "project": self.ara.pk,
                                           "next": c.get_absolute_url()}, follow=True)
        c.refresh_from_db()
        self.assertEqual((c.company, c.opportunity, c.project), (self.acme, self.deal, self.ara))
        self.assertIn("Filed to Acme Aerospace · ARA Facility.", [str(m) for m in r.context["messages"]])

    def test_filing_to_a_tenant_also_finds_its_only_deal_and_the_campus_project(self):
        c = self.lease()
        self.client.post(self.url(c), {"company": self.acme.pk})
        c.refresh_from_db()
        self.assertEqual((c.company, c.opportunity, c.campus, c.project),
                         (self.acme, self.deal, self.meih, self.meih_project))

    def test_a_deal_pick_is_filed_as_picked(self):
        c = self.lease()
        self.client.post(self.url(c), {"opportunity": self.deal.pk})
        c.refresh_from_db()
        self.assertEqual((c.opportunity, c.project), (self.deal, None))  # nothing guessed beyond what the deal implies

    def test_ajax_gets_json(self):
        c = self.lease()
        lonely = Company.objects.create(name="Lonely Co")  # no deal and no campus to find the rest from
        r = self.client.post(self.url(c), {"company": lonely.pk}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.json()["filed"], "Lonely Co")
        self.assertEqual(r.json()["missing"], ["project"])

    def test_unknown_ids_change_nothing_and_next_stays_on_the_site(self):
        c = self.lease()
        r = self.client.post(self.url(c), {"company": "999999", "project": "abc", "next": "https://evil.example/"})
        self.assertEqual(r["Location"], c.get_absolute_url())
        c.refresh_from_db()
        self.assertEqual((c.company, c.project), (None, None))

    def test_clear_takes_a_place_off(self):
        c = self.lease(company=self.acme, project=self.ara)
        self.client.post(self.url(c), {"clear": ["project"]})
        c.refresh_from_db()
        self.assertEqual((c.company, c.project), (self.acme, None))

    def test_filing_is_a_post(self):
        self.assertEqual(self.client.get(self.url(self.lease())).status_code, 405)

    def test_the_picker_offers_tenants_deals_projects_sites_and_campuses(self):
        LandSite.objects.create(name="Acme Hill", campus=self.meih)
        got = self.client.get(reverse("crm:contract_file_options"), {"q": "acm"}).json()["options"]
        kinds = {(o["kind"], o["name"]) for o in got}
        self.assertIn(("company", "Acme Aerospace"), kinds)
        self.assertIn(("opportunity", "Acme Aerospace · Hangar 1"), kinds)
        self.assertIn(("site", "Acme Hill"), kinds)
        got = self.client.get(reverse("crm:contract_file_options"), {"q": "mei"}).json()["options"]
        self.assertEqual({(o["kind"], o["name"]) for o in got},
                         {("project", "MEIH"), ("campus", "MEIH · Maryland Energetics Innovation Hub")})

    def test_with_nothing_typed_it_offers_projects_and_campuses_and_one_letter_nothing(self):
        got = self.client.get(reverse("crm:contract_file_options")).json()["options"]
        self.assertEqual({o["kind"] for o in got}, {"project", "campus"})
        self.assertEqual(self.client.get(reverse("crm:contract_file_options"), {"q": "a"}).json()["options"], [])


class LegalPagesTests(LegalBase):
    def test_the_list_is_legal_and_leads_with_what_is_not_filed(self):
        self.lease("Orphan lease", counterparty="Somebody Inc")
        self.lease("Filed lease", company=self.acme, campus=self.meih)
        r = self.client.get(reverse("crm:contract_list"))
        self.assertContains(r, "<h1>Legal</h1>")
        self.assertContains(r, 'id="unfiled"')
        self.assertEqual([c.title for c in r.context["unfiled"]], ["Orphan lease"])
        self.assertContains(r, "needs a tenant")
        self.assertContains(r, "File a document")

    def test_nothing_unfiled_means_no_tray(self):
        self.lease("Filed lease", company=self.acme, campus=self.meih)
        self.assertNotContains(self.client.get(reverse("crm:contract_list")), 'id="unfiled"')

    def test_the_list_filters_to_a_tenant_or_a_project(self):
        mine = self.lease("Acme lease", opportunity=self.deal, project=self.ara)
        self.lease("Other lease", company=Company.objects.create(name="Other Co"), campus=self.nsih)
        r = self.client.get(reverse("crm:contract_list"), {"company": self.acme.pk})
        self.assertEqual([c.pk for c in r.context["contracts"]], [mine.pk])
        r = self.client.get(reverse("crm:contract_list"), {"project": self.ara.pk})
        self.assertEqual([c.pk for c in r.context["contracts"]], [mine.pk])
        self.assertContains(r, "Showing what is filed to")

    def test_search_finds_an_agreement_by_the_tenant_it_is_filed_to(self):
        self.lease("Hangar paper", company=self.acme, counterparty="A&A Holdings")
        groups = self.client.get(reverse("crm:search"), {"q": "acme aero"}).json()["groups"]
        by_label = {g["label"]: g["items"] for g in groups}
        self.assertEqual([h["title"] for h in by_label["Legal"]], ["Hangar paper"])

    def test_the_menu_says_legal(self):
        r = self.client.get(reverse("crm:contract_list"))
        self.assertContains(r, "<strong>Legal</strong>")
        self.assertNotContains(r, "<strong>Contracts</strong>")

    def test_the_agreement_page_has_a_filed_to_strip(self):
        c = self.lease(company=self.acme)
        r = self.client.get(c.get_absolute_url())
        self.assertContains(r, 'id="filed"')
        self.assertContains(r, "needs a project")
        self.assertContains(r, self.acme.get_absolute_url())
        c.project = self.ara
        c.save()
        self.assertNotContains(self.client.get(c.get_absolute_url()), "needs a project")

    def test_the_company_page_has_a_legal_tab_with_what_is_filed_directly_or_through_a_deal(self):
        direct = self.lease("Direct lease", company=self.acme)
        via_deal = self.agreement("Deal LOI", Contract.Kind.LOI, opportunity=self.deal)
        self.lease("Someone else's", company=Company.objects.create(name="Other Co"))
        r = self.client.get(self.acme.get_absolute_url(), {"tab": "legal"})
        self.assertEqual({c.pk for c in r.context["legal"]}, {direct.pk, via_deal.pk})
        self.assertEqual(r.context["legal"][0].pk, direct.pk)  # its lease first
        self.assertContains(r, "Direct lease")
        self.assertNotContains(r, "Someone else")
        self.assertContains(r, 'data-dropzone')
        self.assertContains(r, 'data-company="%d"' % self.acme.pk)

    def test_the_tab_offers_the_unfiled_agreement_that_names_the_company_and_files_it_in_one_click(self):
        orphan = self.lease("Orphan lease", counterparty="ACME Aerospace, Inc.")
        r = self.client.get(self.acme.get_absolute_url(), {"tab": "legal"})
        self.assertEqual(r.context["legal_suggestions"], [orphan])
        self.assertContains(r, "File to Acme Aerospace")
        self.client.post(reverse("crm:contract_file_to", args=[orphan.pk]), {"company": self.acme.pk})
        r = self.client.get(self.acme.get_absolute_url(), {"tab": "legal"})
        self.assertEqual(r.context["legal_suggestions"], [])
        self.assertEqual([c.pk for c in r.context["legal"]], [orphan.pk])

    def test_the_overview_has_a_legal_widget_and_the_tab_shows_its_count(self):
        self.lease("Direct lease", company=self.acme)
        r = self.client.get(self.acme.get_absolute_url())
        self.assertContains(r, 'data-widget="legal"')
        self.assertContains(r, "Direct lease")
        self.assertEqual(r.context["legal_count"], 1)

    def test_the_company_card_lists_the_lease_and_links_to_the_tab(self):
        lease = self.lease("Direct lease", company=self.acme, project=self.ara)
        self.lease("Orphan lease", counterparty="Acme Aerospace")
        r = self.client.get(self.acme.get_absolute_url() + "card/")
        self.assertContains(r, "Direct lease")
        self.assertContains(r, lease.get_absolute_url())
        self.assertContains(r, "ARA Facility")
        self.assertContains(r, self.acme.get_absolute_url() + "?tab=legal")
        self.assertContains(r, "1 unfiled")

    def test_the_card_of_a_company_with_nothing_filed_says_so(self):
        r = self.client.get(Company.objects.create(name="Empty Co").get_absolute_url() + "card/")
        self.assertContains(r, "Nothing filed yet.")
        self.assertContains(r, "File one")

    def test_the_project_page_lists_what_is_filed_to_it_to_its_sites_and_its_campus(self):
        site = LandSite.objects.create(name="Parcel A", project=self.ara)
        direct = self.lease("Direct", project=self.ara, company=self.acme)
        via_site = self.agreement("Via site", site=site)
        by_campus = self.agreement("Campus level", campus=self.meih)
        elsewhere = self.agreement("Elsewhere", campus=self.nsih)
        r = self.client.get(self.ara.get_absolute_url())
        self.assertEqual({c.pk for c in r.context["legal"]}, {direct.pk, via_site.pk})
        self.assertContains(r, 'data-project="%d"' % self.ara.pk)
        r = self.client.get(self.meih_project.get_absolute_url())  # the campus-level project holds its children's too
        self.assertEqual({c.pk for c in r.context["legal"]}, {direct.pk, via_site.pk, by_campus.pk})
        self.assertNotIn(elsewhere.pk, {c.pk for c in r.context["legal"]})

    def test_the_deal_page_and_card_show_legal(self):
        loi = self.agreement("Deal LOI", Contract.Kind.LOI, opportunity=self.deal)
        r = self.client.get(self.deal.get_absolute_url())
        self.assertContains(r, "Deal LOI")
        self.assertContains(r, 'data-opportunity="%d"' % self.deal.pk)
        self.assertContains(self.client.get(self.deal.get_absolute_url() + "card/"), loi.get_absolute_url())

    def test_a_lease_filed_to_a_project_lands_on_the_rent_roll_under_its_campus(self):
        c = self.lease(company=self.acme)
        self.assertEqual([g[0] for g in leases.rent_roll()["groups"]], ["No campus"])
        filing.file_to(c, project=self.ara)
        self.assertEqual([g[0] for g in leases.rent_roll()["groups"]], ["MEIH"])


class NewRecordTests(LegalBase):
    def test_new_record_from_a_company_starts_filed_to_it(self):
        url = reverse("crm:contract_create") + f"?company={self.acme.pk}"
        page = self.client.get(url)
        self.assertContains(page, 'name="company"')
        self.assertContains(page, f'value="{self.acme.pk}"')
        r = self.client.post(url, {"title": "Acme lease", "kind": "lease", "status": "negotiating",
                                   "company": self.acme.pk})
        c = Contract.objects.get(title="Acme lease")
        self.assertRedirects(r, c.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(c.company, self.acme)

    def test_new_record_from_a_project_carries_the_project_and_its_campus(self):
        r = self.client.get(reverse("crm:contract_create") + f"?project={self.ara.pk}")
        form = r.context["form"]
        self.assertEqual((form.initial["project"], form.initial["campus"]), (self.ara, self.meih))

    def test_saving_details_never_wipes_the_tenant(self):
        c = self.lease(company=self.acme, project=self.ara, campus=self.meih)
        self.client.post(c.get_absolute_url(), {"action": "details", "title": "Acme lease (renamed)", "kind": "lease",
                                                "status": "signed", "project": self.ara.pk, "campus": self.meih.pk})
        c.refresh_from_db()
        self.assertEqual((c.title, c.company, c.project), ("Acme lease (renamed)", self.acme, self.ara))

    def test_the_details_form_offers_the_project(self):
        self.assertIn("project", ContractForm().fields)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DropTests(LegalBase):
    def upload(self, name="Acme lease executed 2026-11-05.txt"):
        return SimpleUploadedFile(name, b"LEASE between Acme Aerospace, Inc. (Tenant) and ACMI Properties LLC.",
                                  content_type="text/plain")

    def test_a_drop_is_filed_where_it_was_dropped(self):
        r = self.client.post(reverse("crm:contract_drop_new"),
                             {"file": self.upload(), "company": self.acme.pk, "project": self.ara.pk})
        data = r.json()
        self.assertTrue(data["ok"])
        c = Contract.objects.get()
        self.assertEqual((c.company, c.project, c.campus), (self.acme, self.ara, self.meih))
        self.assertEqual(data["filed"], "Acme Aerospace · ARA Facility")
        self.assertEqual(data["missing"], [])

    def test_a_drop_with_no_place_says_what_is_missing(self):
        data = self.client.post(reverse("crm:contract_drop_new"), {"file": self.upload("scan.txt")}).json()
        self.assertEqual((data["filed"], data["missing"]), ("", ["project"]))

    def test_when_claude_reads_a_lease_the_tenant_is_filed_by_name(self):
        def read(doc, contract, user=None, new_contract=False):
            contract.kind, contract.status = Contract.Kind.LEASE, Contract.Status.SIGNED
            contract.counterparty = "Acme Aerospace, Inc."
            contract.save()
            return ["the contract's kind"]

        with mock.patch("crm.contracts.views.ai_enabled", return_value=True), \
                mock.patch("crm.contracts.views.intake.read", side_effect=read):
            data = self.client.post(reverse("crm:contract_drop_new"), {"file": self.upload()}).json()
        c = Contract.objects.get()
        self.assertEqual((c.company, c.opportunity, c.campus, c.project),
                         (self.acme, self.deal, self.meih, self.meih_project))
        self.assertEqual(data["filed"], "Acme Aerospace · MEIH")

    def test_a_drop_on_a_site_page_still_files_to_the_site_and_its_project(self):
        site = LandSite.objects.create(name="Parcel A", campus=self.meih, project=self.ara)
        self.client.post(reverse("crm:contract_drop_new"), {"file": self.upload(), "site": site.pk})
        c = Contract.objects.get()
        self.assertEqual((c.site, c.campus, c.project), (site, self.meih, self.ara))

    def test_the_list_page_no_longer_claims_claude_is_off_when_it_is_on(self):
        with mock.patch("crm.contracts.views.ai_enabled", return_value=True):
            r = self.client.get(reverse("crm:contract_list"))
        self.assertNotContains(r, "Claude isn't switched on")
