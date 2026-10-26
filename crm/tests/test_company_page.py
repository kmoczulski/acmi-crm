from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from crm import meetings
from crm.models import Activity, Company, Meeting, Opportunity, OpportunityStage, Task

User = get_user_model()

SUMMARY = """# Program fit

- Wants 40 acres near Huntsville
- Budget **$60M**

# Next Steps

- **Send the Fayetteville site brief** (Kellen)

  Include the QD arcs.
- **Intro to the county EDC** (Simon)
"""


class CompanyPageTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x", first_name="Kellen")
        self.simon = User.objects.create_user("simon", password="x", first_name="Simon")
        self.client.force_login(self.kellen)
        self.co = Company.objects.create(name="Acme Energetics")
        now = timezone.now()
        self.held = Meeting.objects.create(
            source="granola", external_id="granola:1", title="Acme intro", start=now - timedelta(days=2), summary=SUMMARY,
            action_items=meetings.action_items(SUMMARY), attendees=[{"name": "Ann Smith", "email": "ann@acme.test"}])
        self.held.companies.add(self.co)
        self.next = Meeting.objects.create(source="calendar", external_id="calendar:2", title="Acme site visit",
                                           start=now + timedelta(days=5))
        self.next.companies.add(self.co)
        Opportunity.objects.create(name="Acme Huntsville", company=self.co, stage=OpportunityStage.values[0])
        self.url = reverse("crm:company_detail", args=[self.co.pk])

    def test_the_overview_is_one_feed_newest_first_with_the_rest_as_widgets(self):
        """Kellen, 2026-10-26: "more like Trello, where comments and meeting updates/notes are in chronological
        order." Comments, notes, meetings (with their notes and action items) and email share one list."""
        from crm.models import Comment, Note

        now = timezone.now()
        Note.objects.create(body="Owner wants to close by spring", author=self.simon, company=self.co)
        Comment.objects.create(body="Called Ann, she liked the brief", author=self.kellen, company=self.co)
        Activity.objects.create(kind=Activity.Kind.EMAIL_RECEIVED, occurred_at=now - timedelta(days=1), company=self.co,
                                summary="Re: site brief", body="Thanks, looks good", source="gmail")
        # The import also writes a meeting as an activity row; the feed shows the meeting itself, once.
        Activity.objects.create(kind=Activity.Kind.MEETING, occurred_at=self.held.start, company=self.co, summary="Acme intro")
        r = self.client.get(self.url)
        self.assertContains(r, 'data-layout="company"')
        for widget in ("facts", "feed", "details", "meetings", "actions", "people", "deals"):
            self.assertContains(r, f'data-widget="{widget}"')
        feed = r.content.decode().split('id="comments"', 1)[1].split("Full timeline", 1)[0]
        order = [feed.index(s) for s in ("Acme site visit", "Called Ann, she liked", "Owner wants to close",
                                         "Re: site brief", "Acme intro")]
        self.assertEqual(order, sorted(order))  # upcoming meeting, then the comment, the note, yesterday's email, the held meeting
        self.assertEqual(feed.count('data-kind="meeting"'), 2)
        self.assertEqual(feed.count('data-kind="email"'), 1)
        self.assertIn("Send the Fayetteville site brief", feed)  # the meeting's action items ride along
        self.assertContains(r, 'data-feed-filter="comment note"')
        self.assertContains(r, "Customize page")

    def test_the_layout_is_the_persons_own_and_separate_from_the_dashboard(self):
        import json

        self.client.post(reverse("crm:prefs_save"), {"key": "company", "value": json.dumps(
            [{"id": "feed", "w": 12}, {"id": "details", "w": 4, "h": 300}, {"id": "nope", "w": 4}])})
        r = self.client.get(self.url)
        self.assertEqual([(w["id"], w["w"], w["h"]) for w in r.context["grid"]["layout"]], [("feed", 12, 0), ("details", 4, 300)])
        self.assertNotContains(r, 'data-widget="meetings"')
        r = self.client.get(self.url, {"customize": "1"})
        self.assertContains(r, "Customizing your page")
        self.assertContains(r, 'data-add-widget="meetings"')
        self.assertContains(r, 'data-add-widget="feed" disabled')
        self.assertEqual(self.client.get(reverse("crm:home")).context["layout"][0]["id"], "kpis")  # untouched
        self.client.post(reverse("crm:prefs_save"), {"key": "company", "value": "null"})
        self.assertEqual(len(self.client.get(self.url).context["grid"]["layout"]), 12)

    def test_the_overview_widget_leads_the_page_and_formats_the_description(self):
        """Kellen, 2026-10-26: "the company overview section should be more apparent and have proper formatting"."""
        self.co.description = "Employee-owned since 1979.\n\n# What they do\n- Energetics research\n- **Blast** modelling"
        self.co.website, self.co.hq_location, self.co.categories = "https://www.acme.test", "Huntsville, AL", ["R&D"]
        self.co.save()
        r = self.client.get(self.url)
        self.assertEqual([w["id"] for w in r.context["grid"]["layout"]][:2], ["facts", "overview"])
        html = r.content.decode().split('data-widget="overview"', 1)[1].split('data-widget="feed"', 1)[0]
        self.assertIn("<p>Employee-owned since 1979.</p>", html)
        self.assertIn("<h4>What they do</h4>", html)
        self.assertIn("<li><strong>Blast</strong> modelling</li>", html)
        for text in ("acme.test", "Huntsville, AL", "R&amp;D"):
            self.assertIn(text, html)
        self.assertEqual(r.content.decode().count('id="edit-dialog"'), 1)  # one dialog, opened from any widget
        self.co.description = ""
        self.co.save()
        self.assertContains(self.client.get(self.url), "No description yet")
        self.assertContains(self.client.get(self.url, {"tab": "people"}), "<dt>Description</dt>", count=0)

    def test_the_other_tabs_keep_the_details_column(self):
        r = self.client.get(self.url, {"tab": "people"})
        self.assertContains(r, "Team connections")
        self.assertContains(r, 'id="edit-dialog"')
        self.assertNotContains(r, 'data-layout="company"')

    def test_overview_shows_meeting_summary_and_action_items_in_place(self):
        r = self.client.get(self.url)
        self.assertContains(r, "Acme intro")
        self.assertContains(r, "Wants 40 acres near Huntsville")  # the summary itself, not just a link
        self.assertContains(r, "<strong>$60M</strong>", html=True)
        self.assertContains(r, "Send the Fayetteville site brief")
        self.assertContains(r, "Make task")
        self.assertContains(r, "Acme site visit")  # the next meeting leads
        self.assertContains(r, "Upcoming")
        self.assertContains(r, "Acme Huntsville")  # open deals
        self.assertEqual(len(r.context["open_items"]), 2)
        # "Next Steps" is listed as action items, so it isn't repeated inside the summary.
        card = r.context["latest_meetings"][1]
        self.assertNotIn("Next Steps", card["summary_html"])
        self.assertIn("Program fit", card["summary_html"])

    def test_make_task_returns_to_the_company_page_and_marks_the_item(self):
        r = self.client.post(reverse("crm:meeting_make_task", args=[self.held.pk]),
                             {"index": 1, "next": self.url + "?tab=actions"})
        self.assertRedirects(r, self.url + "?tab=actions", fetch_redirect_response=False)
        task = Task.objects.get()
        self.assertEqual((task.assignee, task.company, task.meeting), (self.simon, self.co, self.held))
        r = self.client.get(self.url + "?tab=actions")
        self.assertEqual(len(r.context["open_items"]), 2)  # a task that isn't done keeps the item open
        self.assertEqual(r.context["other_tasks"], [])  # and the task isn't listed twice
        task.done = True
        task.save()
        r = self.client.get(self.url + "?tab=actions")
        self.assertEqual([i["title"] for i in r.context["open_items"]], ["Send the Fayetteville site brief"])
        self.assertEqual(len(r.context["done_items"]), 1)

    def test_make_task_form_on_the_company_page_comes_back_here(self):
        r = self.client.get(self.url + "?tab=actions")
        self.assertContains(r, f'name="next" value="{escape(self.url + "?tab=actions")}"')

    def test_make_task_ignores_an_offsite_next(self):
        r = self.client.post(reverse("crm:meeting_make_task", args=[self.held.pk]), {"index": 0, "next": "//evil.test/"})
        self.assertEqual(r.url, self.held.get_absolute_url())

    def test_meetings_tab_lists_every_meeting(self):
        r = self.client.get(self.url + "?tab=meetings")
        self.assertEqual([c["m"] for c in r.context["meeting_cards"]], [self.next, self.held])

    def test_activity_tab_filters_by_kind(self):
        now = timezone.now()
        Activity.objects.create(company=self.co, kind=Activity.Kind.EMAIL_RECEIVED, summary="Re: site brief", occurred_at=now)
        Activity.objects.create(company=self.co, kind=Activity.Kind.NOTE, summary="Called Ann", occurred_at=now)
        r = self.client.get(self.url + "?tab=activity&kind=email")
        self.assertContains(r, "Re: site brief")
        self.assertNotContains(r, "Called Ann")
        chips = {key: n for key, _, n in r.context["activity_chips"]}
        self.assertEqual((chips[""], chips["email"], chips["notes"]), (2, 1, 1))
        r = self.client.get(self.url + "?tab=activity")
        self.assertContains(r, "Called Ann")

    def test_meeting_popup_still_offers_make_task(self):
        r = self.client.get(reverse("crm:meeting_panel", args=[self.held.pk]))
        self.assertContains(r, "Make task")
        self.assertContains(r, f'name="next" value="{escape(self.held.get_absolute_url())}"')


class SummaryWithoutActionsTests(TestCase):
    def test_drops_only_the_next_steps_section(self):
        text = meetings.summary_without_actions(SUMMARY + "\n# Risks\n\n- Power\n")
        self.assertIn("Program fit", text)
        self.assertIn("Risks", text)
        self.assertNotIn("Fayetteville", text)
