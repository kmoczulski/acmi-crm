"""Changing a contract's effective date moves the critical dates that count from it
(crm/contracts/timeline.py recompute_dates, run when the contract's details are saved)."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from crm.contracts import timeline
from crm.models import Contract, ContractDate, LandSite
from crm.tests.utils import make_manager

User = get_user_model()

OLD, NEW = date(2026, 3, 2), date(2026, 4, 1)  # a Monday and a Wednesday


class RecalcTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen", last_name="Moczulski"))
        self.client.force_login(self.user)
        self.c = Contract.objects.create(title="Wamsley PSA", kind="psa", status="negotiating", owner=self.user,
                                         effective_date=OLD)
        # As they stand once Claude has read the agreement with an effective date of March 2.
        self.feas = self.add("Feasibility ends", "feasibility", date(2026, 5, 1), key="feasibility_end",
                             anchor="effective", offset=60, unit="days")
        self.close = self.add("Closing", "closing", date(2026, 5, 31), key="closing", anchor="feasibility_end",
                              offset=30, unit="days")
        self.deposit = self.add("Deposit due", "deposit", date(2026, 3, 5), key="deposit", anchor="effective",
                                offset=3, unit="business")
        self.title = self.add("Title objections", "title", date(2026, 4, 16), key="title_objection",
                              anchor="effective", offset=45, unit="days", confirmed=False, from_claude=True)
        self.outside = self.add("Outside date", "other", date(2026, 12, 31))  # fixed on the calendar: no rule

    def add(self, label, kind, when, **kw):
        return ContractDate.objects.create(contract=self.c, label=label, kind=kind, date=when, **kw)

    def save_details(self, **data):
        """The details form, posted the way the contract page posts it (it replaces every field it carries)."""
        post = {"action": "details", "title": "Wamsley PSA", "kind": "psa", "status": "negotiating",
                "effective_date": OLD.isoformat(), **data}
        return self.client.post(self.c.get_absolute_url(), post, follow=True)

    def on(self, d):
        d.refresh_from_db()
        return d.date

    def said(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_a_new_effective_date_moves_the_dates_that_count_from_it(self):
        r = self.save_details(effective_date=NEW.isoformat())
        self.assertEqual(self.on(self.feas), date(2026, 5, 31))  # 60 days after April 1
        self.assertEqual(self.on(self.close), date(2026, 6, 30))  # 30 days after the new end of feasibility
        self.assertEqual(self.on(self.deposit), date(2026, 4, 6))  # 3 business days after a Wednesday
        self.assertEqual(self.on(self.title), date(2026, 5, 16))  # a suggestion nobody has kept yet moves too
        self.assertEqual(self.on(self.outside), date(2026, 12, 31))  # no rule: it stays
        self.assertEqual(self.said(r), ["Saved. Recalculated 4 critical dates from the new effective date."])
        move = self.feas.history[-1]  # the move is on the date, to see what it was
        self.assertEqual((move["from"], move["to"], move["by"], move["auto"]),
                         ("2026-05-01", "2026-05-31", "Kellen Moczulski", True))
        self.assertEqual(move["why"], "Effective date changed from Mar 2, 2026 to Apr 1, 2026")
        self.assertContains(r, "Moved 1×")  # and the contract page shows it under the date
        self.assertContains(r, "Effective date changed from Mar 2, 2026 to Apr 1, 2026")
        self.assertEqual(self.outside.history, [])

    def test_saving_with_the_same_effective_date_leaves_every_date_alone(self):
        ContractDate.objects.filter(pk=self.close.pk).update(date=date(2026, 6, 15))  # somebody set it by hand
        r = self.save_details(next_step="Call the title company")
        self.assertEqual(self.on(self.close), date(2026, 6, 15))
        self.assertEqual(self.on(self.feas), date(2026, 5, 1))
        self.assertEqual(self.said(r), ["Saved."])

    def test_dates_that_waited_for_an_effective_date_fill_in(self):
        Contract.objects.filter(pk=self.c.pk).update(effective_date=None)
        rules = (self.feas, self.close, self.deposit, self.title)
        ContractDate.objects.filter(pk__in=[d.pk for d in rules]).update(date=None)  # Claude couldn't work them out
        r = self.save_details(effective_date=NEW.isoformat())
        self.assertEqual([self.on(d) for d in rules],
                         [date(2026, 5, 31), date(2026, 6, 30), date(2026, 4, 6), date(2026, 5, 16)])
        self.assertEqual(self.feas.history[-1]["from"], None)
        self.assertEqual(self.said(r), ["Saved. Recalculated 4 critical dates from the new effective date."])

    def test_done_and_pushed_dates_stay_and_the_rest_count_from_where_they_stand(self):
        ContractDate.objects.filter(pk=self.deposit.pk).update(done=True)  # the deposit was paid
        # An extension pushed feasibility out, and closing with it.
        ContractDate.objects.filter(pk=self.feas.pk).update(date=date(2026, 6, 15), history=[
            {"from": "2026-05-01", "to": "2026-06-15", "why": "Extension exercised", "by": "Kellen",
             "on": "2026-04-20"}])
        ContractDate.objects.filter(pk=self.close.pk).update(date=date(2026, 7, 15), history=[
            {"from": "2026-05-31", "to": "2026-07-15", "why": "Extension exercised (counts from Feasibility ends)",
             "by": "Kellen", "on": "2026-04-20"}])
        possession = self.add("Possession", "other", None, key="possession", anchor="closing", offset=10, unit="days")
        r = self.save_details(effective_date=NEW.isoformat())
        self.assertEqual(self.on(self.deposit), date(2026, 3, 5))
        self.assertEqual(self.on(self.feas), date(2026, 6, 15))
        self.assertEqual(self.on(self.close), date(2026, 7, 15))
        self.assertEqual(self.on(possession), date(2026, 7, 25))  # 10 days after closing where it stands now
        self.assertEqual(self.on(self.title), date(2026, 5, 16))  # nobody touched this one: it follows
        self.assertEqual(self.said(r), ["Saved. Recalculated 2 critical dates from the new effective date. "
                                        "3 stay where they are (done, or moved by hand)."])

    def test_a_second_change_moves_them_again(self):
        self.save_details(effective_date=NEW.isoformat())
        self.save_details(effective_date="2026-05-01")
        self.assertEqual(self.on(self.feas), date(2026, 6, 30))
        self.assertEqual(self.on(self.close), date(2026, 7, 30))
        self.assertEqual(len(self.feas.history), 2)  # the moves the app made don't count as moved by hand

    def test_clearing_the_effective_date_leaves_the_dates_where_they_are(self):
        r = self.save_details(effective_date="")
        self.c.refresh_from_db()
        self.assertIsNone(self.c.effective_date)
        self.assertEqual(self.on(self.feas), date(2026, 5, 1))
        self.assertEqual(self.said(r), ["Saved."])

    def test_a_suggestion_with_a_kept_dates_key_does_not_move_the_kept_date(self):
        twin = self.add("Closing (another reading)", "closing", date(2026, 6, 30), key="closing", anchor="effective",
                        offset=120, unit="days", confirmed=False, from_claude=True)
        self.save_details(effective_date=NEW.isoformat())
        self.assertEqual(self.on(self.close), date(2026, 6, 30))  # the kept date follows its own rule
        self.assertEqual(self.on(twin), date(2026, 7, 30))  # and the suggestion follows its own

    def test_the_site_timeline_follows_the_recalculated_dates(self):
        site = LandSite.objects.create(name="Outside the Gate", status="loi")
        Contract.objects.filter(pk=self.c.pk).update(site=site, status="signed")
        self.save_details(status="signed", site=site.pk, effective_date=NEW.isoformat())
        site.refresh_from_db()
        self.assertEqual(site.milestones["psa_signed"], "2026-04-01")
        self.assertEqual(site.milestones["feasibility_end"], "2026-05-31")  # the new date, not the one before

    def test_nothing_moves_twice(self):
        Contract.objects.filter(pk=self.c.pk).update(effective_date=NEW)
        self.c.refresh_from_db()
        moved, left = timeline.recompute_dates(self.c, OLD, self.user)
        self.assertEqual((len(moved), left), (4, 0))
        self.assertEqual(timeline.recompute_dates(self.c, NEW, self.user), ([], 0))
