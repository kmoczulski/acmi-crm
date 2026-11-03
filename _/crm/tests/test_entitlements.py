"""Entitlements: the permit matrix, approvals with their steps, conditions and papers, Claude on the papers, and the
dashboard / reminders / campus summary hooks."""
import json
import tempfile
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import campus as campus_mod
from crm import notify
from crm.contracts import dates
from crm.entitlements import logic
from crm.models import (
    Approval, ApprovalCondition, ApprovalDocument, ApprovalStep, Campus, Contract, LandSite, Notification, Project,
)
from crm.tests.test_contracts import char_cite, fake_claude, text_block
from crm.tests.utils import make_manager

User = get_user_model()

RESOLUTION = (
    "BOARD OF APPEALS RESOLUTION 2026-14\n"
    "The variance to allow a 24-foot monument sign is GRANTED, subject to the following conditions:\n"
    "1. The applicant shall remove the temporary banner before the sign permit is issued.\n"
    "2. The sign lighting shall be turned off between 11 p.m. and 6 a.m.\n"
    "This variance shall expire on June 30, 2027 unless a sign permit is issued.\n"
    "Any appeal must be filed within 30 days, by November 30, 2026."
)


class EntitlementBase(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("simon", first_name="Simon"))
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.forge = Project.objects.create(name="The Forge", campus=self.campus)
        self.variance = Approval.objects.create(title="Sign height variance", kind="variance", project=self.forge,
                                                campus=self.campus, jurisdiction="Charles County Board of Appeals",
                                                owner=self.user, created_by=self.user)


