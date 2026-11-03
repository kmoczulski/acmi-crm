"""Build records: schedule and slip, team, GC selection with Claude leveling the proposals, budget with change orders,
draws, and the project page / campus summary / dashboard hooks."""
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import campus as campus_mod
from crm.build import logic
from crm.contracts import dates
from crm.models import (
    Bid, BudgetLine, BuildMilestone, Campus, ChangeOrder, Draw, Project, ProjectAnswer, TeamMember,
)
from crm.tests.test_contracts import char_cite, fake_claude, text_block
from crm.tests.utils import make_manager

User = get_user_model()

PROPOSAL_A = ("ACME BUILDERS PROPOSAL\nLump sum price: $41,250,000.\nFee: 4.5% of the cost of the work.\n"
              "General conditions: $2,100,000.\nSchedule: 14 months from notice to proceed.\n"
              "Exclusions: rail spur, off-site utilities, rock excavation.")
PROPOSAL_B = ("BLUE RIDGE CONSTRUCTION\nGuaranteed maximum price: $39,800,000 including a $600,000 contingency.\n"
              "Fee 5%. Schedule 16 months. Excludes the substation pad.")


class BuildBase(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("dave", first_name="Dave"))
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.ara = Project.objects.create(name="ARA building", campus=self.campus)
        self.url = f"/projects/{self.ara.pk}/build/"

    def post(self, **data):
        return self.client.post(self.url, data)


