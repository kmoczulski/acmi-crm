"""Trello history onto the Tenant pipeline board (crm/boards/trello_import.py, manage.py import_trello)."""
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from crm.boards import bound, trello_import
from crm.models import Card, CardAttachment, CardEvent, Comment, Company, Notification, Opportunity

User = get_user_model()

KELLEN = {"id": "m1", "fullName": "Kellen Moczulski"}
JOE = {"id": "m9", "fullName": "Joe Don Bowling"}
DEAL_CARD = "5f0000000000000000000001"
LEAD_CARD = "5f0000000000000000000002"
GONE_CARD = "5f0000000000000000000003"


def board_data():
    lists = [{"id": "l1", "name": "ACTIVE DISCUSSIONS (at least 1 meeting)", "closed": False, "pos": 2},
             {"id": "l2", "name": "Cold", "closed": False, "pos": 3},
             {"id": "l3", "name": "Targets", "closed": True, "pos": 1}]
    labels = [{"id": "lb1", "name": "High Value", "color": "green_dark"}, {"id": "lb2", "name": "Build to Suit Opp", "color": "green"}]
    cards = [
        {"id": DEAL_CARD, "shortLink": "abc123", "name": "Hadrian (Trello name)", "desc": "Trello words", "idList": "l1",
         "closed": False, "pos": 1, "idLabels": ["lb1", "lb2"], "idMembers": ["m1"], "due": None,
         "checklists": [{"name": "Next steps", "pos": 1, "checkItems": [
             {"name": "Send LOI", "state": "complete", "pos": 1, "idMember": "m1", "due": "2026-03-01T17:00:00.000Z"},
             {"name": "Site visit", "state": "incomplete", "pos": 2}]}],
         "attachments": [{"name": "Deck.pdf", "url": "https://trello.com/1/cards/x/attachments/y/download/Deck.pdf",
                          "date": "2025-06-01T12:00:00.000Z", "idMember": "m1"}]},
        {"id": LEAD_CARD, "shortLink": "def456", "name": "Acme Rockets", "desc": "A lead", "idList": "l2", "closed": False,
         "pos": 1, "idLabels": [], "idMembers": [], "checklists": [], "attachments": []},
    ]

    def comment(cid, member, text, date):
        return {"id": f"a{date}", "type": "commentCard", "date": date, "memberCreator": member,
                "data": {"text": text, "card": {"id": cid}, "list": {"name": "x"}}}

    actions = [
        {"id": "c0", "type": "createCard", "date": "2025-05-01T10:00:00.000Z", "memberCreator": KELLEN,
         "data": {"card": {"id": DEAL_CARD}, "list": {"name": "EARLY ENGAGEMENT"}}},
        {"id": "c1", "type": "updateCard", "date": "2025-05-03T10:00:00.000Z", "memberCreator": KELLEN,
         "data": {"card": {"id": DEAL_CARD}, "listBefore": {"name": "EARLY ENGAGEMENT"},
                  "listAfter": {"name": "ACTIVE DISCUSSIONS (at least 1 meeting)"}, "old": {"idList": "l0"}}},
        comment(DEAL_CARD, KELLEN, "Call went well @simon", "2025-05-02T15:30:00.123Z"),
        comment(DEAL_CARD, JOE, "Intro made", "2025-05-04T09:00:00.000Z"),
        comment(LEAD_CARD, KELLEN, "No reply", "2025-07-01T09:00:00.000Z"),
        comment(GONE_CARD, KELLEN, "On a deleted card", "2025-07-02T09:00:00.000Z"),
    ]
    return {"board": {"name": "Properties-Prospecting", "lists": lists, "labels": labels,
                      "members": [KELLEN, JOE]}, "cards": cards, "checklists": [], "actions": actions}