class MatrixAndPagesTests(EntitlementBase):
    def test_permit_matrix_for_a_project(self):
        page = self.client.get(f"/projects/{self.forge.pk}/")
        self.assertContains(page, "Entitlements")
        self.assertContains(page, "Sign height variance")
        defaults = [k for k, _, ticked in logic.MATRIX if ticked]
        r = self.client.post("/entitlements/matrix/", {"project": self.forge.pk, "kinds": defaults + ["essp"],
                                                       "back": f"/projects/{self.forge.pk}/"})
        self.assertRedirects(r, f"/projects/{self.forge.pk}/", fetch_redirect_response=False)
        made = Approval.objects.filter(project=self.forge).exclude(pk=self.variance.pk)
        self.assertEqual(made.count(), len(defaults) + 1)
        self.assertEqual({a.campus for a in made}, {self.campus})
        self.assertTrue(made.filter(kind="essp", title="DDESB explosives safety site plan").exists())
        self.client.post("/entitlements/matrix/", {"project": self.forge.pk, "kinds": defaults})
        self.assertEqual(Approval.objects.filter(project=self.forge).count(), len(defaults) + 2)  # no duplicates
        r = self.client.post("/entitlements/matrix/", {"kinds": defaults, "back": "https://evil.example/"}, follow=True)
        self.assertContains(r, "Pick the project or land site first")
        self.assertEqual(r.redirect_chain[-1][0], "/entitlements/")  # off-site "back" ignored

    def test_create_needs_a_home(self):
        r = self.client.post("/entitlements/new/", {"title": "Rezoning", "kind": "rezoning", "status": "planned"})
        self.assertContains(r, "Pick the project, land site or campus")
        site = LandSite.objects.create(name="Lake Acton", campus=self.campus)
        r = self.client.post("/entitlements/new/", {"title": "Rezoning to I-2", "kind": "rezoning",
                                                    "status": "planned", "site": site.pk})
        a = Approval.objects.get(title="Rezoning to I-2")
        self.assertRedirects(r, a.get_absolute_url())
        self.assertEqual((a.campus, a.created_by), (self.campus, self.user))
        self.assertContains(self.client.get(f"/land/sites/{site.pk}/manage/"), "Rezoning to I-2")

    def test_steps_move_status_forward_and_list_shows_next(self):
        url = self.variance.get_absolute_url()
        self.client.post(url, {"action": "step_add", "kind": "submitted", "date": str(self.today - timedelta(days=20))})
        self.variance.refresh_from_db()
        self.assertEqual((self.variance.status, self.variance.submitted_on), ("submitted", self.today - timedelta(days=20)))
        hearing = self.today + timedelta(days=5)
        self.client.post(url, {"action": "step_add", "kind": "hearing", "date": str(hearing), "body": "Board of Appeals"})
        self.variance.refresh_from_db()
        self.assertEqual(self.variance.status, "hearing")
        self.client.post(url, {"action": "step_add", "kind": "comments", "date": str(self.today)})
        self.variance.refresh_from_db()
        self.assertEqual(self.variance.status, "hearing")  # never moves back
        listing = self.client.get("/entitlements/")
        self.assertContains(listing, "Hearing / meeting (Board of Appeals) · " + f"{hearing:%b} {hearing.day}")
        step = ApprovalStep.objects.get(kind="hearing")
        step.date = self.today - timedelta(days=1)
        step.save()
        self.assertContains(self.client.get("/entitlements/"), "outcome?")
        self.client.post(url, {"action": "step_done", "step": step.pk})
        step.refresh_from_db()
        self.assertTrue(step.done)
        self.client.post(url, {"action": "status", "status": "approved"})
        self.variance.refresh_from_db()
        self.assertEqual((self.variance.status, self.variance.approved_on), ("approved", self.today))

    def test_conditions_and_permissions(self):
        url = self.variance.get_absolute_url()
        self.client.post(url, {"action": "condition_add", "text": "Remove the temporary banner",
                               "timing": "Before the sign permit", "due_on": str(self.today + timedelta(days=3))})
        c = ApprovalCondition.objects.get()
        self.assertEqual((c.timing, c.created_by, c.confirmed), ("Before the sign permit", self.user, True))
        self.client.post(url, {"action": "condition_status", "condition": c.pk, "status": "met"})
        c.refresh_from_db()
        self.assertEqual(c.status, "met")
        joe = User.objects.create_user("joe")
        self.client.force_login(joe)
        self.client.post(url, {"action": "condition_delete", "condition": c.pk})
        self.assertTrue(ApprovalCondition.objects.filter(pk=c.pk).exists())  # not theirs
        self.client.post(url, {"action": "delete"})
        self.assertTrue(Approval.objects.filter(pk=self.variance.pk).exists())  # managers only


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
@mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"})
class ClaudeOnPapersTests(EntitlementBase):
    def upload(self):
        self.client.post(self.variance.get_absolute_url(), {
            "action": "doc_upload", "kind": "decision", "title": "", "dated": "2026-10-30",
            "file": SimpleUploadedFile("BOA Resolution 2026-14.txt", RESOLUTION.encode())})
        return ApprovalDocument.objects.get()

    def test_upload_and_cited_summary(self):
        doc = self.upload()
        self.assertEqual((doc.title, doc.text_status, doc.approval), ("BOA Resolution 2026-14", "ok", self.variance))
        quote = "This variance shall expire on June 30, 2027 unless a sign permit is issued."
        patcher, stream = fake_claude([text_block("## Approved\n- Expires June 30, 2027 ", [char_cite(0, RESOLUTION, quote)])])
        with patcher:
            self.client.post(self.variance.get_absolute_url(), {"action": "doc_summarize", "doc": doc.pk})
        content = stream.call_args.kwargs["messages"][0]["content"]
        self.assertEqual((content[0]["citations"], content[0]["context"]), ({"enabled": True},
                                                                           "Resolution / approval letter"))
        self.assertIn("APPROVAL: Sign height variance (Variance) before Charles County Board of Appeals", content[1]["text"])
        doc.refresh_from_db()
        self.assertEqual(doc.summary[0]["cites"][0]["quote"], quote)
        page = self.client.get(self.variance.get_absolute_url())
        self.assertContains(page, "Claude's summary")
        self.assertContains(page, f"/entitlements/files/{doc.pk}/#page=1")

    def test_find_conditions_then_keep(self):
        doc = self.upload()
        found = {"conditions": [
            {"text": "Remove the temporary banner", "timing": "Before the sign permit is issued", "due": "",
             "quote": "1. The applicant shall remove the temporary banner before the sign permit is issued."},
            {"text": "Sign lights off 11 p.m. to 6 a.m.", "timing": "Ongoing", "due": "",
             "quote": "The sign lighting shall be turned off between 11 p.m. and 6 a.m."},
            {"text": "File the landscape plan", "timing": "", "due": "2026-12-01", "quote": "Not in the text"},
        ], "expires_on": "2027-06-30", "appeal_ends_on": "2026-11-30"}
        patcher, stream = fake_claude([text_block(json.dumps(found))])
        with patcher:
            r = self.client.post(self.variance.get_absolute_url(), {"action": "doc_conditions", "doc": doc.pk},
                                 follow=True)
        self.assertContains(r, "Claude found 3 conditions")
        self.assertContains(r, "Filled in the expiration Jun 30, 2027 and end of the appeal period Nov 30, 2026")
        kwargs = stream.call_args.kwargs
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("citations", kwargs["messages"][0]["content"][0])
        proposed = list(ApprovalCondition.objects.filter(confirmed=False).order_by("id"))
        self.assertEqual(len(proposed), 3)
        self.assertTrue(RESOLUTION[proposed[0].source_start:].startswith("1. The applicant shall remove"))
        self.assertIsNone(proposed[2].source_start)  # quote not found: unverified
        self.assertEqual(proposed[2].due_on, date(2026, 12, 1))
        self.variance.refresh_from_db()
        self.assertEqual((self.variance.expires_on, self.variance.appeal_ends_on), (date(2027, 6, 30), date(2026, 11, 30)))
        self.assertContains(r, "quote (not found in the text)")
        self.client.post(self.variance.get_absolute_url(), {"action": "condition_keep",
                                                            "condition": [proposed[0].pk, proposed[1].pk]})
        self.client.post(self.variance.get_absolute_url(), {"action": "condition_discard"})
        self.assertEqual(list(ApprovalCondition.objects.values_list("confirmed", flat=True)), [True, True])
        # Asking again doesn't repeat kept conditions.
        with patcher:
            self.client.post(self.variance.get_absolute_url(), {"action": "doc_conditions", "doc": doc.pk})
        self.assertEqual(ApprovalCondition.objects.filter(confirmed=False).count(), 1)

        patcher, _ = fake_claude([text_block("not json")])
        with patcher:
            r = self.client.post(self.variance.get_absolute_url(), {"action": "doc_conditions", "doc": doc.pk},
                                 follow=True)
        self.assertContains(r, "couldn&#x27;t be read")