class ScheduleAndBudgetTests(BuildBase):
    def test_schedule_slip_and_late_milestones(self):
        self.assertContains(self.client.get(self.url), "Start the standard schedule")
        self.post(action="schedule_start")
        self.assertEqual(self.ara.milestones.count(), len(logic.SCHEDULE))
        self.assertEqual(logic.start_schedule(self.ara), 0)  # no duplicates
        pad = self.ara.milestones.get(name="Pad ready")
        self.post(action="milestone_edit", id=pad.pk, name=pad.name, phase="construction",
                  baseline=str(self.today + timedelta(days=10)), forecast=str(self.today + timedelta(days=31)))
        pad.refresh_from_db()
        self.assertEqual(pad.slip, 21)
        ntp = self.ara.milestones.get(name="Notice to proceed")
        self.post(action="milestone_edit", id=ntp.pk, name=ntp.name, phase="construction",
                  baseline=str(self.today - timedelta(days=9)), forecast=str(self.today - timedelta(days=2)))
        s = logic.schedule(self.ara, self.today)
        self.assertEqual(s["next"], ntp)  # forecast passed without an actual date: still next, and late
        self.assertEqual([m.name for m in s["late"]], ["Notice to proceed"])
        self.assertEqual(s["worst_slip"], 21)
        page = self.client.get(self.url)
        self.assertContains(page, "+21 d")
        self.assertContains(page, "passed: record or re-forecast")
        self.post(action="milestone_edit", id=ntp.pk, name=ntp.name, phase="construction",
                  baseline=str(self.today - timedelta(days=9)), forecast=str(self.today - timedelta(days=2)),
                  actual=str(self.today - timedelta(days=1)))
        self.assertEqual(logic.schedule(self.ara, self.today)["next"].name, "Pad ready")

    def test_budget_change_orders_and_draws(self):
        self.post(action="budget_start")
        self.assertEqual(self.ara.budget.count(), len(logic.BUDGET))
        shell = self.ara.budget.get(code="20")
        self.post(action="line_edit", id=shell.pk, group="hard", code="20", name=shell.name, original="20000000",
                  committed="19500000", paid="2500000")
        site = self.ara.budget.get(code="10")
        self.post(action="line_edit", id=site.pk, group="hard", code="10", name=site.name, original="5000000",
                  forecast="5400000")
        self.post(action="co_add", number="1", title="Add a second dock door", reason="owner", amount="85000",
                  days="0", status="pending", line=shell.pk, submitted_on=str(self.today))
        co = ChangeOrder.objects.get()
        b = logic.budget(self.ara)
        self.assertEqual(b["totals"]["current"], Decimal("25000000"))  # pending changes don't count yet
        self.post(action="co_decide", id=co.pk, status="approved")
        co.refresh_from_db()
        self.assertEqual((co.status, co.decided_on), ("approved", self.today))
        self.post(action="co_add", number="2", title="Rock excavation", reason="unforeseen", amount="120000",
                  days="9", status="approved")  # on no line
        b = logic.budget(self.ara)
        shell_line = next(line for line in b["lines"] if line.code == "20")
        self.assertEqual((shell_line.changes, shell_line.current), (Decimal("85000"), Decimal("20085000")))
        t = b["totals"]
        self.assertEqual((t["original"], t["changes"], t["current"]), (Decimal("25000000"), Decimal("205000"),
                                                                       Decimal("25205000")))
        self.assertEqual((t["forecast_total"], t["variance"]), (Decimal("25605000"), Decimal("400000")))
        self.assertEqual(b["loose"], Decimal("120000"))
        page = self.client.get(self.url)
        self.assertContains(page, "of changes on no line")
        self.assertContains(page, "$25,205,000")  # tables in whole dollars, so the columns add up
        self.assertContains(page, "$25.2M")  # the strip stays short

        self.post(action="draw_add", number="1", requested="1800000", retainage="90000", status="draft",
                  submitted_on="")
        draw = Draw.objects.get()
        self.post(action="draw_edit", id=draw.pk, number="1", requested="1800000", retainage="90000",
                  status="funded")
        draw.refresh_from_db()
        self.assertEqual((draw.funded, draw.funded_on), (Decimal("1800000"), self.today))
        s = logic.status(self.ara, self.today)
        self.assertEqual((s["funded"], s["approved_days"]), (Decimal("1800000"), 9))

        joe = User.objects.create_user("joe")
        self.client.force_login(joe)
        self.post(action="co_delete", id=co.pk)
        self.assertTrue(ChangeOrder.objects.filter(pk=co.pk).exists())  # managers only


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class GcSelectionTests(BuildBase):
    def add_bid(self, firm, text, amount=""):
        self.post(action="bid_add", firm=firm, status="invited", amount=amount,
                  file=SimpleUploadedFile(f"{firm}.txt", text.encode()))
        return Bid.objects.get(firm=firm)

    def test_bids_select_and_team(self):
        self.post(action="schedule_start")
        acme = self.add_bid("Acme Builders", PROPOSAL_A, amount="41250000")
        self.assertEqual((acme.status, acme.received_on, acme.text_status), ("received", self.today, "ok"))
        blue = self.add_bid("Blue Ridge Construction", PROPOSAL_B, amount="39800000")
        Bid.objects.create(project=self.ara, firm="Late Co", status="declined")
        page = self.client.get(self.url)
        self.assertLess(page.content.find(b"Blue Ridge"), page.content.find(b"Acme Builders"))  # cheapest first
        self.post(action="bid_select", id=blue.pk)
        acme.refresh_from_db()
        blue.refresh_from_db()
        self.assertEqual((blue.status, acme.status), ("selected", "not_selected"))
        self.assertEqual(Bid.objects.get(firm="Late Co").status, "declined")  # out already: left alone
        gc = TeamMember.objects.get(project=self.ara, role="gc")
        self.assertEqual((gc.firm, gc.fee, gc.status), ("Blue Ridge Construction", Decimal("39800000"), "engaged"))
        self.assertEqual(self.ara.milestones.get(name="GC selected").actual, self.today)
        self.assertEqual(logic.status(self.ara)["gc"], "Blue Ridge Construction")

    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"})
    def test_claude_levels_asks_and_reads_terms(self):
        acme = self.add_bid("Acme Builders", PROPOSAL_A)
        blue = self.add_bid("Blue Ridge Construction", PROPOSAL_B)
        self.assertContains(self.client.get(self.url), "Level the bids with Claude")
        patcher, stream = fake_claude([  # no prices entered yet, so the proposals go in by firm name
            text_block("## Price\n- Blue Ridge is lower ", [char_cite(1, PROPOSAL_B, "$39,800,000")]),
            text_block(" but Acme's lump sum is firm ", [char_cite(0, PROPOSAL_A, "Lump sum price: $41,250,000.")]),
        ])
        with patcher:
            self.post(action="level")
        answer = ProjectAnswer.objects.get()
        self.assertEqual(answer.question, "Bid leveling")
        kwargs = stream.call_args.kwargs
        docs = kwargs["messages"][0]["content"][:-1]
        self.assertEqual([d["title"] for d in docs], ["Acme Builders proposal", "Blue Ridge Construction proposal"])
        self.assertTrue(all(d["citations"] == {"enabled": True} for d in docs))
        self.assertEqual(docs[-1]["cache_control"], {"type": "ephemeral"})
        self.assertIn("PROJECT: ARA building (MEIH campus)", kwargs["messages"][0]["content"][-1]["text"])
        self.assertEqual([c["doc"] for b in answer.blocks for c in b["cites"]], [blue.pk, acme.pk])
        page = self.client.get(self.url)
        self.assertContains(page, f"/projects/bids/{blue.pk}/file/#page=1")
        self.assertContains(page, "Blue Ridge is lower")

        with patcher:
            self.post(action="ask", question="Which bids exclude the rail spur?")
        self.assertTrue(ProjectAnswer.objects.filter(question="Which bids exclude the rail spur?").exists())

        terms = {"amount": "$41,250,000", "fee_pct": "4.5", "general_conditions": "2100000", "duration_months": "14",
                 "exclusions": ["rail spur", "off-site utilities", "rock excavation"]}
        acme.fee_pct = Decimal("4.0")  # already set by hand: kept
        acme.save()
        patcher, stream = fake_claude([text_block(json.dumps(terms))])
        with patcher:
            r = self.client.post(self.url, {"action": "bid_terms", "id": acme.pk}, follow=True)
        self.assertEqual(stream.call_args.kwargs["output_config"]["format"]["type"], "json_schema")
        acme.refresh_from_db()
        self.assertEqual((acme.amount, acme.fee_pct, acme.general_conditions, acme.duration_months),
                         (Decimal("41250000"), Decimal("4.0"), Decimal("2100000"), Decimal("14")))
        self.assertIn("rail spur; off-site utilities", acme.concerns)
        self.assertContains(r, "Filled in price ($), general conditions ($), schedule (months), concerns")


