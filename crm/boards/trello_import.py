"""Bring a Trello board's history into the hub: comments, checklists, attachments and the card's activity, read from
`scripts/trello_fetch.py`'s output (one JSON file per board, every action back to the board's start).

Properties-Prospecting goes onto the Tenant pipeline record board (docs/TRELLO_MAPPING.md). A Trello card that is a
deal (`Opportunity.source_key == "trello:<id>"`, or the deal's Trello link) adds its history to that deal's card; the
CRM wins, so the card's title, description, list and dates stay as they are, and only labels and members are added.
Any other card (New Leads, Cold, Partners, archived lists) becomes a card of its own on an unbound list named after
its Trello list, linked to its company where the name matches.

Comments keep their author and date. The team is matched by full name; anyone else keeps their name at the start of
the comment. Nothing notifies anyone. A re-run adds only what isn't there yet: comments and events match on card,
time and text, attachments on card and link, checklists on card, name and position.
"""
import json
from collections import Counter
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import transaction

from .. import merge
from ..models import (
    Board, BoardLabel, BoardList, Card, CardAttachment, CardEvent, Checklist, ChecklistItem, Comment, Company,
    Opportunity, LABEL_COLOURS,
)
from . import bound, logic

User = get_user_model()

# The team by their Trello full name, matched to accounts by first and last name (never pipeline_import.TEAM, whose
# old lowercase usernames would make duplicates).
TEAM = ["Kellen Moczulski", "Simon Shewmaker", "David DeNardo", "David Dowell", "Paul Kadzielski", "John Burer"]

# Prospecting labels the deal already carries as its priority or campus.
SKIP_LABELS = {"High Value", "Medium Value", "Low Value", "NSIH", "MEIH", "Exploration Park"}
# The deal-stage lists: their cards are deals, so a card there with no deal is an old one and comes over archived.
STAGE_LISTS = {"EARLY ENGAGEMENT", "ACTIVE DISCUSSIONS (at least 1 meeting)", "LOI / PDA NEGOTIATIONS", "LOIs Executed",
               "LEASE NEOGTIATIONS", "LEASE NEGOTIATIONS", "LEASE / PDA SIGNED"}
COLOURS = {value for value, _ in LABEL_COLOURS}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_board_file(folder, name):
    """The fetched file for the Trello board called `name` (case-insensitive)."""
    for f in sorted(Path(folder).glob("*.json")):
        if f.name == "index.json":
            continue
        data = load(f)
        if data.get("board", {}).get("name", "").lower() == name.lower():
            return data
    raise FileNotFoundError(f"No fetched board called {name!r} in {folder}")


