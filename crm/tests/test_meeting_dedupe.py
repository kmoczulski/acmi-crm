from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from crm import meetings
from crm.models import Activity, Company, Meeting, Task

User = get_user_model()

SUMMARY = "# Notes\n\n- Talked scope\n\n# Next Steps\n\n- **Send the deck** (Kellen)\n"


class CalendarFirstMergeTests(TestCase):
    """The calendar sync runs every 15 minutes, so the calendar event usually lands before the Granola note."""

    def setUp(self):
        User.objects.create_user("kellen", password="x", first_name="Kellen", last_name="Moczulski",
                                 email="kmoczulski@acmigroup.com")
        self.co = Company.objects.create(name="Booz Allen", domains=["bah.com"])
        when = timezone.now() - timedelta(days=1)
        people = [{"email": "kmoczulski@acmigroup.com"}, {"email": "smith_j@bah.com"}]
        self.cal = {"source": "calendar", "id": "evt1", "title": "Booz Allen / ACMI intro", "start": when.isoformat(),
                    "end": (when + timedelta(minutes=30)).isoformat(), "url": "https://calendar.google.com/e",
                    "attendees": people}
        self.note = {"source": "granola", "id": "n1", "title": "Booz Allen / ACMI Intro", "start": when.isoformat(),
                     "url": "https://notes.granola.ai/d/n1", "summary": SUMMARY, "attendees": people}

    def test_note_takes_over_the_calendar_row(self):
        meetings.import_meetings([self.cal])
        cal_row = Meeting.objects.get()
        task = Task.objects.create(title="Prep", meeting=cal_row)
        meetings.import_meetings([self.note])
        m = Meeting.objects.get()
        self.assertEqual((m.pk, m.source, m.external_id, m.calendar_id), (cal_row.pk, "granola", "granola:n1", "evt1"))
        self.assertEqual(m.url, "https://notes.granola.ai/d/n1")
        self.assertEqual(len(m.action_items), 1)
        self.assertEqual(list(m.companies.all()), [self.co])
        self.assertEqual(Task.objects.get(pk=task.pk).meeting, m)
        # One timeline entry, Granola's.
        self.assertEqual(list(Activity.objects.values_list("source", flat=True)), [Activity.Source.GRANOLA])

    def test_later_calendar_syncs_keep_granola_title_link_and_single_entry(self):
        meetings.import_meetings([self.note])
        meetings.import_meetings([self.cal])
        meetings.import_meetings([self.cal])
        m = Meeting.objects.get()
        self.assertEqual((m.title, m.url, m.calendar_id), ("Booz Allen / ACMI Intro", "https://notes.granola.ai/d/n1", "evt1"))
        self.assertEqual(Activity.objects.count(), 1)

    def test_merge_command_folds_existing_duplicates(self):
        # How the rows looked before the fix: a calendar meeting and a separate Granola meeting, two timeline entries.
        cal = Meeting.objects.create(source="calendar", external_id="calendar:evt1", title="Booz Allen / ACMI intro",
                                     start=timezone.now(), end=timezone.now() + timedelta(minutes=30))
        cal.companies.add(self.co)
        note = Meeting.objects.create(source="granola", external_id="granola:n1", title="Booz Allen / ACMI Intro",
                                      start=timezone.now(), summary=SUMMARY)
        Task.objects.create(title="Prep", meeting=cal)
        for src, sid in ((Activity.Source.CALENDAR, "evt1:1"), (Activity.Source.GRANOLA, "n1:1")):
            Activity.objects.create(company=self.co, kind=Activity.Kind.MEETING, source=src, source_id=sid,
                                    summary="Booz Allen / ACMI intro", occurred_at=timezone.now())
        out = StringIO()
        call_command("merge_duplicate_meetings", stdout=out)
        self.assertIn("Would merge 1", out.getvalue())
        self.assertEqual(Meeting.objects.count(), 2)  # dry run changes nothing
        call_command("merge_duplicate_meetings", "--apply", stdout=StringIO())
        m = Meeting.objects.get()
        self.assertEqual((m.pk, m.calendar_id), (note.pk, "evt1"))
        self.assertIsNotNone(m.end)
        self.assertEqual(list(m.companies.all()), [self.co])
        self.assertEqual(Task.objects.get().meeting, m)
        self.assertEqual(list(Activity.objects.values_list("source", flat=True)), [Activity.Source.GRANOLA])

    def test_different_meetings_at_the_same_time_stay_apart(self):
        meetings.import_meetings([{**self.cal, "title": "Dentist with Jo"}])
        meetings.import_meetings([self.note])
        self.assertEqual(Meeting.objects.count(), 2)