class HooksTests(BuildBase):
    def test_project_page_campus_summary_and_dashboard(self):
        self.assertContains(self.client.get(f"/projects/{self.ara.pk}/"), "Schedule, team, GC selection")
        logic.start_schedule(self.ara)
        pad = self.ara.milestones.get(name="Pad ready")
        pad.baseline, pad.forecast = self.today, self.today + timedelta(days=20)
        pad.save()
        site_work = self.ara.milestones.get(name="Site work starts")
        site_work.forecast = self.today - timedelta(days=3)
        site_work.save()
        Bid.objects.create(project=self.ara, firm="Acme Builders", status="shortlisted",
                           interview_on=self.today + timedelta(days=2))
        Bid.objects.create(project=self.ara, firm="Blue Ridge", status="received")
        BudgetLine.objects.create(project=self.ara, name="Building shell", original=Decimal("20000000"),
                                  forecast=Decimal("20500000"))
        ChangeOrder.objects.create(project=self.ara, title="Dock door", amount=Decimal("85000"))
        Draw.objects.create(project=self.ara, number=1, requested=Decimal("1000000"), status="submitted",
                            submitted_on=self.today - timedelta(days=40))
        item = campus_mod.summary(self.campus, today=self.today)["projects"][0]
        self.assertEqual(item["name"], "ARA building")
        for fact in ("GC selection: 2 proposals in, shortlist Acme Builders", "budget $20M, forecast $20.5M",
                     "1 change order pending ($85K)"):
            self.assertIn(fact, item["facts"])
        self.assertTrue(item["facts"][0].startswith("next milestone: Site work starts"))
        for flag in ("forecast $500K over budget", f"Site work starts was forecast for "
                     f"{site_work.forecast:%b} {site_work.forecast.day}; record it or re-forecast",
                     f"draw 1 submitted {(self.today - timedelta(days=40)):%b} {(self.today - timedelta(days=40)).day}"
                     ", not funded yet"):
            self.assertIn(flag, item["flags"])
        rows = dates.upcoming(14, self.today)
        self.assertEqual([(r.tag, r.label) for r in rows["overdue"]], [("Build", "Site work starts")])
        self.assertEqual([(r.tag, r.label) for r in rows["soon"]], [("Build", "GC interview: Acme Builders")])
        page = self.client.get(f"/projects/{self.ara.pk}/")
        self.assertContains(page, "Change orders pending")
        build = self.client.get(self.url)
        self.assertContains(build, "report-logo")