class TrelloImportTests(TestCase):
    def setUp(self):
        self.kellen = User.objects.create_user("Kellen", first_name="Kellen", last_name="Moczulski")
        User.objects.create_user("Simon", first_name="Simon", last_name="Shewmaker")
        self.hadrian = Opportunity.objects.create(name="Hadrian", company=Company.objects.create(name="Hadrian"),
                                                  stage="lease_neg", source_key=f"trello:{DEAL_CARD}")
        self.acme = Company.objects.create(name="Acme Rockets, Inc.")
        self.board = bound.create_record_board("opportunity", self.kellen)
        self.card = Card.objects.get(list__board=self.board, opportunity=self.hadrian)

    def run_import(self):
        return trello_import.import_prospecting(board_data(), self.kellen)

    def test_deal_history_lands_on_the_deal_card_and_the_crm_wins(self):
        imp = self.run_import()
        self.card.refresh_from_db()
        self.assertEqual(self.card.title, "Hadrian")  # not the Trello name
        self.assertEqual(self.card.list.maps_to, "lease_neg")  # not moved to Trello's list
        self.assertEqual(self.card.description, "")
        mine = Comment.objects.get(card=self.card, author=self.kellen)
        self.assertEqual(mine.opportunity, self.hadrian)  # shows on the deal page too
        self.assertEqual(mine.created_at.isoformat(), "2025-05-02T15:30:00.123000+00:00")
        self.assertIn(mine, self.hadrian.comments.all())
        joe = Comment.objects.get(card=self.card, author=None)
        self.assertEqual(joe.body, "Joe Don Bowling (in Trello): Intro made")
        self.assertEqual(imp.counts["comments onto deal cards"], 2)
        self.assertEqual(imp.outsiders["Joe Don Bowling"], 1)

    def test_labels_members_checklists_attachments_and_events(self):
        self.run_import()
        self.assertEqual([l.name for l in self.card.labels.all()], ["Build to Suit Opp"])  # High Value is priority
        self.assertIn(self.kellen, self.card.members.all())
        checklist = self.card.checklists.get()
        items = list(checklist.items.all())
        self.assertEqual([(i.text, i.done, i.assignee) for i in items],
                         [("Send LOI", True, self.kellen), ("Site visit", False, None)])
        self.assertEqual(str(items[0].due), "2026-03-01")
        att = CardAttachment.objects.get(card=self.card)
        self.assertEqual((att.name, att.added_by, att.added_at.year), ("Deck.pdf", self.kellen, 2025))
        texts = list(CardEvent.objects.filter(card=self.card, kind="trello").order_by("when").values_list("text", flat=True))
        self.assertEqual(texts, ["created this card in Trello on EARLY ENGAGEMENT",
                                 "moved this card from EARLY ENGAGEMENT to ACTIVE DISCUSSIONS (at least 1 meeting) in Trello"])

    def test_a_card_with_no_deal_gets_its_own_card_linked_to_its_company(self):
        imp = self.run_import()
        lead = Card.objects.get(source_key=f"trello:{LEAD_CARD}")
        self.assertEqual((lead.list.name, lead.list.maps_to, lead.list.archived), ("Cold", "", False))
        self.assertEqual(lead.company, self.acme)
        self.assertEqual(lead.description, "A lead")
        c = Comment.objects.get(card=lead)
        self.assertEqual((c.company, c.opportunity), (self.acme, None))
        self.assertEqual(imp.counts["comments skipped (card deleted or moved off the board)"], 1)

    def test_a_rerun_adds_nothing(self):
        self.run_import()
        before = (Comment.objects.count(), CardEvent.objects.count(), CardAttachment.objects.count(),
                  Card.objects.count(), self.card.checklists.count())
        imp = self.run_import()
        after = (Comment.objects.count(), CardEvent.objects.count(), CardAttachment.objects.count(),
                 Card.objects.count(), self.card.checklists.count())
        self.assertEqual(before, after)
        self.assertEqual(imp.counts["comments already there"], 3)

    def test_nobody_is_notified(self):
        self.run_import()
        self.assertFalse(Notification.objects.exists())

    def test_command_is_a_dry_run_unless_apply(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "ucXAoRb7-properties-prospecting.json").write_text(json.dumps(board_data()), encoding="utf-8")
            out = StringIO()
            call_command("import_trello", d, stdout=out)
            self.assertIn("comments onto deal cards: 2", out.getvalue())
            self.assertFalse(Comment.objects.exists())
            call_command("import_trello", d, "--apply", stdout=StringIO())
            self.assertEqual(Comment.objects.count(), 3)


