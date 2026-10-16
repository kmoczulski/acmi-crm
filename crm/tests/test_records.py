from django.contrib.auth import get_user_model
from django.test import TestCase

from crm.models import Campus, Company, FieldHistory


class FitAndCampusTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="kellen")
        self.client.force_login(self.user)
        self.nsih = Campus.objects.create(code="NSIH", name="National Security Industrial Hub")
        self.company = Company.objects.create(name="Firehawk Aerospace")

    def save(self, **fields):
        data = {"name": self.company.name, "status": "prospect", "domains": "", "flags": "", "categories": ""}
        data.update(fields)
        return self.client.post(f"/companies/{self.company.pk}/", data)

    def test_fit_score_capped_at_five(self):
        r = self.save(fit_score="6")
        self.assertEqual(r.status_code, 200)  # form re-shown with an error
        self.company.refresh_from_db()
        self.assertIsNone(self.company.fit_score)
        self.save(fit_score="5")
        self.company.refresh_from_db()
        self.assertEqual(self.company.fit_score, 5)
        self.assertContains(self.client.get(f"/companies/{self.company.pk}/"), "5 / 5")
        self.assertTrue(FieldHistory.objects.filter(field="fit_score").exists())

    def test_campus_choices(self):
        self.save(campus_choice=["NSIH"])  # ticking a campus means "an ACMI campus"
        self.company.refresh_from_db()
        self.assertEqual((self.company.campus_plan, self.company.campus_label), ("acmi", "NSIH"))
        self.save(campus_choice=["own"])
        self.company.refresh_from_db()
        self.assertEqual(self.company.campus_label, "Their own campus")
        r = self.save(campus_choice=["none", "NSIH"])
        self.assertContains(r, "Pick ACMI campuses, or Own campus, or None.")
        self.save(campus_choice=[])
        self.company.refresh_from_db()
        self.assertEqual(self.company.campus_label, "")

    def test_filters(self):
        Company.objects.create(name="Other", fit_score=2, campus_plan="none")
        self.company.fit_score = 4
        self.company.campus_plan = "own"
        self.company.save()
        r = self.client.get("/companies/?fit=4&campus=own")
        self.assertContains(r, "Firehawk Aerospace")
        self.assertNotContains(r, ">Other<")


class ReplyGistTests(TestCase):
    def test_gist_drops_greeting_quote_and_signature(self):
        from crm.models import Person
        from crm.views import reply_gist

        p = Person(first_name="William", last_name="Durant", full_name="William Durant")
        body = "Hi Kellen,\nDone, thanks.\nWill Durant, PMP CEO & President\nMOBILE: 251-776-4400\n\nOn Mon, Kellen wrote:\n> old"
        self.assertEqual(reply_gist(body, p), "Done, thanks.")
        self.assertEqual(reply_gist("We\u2019re at booth 4303.\n\nBen Allen\n(469) 669-5003"), "We\u2019re at booth 4303.")


class AISummaryTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        from crm.models import Activity

        self.user = get_user_model().objects.create(username="kellen", first_name="Kellen")
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="Firehawk Aerospace")
        Activity.objects.create(kind="reply", occurred_at=timezone.now(), company=self.company,
                                summary="Reply from Ben Allen: Re: PDA", body="Looks good, send the draft.")

    def test_panel_explains_setup_without_key(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            page = self.client.get(f"/companies/{self.company.pk}/")
        self.assertContains(page, "AI summaries are off")

    def test_generate_and_show_summary(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        payload = {"headline": "PDA under review with Firehawk.", "history": "Met at AUSA.",
                   "recent": ["Ben Allen asked for the draft PDA"], "documents": "PDA sent Oct 13.",
                   "next_step": "Follow up on PDA comments."}
        fake = MagicMock()
        fake.beta.messages.create.return_value = SimpleNamespace(
            stop_reason="end_turn", model="claude-opus-5-5",
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        )
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), patch("anthropic.Anthropic", return_value=fake):
            r = self.client.post(f"/summary/company/{self.company.pk}/")
            self.assertRedirects(r, f"/companies/{self.company.pk}/")
            page = self.client.get(f"/companies/{self.company.pk}/")
        self.assertContains(page, "PDA under review with Firehawk.")
        self.assertContains(page, "Follow up on PDA comments.")
        sent = fake.beta.messages.create.call_args.kwargs
        self.assertEqual(sent["model"], "claude-opus-5-5")
        self.assertIn("Looks good, send the draft.", sent["messages"][0]["content"])
        self.assertEqual(sent["fallbacks"], "default")

    def test_refusal_shows_message(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        fake = MagicMock()
        fake.beta.messages.create.return_value = SimpleNamespace(stop_reason="refusal", content=[], model="x")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}), patch("anthropic.Anthropic", return_value=fake):
            r = self.client.post(f"/summary/company/{self.company.pk}/", follow=True)
        self.assertContains(r, "declined to summarize")


class MergeTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        from crm.models import Activity, Attachment, Person

        self.user = get_user_model().objects.create(username="kellen")
        self.client.force_login(self.user)
        self.keep = Company.objects.create(name="L3Harris Technologies", domains=["l3harris.com"], priority="Mid")
        self.dup = Company.objects.create(name="L3Harris", domains=["l3harris.com", "harris.com"], priority="High",
                                          sector="defense")
        self.other = Company.objects.create(name="Aerojet Rocketdyne", domains=["l3harris.com"])
        self.p = Person.objects.create(email="a@l3harris.com", company=self.dup)
        now = timezone.now()
        a1 = Activity.objects.create(kind="email_sent", occurred_at=now, summary="x", company=self.keep,
                                     source="gmail", source_id="m|k", message_id="<m@x>")
        a2 = Activity.objects.create(kind="email_sent", occurred_at=now, summary="x", company=self.dup,
                                     source="gmail", source_id="m|d", message_id="<m@x>")
        Attachment.objects.create(activity=a2, filename="PDA.pdf", category="PDA")
        Activity.objects.create(kind="reply", occurred_at=now, summary="y", company=self.dup, source="gmail",
                                source_id="r|d", message_id="<r@x>")

    def test_suggestions_pre_tick_only_name_variants(self):
        from crm import merge

        group = next(g for g in merge.suggestions() if self.keep.pk in [c["pk"] for c in g["companies"]])
        ticks = {c["name"]: c["suggest"] for c in group["companies"]}
        self.assertTrue(ticks["L3Harris"])
        self.assertFalse(ticks["Aerojet Rocketdyne"])

    def test_merge_moves_everything_once(self):
        from crm.models import Activity, Attachment, Person

        self.client.post("/companies/duplicates/merge/", {"keep": self.keep.pk, "merge": [self.dup.pk]})
        self.keep.refresh_from_db()
        self.assertFalse(Company.objects.filter(pk=self.dup.pk).exists())
        self.assertEqual(Person.objects.get(pk=self.p.pk).company, self.keep)
        self.assertEqual(self.keep.domains, ["l3harris.com", "harris.com"])
        self.assertEqual((self.keep.priority, self.keep.sector), ("High", "defense"))
        self.assertEqual(Activity.objects.filter(message_id="<m@x>").count(), 1)  # duplicate email dropped
        self.assertEqual(Attachment.objects.get().activity.company, self.keep)  # its attachment kept
        self.assertTrue(Activity.objects.filter(message_id="<r@x>", company=self.keep).exists())

    def test_not_duplicates_hides_group(self):
        from crm import merge

        group = next(g for g in merge.suggestions() if self.keep.pk in [c["pk"] for c in g["companies"]])
        self.client.post("/companies/duplicates/dismiss/", {"key": group["key"]})
        self.assertFalse(any(self.keep.pk in [c["pk"] for c in g["companies"]] for g in merge.suggestions()))
        self.assertEqual(self.client.get("/companies/duplicates/").status_code, 200)

    def test_merge_from_company_page(self):
        self.client.post(f"/companies/{self.keep.pk}/merge/", {"other": "l3harris"})
        self.assertFalse(Company.objects.filter(pk=self.dup.pk).exists())
