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