def meih_data():
    lists = [{"id": n, "name": name, "closed": False, "pos": i} for i, (n, name) in enumerate([
        ("s", "Land Sites"), ("c", "COLD - Land Sites"), ("d", "4390 Indian Head Hwy - Critical Dates"), ("a", "ARA"),
        ("k", "Contacts and Consultants")])]

    def card(cid, name, lst, **extra):
        return {"id": cid, "shortLink": cid, "name": name, "desc": "", "idList": lst, "closed": False, "pos": 1,
                "idLabels": extra.get("labels", []), "idMembers": [], "checklists": [], "attachments": extra.get("att", [])}

    cards = [card("5f01", "Lewis Place", "s"), card("5f02", "NSWC - IHD", "s"), card("5f03", "Berry Rd - Assemblage", "c"),
             card("5f04", "4390 Indian Head Highway PSA", "d"), card("5f05", "Feasibility Period Expiration", "d"),
             card("5f06", "Contact List & Agreement", "a", labels=["lb1"]),
             card("5f07", "Invoice ARA – Month 3 ($18K)", "a", att=[{"name": "inv.pdf", "url": "https://trello.com/inv.pdf"}]),
             card("5f08", "Kimley Horn", "k")]
    actions = [{"id": f"x{n}", "type": "commentCard", "date": f"2026-0{n}-01T10:00:00.000Z", "memberCreator": KELLEN,
                "data": {"text": f"note {n}", "card": {"id": cid}}}
               for n, cid in enumerate(["5f01", "5f05", "5f07", "5f03"], 1)]
    return {"board": {"name": "Maryland / MEIH", "lists": lists, "members": [KELLEN],
                      "labels": [{"id": "lb1", "name": "Program/Procurement", "color": "green"}]},
            "cards": cards, "checklists": [], "actions": actions}


class MeihImportTests(TestCase):
    def setUp(self):
        from crm.models import Board, Campus, Contract, Firm, LandSite

        self.kellen = User.objects.create_user("Kellen", first_name="Kellen", last_name="Moczulski")
        meih = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.bryans = LandSite.objects.create(name="Bryans Road Assemblage", campus=meih)
        self.berry = LandSite.objects.create(name="Berry Rd / Tara Pl Assemblage", campus=meih, status="dropped")
        self.psa = Contract.objects.create(title="4390 Indian Head Hwy PSA", campus=meih)
        self.ara = Opportunity.objects.create(name="ARA · MEIH", campus=meih, stage="loi_signed",
                                              company=Company.objects.create(name="Applied Research Associates"))
        call_command("boards_setup", "--apply", stdout=StringIO())
        self.board = Board.objects.get(name="MEIH")
        self.Firm = Firm

    def test_cards_land_on_the_hand_mapped_lists_and_records(self):
        imp = trello_import.import_meih(meih_data(), self.kellen)
        lewis = Card.objects.get(source_key="trello:5f01")
        self.assertEqual((lewis.list.name, lewis.site), ("Land sites", self.bryans))
        self.assertEqual(Comment.objects.get(card=lewis).site, self.bryans)  # also on the site's page
        self.assertEqual(Card.objects.get(source_key="trello:5f02").list.name, "Consultants")
        berry = Card.objects.get(source_key="trello:5f03")
        self.assertTrue(berry.archived)
        kimley = Card.objects.get(source_key="trello:5f08")
        self.assertEqual(kimley.firm, self.Firm.objects.get(name="Kimley Horn"))
        self.assertEqual(imp.counts["firms added to the directory"], 3)
        ara = Card.objects.get(source_key="trello:5f06")
        self.assertEqual((ara.list.name, ara.opportunity), ("ARA Facility – deal", self.ara))
        self.assertEqual([l.name for l in ara.labels.all()], ["Program / procurement"])  # matched, not duplicated

    def test_date_cards_are_skipped_but_their_history_moves(self):
        trello_import.import_meih(meih_data(), self.kellen)
        self.assertFalse(Card.objects.filter(source_key__in=["trello:5f05", "trello:5f07"]).exists())
        psa_card = Card.objects.get(source_key="trello:5f04")
        self.assertEqual((psa_card.list.name, psa_card.contract), ("Critical dates", self.psa))
        self.assertEqual(Comment.objects.get(card=psa_card).body, "note 2")
        ara = Card.objects.get(source_key="trello:5f06")
        self.assertEqual(Comment.objects.get(card=ara).opportunity, self.ara)
        self.assertTrue(ara.attachments.filter(name="inv.pdf").exists())

    def test_a_rerun_adds_nothing(self):
        trello_import.import_meih(meih_data(), self.kellen)
        counts = (Card.objects.count(), Comment.objects.count(), CardAttachment.objects.count(), self.Firm.objects.count())
        trello_import.import_meih(meih_data(), self.kellen)
        self.assertEqual(counts, (Card.objects.count(), Comment.objects.count(), CardAttachment.objects.count(),
                                  self.Firm.objects.count()))


