"""Record boards (bound lists) and feed lists: cards that are views of the CRM's records (crm/boards/bound.py)."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from crm.boards import bound, logic
from crm.models import (
    Board, Campus, Card, CardEvent, Company, Contract, ContractDate, LandSite, LandSiteEvent, Notification, Opportunity,
    Task,
)

User = get_user_model()


class RecordBoardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen", last_name="M")
        self.simon = User.objects.create_user("simon", password="x", first_name="Simon", last_name="S")
        self.meih = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.nsih = Campus.objects.create(code="NSIH", name="National Security Industrial Hub")
        self.ara = Opportunity.objects.create(name="ARA · MEIH", company=Company.objects.create(name="ARA"),
                                              campus=self.meih, stage="loi_signed", owner=self.simon)
        self.spinner = Opportunity.objects.create(name="Spinner · MEIH", company=Company.objects.create(name="Spinner"),
                                                  campus=self.meih, stage="qualified")
        self.elsewhere = Opportunity.objects.create(name="Elsewhere", company=Company.objects.create(name="Other"),
                                                    campus=self.nsih, stage="qualified")
        self.board = bound.create_record_board("opportunity", self.user, campus=self.meih)
        self.client.force_login(self.user)

    def card_for(self, opp):
        return Card.objects.get(list__board=self.board, opportunity=opp)

    def test_a_list_per_stage_and_a_card_per_deal_in_scope(self):
        self.assertEqual(self.board.kind, Board.Kind.BOUND)
        self.assertEqual(self.board.name, "Tenant pipeline")
        self.assertEqual([l.maps_to for l in self.board.lists.all()][:3], ["prospect", "qualified", "loi_neg"])
        self.assertEqual(self.card_for(self.ara).list.maps_to, "loi_signed")
        self.assertEqual(self.card_for(self.spinner).list.maps_to, "qualified")
        self.assertFalse(Card.objects.filter(opportunity=self.elsewhere).exists())  # NSIH, outside the board

    def test_a_stage_changed_elsewhere_moves_the_card(self):
        self.spinner.stage = "loi_neg"
        self.spinner.save()
        card = self.card_for(self.spinner)
        self.assertEqual(card.list.maps_to, "loi_neg")
        self.assertTrue(CardEvent.objects.filter(card=card, kind="moved", text__contains="followed the deal").exists())

    def test_a_new_deal_gets_a_card(self):
        opp = Opportunity.objects.create(name="Hadrian · MEIH", company=Company.objects.create(name="Hadrian"),
                                         campus=self.meih, stage="prospect")
        self.assertEqual(self.card_for(opp).list.maps_to, "prospect")

    def test_an_archived_card_stays_archived(self):
        card = self.card_for(self.spinner)
        card.archived = True
        card.save()
        self.spinner.stage = "lost"
        self.spinner.save()
        card.refresh_from_db()
        self.assertTrue(card.archived)
        self.assertEqual(Card.objects.filter(opportunity=self.spinner).count(), 1)

    def test_dropping_a_card_changes_the_stage_and_tells_the_owner(self):
        card = self.card_for(self.ara)
        target = self.board.lists.get(maps_to="lease_neg")
        r = self.client.post(reverse("crm:card_move", args=[card.pk]), {"list": target.pk, "index": 0})
        self.assertEqual(r.status_code, 200)
        self.ara.refresh_from_db()
        card.refresh_from_db()
        self.assertEqual(self.ara.stage, "lease_neg")
        self.assertEqual(card.list, target)
        self.assertEqual(CardEvent.objects.filter(card=card, kind="moved").count(), 1)  # the drop, not a second follow
        self.assertTrue(Notification.objects.filter(user=self.simon, text__contains="Lease negotiation").exists())

    def test_an_unlinked_card_cant_go_in_a_bound_list(self):
        src = self.board.lists.get(maps_to="prospect")
        loose = Card.objects.create(list=src, title="An idea", position=99000)
        r = self.client.post(reverse("crm:card_move", args=[loose.pk]), {"list": self.board.lists.get(maps_to="signed").pk})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Link this card to a deal", r.json()["error"])

    def test_cards_cant_be_typed_into_a_bound_list(self):
        lst = self.board.lists.get(maps_to="prospect")
        r = self.client.post(reverse("crm:card_add", args=[lst.pk]), {"title": "Loose"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.status_code, 400)

    def test_sync_is_idempotent(self):
        self.assertEqual(bound.sync_board(self.board), {"created": 0, "moved": 0})

    def test_record_board_from_the_new_board_form(self):
        r = self.client.post(reverse("crm:board_create"), {"name": "NSIH pipeline", "template": "record:opportunity",
                                                           "kind": "project", "campus": self.nsih.pk, "colour": "navy"})
        board = Board.objects.get(name="NSIH pipeline")
        self.assertRedirects(r, board.get_absolute_url())
        self.assertEqual(board.record_type, "opportunity")
        self.assertTrue(Card.objects.filter(list__board=board, opportunity=self.elsewhere).exists())
        page = self.client.get(board.get_absolute_url())
        self.assertContains(page, "Moving a card here changes the deal")
        self.assertNotContains(page, "+ Add a card")


class BoardsSetupCommandTests(TestCase):
    def test_dry_run_saves_nothing_and_apply_makes_both_boards_once(self):
        from io import StringIO

        from django.core.management import call_command

        kellen = User.objects.create_user("Kellen", first_name="Kellen", last_name="Moczulski")
        User.objects.create_user("Dave", first_name="David", last_name="Dowell")
        meih = Campus.objects.create(code="MEIH", name="MEIH")
        Opportunity.objects.create(name="ARA", company=Company.objects.create(name="ARA"), campus=meih, stage="loi_signed")
        out = StringIO()
        call_command("boards_setup", stdout=out)
        self.assertIn("Dry run", out.getvalue())
        self.assertFalse(Board.objects.exists())
        call_command("boards_setup", "--apply", stdout=StringIO())
        pipeline = Board.objects.get(record_type="opportunity")
        self.assertEqual(Card.objects.filter(list__board=pipeline).count(), 1)
        board = Board.objects.get(name="MEIH")
        self.assertEqual([l.feed for l in board.lists.all()], ["sites", "critical_dates", "", "", "", "", "open_tasks"])
        self.assertEqual(set(board.members.values_list("username", flat=True)), {"Kellen", "Dave"})
        self.assertEqual(board.created_by, kellen)
        call_command("boards_setup", "--apply", stdout=out)
        self.assertEqual(Board.objects.count(), 2)
        self.assertIn("already there", out.getvalue())


class SiteBoardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x")
        self.meih = Campus.objects.create(code="MEIH", name="MEIH")
        self.site = LandSite.objects.create(name="Lake Acton Business Park", campus=self.meih, status="loi")
        self.board = bound.create_record_board("site", self.user, campus=self.meih)
        self.client.force_login(self.user)

    def test_dropping_a_site_card_changes_its_status_and_logs_it(self):
        card = Card.objects.get(site=self.site)
        target = self.board.lists.get(maps_to="contract")
        r = self.client.post(reverse("crm:card_move", args=[card.pk]), {"list": target.pk, "index": 0})
        self.assertEqual(r.status_code, 200)
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, "contract")
        self.assertTrue(LandSiteEvent.objects.filter(site=self.site, kind="status", text__contains="moved on a board").exists())


class FeedListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", first_name="Kellen", last_name="M")
        self.meih = Campus.objects.create(code="MEIH", name="MEIH")
        self.nsih = Campus.objects.create(code="NSIH", name="NSIH")
        self.board = logic.create_board("MEIH", self.user, template="project", campus=self.meih)
        self.sites, self.dates, self.design, _, self.tasks = self.board.lists.all()
        today = timezone.localdate()
        self.site = LandSite.objects.create(name="4390 Indian Head Hwy", campus=self.meih, status="contract")
        LandSite.objects.create(name="Berry Pointe", campus=self.meih, status="dropped")
        LandSite.objects.create(name="Hostetler", campus=self.nsih, status="contract")
        self.psa = Contract.objects.create(title="4390 PSA", kind="psa", status="signed", campus=self.meih, site=self.site)
        ContractDate.objects.create(contract=self.psa, kind="closing", label="Closing Date Deadline", date=today + timedelta(days=60))
        ContractDate.objects.create(contract=self.psa, kind="deposit", label="Initial Deposit", date=today - timedelta(days=90), done=True)
        Task.objects.create(title="Order the survey", site=self.site, due_date=today + timedelta(days=3), assignee=self.user)
        Task.objects.create(title="Hostetler title work", site=LandSite.objects.get(name="Hostetler"))
        self.client.force_login(self.user)

    def test_project_template_lists_are_feeds(self):
        self.assertEqual([l.feed for l in self.board.lists.all()], ["sites", "critical_dates", "", "", "open_tasks"])

    def test_feeds_fill_from_the_boards_campus(self):
        self.assertEqual([i.title for i in bound.feed_items(self.sites, [])], ["4390 Indian Head Hwy"])  # not dropped, not NSIH
        dates = bound.feed_items(self.dates, [])
        self.assertEqual([i.title for i in dates], ["Closing Date Deadline"])  # done dates drop out
        self.assertEqual(dates[0].sub, "4390 PSA")
        self.assertEqual([i.title for i in bound.feed_items(self.tasks, [])], ["Order the survey"])

    def test_a_stored_card_shows_in_its_records_place(self):
        card = Card.objects.create(list=self.sites, title="4390 Indian Head (from Trello)", site=self.site, position=1000)
        items = bound.feed_items(self.sites, [card])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].card, card)

    def test_board_page_renders_feeds_without_add_forms_there(self):
        r = self.client.get(self.board.get_absolute_url())
        self.assertContains(r, "Closing Date Deadline")
        self.assertContains(r, "Order the survey")
        self.assertContains(r, "Fills itself from MEIH")
        self.assertContains(r, "+ Add a card", count=2)  # Deal and design, Contacts and consultants

    def test_nothing_can_be_moved_or_added_into_a_feed(self):
        card = logic.add_card(self.design, "Survey", self.user)
        r = self.client.post(reverse("crm:card_move", args=[card.pk]), {"list": self.dates.pk, "index": 0})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(reverse("crm:card_add", args=[self.dates.pk]), {"title": "x"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.status_code, 400)

    def test_contract_card_shows_who_has_the_ball(self):
        self.psa.waiting_on = Contract._meta.get_field("waiting_on").choices[1][0]
        self.psa.waiting_since = timezone.localdate()
        self.psa.save()
        card = logic.add_card(self.design, "PSA", self.user, contract=self.psa)
        facts = dict(logic.record_facts(card))
        self.assertIn("since", facts["Waiting on"])
