"""Due diligence on ACMI's own land deals, the feasibility decision, the closing checklist and the Acquired flip."""
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import campus as campus_mod
from crm import notify
from crm.contracts import closing, dates
from crm.land import diligence, tracker
from crm.models import (
    Campus, ClosingItem, Contract, ContractDate, DiligenceItem, LandCounty, LandSite, LandSiteEvent, Notification,
    SiteParcelDeal,
)
from crm.tests.test_contracts import char_cite, fake_claude, text_block
from crm.tests.test_land import make_parcel
from crm.tests.utils import make_manager

User = get_user_model()

PHASE_I = ("PHASE I ENVIRONMENTAL SITE ASSESSMENT\nThis assessment has revealed no evidence of recognized environmental "
           "conditions in connection with the Property, except for the following: a former 500-gallon heating oil "
           "underground storage tank near the barn.\n\nWe recommend a limited Phase II subsurface investigation.")


class DiligenceBase(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("simon", first_name="Simon"))
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        self.site = LandSite.objects.create(name="Outside the Gate", status="contract", campus=self.campus)
        self.p1 = make_parcel(county, "1", -77.0, 38.4, owner_name="WAMSLEY", acres=2.0)
        self.p2 = make_parcel(county, "2", -76.98, 38.4, owner_name="WAMSLEY", acres=1.0)
        self.site.parcels.add(self.p1, self.p2)
        tracker.ensure_deals(self.site)
        self.psa = Contract.objects.create(title="Wamsley PSA", kind="psa", status="signed", site=self.site,
                                           campus=self.campus, owner=self.user, our_entity="ACMI Properties MEIH LLC",
                                           effective_date=date(2026, 3, 2))
        self.feasibility = ContractDate.objects.create(contract=self.psa, kind="feasibility", label="Feasibility ends",
                                                       date=self.today + timedelta(days=10))

    def page(self):
        return f"/land/sites/{self.site.pk}/diligence/"