class NsihImportTests(TestCase):
    def setUp(self):
        from crm.models import Campus, Firm, Project

        self.kellen = User.objects.create_user("Kellen", first_name="Kellen", last_name="Moczulski")
        self.nsih = Campus.objects.create(code="NSIH", name="National Security Industrial Hub")
        Project.objects.create(name="NSIH", campus=self.nsih)
        self.prometheus = Opportunity.objects.create(name="Prometheus", stage="signed",
                                                     company=Company.objects.create(name="Prometheus (Kratos / Rafael)"))
        self.haskell = Firm.objects.create(name="Haskell", kind="contractor")
        lists = [{"id": "t", "name": "Tenancy/Contracts", "closed": False, "pos": 1},
                 {"id": "d", "name": "Design & Construction", "closed": False, "pos": 2},
                 {"id": "p", "name": "Prospecting", "closed": True, "pos": 3}]

        def card(cid, name, lst):
            return {"id": cid, "shortLink": cid, "name": name, "desc": "", "idList": lst, "closed": False, "pos": 1,
                    "idLabels": [], "idMembers": [], "checklists": [], "attachments": []}

        self.data = {
            "board": {"name": "Properties-NSIH", "lists": lists, "labels": [], "members": [KELLEN]},
            "cards": [card("6a01", "Prometheus - DMA", "t"), card("6a02", "Architect - Haskell", "d"),
                      card("6a03", "LOI - Kratos", "p"), card("6a04", "https://trello.com/c/xyz", "p")],
            "checklists": [],
            "actions": [{"id": "y1", "type": "commentCard", "date": "2026-02-01T10:00:00.000Z", "memberCreator": KELLEN,
                         "data": {"text": "DMA signed", "card": {"id": "6a01"}}}],
        }

    def test_a_new_board_with_cards_on_their_deals_and_firms(self):
        from crm.models import Board, Firm

        imp = trello_import.import_nsih(self.data, self.kellen)
        board = Board.objects.get(name="NSIH")
        self.assertEqual(board.campus, self.nsih)
        self.assertEqual(board.lists.get(feed="open_tasks").name, "Open tasks")
        dma = Card.objects.get(source_key="trello:6a01")
        self.assertEqual((dma.list.name, dma.opportunity), ("Tenants and leases", self.prometheus))
        self.assertEqual(Comment.objects.get(card=dma).opportunity, self.prometheus)
        self.assertEqual(Card.objects.get(source_key="trello:6a02").firm, self.haskell)
        kratos = Card.objects.get(source_key="trello:6a03")
        self.assertTrue(kratos.archived)
        self.assertEqual(kratos.opportunity, self.prometheus)
        self.assertFalse(Card.objects.filter(source_key="trello:6a04").exists())  # a link card
        self.prometheus.refresh_from_db()
        self.assertEqual(self.prometheus.campus, self.nsih)
        self.assertTrue(Firm.objects.filter(name="Fortress", kind="investor").exists())
        self.assertEqual(imp.counts["firms added to the directory"], 14)

    def test_a_rerun_adds_nothing(self):
        from crm.models import Board, Firm

        trello_import.import_nsih(self.data, self.kellen)
        counts = (Board.objects.count(), Card.objects.count(), Comment.objects.count(), Firm.objects.count())
        trello_import.import_nsih(self.data, self.kellen)
        self.assertEqual(counts, (Board.objects.count(), Card.objects.count(), Comment.objects.count(),
                                  Firm.objects.count()))


