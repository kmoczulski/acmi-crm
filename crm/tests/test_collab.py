from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm import meetings, notify
from crm.models import (
    Activity, Comment, Company, CompanyStatus, Meeting, Notification, Opportunity, OpportunityStage, Repeat, Task,
)

User = get_user_model()

SUMMARY = """# HITT Presentation Team

- Josh Foreso, EVP
  - with HITT since 1998
- Cost: \\~$35M vs **$85-95M**

# Next Steps

- **Complete individual qualitative assessments**

  Submit before group debrief to avoid groupthink.
- **Wrap up remaining GC interviews** (Simon)

  Respond to teams after final interviews next week.
"""


class GranolaParsingTests(TestCase):
    def test_action_items(self):
        items = meetings.action_items(SUMMARY)
        self.assertEqual([i["title"] for i in items],
                         ["Complete individual qualitative assessments", "Wrap up remaining GC interviews"])
        self.assertEqual(items[1]["owner"], "Simon")
        self.assertIn("final interviews", items[1]["detail"])

    def test_markdown_is_rendered_safely(self):
        html = meetings.render_markdown(SUMMARY + "\n- <script>alert(1)</script>")
        self.assertIn("<h4>HITT Presentation Team</h4>", html)
        self.assertIn("<strong>$85-95M</strong>", html)
        self.assertIn("~$35M", html)
        self.assertNotIn("<script>", html)
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))


class MeetingTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x", first_name="Kellen", last_name="Moczulski")
        self.simon = User.objects.create_user("simon", password="x", first_name="Simon", last_name="Shewmaker")
        self.client.force_login(self.kellen)
        self.company = Company.objects.create(name="ARA", domains=["ara.com"], status=CompanyStatus.ENGAGED)
        when = timezone.now() - timedelta(days=2)
        self.data = [
            {"source": "granola", "id": "g1", "title": "HITT / ARA interview", "start": when.isoformat(),
             "url": "https://notes.granola.ai/d/g1", "summary": SUMMARY,
             "attendees": [{"email": "sshewmaker@acmigroup.com"}, {"email": "kmoczulski@acmigroup.com"},
                           {"email": "kherrle@ara.com"}, {"email": "x@hitt-gc.com"}]},
            {"source": "calendar", "id": "c1", "title": "HITT / ARA Interview", "start": (when + timedelta(minutes=5)).isoformat(),
             "attendees": [{"email": "kherrle@ara.com"}, {"email": "kmoczulski@acmigroup.com"}]},
            {"source": "calendar", "id": "c2", "title": "ARA follow-up", "start": (timezone.now() + timedelta(days=3)).isoformat(),
             "attendees": [{"email": "kherrle@ara.com"}, {"email": "kmoczulski@acmigroup.com"}]},
            {"source": "calendar", "id": "gym", "title": "Gym", "start": timezone.now().isoformat(), "attendees": []},
            {"source": "calendar", "id": "c3", "title": "Internal sync", "start": timezone.now().isoformat(),
             "attendees": [{"email": "sshewmaker@acmigroup.com"}, {"email": "kmoczulski@acmigroup.com"}]},
        ]

    def test_import_merges_calendar_into_granola_and_skips_solo_blocks(self):
        stats = meetings.import_meetings(self.data)
        self.assertEqual(Meeting.objects.count(), 3)  # g1 (+c1 merged), c2, c3; gym skipped
        self.assertEqual(stats["merged"], 1)
        g = Meeting.objects.get(external_id="granola:g1")
        self.assertEqual(g.calendar_id, "c1")
        self.assertEqual(len(g.action_items), 2)
        self.assertEqual(set(g.team.values_list("username", flat=True)), {"kellen", "simon"})
        self.assertEqual(list(g.companies.all()), [self.company])
        self.company.refresh_from_db()
        self.assertEqual(self.company.status, CompanyStatus.MEETING)
        self.assertIsNotNone(self.company.next_interaction_at)
        meetings.import_meetings(self.data)
        self.assertEqual(Meeting.objects.count(), 3)

    def test_calendar_popup_and_make_task(self):
        meetings.import_meetings(self.data)
        g = Meeting.objects.get(external_id="granola:g1")
        r = self.client.get(reverse("crm:meeting_list") + f"?month={g.start:%Y-%m}")
        self.assertContains(r, "HITT / ARA interview")
        panel = self.client.get(reverse("crm:meeting_panel", args=[g.pk]))
        self.assertContains(panel, "Wrap up remaining GC interviews")
        self.assertContains(panel, "Make task")
        self.client.post(reverse("crm:meeting_make_task", args=[g.pk]), {"index": 1})
        task = Task.objects.get()
        self.assertEqual(task.assignee, self.simon)  # owner "(Simon)" from the notes
        self.assertEqual(task.company, self.company)
        self.assertEqual(task.meeting, g)
        g.refresh_from_db()
        self.assertEqual(g.action_items[1]["task_id"], task.pk)
        self.assertTrue(Notification.objects.filter(user=self.simon, kind=Notification.Kind.ASSIGNED).exists())
        # Re-importing keeps the link to the task.
        meetings.import_meetings(self.data)
        g.refresh_from_db()
        self.assertEqual(g.action_items[1]["task_id"], task.pk)


class TaskAndCommentTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x", first_name="Kellen", last_name="Moczulski")
        self.simon = User.objects.create_user("simon", password="x", first_name="Simon", last_name="Shewmaker")
        self.client.force_login(self.kellen)
        self.company = Company.objects.create(name="Firehawk", owner=self.simon)

    def test_next_due_dates(self):
        t = Task(title="x", due_date=date(2026, 1, 30), repeat=Repeat.MONTHLY)
        self.assertEqual(t.next_due(), date(2026, 2, 28))
        t.repeat = Repeat.DAILY
        t.due_date = date(2026, 10, 16)  # Friday -> Monday
        self.assertEqual(t.next_due(), date(2026, 10, 19))
        t.repeat, t.repeat_until = Repeat.WEEKLY, date(2026, 10, 20)
        self.assertIsNone(t.next_due())

    def test_completing_a_repeating_task_creates_the_next_one(self):
        self.client.post(reverse("crm:task_add"), {
            "title": "Weekly pipeline update", "due_date": "2026-10-14", "assignee": self.simon.pk,
            "repeat": "weekly", "company": self.company.pk,
        })
        task = Task.objects.get()
        self.assertTrue(Notification.objects.filter(user=self.simon, kind="assigned").exists())
        self.client.post(reverse("crm:task_toggle", args=[task.pk]))
        nxt = Task.objects.get(done=False)
        self.assertEqual(nxt.due_date, date(2026, 10, 21))
        self.assertEqual(nxt.repeat, Repeat.WEEKLY)
        self.assertEqual(nxt.company, self.company)

    def test_task_card_page_and_edit(self):
        task = Task.objects.create(title="Call Ben", assignee=self.kellen, company=self.company)
        r = self.client.get(task.get_absolute_url())
        self.assertContains(r, "Call Ben")
        self.client.post(task.get_absolute_url(), {"title": "Call Ben Allen", "assignee": self.simon.pk, "repeat": "",
                                                   "due_date": "", "repeat_until": "", "notes": "re: PDA"})
        task.refresh_from_db()
        self.assertEqual(task.title, "Call Ben Allen")
        self.assertTrue(Notification.objects.filter(user=self.simon, kind="assigned").exists())

    def test_comment_mentions_and_owner_get_notified(self):
        dave = User.objects.create_user("dave", first_name="Dave", last_name="DeNardo")
        self.client.post(reverse("crm:comment_add"), {"company": self.company.pk, "body": "@Dave can you send the PDA?"})
        c = Comment.objects.get()
        self.assertEqual(list(c.mentions.all()), [dave])
        self.assertEqual(Notification.objects.get(user=dave).kind, "mention")
        self.assertEqual(Notification.objects.get(user=self.simon).kind, "comment")  # company owner
        self.assertFalse(Notification.objects.filter(user=self.kellen).exists())  # not your own comment
        r = self.client.get(self.company.get_absolute_url())
        self.assertContains(r, '<span class="mention">@Dave</span>', html=False)

    def test_task_comment_notifies_assignee(self):
        task = Task.objects.create(title="Call Ben", assignee=self.simon, company=self.company)
        self.client.post(reverse("crm:comment_add"), {"task": task.pk, "body": "Left a voicemail"})
        n = Notification.objects.get(user=self.simon)
        self.assertIn("Left a voicemail", n.text)
        self.assertEqual(Comment.objects.get().company, self.company)

    def test_due_sweep_once_a_day_and_bell(self):
        Task.objects.create(title="Send LOI", assignee=self.kellen, due_date=timezone.localdate() - timedelta(days=2))
        self.client.get(reverse("crm:home"))
        self.client.get(reverse("crm:home"))
        self.assertEqual(Notification.objects.filter(user=self.kellen, kind="due").count(), 1)
        r = self.client.get(reverse("crm:notification_list"))
        self.assertContains(r, "Overdue 2 days: Send LOI")
        n = Notification.objects.get()
        self.client.get(reverse("crm:notification_open", args=[n.pk]))
        n.refresh_from_db()
        self.assertIsNotNone(n.read_at)

    def test_stage_change_and_reply_notifications(self):
        opp = Opportunity.objects.create(name="Firehawk · TBD", company=self.company, owner=self.simon,
                                         stage=OpportunityStage.QUALIFIED)
        self.client.post(reverse("crm:opportunity_set_stage", args=[opp.pk]), {"stage": OpportunityStage.LOI_NEGOTIATION})
        self.assertTrue(Notification.objects.filter(user=self.simon, kind="stage").exists())
        Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=timezone.now(), company=self.company,
                                summary="Reply from Ben: Re: PDA", source=Activity.Source.GMAIL, source_id="r1")
        self.assertTrue(Notification.objects.filter(user=self.simon, kind="reply").exists())
        Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=timezone.now() - timedelta(days=30), company=self.company,
                                summary="old", source=Activity.Source.GMAIL, source_id="r2")
        self.assertEqual(Notification.objects.filter(user=self.simon, kind="reply").count(), 1)

    def test_top_menus(self):
        r = self.client.get(reverse("crm:home"))
        for label in (">Home<", "Possible duplicates", ">Tenant pipeline<", ">Calendar<", "Needs filing", ">Sites<", ">Map<",
                      ">Companies<", "Rent roll"):
            self.assertContains(r, label)


class DashboardTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("kellen", password="x", first_name="Kellen", last_name="Moczulski")
        self.simon = User.objects.create_user("simon", password="x", first_name="Simon")
        self.client.force_login(self.kellen)
        self.company = Company.objects.create(name="ARA")

    def test_week_calendar_shows_my_meetings_and_toggles(self):
        today = timezone.localtime().replace(hour=10, minute=0)
        mine = Meeting.objects.create(external_id="calendar:a", source="calendar", title="ARA design review", start=today)
        mine.team.add(self.kellen)
        mine.companies.add(self.company)
        other = Meeting.objects.create(external_id="calendar:b", source="calendar", title="Simon only", start=today)
        other.team.add(self.simon)
        r = self.client.get(reverse("crm:home"))
        self.assertContains(r, "This week")
        self.assertContains(r, "ARA design review")
        self.assertNotContains(r, "Simon only")
        r = self.client.get(reverse("crm:home") + "?cal=all")
        self.assertContains(r, "Simon only")
        r = self.client.get(reverse("crm:home") + "?week=1")
        self.assertContains(r, "Next week")
        self.assertNotContains(r, "ARA design review")

    def test_recent_activity_is_last_7_days_with_show_more(self):
        now = timezone.now()
        Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=now - timedelta(days=2), company=self.company,
                                summary="Reply from Ken: this week", source=Activity.Source.GMAIL, source_id="a")
        Activity.objects.create(kind=Activity.Kind.REPLY, occurred_at=now - timedelta(days=10), company=self.company,
                                summary="Reply from Ken: last week", source=Activity.Source.GMAIL, source_id="b")
        r = self.client.get(reverse("crm:home"))
        self.assertContains(r, "this week")
        self.assertNotContains(r, "Reply from Ken: last week")
        self.assertContains(r, "Show 7 more days")
        r = self.client.get(reverse("crm:home") + "?days=14")
        self.assertContains(r, "Reply from Ken: last week")
        self.assertContains(r, "last 14 days")


class TaskTimeAndLiveAlertTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user("kellen", first_name="Kellen", password="x")
        self.client.force_login(self.user)

    def test_task_time_reminders_and_the_poll_endpoint(self):
        from datetime import datetime, time, timedelta

        from django.utils import timezone

        from crm import notify
        from crm.models import Notification, Task

        now = timezone.localtime()
        soon = (now + timedelta(minutes=10)).time().replace(second=0, microsecond=0)
        t = Task.objects.create(title="Call Dave", due_date=now.date(), due_time=soon, assignee=self.user)
        if soon < now.time():  # the 10 minutes ran past midnight
            return
        first = self.client.get("/notifications/poll/").json()
        self.assertEqual(first["items"], [])  # nothing replayed without ?after=
        n = Notification.objects.get(user=self.user)
        self.assertIn("Due at", n.text)
        notify.timed_sweep(self.user, now=timezone.make_aware(datetime.combine(now.date(), soon)) + timedelta(minutes=1))
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 2)
        notify.timed_sweep(self.user, now=timezone.make_aware(datetime.combine(now.date(), soon)) + timedelta(minutes=2))
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 2)  # keyed: no repeats
        poll = self.client.get(f"/notifications/poll/?after={n.pk}").json()
        self.assertEqual(poll["unread"], 2)
        self.assertEqual(len(poll["items"]), 1)
        self.assertIn("Due now", poll["items"][0]["text"])
        self.assertTrue(t.is_overdue is False or t.due_time < timezone.localtime().time())

    def test_task_form_takes_a_time(self):
        from crm.models import Task

        self.client.post("/tasks/add/", {"title": "Site visit", "due_date": "2026-11-05", "due_time": "09:15",
                                         "assignee": self.user.pk})
        t = Task.objects.filter(title="Site visit").first()
        self.assertIsNotNone(t)
        self.assertEqual(f"{t.due_time:%H:%M}", "09:15")
        self.assertContains(self.client.get(t.get_absolute_url()), "9:15 AM")

    def test_alert_script_is_on_every_page(self):
        r = self.client.get("/notifications/")
        self.assertContains(r, "alerts.js")
        self.assertContains(r, "Notification settings")  # browser notifications are switched on there (crm/push.py)
