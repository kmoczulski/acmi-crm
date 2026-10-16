"""Meeting transcript archive and the campus Summary tab."""
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import campus as campus_mod
from crm import meetings, transcripts
from crm.models import (
    Activity, Campus, CampusUpdate, Company, LandCounty, LandSite, Meeting, MeetingTranscript, Opportunity, Project,
)
from crm.tests.test_land import make_parcel
from crm.tests.utils import make_manager

User = get_user_model()


def granola(id_, title="Thales intro", transcript=None, attendees=None, start=None):
    return {"source": "granola", "id": id_, "title": title, "start": (start or timezone.now() - timedelta(days=2)).isoformat(),
            "summary": "### Notes\n- Power needs 20 MW", "url": f"https://notes.granola.ai/d/{id_}",
            "attendees": attendees if attendees is not None else [
                {"email": "kmoczulski@acmigroup.com", "name": "Kellen"}, {"email": "jane@thalesgroup.com", "name": "Jane"}],
            **({"transcript": transcript} if transcript is not None else {})}


class TranscriptArchiveTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(TRANSCRIPT_DIR=self.tmp.name)
        override.enable()
        self.addCleanup(override.disable)
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.thales = Company.objects.create(name="Thales", domains=["thalesgroup.com"])
        Opportunity.objects.create(name="Thales MEIH", company=self.thales, campus=self.campus)

    def test_pieces_become_speaker_lines(self):
        text = transcripts.normalize([{"speaker": "Jane", "text": "We need 20 MW.", "start": "2026-10-14T09:01:05Z"},
                                      {"speaker": "Kellen", "text": "Understood."}])
        self.assertEqual(text, "[09:01:05] Jane: We need 20 MW.\nKellen: Understood.")

    def test_import_keeps_transcripts_for_good_and_writes_txt(self):
        with self.captureOnCommitCallbacks(execute=True):
            stats = meetings.import_meetings([granola("g1", transcript="Jane: We need 20 MW of power by 2028.")])
        self.assertEqual(stats["transcripts"], 1)
        m = Meeting.objects.get(external_id="granola:g1")
        self.assertIn("20 MW", m.transcript.text)
        files = list(Path(self.tmp.name).rglob("*.txt"))
        self.assertEqual([f.parent.name for f in files], ["MEIH"])  # filed under the campus of Thales's deal
        body = files[0].read_text(encoding="utf-8")
        for part in ("Thales intro", "Attendees: Kellen <kmoczulski@acmigroup.com>", "== Notes ==", "== Transcript =="):
            self.assertIn(part, body)
        # Aged out of Granola: a later import without the transcript (or with a cut-off one) keeps the saved one.
        meetings.import_meetings([granola("g1")])
        meetings.import_meetings([granola("g1", transcript="Jane: We")])
        self.assertIn("20 MW", MeetingTranscript.objects.get().text)
        self.assertEqual(transcripts.export_all(), [])  # nothing changed: no rewrite

    def test_a_meeting_with_only_a_transcript_is_not_skipped(self):
        meetings.import_meetings([granola("g2", title="Voice memo", attendees=[], transcript="Notes to self")])
        self.assertTrue(MeetingTranscript.objects.filter(meeting__external_id="granola:g2").exists())

    def test_transcript_download_is_for_managers(self):
        meetings.import_meetings([granola("g3", transcript="Confidential numbers")])
        m = Meeting.objects.get(external_id="granola:g3")
        member = User.objects.create_user("simon")
        self.client.force_login(member)
        r = self.client.get(f"/meetings/{m.pk}/transcript.txt")
        self.assertNotContains(r, "Confidential numbers", status_code=r.status_code)
        self.assertNotContains(self.client.get(f"/meetings/{m.pk}/panel/"), "Transcript (.txt")
        self.client.force_login(make_manager(User.objects.create_user("kellen")))
        r = self.client.get(f"/meetings/{m.pk}/transcript.txt")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertIn("Confidential numbers", r.content.decode())
        self.assertContains(self.client.get(f"/meetings/{m.pk}/panel/"), "Transcript (.txt")


class CampusSummaryTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("simon", first_name="Simon"))
        self.client.force_login(self.user)
        self.meih = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.other = Campus.objects.create(code="NSIH", name="Naval Support Innovation Hub")
        self.today = timezone.localdate()
        self.ara = Company.objects.create(name="ARA")
        self.deal = Opportunity.objects.create(name="ARA building", company=self.ara, campus=self.meih,
                                               stage="loi_neg", next_step="Redlines back",
                                               next_step_due=self.today - timedelta(days=3))
        Activity.objects.create(kind=Activity.Kind.EMAIL_SENT, occurred_at=timezone.now() - timedelta(days=20),
                                company=self.ara, summary="Email to ARA: LOI draft")
        CampusUpdate.objects.create(campus=self.meih, workstream="tenant", opportunity=self.deal,
                                    text="Finished GC interviews; working on the downselect.", waiting_on="them",
                                    promised_on=self.today - timedelta(days=35), author=self.user)
        county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        self.site = LandSite.objects.create(name="Outside the Gate", status="loi", campus=self.meih)
        self.site.parcels.add(make_parcel(county, "1", -77.0, 38.4, owner_name="WAMSLEY", acres=2.0),
                              make_parcel(county, "2", -76.98, 38.4, owner_name="KROC DAVID", acres=1.0))
        self.forge = Project.objects.create(name="The Forge", campus=self.meih)
        CampusUpdate.objects.create(campus=self.meih, workstream="project", project=self.forge,
                                    text="Sign installed; variance submitted to the town.")

    def test_summary_lines_flags_and_waiting(self):
        data = campus_mod.summary(self.meih, today=self.today)
        ara = data["tenants"][0]
        self.assertEqual(ara["name"], "ARA")
        self.assertTrue(any(f.startswith("no reply in 20 days") for f in ara["flags"]))
        self.assertTrue(any("next step overdue" in f for f in ara["flags"]))
        self.assertIn("Waiting on them since", ara["waiting"])
        self.assertIn("(5 weeks ago)", ara["waiting"])
        self.assertEqual({o["owner"] for o in data["land"][0]["owners"]}, {"Wamsley", "Kroc David"})
        self.assertEqual(data["projects"][0]["update"].text, "Sign installed; variance submitted to the town.")
        text = campus_mod.email_text(data)
        for part in ("Tenants", "  - ARA (LOI / PDA negotiation): Finished GC interviews", "Land",
                     "      - Wamsley:", "Projects", "  - The Forge: Sign installed"):
            self.assertIn(part, text)

    def test_page_and_adding_updates(self):
        page = self.client.get("/campuses/meih/")
        for part in ("ARA", "no reply in 20 days", "Outside the Gate", "The Forge", "Weekly update email"):
            self.assertContains(page, part)
        self.client.post("/campuses/MEIH/", {"target": f"site:{self.site.pk}", "text": "Kroc: attorneys connecting.",
                                             "waiting_on": "third", "anchor": f"site-{self.site.pk}"})
        u = CampusUpdate.objects.get(site=self.site)
        self.assertEqual((u.workstream, u.waiting_on, u.author), ("land", "third", self.user))
        other_deal = Opportunity.objects.create(name="Elsewhere", company=self.ara, campus=self.other)
        r = self.client.post("/campuses/MEIH/", {"target": f"opp:{other_deal.pk}", "text": "x"})
        self.assertEqual(r.status_code, 404)  # can't hang an update on another campus's deal
        self.assertEqual(self.client.get("/campuses/").url, "/campuses/MEIH/")

    def test_only_author_or_manager_removes_an_update(self):
        u = CampusUpdate.objects.get(project=self.forge)  # no author
        self.client.force_login(User.objects.create_user("joe"))
        self.client.post(f"/campuses/updates/{u.pk}/delete/")
        self.assertTrue(CampusUpdate.objects.filter(pk=u.pk).exists())
        self.client.force_login(self.user)
        self.client.post(f"/campuses/updates/{u.pk}/delete/")
        self.assertFalse(CampusUpdate.objects.filter(pk=u.pk).exists())

    def test_feed_groups_the_week_by_day(self):
        days = campus_mod.feed(self.meih, self.today)
        whats = [r["what"] for d in days for r in d["rows"]]
        self.assertTrue(any("Finished GC interviews" in w for w in whats))
        self.assertFalse(any("LOI draft" in w for w in whats))  # 20 days ago: outside the week