class GeneralImportTests(TestCase):
    def setUp(self):
        from crm.models import Campus, Project

        self.kellen = User.objects.create_user("Kellen", first_name="Kellen", last_name="Moczulski")
        nsih = Campus.objects.create(code="NSIH", name="National Security Industrial Hub")
        Project.objects.create(name="NSIH", campus=nsih)
        Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        call_command("boards_setup", "--apply", stdout=StringIO())
        self.nsih_board = trello_import.nsih_board(self.kellen)
        self.hostetler = Card.objects.create(list=self.nsih_board.lists.get(name="Land contracts"), title="Land PSA - Hostetler")
        self.spinner = Opportunity.objects.create(name="Spinner", stage="signed", company=Company.objects.create(name="Spinner"))
        lists = [{"id": "a", "name": "Airport Commerce", "closed": False, "pos": 1},
                 {"id": "h", "name": "Critical Dates - Hostetler / NSIH", "closed": False, "pos": 2},
                 {"id": "v", "name": "Avio", "closed": True, "pos": 3},
                 {"id": "e", "name": "Exploration Park - Archive", "closed": True, "pos": 4}]

        def card(cid, name, lst, att=()):
            return {"id": cid, "shortLink": cid, "name": name, "desc": "", "idList": lst, "closed": False, "pos": 1,
                    "idLabels": [], "idMembers": [], "checklists": [], "attachments": list(att)}

        def comment(n, cid):
            return {"id": f"z{n}", "type": "commentCard", "date": f"2026-0{n}-01T10:00:00.000Z", "memberCreator": KELLEN,
                    "data": {"text": f"c{n}", "card": {"id": cid}}}

        self.data = {
            "board": {"name": "Properties-General", "lists": lists, "labels": [], "members": [KELLEN]},
            "cards": [card("7a01", "Spinner Modifications", "a"),
                      card("7a02", "Closing Date (No Extensions)", "h", [{"name": "PSA.pdf", "url": "https://trello.com/psa.pdf"}]),
                      card("7a03", "Programming", "v"), card("7a04", "Houston site", "e")],
            "checklists": [],
            "actions": [comment(1, "7a01"), comment(2, "7a02"), comment(3, "7a03"), comment(4, "7a04")],
        }

    def test_cards_go_to_the_team_and_archive_boards_and_dates_wait_for_the_psa(self):
        from crm.models import Board

        imp = trello_import.import_general(self.data, self.kellen)
        spinner = Card.objects.get(source_key="trello:7a01")
        self.assertEqual((spinner.list.board.name, spinner.list.name, spinner.opportunity),
                         ("ACMI general", "Airport Commerce", self.spinner))
        avio = Card.objects.get(source_key="trello:7a03")
        self.assertEqual(avio.list.board.name, "Properties-General archive")
        self.assertTrue(Board.objects.get(name="Properties-General archive").archived)
        self.assertFalse(Card.objects.filter(source_key__in=["trello:7a02", "trello:7a04"]).exists())
        self.assertEqual(Comment.objects.get(card=self.hostetler).body, "c2")  # the date card's history moved
        self.assertTrue(self.hostetler.attachments.filter(name="PSA.pdf").exists())
        self.assertEqual(imp.counts["comments skipped (card not imported)"], 1)  # Exploration Park

    def test_a_rerun_adds_nothing(self):
        from crm.models import Board

        trello_import.import_general(self.data, self.kellen)
        counts = (Board.objects.count(), Card.objects.count(), Comment.objects.count(), CardAttachment.objects.count())
        trello_import.import_general(self.data, self.kellen)
        self.assertEqual(counts, (Board.objects.count(), Card.objects.count(), Comment.objects.count(),
                                  CardAttachment.objects.count()))