def when(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


def created_from_id(trello_id):
    """Trello ids start with the creation time in seconds."""
    return datetime.fromtimestamp(int(trello_id[:8], 16), tz=dt_timezone.utc)


def colour(trello_colour):
    base = (trello_colour or "").split("_")[0]
    return base if base in COLOURS else "blue"


def team_users():
    users = {}
    for full in TEAM:
        first, last = full.split(" ", 1)
        u = User.objects.filter(first_name=first, last_name=last, is_active=True).order_by("pk").first()
        if u:
            users[full] = u
    return users


class Importer:
    def __init__(self, data, board, by=None):
        self.data, self.board, self.by = data, board, by
        self.users = team_users()
        tb = data["board"]
        self.member_names = {m["id"]: m["fullName"] for m in tb.get("members", [])}
        self.lists = {l["id"]: l for l in tb.get("lists", [])}
        self.labels = {l["id"]: l for l in tb.get("labels", [])}
        self.counts = Counter()
        self.outsiders = Counter()
        self._board_labels = {}
        self._hub_lists = {}

    # -- people ---------------------------------------------------------------------------------------------------

    def person(self, member):
        """(user, name): a team member's account, or None and the name for anyone else."""
        name = (member or {}).get("fullName") or self.member_names.get((member or {}).get("id"), "") or "Someone"
        return self.users.get(name), name

    def member_user(self, member_id):
        return self.users.get(self.member_names.get(member_id, ""))

    # -- matching -------------------------------------------------------------------------------------------------

    def deals(self):
        by_key, by_link = {}, {}
        for o in Opportunity.objects.exclude(source_key=None).filter(source_key__startswith="trello:"):
            by_key[o.source_key[7:]] = o
        for o in Opportunity.objects.exclude(trello_url=""):
            by_link[o.trello_url] = o
        return by_key, by_link

    def deal_for(self, card, by_key, by_link):
        if card["id"] in by_key:
            return by_key[card["id"]]
        for link, o in by_link.items():
            if f"/c/{card['shortLink']}" in link or card["id"] in link:
                return o
        return None

    def companies(self):
        index = {}
        for pk, name in Company.objects.values_list("pk", "name").order_by("pk"):
            index.setdefault(merge.core_name(name), pk)
        return index

    # -- hub side -------------------------------------------------------------------------------------------------

    def hub_list(self, trello_list):
        """The unbound list on the board for a Trello list (made the first time)."""
        name = trello_list["name"].strip()[:120]
        if name in self._hub_lists:
            return self._hub_lists[name]
        lst = self.board.lists.filter(name=name, maps_to="", feed="").first()
        if lst is None:
            archived = trello_list.get("closed", False) or trello_list["name"] in STAGE_LISTS
            last = self.board.lists.order_by("-position").first()
            lst = BoardList.objects.create(board=self.board, name=name, archived=archived,
                                           position=(last.position if last else 0) + logic.GAP)
            self.counts["lists created"] += 1
        self._hub_lists[name] = lst
        return lst

    def board_label(self, trello_label):
        name = (trello_label.get("name") or "").strip()
        if not name or name in SKIP_LABELS:
            return None
        if name not in self._board_labels:
            label = self.board.labels.filter(name__iexact=name).first()
            if label is None:
                label = BoardLabel.objects.create(board=self.board, name=name[:60], colour=colour(trello_label.get("color")),
                                                  position=self.board.labels.count() + 1)
                self.counts["labels created"] += 1
            self._board_labels[name] = label
        return self._board_labels[name]

    # -- the run --------------------------------------------------------------------------------------------------

    def run(self):
        bound.sync_board(self.board, self.by)
        by_key, by_link = self.deals()
        companies = self.companies()
        cards = {c["id"]: c for c in self.data["cards"]}
        hub = {}  # Trello card id -> (Card, opportunity, company)
        list_pos = {i: l.get("pos") or 0 for i, l in self.lists.items()}  # new lists come in Trello's order
        for tc in sorted(self.data["cards"], key=lambda c: (list_pos.get(c["idList"], 0), c.get("pos") or 0)):
            if tc.get("cardRole") == "mirror" or tc.get("mirrorSourceId"):
                self.counts["mirror cards skipped"] += 1
                continue
            deal = self.deal_for(tc, by_key, by_link)
            card = None
            if deal is not None:
                card = (Card.objects.filter(list__board=self.board, opportunity=deal).order_by("archived", "id").first())
            if card is not None:
                self.counts["cards: deal"] += 1
                hub[tc["id"]] = (card, deal, None)
            else:
                company_id = deal.company_id if deal else companies.get(merge.core_name(tc["name"]))
                card = self.own_card(tc, company_id)
                hub[tc["id"]] = (card, None, card.company)
                self.counts["cards: company" if company_id else "cards: unlinked"] += 1
            self.add_labels_and_members(card, tc)
            self.checklists(card, tc)
            self.attachments(card, tc)
        self.actions(hub, cards)
        return self.counts

    def own_card(self, tc, company_id):
        key = f"trello:{tc['id']}"
        card = Card.objects.filter(source_key=key).first()
        if card is not None:
            return card
        lst = self.hub_list(self.lists.get(tc["idList"], {"name": "Trello", "closed": True}))
        card = Card.objects.create(
            list=lst, position=logic.next_position(lst), title=(tc["name"] or "(no name)").strip()[:300],
            description=tc.get("desc") or "", due=(when(tc.get("due")).date() if tc.get("due") else None),
            start=(when(tc.get("start")).date() if tc.get("start") else None), done=bool(tc.get("dueComplete")),
            archived=bool(tc.get("closed")), company_id=company_id, source_key=key, created_by=self.by,
        )
        Card.objects.filter(pk=card.pk).update(created_at=created_from_id(tc["id"]))
        self.counts["cards created"] += 1
        return card

    def add_labels_and_members(self, card, tc):
        labels = [l for l in (self.board_label(self.labels.get(i, {})) for i in tc.get("idLabels", [])) if l]
        if labels:
            card.labels.add(*labels)
        members = [u for u in (self.member_user(i) for i in tc.get("idMembers", [])) if u]
        if members:
            card.members.add(*members)

    def checklists(self, card, tc):
        for n, cl in enumerate(sorted(tc.get("checklists", []), key=lambda c: c.get("pos") or 0), 1):
            name = (cl.get("name") or "Checklist")[:120]
            if card.checklists.filter(name=name, position=n).exists():
                continue
            checklist = Checklist.objects.create(card=card, name=name, position=n)
            self.counts["checklists"] += 1
            for m, item in enumerate(sorted(cl.get("checkItems", []), key=lambda i: i.get("pos") or 0), 1):
                ChecklistItem.objects.create(
                    checklist=checklist, text=(item.get("name") or "")[:300], done=item.get("state") == "complete",
                    due=(when(item["due"]).date() if item.get("due") else None),
                    assignee=self.member_user(item.get("idMember")), position=m,
                )
                self.counts["checklist items"] += 1

    def attachments(self, card, tc):
        for a in tc.get("attachments", []):
            url = (a.get("url") or "")[:1000]
            if not url or card.attachments.filter(url=url).exists():
                continue
            att = CardAttachment.objects.create(card=card, name=(a.get("name") or a.get("fileName") or url)[:300], url=url,
                                                added_by=self.member_user(a.get("idMember")))
            if a.get("date"):
                CardAttachment.objects.filter(pk=att.pk).update(added_at=when(a["date"]))
            self.counts["attachments"] += 1

    def actions(self, hub, cards):
        for a in sorted(self.data["actions"], key=lambda a: a["date"]):
            card_id = (a.get("data", {}).get("card") or {}).get("id")
            if not card_id:
                continue
            target = hub.get(card_id)
            if a["type"] == "commentCard":
                if target is None:
                    self.counts["comments skipped (card deleted or moved off the board)" if card_id not in cards
                                else "comments skipped (mirror card)"] += 1
                    continue
                self.comment(target, a)
            elif target is not None:
                self.event(target[0], a)

    def comment(self, target, a):
        card, deal, company = target
        user, name = self.person(a.get("memberCreator"))
        text = a["data"].get("text") or ""
        body = text if user else f"{name} (in Trello): {text}"
        at = when(a["date"])
        if Comment.objects.filter(card=card, created_at=at, body=body).exists():
            self.counts["comments already there"] += 1
            return
        c = Comment.objects.create(body=body, author=user, card=card, opportunity=deal, company=company)
        Comment.objects.filter(pk=c.pk).update(created_at=at)
        kind = "deal" if deal else "company" if company else "unlinked"
        self.counts[f"comments onto {kind} cards"] += 1
        if not user:
            self.outsiders[name] += 1

    def event(self, card, a):
        data = a["data"]
        if a["type"] == "createCard":
            text = f"created this card in Trello on {(data.get('list') or {}).get('name', 'a list')}"
        elif a["type"] == "updateCard" and "listAfter" in data:
            text = f"moved this card from {data['listBefore']['name']} to {data['listAfter']['name']} in Trello"
        elif a["type"] == "updateCard" and "closed" in data.get("old", {}):
            text = "archived this card in Trello" if data["card"].get("closed") else "sent this card back to the board in Trello"
        elif a["type"] in ("copyCard", "moveCardToBoard"):
            text = "copied this card in Trello" if a["type"] == "copyCard" else "moved this card onto the board in Trello"
        else:
            return
        user, name = self.person(a.get("memberCreator"))
        if not user:
            text = f"{name} {text}"
        text = text[:300]
        at = when(a["date"])
        if CardEvent.objects.filter(card=card, kind="trello", when=at, text=text).exists():
            return
        CardEvent.objects.create(card=card, user=user, kind="trello", text=text, when=at)
        self.counts["events"] += 1


def tenant_board():
    return Board.objects.filter(kind=Board.Kind.BOUND, record_type="opportunity", campus=None, project=None).first()


@transaction.atomic
def import_prospecting(data, by=None):
    board = tenant_board()
    if board is None:
        raise ValueError("No Tenant pipeline board yet. Run manage.py boards_setup --apply first.")
    imp = Importer(data, board, by)
    imp.run()
    return imp
