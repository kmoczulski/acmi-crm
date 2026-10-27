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
import re
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


def label_key(name):
    """'Program/Procurement' and 'Program / procurement' are the same label."""
    return re.sub(r"\s*/\s*", "/", name.strip().lower())


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
            label = next((l for l in self.board.labels.all() if label_key(l.name) == label_key(name)), None)
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
        hub = {}  # Trello card id -> (Card, {Comment field: record} its comments also show on)
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
                hub[tc["id"]] = (card, {"opportunity": deal})
            else:
                company_id = deal.company_id if deal else companies.get(merge.core_name(tc["name"]))
                card = self.own_card(tc, self.hub_list(self.lists.get(tc["idList"], {"name": "Trello", "closed": True})),
                                     company_id=company_id)
                hub[tc["id"]] = (card, {"company": card.company} if card.company_id else {})
                self.counts["cards: company" if company_id else "cards: unlinked"] += 1
            self.add_labels_and_members(card, tc)
            self.checklists(card, tc)
            self.attachments(card, tc)
        self.actions(hub, cards)
        return self.counts

    def own_card(self, tc, lst, archived=None, **links):
        """The hub card made for a Trello card (keyed `trello:<id>`), on `lst`, linked by `links` (company_id=…)."""
        key = f"trello:{tc['id']}"
        card = Card.objects.filter(source_key=key).first()
        if card is not None:
            return card
        card = Card.objects.create(
            list=lst, position=logic.next_position(lst), title=(tc["name"] or "(no name)").strip()[:300],
            description=tc.get("desc") or "", due=(when(tc.get("due")).date() if tc.get("due") else None),
            start=(when(tc.get("start")).date() if tc.get("start") else None), done=bool(tc.get("dueComplete")),
            archived=bool(tc.get("closed")) if archived is None else archived, source_key=key, created_by=self.by,
            **links,
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

    def actions(self, hub, cards, redirect=None):
        """Comments and card activity, oldest first. `redirect` sends a skipped card's comments to another card."""
        redirect = redirect or {}
        for a in sorted(self.data["actions"], key=lambda a: a["date"]):
            card_id = (a.get("data", {}).get("card") or {}).get("id")
            if not card_id:
                continue
            target = hub.get(card_id)
            if a["type"] == "commentCard" and target is None and card_id in redirect:
                target = hub.get(redirect[card_id])
            if a["type"] == "commentCard":
                if target is None:
                    self.counts["comments skipped (card deleted or moved off the board)" if card_id not in cards
                                else "comments skipped (mirror card)"] += 1
                    continue
                self.comment(target, a)
            elif target is not None:
                self.event(target[0], a)

    def comment(self, target, a):
        card, links = target
        user, name = self.person(a.get("memberCreator"))
        text = a["data"].get("text") or ""
        body = text if user else f"{name} (in Trello): {text}"
        at = when(a["date"])
        if Comment.objects.filter(card=card, created_at=at, body=body).exists():
            self.counts["comments already there"] += 1
            return
        c = Comment.objects.create(body=body, author=user, card=card, **links)
        Comment.objects.filter(pk=c.pk).update(created_at=at)
        field = card.record_field()
        kind = {"opportunity": "deal"}.get(field, field) or "unlinked"
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


# ---------------------------------------------------------------------------
# Maryland / MEIH → the MEIH board (docs/TRELLO_MAPPING.md, "The two MEIH boards", with Kellen's answers of
# 2026-10-27). Cards are placed by hand: MEIH cards are named after addresses and tasks, so names don't match records.
# ---------------------------------------------------------------------------

FORGE, ARA_DEAL, ARA_SITES, CONSULTANTS, SITES, DATES = (
    "The Forge – 4390 / ETC", "ARA Facility – deal", "ARA Facility – sites and design", "Consultants", "Land sites",
    "Critical dates")
AIRPORT_LAND = "Airport Land – 4075 Livingston Rd (County)"
FORGE_SITE = "4390 Indian Head Hwy (ETC)"
ARA = "Applied Research Associates"

# Trello list → (hub list for its cards, True to put them all there archived).
MEIH_LISTS = {
    "Land Sites": (SITES, False),
    "COLD - Land Sites": (SITES, True),
    "4390 / ETC - Deal and Design": (FORGE, False),
    "County Land - Deal and Design": (ARA_SITES, False),
    "LABP - Deal and Design": (ARA_SITES, False),
    "ARA Building": (ARA_SITES, False),
    "Land Use": (ARA_SITES, False),
    "Architecture & Engineering": (ARA_SITES, True),
    "ARA": (ARA_DEAL, False),
    "Contacts and Consultants": (CONSULTANTS, False),
    "Consultants / Stakeholders": (CONSULTANTS, True),
    "Transaction & Construction Attorney": (CONSULTANTS, True),
}
# (Trello list, card name) → (hub list, or None for the list's own; record kind; record name). Records are found by
# name. A list's other cards are plain, except ARA's, which all link to the ARA deal.
MEIH_CARDS = {
    ("Land Sites", "NSWC - IHD"): (CONSULTANTS, None, None),  # Kellen: managing the relationship with the base
    ("Land Sites", "Lake Acton Business Park"): (None, "site", "Lake Acton Business Park"),
    ("Land Sites", "County Land"): (None, "site", AIRPORT_LAND),
    ("Land Sites", "Garner Land"): (None, "site", "Garner – 9355 Parkway Subdivision Rd"),
    ("Land Sites", "Rosewick Development Site"): (None, "site", "Rosewick Road"),
    ("Land Sites", "ETC Building"): (None, "site", FORGE_SITE),
    ("Land Sites", "Lewis Place"): (None, "site", "Bryans Road Assemblage"),  # Kellen: Lewis Place is Bryans Road
    ("Land Sites", "Billingsley Assemblage"): (None, "site", "Billingsley WCD Assemblage"),
    ("Land Sites", "4390 Indian Head"): (None, "site", FORGE_SITE),
    ("Land Sites", "Groundbreaking"): (FORGE, None, None),
    ("Land Sites", "Civl Engineering"): (FORGE, None, None),
    ("Land Sites", "Architectural RFP"): (FORGE, None, None),
    ("Land Sites", "Notice required for Prometheus extension (1/27)"): (FORGE, None, None),
    ("COLD - Land Sites", "CA Billingsley Site: Off-market"): (None, "site", "Billingsley Rd – CA Billingsley / St Charles Community"),
    ("COLD - Land Sites", "Berry Pointe - St. Johns"): (None, "site", "Berry Pointe (St. John Properties)"),
    ("COLD - Land Sites", "Berry Rd - Assemblage"): (None, "site", "Berry Rd / Tara Pl Assemblage"),
    ("COLD - Land Sites", "Copart Site"): (None, "site", "Fox Tail Pl / Crain Hwy Assemblage (Copart)"),
    ("Contacts and Consultants", "Construction Attorney - Cozen O'Connor (Engaged)"): (None, "firm", "Cozen O'Connor"),
    ("Contacts and Consultants", "Kimley Horn"): (None, "firm", "Kimley Horn"),
    ("Contacts and Consultants", "Maryland Department of Commerce"): (None, "firm", "Maryland Department of Commerce"),
    ("County Land - Deal and Design", "County Land PSA"): (None, "site", AIRPORT_LAND),
    ("County Land - Deal and Design", "County Land"): (None, "site", AIRPORT_LAND),
    ("LABP - Deal and Design", "PSA Negotiation"): (None, "site", "Lake Acton Business Park"),
    ("LABP - Deal and Design", "4390 Indian Head Hwy - Design Meetings / Plans"): (FORGE, "site", FORGE_SITE),
    ("4390 / ETC - Deal and Design", "4390 Indian Head Hwy - PSA & ETC Lease"): (None, "contract", "4390 Indian Head Hwy PSA"),
    ("4390 / ETC - Deal and Design", "4390 Indian Head Hwy"): (None, "site", FORGE_SITE),
    ("4390 / ETC - Deal and Design", "4390 Indian Head Hwy - Powers Brown B101"): (None, "firm", "Powers Brown"),
}
# ARA's dated cards (invoices, lease and PDA dates) wait for the final PDA (Kellen, 2026-10-27); their comments and
# attachments go onto the ARA deal's "Contact List & Agreement" card.
ARA_DATED = ("Invoice ARA", "Update Pre-Development budget", "Update pre-development budget", "Initiate Lease",
             "Complete Lease", "Non-renewal notice", "PDA initial term")
PSA_DATES_LIST = "4390 Indian Head Hwy - Critical Dates"
# Firms Kellen had added to the directory for these cards (2026-10-27).
NEW_FIRMS = [("Kimley Horn", "engineer"), ("Maryland Department of Commerce", "agency"), ("Powers Brown", "architect")]
# The Comment field a record kind's comments also show on (firms have none).
COMMENT_FIELD = {"site": "site", "contract": "contract", "opportunity": "opportunity", "company": "company"}


class MeihImporter(Importer):
    def record(self, kind, name):
        from ..models import Contract, Firm, LandSite

        if kind == "site":
            return LandSite.objects.filter(name=name).first()
        if kind == "contract":
            return Contract.objects.filter(title=name).first()
        if kind == "firm":
            return Firm.objects.filter(name=name).first()
        if kind == "opportunity":
            return Opportunity.objects.filter(company__name=name, campus=self.board.campus).order_by("pk").first()
        return None

    def target_list(self, name):
        lst = self.board.lists.filter(name=name).first()
        if lst is None:
            raise ValueError(f"The MEIH board has no list called {name!r}. Run manage.py boards_setup --apply first.")
        return lst

    def place(self, hub, tc, list_name, kind=None, rec_name=None, archived=None):
        record = self.record(kind, rec_name) if kind else None
        if kind and record is None:
            self.counts[f"record not found: {rec_name}"] += 1
        card = self.own_card(tc, self.target_list(list_name), archived=archived,
                             **({kind: record} if record is not None else {}))
        hub[tc["id"]] = (card, {COMMENT_FIELD[kind]: record} if record is not None and kind in COMMENT_FIELD else {})
        self.counts[f"cards: {kind if record is not None else 'plain'}"] += 1
        self.add_labels_and_members(card, tc)
        self.checklists(card, tc)
        self.attachments(card, tc)
        return card

    def run(self):
        from ..models import Firm

        for name, kind in NEW_FIRMS:
            if not Firm.objects.filter(name__iexact=name).exists():
                Firm.objects.create(name=name, kind=kind)
                self.counts["firms added to the directory"] += 1
        hub, cards = {}, {c["id"]: c for c in self.data["cards"]}
        list_pos = {i: l.get("pos") or 0 for i, l in self.lists.items()}
        held = {"psa": None, "ara": None}  # the cards skipped date cards' history goes onto
        dated = []
        for tc in sorted(self.data["cards"], key=lambda c: (list_pos.get(c["idList"], 0), c.get("pos") or 0)):
            tlist = self.lists.get(tc["idList"], {}).get("name", "")
            if tlist == PSA_DATES_LIST:
                if tc["name"] == "4390 Indian Head Highway PSA":
                    held["psa"] = tc["id"]
                    self.place(hub, tc, DATES, "contract", "4390 Indian Head Hwy PSA")
                else:
                    dated.append((tc, "psa"))
                continue
            if tlist == "ARA" and tc["name"].startswith(ARA_DATED):
                dated.append((tc, "ara"))
                continue
            if tlist not in MEIH_LISTS:
                self.counts[f"cards skipped (list {tlist})"] += 1
                continue
            hub_name, all_archived = MEIH_LISTS[tlist]
            over, kind, rec_name = MEIH_CARDS.get((tlist, tc["name"]), (None, None, None))
            if tlist == "ARA":
                kind, rec_name = "opportunity", ARA
                if tc["name"] == "Contact List & Agreement":
                    held["ara"] = tc["id"]
            self.place(hub, tc, over or hub_name, kind, rec_name, archived=True if all_archived else None)
        redirect = {}
        for tc, where in dated:
            self.counts["4390 date cards not imported (the contract has the dates)" if where == "psa" else
                        "ARA date cards not imported (waiting for the PDA)"] += 1
            into = held[where]
            if into in hub:
                redirect[tc["id"]] = into
                self.attachments(hub[into][0], tc)
        self.actions(hub, cards, redirect)
        return self.counts


def meih_board():
    from ..models import Campus

    campus = Campus.objects.filter(code="MEIH").first()
    return Board.objects.filter(campus=campus, name="MEIH").first() if campus else None


@transaction.atomic
def import_meih(data, by=None):
    board = meih_board()
    if board is None:
        raise ValueError("No MEIH board yet. Run manage.py boards_setup --apply first.")
    imp = MeihImporter(data, board, by)
    imp.run()
    return imp