class ChecklistTests(DiligenceBase):
    def test_start_standard_then_full(self):
        self.assertContains(self.client.get(self.page()), "Start the checklist")
        self.client.post(self.page(), {"action": "start", "which": "standard"})
        items = list(self.site.diligence.all())
        self.assertEqual(len(items), len(diligence.STANDARD))
        self.assertEqual(items[0].ref, "A-2")
        self.assertEqual({i.contract for i in items}, {self.psa})  # runs against the signed PSA
        self.client.post(self.page(), {"action": "start", "which": "full"})
        self.assertEqual(self.site.diligence.count(), len(diligence.CHECKLIST))  # no duplicates
        self.assertEqual(diligence.start(self.site, "full"), 0)

    def test_summary_deadline_risk_and_money(self):
        diligence.start(self.site)
        phase1 = self.site.diligence.get(ref="D-1")
        phase1.status, phase1.budget, phase1.due_on = "ordered", Decimal("3500"), self.today - timedelta(days=1)
        phase1.save()
        survey = self.site.diligence.get(ref="A-2")
        survey.status, survey.budget, survey.cost, survey.due_on = ("clear", Decimal("18000"), Decimal("17250"),
                                                                    self.today - timedelta(days=5))
        survey.save()
        geo = self.site.diligence.get(ref="E-3")
        geo.status, geo.due_on = "ordered", self.today + timedelta(days=20)  # after feasibility ends
        geo.save()
        wetlands = self.site.diligence.get(ref="D-4")
        wetlands.status, wetlands.findings = "issue", "0.4 ac jurisdictional wetland on the north edge"
        wetlands.save()
        s = diligence.summary(self.site, self.today)
        self.assertEqual((s["end"], s["days_left"], s["end_from"]), (self.feasibility.date, 10, "Wamsley PSA"))
        self.assertEqual((s["count"], s["cleared"], len(s["issues"])), (14, 1, 1))
        self.assertEqual((s["budget"], s["invoiced"], s["committed"]), (Decimal("21500"), Decimal("17250"),
                                                                         Decimal("3500")))
        by_ref = {i.ref: i for i in s["items"]}
        self.assertTrue(by_ref["D-1"].late)
        self.assertTrue(by_ref["E-3"].after_deadline)
        self.assertFalse(by_ref["A-2"].late)  # cleared, so not late
        self.assertIn(by_ref["D-1"], s["at_risk"])
        self.assertEqual(len(s["at_risk"]), 12)  # feasibility within two weeks: everything open is at risk
        page = self.client.get(self.page())
        self.assertContains(page, "At risk before the deadline")
        self.assertContains(page, "after feasibility ends")
        self.assertContains(page, "report-logo")

        # No kept date on the contract: the site's own milestone is the deadline.
        self.feasibility.delete()
        self.site.milestones = {"feasibility_end": "2026-12-01"}
        self.site.save()
        self.assertEqual(diligence.feasibility_end(self.site, self.psa), (date(2026, 12, 1), "the site's timeline"))

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_status_edit_upload_delete_and_decision(self):
        diligence.start(self.site)
        item = self.site.diligence.get(ref="D-1")
        self.client.post(self.page(), {"action": "status", "item": item.pk, "status": "ordered"})
        item.refresh_from_db()
        self.assertEqual((item.status, item.ordered_on), ("ordered", self.today))
        self.assertTrue(LandSiteEvent.objects.filter(site=self.site, kind="diligence",
                                                     text__contains="Phase I ESA obtained and reviewed → Ordered").exists())
        self.client.post(self.page(), {
            "action": "edit", "item": item.pk, "name": item.name, "block": item.block, "status": "received",
            "consultant": "ECS Mid-Atlantic", "owner": self.user.pk, "budget": "3500", "cost": "3500",
            "findings": "One UST near the barn.", "file": SimpleUploadedFile("Phase I.txt", PHASE_I.encode())})
        item.refresh_from_db()
        self.assertEqual((item.status, item.received_on, item.consultant, item.text_status), ("received", self.today,
                                                                                            "ECS Mid-Atlantic", "ok"))
        self.assertIn("underground storage tank", item.text)
        page = self.client.get(self.page())
        self.assertContains(page, "ECS Mid-Atlantic")
        self.assertContains(page, f"/land/diligence/{item.pk}/file/")
        f = self.client.get(f"/land/diligence/{item.pk}/file/")
        self.assertIn(b"PHASE I", b"".join(f.streaming_content))

        joe = User.objects.create_user("joe")
        self.client.force_login(joe)
        self.client.post(self.page(), {"action": "delete", "item": item.pk})
        self.assertTrue(DiligenceItem.objects.filter(pk=item.pk).exists())  # managers only
        self.client.post(self.page(), {"action": "decision", "decision": "terminate", "decision_note": "UST + wetlands"})
        self.psa.refresh_from_db()
        self.assertEqual((self.psa.decision, self.psa.decision_by, self.psa.decision_on), ("terminate", joe, self.today))
        self.assertTrue(LandSiteEvent.objects.filter(site=self.site, text__contains="Feasibility decision").exists())
        contract_page = self.client.get(self.psa.get_absolute_url())
        self.assertContains(contract_page, "Terminate")
        self.assertContains(contract_page, "UST + wetlands")

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"})
    def test_claude_summarizes_a_report_with_page_citations(self):
        item = DiligenceItem.objects.create(site=self.site, ref="D-1", block="Environmental", name="Phase I ESA",
                                            text=PHASE_I, page_starts=[0, PHASE_I.index("We recommend")],
                                            pages=2, text_status="ok",
                                            file=SimpleUploadedFile("phase1.pdf", b"%PDF-1.4 fake"))
        tank = "a former 500-gallon heating oil underground storage tank near the barn"
        phase2 = "We recommend a limited Phase II subsurface investigation."
        patcher, stream = fake_claude([text_block("## Could change the deal\n- An old heating-oil tank ",
                                                  [char_cite(0, PHASE_I, tank)]),
                                       text_block("\n- Phase II recommended", [char_cite(0, PHASE_I, phase2)])])
        with patcher:
            self.client.post(self.page(), {"action": "summarize", "item": item.pk})
        content = stream.call_args.kwargs["messages"][0]["content"]
        self.assertEqual(content[0]["citations"], {"enabled": True})
        self.assertEqual(content[0]["source"]["data"], PHASE_I)
        self.assertIn("recognized environmental conditions", content[1]["text"])  # the Phase I focus
        item.refresh_from_db()
        self.assertEqual([c["page"] for b in item.summary for c in b["cites"]], [1, 2])
        page = self.client.get(self.page())
        self.assertContains(page, "Claude's summary of the report")
        self.assertContains(page, f"/land/diligence/{item.pk}/file/#page=2")


class RemindersTests(DiligenceBase):
    def test_dd_and_closing_reminders(self):
        chased = DiligenceItem.objects.create(site=self.site, contract=self.psa, name="ALTA survey", owner=self.user,
                                              status="ordered", due_on=self.today + timedelta(days=1))
        DiligenceItem.objects.create(site=self.site, contract=self.psa, name="Phase I", status="todo",
                                     due_on=self.today - timedelta(days=2))  # nobody on it: the PSA's owner
        DiligenceItem.objects.create(site=self.site, contract=self.psa, name="Geotech", owner=self.user,
                                     status="received", due_on=self.today)  # in hand: no reminder
        DiligenceItem.objects.create(site=self.site, contract=self.psa, name="Zoning letter", owner=self.user,
                                     status="ordered", due_on=self.today + timedelta(days=12))  # not yet
        ClosingItem.objects.create(contract=self.psa, name="Deed signed", due_on=self.today)
        notify.due_sweep(self.user)
        texts = sorted(Notification.objects.filter(user=self.user, kind="deadline").values_list("text", flat=True))
        self.assertEqual(len(texts), 4)  # survey, Phase I, deed, and the PSA's feasibility date
        self.assertIn("Due diligence, Outside the Gate: ALTA survey due in 1 day (" + f"{chased.due_on:%b} "
                      f"{chased.due_on.day})", texts)
        self.assertTrue(any(t.startswith("Due diligence, Outside the Gate: Phase I was due") for t in texts))
        self.assertIn("Closing, Wamsley PSA: Deed signed due today", texts)
        notify.due_sweep(self.user)
        self.assertEqual(Notification.objects.filter(user=self.user, kind="deadline").count(), 4)


