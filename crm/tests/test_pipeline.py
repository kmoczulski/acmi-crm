import json
import tempfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import openpyxl
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import meetings, pipeline, pipeline_import
from crm.models import (
    Activity, Attachment, Campus, Company, CompanyStatus, Document, Opportunity, OpportunityContact, OpportunityStage,
    Person,
)

User = get_user_model()


def tracker_file(rows):
    """A minimal copy of the Fortress tracker's Tenants sheet."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tenants"
    header = ["Site", "Deal Type", "Stage", "Tenant / Counterparty", "Asset Type", "Acres", "Estimated Size (SF)",
              "Estimated Project Cost ($)", "Estimated Cost PSF", "Lead / Source", "Target Close", "Est. Delivery",
              "Next Step / Notes"]
    for section, items in rows.items():
        ws.append([section])
        ws.append(header)
        for item in items:
            ws.append(item)
    path = Path(tempfile.mkdtemp()) / "tracker.xlsx"
    wb.save(path)
    return str(path)


def trello_file(cards):
    path = Path(tempfile.mkdtemp()) / "trello.json"
    path.write_text(json.dumps({"cards": {"nodes": cards}}), encoding="utf-8")
    return str(path)


def card(cid, name, list_name, members=(), labels=(), desc=""):
    return {"id": f"ari:cloud:trello::card/workspace/x/{cid}", "name": name, "desc": desc, "url": f"https://trello.com/c/{cid}",
            "list": {"name": list_name}, "labels": [{"name": l} for l in labels],
            "members": [{"fullName": m} for m in members], "due": None, "dueComplete": False}


class PipelineImportTests(TestCase):
    def setUp(self):
        self.meih = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        Campus.objects.create(code="NSIH", name="National Security Industrial Hub")
        self.ara = Company.objects.create(name="Applied Research Associates", status=CompanyStatus.ENGAGED)
        self.ursa = Company.objects.create(name="Ursa Major")

    def run_import(self):
        tracker = tracker_file({
            "Signed Agreements": [
                ["MEIH", "Build-to-Suit", "LOI / PDA Signed", "Applied Research Associates", "Energetics / Munitions Mfg.",
                 "N/A", 52000, 75000000, None, "-", None, None, "PDA signed / negotiating lease"],
            ],
            "Prospects": [
                ["TBD ", "Build-to-Suit", "Prospect", "Ursa Major", "Energetics / Munitions Mfg.", 1000, 250000,
                 300000000, None, "Kellen", None, None, "Additional campus"],
            ],
        })
        trello = trello_file([
            card("a1", "ARA (Applied Research Associates)", "LEASE / PDA SIGNED", ["Simon Shewmaker", "Kellen Moczulski"],
                 ["PDA Executed", "High Value", "MEIH"], "POCs: Ken Herrle"),
            card("u1", "Ursa Major", "LOIs Executed", ["Kellen Moczulski"]),
            card("n1", "Brand New Co", "ACTIVE DISCUSSIONS (at least 1 meeting)", ["Paul Kadzielski"], ["NSIH"]),
            card("x1", "Someone Cold", "Cold", ["Simon Shewmaker"]),
        ])
        return pipeline_import.run(tracker, trello, stdout=open(Path(tempfile.mkdtemp()) / "log", "w"))

    def test_tracker_rows_and_trello_cards_become_deals(self):
        report = self.run_import()
        ara = Opportunity.objects.get(company=self.ara)
        self.assertEqual(ara.stage, OpportunityStage.LOI_SIGNED)  # "PDA Executed" label, not a signed lease
        self.assertEqual(ara.campus, self.meih)
        self.assertEqual(ara.value, 75000000)
        self.assertTrue(ara.investor_report)
        self.assertEqual(ara.owner.username, "simon")
        self.assertEqual({u.username for u in ara.team.all()}, {"simon", "kellen"})
        self.assertIn("Ken Herrle", ara.notes_set.get().body)
        # Ursa Major's tracker row is a separate deal from its executed-LOI Trello card.
        self.assertEqual(Opportunity.objects.filter(company=self.ursa).count(), 2)
        self.assertEqual(
            set(Opportunity.objects.filter(company=self.ursa).values_list("stage", flat=True)),
            {OpportunityStage.PROSPECT, OpportunityStage.LOI_SIGNED},
        )
        new = Opportunity.objects.get(company__name="Brand New Co")
        self.assertEqual(new.stage, OpportunityStage.QUALIFIED)
        self.assertEqual(new.campus.code, "NSIH")
        self.assertFalse(Company.objects.filter(name="Someone Cold").exists())
        self.assertIn("Brand New Co", report["companies_created"])
        self.ara.refresh_from_db()
        self.assertEqual(self.ara.status, CompanyStatus.OPPORTUNITY)
        self.assertEqual(self.ara.owner.username, "simon")

    def test_rerun_updates_instead_of_duplicating_and_never_moves_backwards(self):
        self.run_import()
        ara = Opportunity.objects.get(company=self.ara)
        ara.stage = OpportunityStage.SIGNED
        ara.save()
        report = self.run_import()
        self.assertEqual(report["created"], 0)
        self.assertEqual(Opportunity.objects.count(), 4)
        ara.refresh_from_db()
        self.assertEqual(ara.stage, OpportunityStage.SIGNED)


class PipelineLogicTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="Firehawk Aerospace", owner=self.user)

    def test_risk_reasons(self):
        opp = Opportunity.objects.create(name="Deal", company=self.company, stage=OpportunityStage.QUALIFIED)
        self.assertIn("No next step", pipeline.risk_reasons(opp))
        self.assertIn("Never contacted", pipeline.risk_reasons(opp))
        opp.next_step = "Send LOI"
        opp.next_step_due = timezone.localdate() - timedelta(days=2)
        self.company.last_interaction_at = timezone.now()
        self.assertEqual(len(pipeline.risk_reasons(opp)), 1)
        self.assertTrue(pipeline.risk_reasons(opp)[0].startswith("Next step overdue"))

    def test_weighted_value_uses_stage_probability_or_override(self):
        opp = Opportunity(name="D", company=self.company, stage=OpportunityStage.LOI_NEGOTIATION, value=1000)
        self.assertEqual(opp.weighted_value, 500)
        opp.probability = 90
        self.assertEqual(opp.weighted_value, 900)

    def test_documents_in_email_suggest_a_deal(self):
        act = Activity.objects.create(kind=Activity.Kind.EMAIL_SENT, occurred_at=timezone.now(), company=self.company,
                                      summary="PDA", source=Activity.Source.GMAIL, source_id="1")
        Attachment.objects.create(activity=act, filename="ACMI Firehawk PDA.docx", category="PDA")
        rows = pipeline.suggestions()
        self.assertEqual(rows[0]["company"], self.company)
        self.assertEqual(rows[0]["stage"], OpportunityStage.LOI_NEGOTIATION)
        r = self.client.post(reverse("crm:suggestion_create", args=[self.company.pk]))
        opp = Opportunity.objects.get(company=self.company)
        self.assertRedirects(r, opp.get_absolute_url())
        self.assertEqual(opp.stage, OpportunityStage.LOI_NEGOTIATION)
        self.assertEqual(pipeline.suggestions(), [])

    def test_dismissed_suggestion_stays_hidden(self):
        act = Activity.objects.create(kind=Activity.Kind.EMAIL_RECEIVED, occurred_at=timezone.now(), company=self.company,
                                      summary="NDA", source=Activity.Source.GMAIL, source_id="2")
        Attachment.objects.create(activity=act, filename="Mutual NDA.pdf", category="NDA")
        self.client.post(reverse("crm:suggestion_dismiss", args=[self.company.pk]))
        self.assertEqual(pipeline.suggestions(), [])

    def test_unanswered_reply_shows_in_my_day_until_we_answer(self):
        now = timezone.now()
        Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=now - timedelta(hours=3), company=self.company,
                                summary="Reply", outcome="Responded", thread_id="t1", source=Activity.Source.GMAIL, source_id="r")
        self.assertEqual(len(pipeline.awaiting_reply(self.user)), 1)
        Activity.objects.create(kind=Activity.Kind.EMAIL_SENT, occurred_at=now - timedelta(hours=1), company=self.company,
                                summary="Answer", thread_id="t1", source=Activity.Source.GMAIL, source_id="s")
        self.assertEqual(pipeline.awaiting_reply(self.user), [])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PipelinePageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.other = User.objects.create_user("simon", password="x", first_name="Simon")
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="ARA", owner=self.user)
        self.person = Person.objects.create(first_name="Ken", last_name="Herrle", email="k@ara.com", company=self.company)
        self.opp = Opportunity.objects.create(
            name="ARA · MEIH", company=self.company, stage=OpportunityStage.LOI_SIGNED, value=75_000_000,
            sq_ft_need=52000, investor_report=True, owner=self.user,
        )

    def test_pages_render(self):
        for url in [reverse("crm:home"), reverse("crm:opportunity_board"), reverse("crm:opportunity_board") + "?view=table",
                    reverse("crm:opportunity_board") + "?risk=1", self.opp.get_absolute_url(),
                    self.company.get_absolute_url(), reverse("crm:pipeline_report"),
                    reverse("crm:pipeline_report") + "?all=1", reverse("crm:company_list")]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_home_shows_pipeline_value(self):
        r = self.client.get(reverse("crm:home"))
        self.assertContains(r, "$75M")
        self.assertContains(r, "$56.2M")  # 75% weighted

    def test_excel_report(self):
        r = self.client.get(reverse("crm:pipeline_report_xlsx"))
        self.assertEqual(r.status_code, 200)
        wb = openpyxl.load_workbook(BytesIO(r.content))
        values = [c.value for row in wb["Tenants"].iter_rows() for c in row]
        self.assertIn("ARA", values)
        self.assertIn("Signed agreements", values)

    def test_stakeholder_roles(self):
        self.client.post(reverse("crm:opportunity_contact_add", args=[self.opp.pk]),
                         {"person": self.person.pk, "role": "decision_maker"})
        contact = OpportunityContact.objects.get()
        self.assertEqual(contact.get_role_display(), "Decision maker")
        self.assertContains(self.client.get(self.opp.get_absolute_url()), "Decision maker")

    def test_drive_link_and_upload(self):
        self.client.post(reverse("crm:document_add"), {
            "company": self.company.pk, "opportunity": self.opp.pk, "title": "ARA PDA signed",
            "url": "https://drive.google.com/file/d/abc/view",
        })
        doc = Document.objects.get()
        self.assertEqual(doc.category, "PDA")
        self.assertEqual(doc.href, "https://drive.google.com/file/d/abc/view")
        self.client.post(reverse("crm:document_add"), {
            "company": self.company.pk, "file": SimpleUploadedFile("site plan.pdf", b"%PDF-1.4"),
        })
        self.assertEqual(Document.objects.count(), 2)
        self.assertTrue(Document.objects.get(title="site plan.pdf").file)
        r = self.client.post(reverse("crm:document_add"), {"company": self.company.pk, "title": "nothing"})
        self.assertEqual(Document.objects.count(), 2)

    def test_bulk_owner(self):
        c2 = Company.objects.create(name="Other", strongest_connection_user=self.other)
        self.client.post(reverse("crm:company_bulk_owner"), {"ids": [c2.pk], "owner": "connection"})
        c2.refresh_from_db()
        self.assertEqual(c2.owner, self.other)
        self.client.post(reverse("crm:company_bulk_owner"), {"ids": [c2.pk, self.company.pk], "owner": self.user.pk})
        self.assertEqual(Company.objects.filter(owner=self.user).count(), 2)

    def test_recent_activity_hides_calendar_rsvps(self):
        Activity.objects.create(kind=Activity.Kind.EMAIL_RECEIVED, occurred_at=timezone.now(), company=self.company,
                                summary="Email from X: Accepted: HITT / ARA interview", source=Activity.Source.GMAIL, source_id="a")
        Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=timezone.now(), company=self.company,
                                summary="Reply from Ken: Re: lease draft", source=Activity.Source.GMAIL, source_id="b")
        r = self.client.get(reverse("crm:home"))
        self.assertNotContains(r, "Accepted: HITT")
        self.assertContains(r, "Re: lease draft")