class HooksTests(EntitlementBase):
    def test_reminders(self):
        ApprovalStep.objects.create(approval=self.variance, kind="hearing", body="Board of Appeals",
                                    date=self.today + timedelta(days=7))
        ApprovalStep.objects.create(approval=self.variance, kind="submitted", date=self.today - timedelta(days=2))
        ApprovalStep.objects.create(approval=self.variance, kind="comments", date=self.today - timedelta(days=9),
                                    done=True)
        lapsing = Approval.objects.create(title="Site plan", kind="site_plan", project=self.forge, status="approved",
                                          expires_on=self.today + timedelta(days=30), owner=self.user)
        ApprovalCondition.objects.create(approval=lapsing, text="Post the bond", due_on=self.today + timedelta(days=1))
        ApprovalCondition.objects.create(approval=lapsing, text="Suggested only", due_on=self.today, confirmed=False)
        notify.due_sweep(self.user)
        texts = sorted(Notification.objects.filter(user=self.user, kind="deadline").values_list("text", flat=True))
        self.assertEqual(len(texts), 4)
        self.assertTrue(any("Sign height variance: Hearing / meeting (Board of Appeals) in 7 days" in t for t in texts))
        self.assertTrue(any("Submitted was" in t and "record what happened" in t for t in texts))
        self.assertTrue(any(t.startswith("Entitlements, Site plan expires in 30 days") for t in texts))
        self.assertTrue(any("condition due in 1 day" in t and "Post the bond" in t for t in texts))
        notify.due_sweep(self.user)
        self.assertEqual(Notification.objects.filter(user=self.user, kind="deadline").count(), 4)

    def test_dashboard_campus_and_contract_hooks(self):
        hearing = self.today + timedelta(days=5)
        ApprovalStep.objects.create(approval=self.variance, kind="hearing", body="Board of Appeals", date=hearing)
        rows = dates.upcoming(14, self.today)["soon"]
        self.assertEqual([(r.tag, r.what) for r in rows], [("Entitlement", "Sign height variance")])
        self.assertContains(self.client.get("/"), "Sign height variance")
        item = campus_mod.summary(self.campus, today=self.today)["projects"][0]
        self.assertEqual(item["name"], "The Forge")
        self.assertIn(f"Sign height variance: not filed, hearing / meeting (board of appeals) {hearing:%b} {hearing.day} "
                      "(Charles County Board of Appeals)", item["facts"])
        self.assertIn("Sign height variance: hearing / meeting (board of appeals) in 5 days", item["flags"])
        site = LandSite.objects.create(name="Outside the Gate", campus=self.campus)
        psa = Contract.objects.create(title="Wamsley PSA", kind="psa", site=site, status="signed")
        Approval.objects.create(title="Rezoning to I-2", kind="rezoning", site=site, contract=psa)
        page = self.client.get(psa.get_absolute_url())
        self.assertContains(page, "Rezoning to I-2")
        self.assertContains(page, f"/entitlements/new/?contract={psa.pk}")