class ClosingTests(DiligenceBase):
    def test_checklist_close_and_acquired_flip(self):
        closing_day = self.today + timedelta(days=30)
        ContractDate.objects.create(contract=self.psa, kind="closing", label="Closing", date=closing_day)
        outside = ContractDate.objects.create(contract=self.psa, kind="closing", label="Outside closing date",
                                              date=self.today + timedelta(days=90))
        page = self.client.get(self.psa.get_absolute_url())
        self.assertContains(page, "Start the closing checklist")
        self.client.post(self.psa.get_absolute_url(), {"action": "closing_start"})
        items = list(self.psa.closing_items.all())
        self.assertEqual(len(items), len(closing.DEFAULTS))
        self.assertEqual({i.due_on for i in items if i.side != "post"}, {closing_day})
        self.assertEqual({i.due_on for i in items if i.side == "post"}, {closing_day + timedelta(days=30)})
        deed = next(i for i in items if i.name == "Deed signed and notarized")
        self.client.post(self.psa.get_absolute_url(), {"action": "closing_status", "item": deed.pk, "status": "done"})
        deed.refresh_from_db()
        self.assertEqual((deed.status, deed.done_on, deed.done_by), ("done", self.today, self.user))
        lender = next(i for i in items if i.side == "lender")
        self.client.post(self.psa.get_absolute_url(), {"action": "closing_status", "item": lender.pk, "status": "na"})
        p = closing.progress(self.psa)
        self.assertEqual((p["done"], p["total"]), (1, len(closing.DEFAULTS) - 1))
        self.client.post(self.psa.get_absolute_url(), {"action": "closing_add", "name": "1031 exchange docs",
                                                       "side": "buyer", "responsible": "QI"})
        self.assertTrue(self.psa.closing_items.filter(name="1031 exchange docs", responsible="QI").exists())

        self.client.post(self.psa.get_absolute_url(), {"action": "close", "closed_on": str(self.today)})
        self.psa.refresh_from_db()
        self.site.refresh_from_db()
        self.assertEqual(self.psa.status, "closed")
        kept = self.psa.dates.get(kind="closing", label="Closing")
        self.assertEqual((kept.date, kept.done), (self.today, True))  # the actual day, not the planned one
        outside.refresh_from_db()
        self.assertEqual((outside.date, outside.done), (self.today + timedelta(days=90), False))  # left alone
        self.assertEqual((self.site.status, self.site.milestones["closed"]), ("acquired", self.today.isoformat()))
        deals = SiteParcelDeal.objects.filter(site=self.site)
        self.assertEqual({d.status for d in deals}, {"acquired"})  # every parcel of a whole-site contract
        events = LandSiteEvent.objects.filter(site=self.site, kind="milestone", text__startswith="Closed")
        self.assertEqual(events.count(), 1)  # logged once for the site, not per parcel
        card = self.client.get(f"/land/parcel/{self.p1.pk}/")
        self.assertContains(card, "Owned by ACMI Properties MEIH LLC since")

    def test_dashboard_lists_dd_and_closing_items(self):
        DiligenceItem.objects.create(site=self.site, name="Zoning letter", status="ordered",
                                     due_on=self.today + timedelta(days=3))
        ClosingItem.objects.create(contract=self.psa, name="Wire confirmed", due_on=self.today + timedelta(days=4))
        rows = dates.upcoming(14, self.today)
        self.assertEqual([(r.tag, r.label) for r in rows["soon"]],
                         [("DD", "Zoning letter"), ("Closing", "Wire confirmed"), ("", "Feasibility ends")])
        home = self.client.get("/")
        self.assertContains(home, "Zoning letter")
        self.assertContains(home, "Wire confirmed")


class CampusTests(DiligenceBase):
    def test_campus_summary_has_diligence_lines(self):
        diligence.start(self.site)
        wetlands = self.site.diligence.get(ref="D-4")
        wetlands.status, wetlands.findings = "issue", "0.4 ac jurisdictional wetland"
        wetlands.save()
        item = campus_mod.summary(self.campus, today=self.today)["land"][0]
        self.assertIn("due diligence: 0 of 14 cleared, 1 issue", item["facts"])
        self.assertIn("DD issue: Wetlands delineation (0.4 ac jurisdictional wetland)", item["flags"])
        self.assertIn("feasibility ends in 10 days with 13 DD items open", item["flags"])
        site_page = self.client.get(f"/land/sites/{self.site.pk}/manage/")
        self.assertContains(site_page, "0 of 14")
