from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from crm import projects
from crm.models import Activity, Company, EmailTemplate, Person, Project


def email(kind, subject, company, thread, body="", source="gmail", **kw):
    verb = {"email_sent": "Email to", "reply": "Reply from", "email_received": "Email from"}[kind]
    return Activity.objects.create(kind=kind, occurred_at=timezone.now(), company=company, source=source,
                                   source_id=f"{thread}-{Activity.objects.count()}", thread_id=thread,
                                   summary=f"{verb} X: {subject}", body=body, **kw)


class ProjectTaggingTests(TestCase):
    def setUp(self):
        self.meih = Project.objects.create(name="MEIH", keywords=["MEIH", "Maryland Energetics"])
        self.site = Project.objects.create(name="4390 Indian Head Hwy", parent=self.meih, keywords=["4390 Indian Head"])
        self.etc = Company.objects.create(name="Energetics Technology Center")

    def test_site_conversation_tags_site_and_campus(self):
        email("email_sent", "4390 Indian Head <> Variance Signatures", self.etc, "t1")
        r = email("reply", "RE: 4390 Indian Head <> Variance Signatures", self.etc, "t1", body="Done, thanks.")
        projects.tag()
        r.refresh_from_db()
        self.assertEqual(r.context, "project")
        self.assertEqual({p.name for p in r.projects.all()}, {"MEIH", "4390 Indian Head Hwy"})

    def test_pitch_without_reply_is_prospecting(self):
        a = email("email_sent", "Interest in MEIH", self.etc, "t2")
        projects.tag()
        a.refresh_from_db()
        self.assertEqual(a.context, "prospecting")
        self.assertFalse(a.projects.exists())

    def test_campaign_and_template_threads_are_prospecting(self):
        EmailTemplate.objects.create(template_id="T1", name="t", subject="Upcoming Groundbreakings - ACMI Properties")
        email("email_sent", "Upcoming Groundbreakings - ACMI Properties", self.etc, "t3")
        r = email("reply", "RE: Upcoming Groundbreakings - ACMI Properties", self.etc, "t3", body="Tell me about MEIH")
        projects.tag()
        r.refresh_from_db()
        self.assertEqual(r.context, "prospecting")

    def test_blast_to_many_companies_is_prospecting(self):
        for i in range(8):
            c = Company.objects.create(name=f"Co {i}")
            email("email_sent", "New project announced: MEIH & Castelion", c, f"b{i}")
        r = email("reply", "RE: New project announced: MEIH & Castelion", Company.objects.get(name="Co 0"), "b0",
                  body="Congrats on MEIH")
        projects.tag()
        r.refresh_from_db()
        self.assertEqual(r.context, "prospecting")

    def test_quoted_text_and_signature_ignored(self):
        email("email_sent", "Quick question", self.etc, "t4")
        r = email("reply", "RE: Quick question", self.etc, "t4",
                  body="Sure, call me.\nBest,\nWill\n4390 Indian Head Hwy\n\nOn Mon, Kellen wrote:\n> MEIH update")
        projects.tag()
        r.refresh_from_db()
        self.assertEqual(r.context, "")

    def test_chips_on_home_and_project_page(self):
        user = get_user_model().objects.create(username="kellen")
        self.client.force_login(user)
        p = Person.objects.create(full_name="William Durant", first_name="William", company=self.etc)
        email("email_sent", "MEIH site", self.etc, "t5")
        email("reply", "RE: MEIH site", self.etc, "t5", body="Looks good.", outcome="Responded", person=p)
        projects.tag()
        self.assertContains(self.client.get("/"), 'chip-project')
        page = self.client.get(f"/projects/{self.meih.pk}/")
        self.assertContains(page, "Energetics Technology Center")
        self.assertEqual(self.client.get("/projects/").status_code, 200)
        self.assertContains(self.client.get(f"/companies/?project={self.meih.pk}"), "Energetics Technology Center")