class PursuitsAndCapitalImportTests(TestCase):
    def setUp(self):
        from crm.models import DataRoom, Firm

        self.kellen = User.objects.create_user("Kellen", first_name="Kellen", last_name="Moczulski")
        self.saab = Opportunity.objects.create(name="Saab", stage="qualified", company=Company.objects.create(name="Saab"))
        self.fortress = Firm.objects.create(name="Fortress", kind="investor")
        self.room = DataRoom.objects.create(name="Fortress")

    def board(self, name, lists, cards):
        return {"board": {"name": name, "lists": [{"id": f"l{i}", "name": n, "closed": False, "pos": i} for i, n in enumerate(lists)],
                          "labels": [], "members": [KELLEN]},
                "cards": [{"id": cid, "shortLink": cid, "name": title, "desc": "", "idList": lst, "closed": False, "pos": 1,
                           "idLabels": [], "idMembers": [], "checklists": [], "attachments": []} for cid, title, lst in cards],
                "checklists": [],
                "actions": [{"id": "q1", "type": "commentCard", "date": "2026-03-01T10:00:00.000Z", "memberCreator": KELLEN,
                             "data": {"text": "hello", "card": {"id": cards[0][0]}}}]}

    def test_pursuits_link_their_deals(self):
        data = self.board("Properties-Campus Pursuits", ["Holding / Overflow"],
                          [("8a01", "Project:\xa0Saab  (supplier campus in Michigan)", "l0"), ("8a02", "Florida", "l0")])
        trello_import.import_pursuits(data, self.kellen)
        saab = Card.objects.get(source_key="trello:8a01")
        self.assertEqual((saab.list.board.name, saab.list.name, saab.opportunity),
                         ("Campus pursuits", "Holding / overflow", self.saab))
        self.assertEqual(Comment.objects.get(card=saab).opportunity, self.saab)
        self.assertIsNone(Card.objects.get(source_key="trello:8a02").record())

    def test_capital_cards_link_firms_and_fortress_becomes_the_rooms_partner(self):
        from crm.models import Firm

        data = self.board("Properties - Capitalization", ["Committed / Closed", "Intro Conversations"],
                          [("9a01", "Fortress Investment Group", "l0"), ("9a02", "Blue Owl", "l1")])
        imp = trello_import.import_capital(data, self.kellen)
        self.fortress.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(self.fortress.name, "Fortress Investment Group")
        self.assertEqual(self.room.partner, self.fortress)
        card = Card.objects.get(source_key="trello:9a01")
        self.assertEqual((card.list.name, card.firm), ("Committed / closed", self.fortress))
        self.assertEqual(Card.objects.get(source_key="trello:9a02").firm, Firm.objects.get(name="Blue Owl"))
        self.assertEqual(imp.counts["firms added to the directory"], 7)
        trello_import.import_capital(data, self.kellen)  # a re-run adds nothing
        self.assertEqual((Card.objects.count(), Firm.objects.count(), Comment.objects.count()), (2, 8, 1))
