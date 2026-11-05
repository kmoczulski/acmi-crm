"""Spot a message that settles on a meeting time (crm/mail/meeting_hint.py)."""
from datetime import date, datetime, time

from django.test import SimpleTestCase

from crm.mail import meeting_hint as mh

SENT = datetime(2026, 11, 5, 9, 0)  # a Thursday


class FindTests(SimpleTestCase):
    def test_the_example_from_kellen(self):
        text = "Sorry. Lets shoot for 12 noon tomorrow.\n\nKellen- can you please send out a meeting planner, and include Simon."
        hint = mh.find(text, SENT)
        self.assertEqual((hint["kind"], hint["date"], hint["time"]), ("agreed", date(2026, 11, 6), time(12, 0)))
        self.assertEqual(hint["label"], "Fri, Nov 6 at 12:00 PM")
        self.assertEqual(hint["names"], ["Simon"])
        self.assertTrue(hint["request"])

    def test_a_weekday_and_a_time_in_one_sentence_is_agreed(self):
        for text, want in [("Tuesday at 2pm works for me.", (date(2026, 11, 10), time(14, 0))),
                           ("How about next Tuesday at 10:30 am for a call?", (date(2026, 11, 10), time(10, 30))),
                           ("Let's meet Nov 30 (Monday) at 2:15 PM.", (date(2026, 11, 30), time(14, 15))),
                           ("12/3 at 9am?", (date(2026, 12, 3), time(9, 0))),
                           ("See you Friday at noon.", (date(2026, 11, 6), time(12, 0))),
                           ("Today at 4 works.", (date(2026, 11, 5), time(16, 0)))]:
            hint = mh.find(text, SENT)
            self.assertEqual((hint["kind"], hint["date"], hint["time"]), ("agreed", *want), text)

    def test_relative_days_follow_when_the_message_was_sent(self):
        monday = datetime(2026, 11, 9, 8, 0)
        self.assertEqual(mh.find("Tomorrow at 3pm?", monday)["date"], date(2026, 11, 10))
        self.assertEqual(mh.find("Let's do Friday at 3pm.", monday)["date"], date(2026, 11, 13))
        self.assertEqual(mh.find("Next Tuesday at 3pm.", monday)["date"], date(2026, 11, 17))  # not the one tomorrow

    def test_offering_days_and_times_without_matching_them_up_is_an_offer(self):
        text = ("Mark and I will be traveling during part of the first week of December, but we're available Monday, "
                "November 30, or Tuesday, December 1, if you'll be in town. On either day, we're free before 11:00 AM or "
                "between 1:30 and 3:00 PM. Would either time work for you?")
        hint = mh.find(text, SENT)
        self.assertEqual(hint["kind"], "offer")
        self.assertEqual(hint["dates"], [date(2026, 11, 30), date(2026, 12, 1)])  # a weekday beside a date counts once
        self.assertEqual(hint["times"], [time(13, 30)])  # "before 11:00" is a limit and 3:00 PM the end of a range; the PM carries back
        self.assertIsNone(hint["time"])  # nothing is pre-picked: the person chooses

    def test_a_day_with_a_meeting_word_but_no_time_is_an_offer(self):
        hint = mh.find("Can we do a call on 12/3?", SENT)
        self.assertEqual((hint["kind"], hint["date"], hint["time"]), ("offer", date(2026, 12, 3), None))

    def test_asking_for_an_invite_alone_is_a_request(self):
        for text in ("Please send out a calendar invite.", "Can you set up a call?", "Kellen, send the meeting planner."):
            self.assertEqual(mh.find(text, SENT)["kind"], "request", text)

    def test_ordinary_mail_shows_nothing(self):
        for text in ("Thanks, I will review the PDA and get back to you.",
                     "We have 30 people and 12 buildings, about 1,500 sf each, due December 1.",
                     "The price is $2.50 per sf, 3 stories, 10 acres.", "Section 4.2 and 12:1 ratio", "", None):
            self.assertIsNone(mh.find(text, SENT), text)

    def test_times_and_ranges(self):
        self.assertEqual(mh.find("Wednesday from 9 to 10 AM", SENT)["time"], time(9, 0))
        self.assertEqual(mh.find("Wed 2:30 p.m.", SENT)["time"], time(14, 30))
        self.assertEqual(mh.find("Friday at 14:00", SENT)["time"], time(14, 0))
        self.assertEqual(mh.find("Friday at 2", SENT)["time"], time(14, 0))  # "at 2" in a business email is the afternoon

    def test_names_after_include_add_invite(self):
        self.assertEqual(mh.names("please include Simon and add Dave Dowell"), [("Simon", ""), ("Dave", "Dowell")])
        self.assertEqual(mh.names("send it to me and you, add Monday"), [])  # not names
