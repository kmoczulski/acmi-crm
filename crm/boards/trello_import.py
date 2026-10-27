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
    """'Program/Procurement' and 'Program / procurement', 'To Do' and 'To-Do', 'Ongoing' and 'On-Going' are the same."""
    return re.sub(r"[\s-]+", "", name.strip().lower())


def team_users():
    users = {}
    for full in TEAM:
        first, last = full.split(" ", 1)
        u = User.objects.filter(first_name=first, last_name=last, is_active=True).order_by("pk").first()
        if u:
            users[full] = u
    return users


class Importer:
    skip_labels = SKIP_LABELS

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
        if not name or name in self.skip_labels:
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
                                else "comments skipped (card not imported)"] += 1
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


# ---------------------------------------------------------------------------
# Properties-NSIH → a new NSIH board (docs/TRELLO_MAPPING.md, "Properties-NSIH", Kellen took the six recommended
# answers on 2026-10-27): Trello's workstream lists kept, cards linked to the deals and directory firms they name.
# ---------------------------------------------------------------------------

PROMETHEUS = "Prometheus (Kratos / Rafael)"
# (hub list, feed): the board's lists in order.
NSIH_BOARD_LISTS = [("Administration", ""), ("Design & construction", ""), ("Due diligence", ""),
                    ("Infrastructure / utilities", ""), ("Land contracts", ""), ("Parcel owners", ""),
                    ("Tenants and leases", ""), ("Capitalization", ""), ("Models & decks", ""), ("Open tasks", "open_tasks")]
# Trello list → (hub list, True to put all its cards there archived).
NSIH_LISTS = {
    "Administration": ("Administration", False),
    "Design & Construction": ("Design & construction", False),
    "Due Diligence": ("Due diligence", False),
    "Infrastructure / Utilities": ("Infrastructure / utilities", False),
    "Land Contracts": ("Land contracts", False),
    "Parcel Owners": ("Parcel owners", False),
    "Tenancy/Contracts": ("Tenants and leases", False),
    "Capitalization": ("Capitalization", False),
    "Models & Decks": ("Models & decks", False),
    "Prospecting": ("Tenants and leases", True),
}
# Card name → (record kind, record name). Deals are found by their company's name; firms by name.
NSIH_CARDS = {
    "Insurance - Marsh": ("firm", "Marsh McLennan Agency"),
    "Tax Incentives - Ryan LLC": ("firm", "Ryan LLC"),
    "Legal - Drewry Simmons Vornehm": ("firm", "Drewry Simmons Vornehm"),
    "Architect - Haskell": ("firm", "Haskell"),
    "Scope Comparison - Haskell Contract and RFP": ("firm", "Haskell"),
    "Change Orders - Haskell - Landscape & Interior Design": ("firm", "Haskell"),
    "Design Deliverables - Haskell": ("firm", "Haskell"),
    "Value Engineering - Haskell / P|W": ("firm", "Haskell"),
    "CM - Pepper / Weddle": ("firm", "Pepper Construction"),
    "Civil - Structurepoint": ("firm", "Structurepoint"),
    "Schreifer Group": ("firm", "The Schreifer Group"),
    "CMc Contract - Shiel Sexton": ("firm", "Shiel Sexton Company, Inc."),
    "Ryan, LLC - Credits & Incentives": ("firm", "Ryan LLC"),
    "Environmental Permitting - Earthsource": ("firm", "Earthsource"),
    "Risk Management Plan - SMS Energetics": ("firm", "SMS Energetics"),
    "Environmental - TRC Companies": ("firm", "TRC Companies"),
    "Site Signage - Johnny's Signs": ("firm", "Johnny's Signs"),
    "Fiber - Smithville": ("firm", "Smithville"),
    "ROW Acquisition - First Group Engineering": ("firm", "First Group Engineering"),
    "Solar - APT Solar Solutions": ("firm", "APT Solar Solutions"),
    "Prometheus - Ground Lease": ("opportunity", PROMETHEUS),
    "Prometheus - DMA": ("opportunity", PROMETHEUS),
    "Prometheus Points of Contact Rules": ("opportunity", PROMETHEUS),
    "Export Control (MLA) - Prometheus": ("opportunity", PROMETHEUS),
    "Lease - Prometheus": ("opportunity", PROMETHEUS),
    "Kratos": ("opportunity", PROMETHEUS),  # Kellen: the Kratos LOI cards belong to the Prometheus deal
    "LOI - Kratos": ("opportunity", PROMETHEUS),
    "Ground Lease - iRocket": ("opportunity", "iRocket"),
    "Tenant - Spinner": ("opportunity", "Spinner"),
    "CFIUS Review - Holland & Knight": ("firm", "Holland & Knight"),
    "CTL Loan - CTL Capital": ("firm", "CTL Capital"),
    "Strongbox Partnership": ("firm", "Strongbox"),
    "Browning Partnership": ("firm", "Browning"),
    "OneIM": ("firm", "OneIM"),
    "Bain": ("firm", "Bain Capital"),
    "Fortress": ("firm", "Fortress"),
    "Cerebus": ("firm", "Cerberus"),
}
# Firms Kellen had added to the directory for these cards (2026-10-27).
NSIH_NEW_FIRMS = [
    ("Ryan LLC", "accounting"), ("Drewry Simmons Vornehm", "law"), ("Earthsource", "environmental"),
    ("SMS Energetics", "consultant"), ("TRC Companies", "environmental"), ("Johnny's Signs", "vendor"),
    ("APT Solar Solutions", "contractor"), ("Weddle", "contractor"), ("Fortress", "investor"), ("Strongbox", "investor"),
    ("Browning", "investor"), ("OneIM", "investor"), ("Cerberus", "investor"), ("CTL Capital", "debt_fund"),
]


class NsihImporter(MeihImporter):
    def record(self, kind, name):
        if kind == "opportunity":  # prefer the NSIH deal when a company has deals on several campuses
            qs = Opportunity.objects.filter(company__name=name)
            return qs.filter(campus=self.board.campus).order_by("pk").first() or qs.order_by("pk").first()
        return super().record(kind, name)

    def run(self):
        from ..models import Firm

        for name, kind in NSIH_NEW_FIRMS:
            if not Firm.objects.filter(name__iexact=name).exists():
                Firm.objects.create(name=name, kind=kind)
                self.counts["firms added to the directory"] += 1
        # Kellen: the Prometheus deal is an NSIH deal (it had no campus).
        if Opportunity.objects.filter(company__name=PROMETHEUS, campus=None).update(campus=self.board.campus):
            self.counts["Prometheus deal set to NSIH"] += 1
        hub, cards = {}, {c["id"]: c for c in self.data["cards"]}
        list_pos = {i: l.get("pos") or 0 for i, l in self.lists.items()}
        for tc in sorted(self.data["cards"], key=lambda c: (list_pos.get(c["idList"], 0), c.get("pos") or 0)):
            tlist = self.lists.get(tc["idList"], {}).get("name", "")
            if tc.get("cardRole") == "mirror" or tc.get("mirrorSourceId") or tc["name"].startswith("https://trello.com/c/"):
                self.counts["mirror and link cards skipped"] += 1
                continue
            if tlist not in NSIH_LISTS:
                self.counts[f"cards skipped (list {tlist})"] += 1
                continue
            hub_name, all_archived = NSIH_LISTS[tlist]
            kind, rec_name = NSIH_CARDS.get(tc["name"], (None, None))
            self.place(hub, tc, hub_name, kind, rec_name, archived=True if all_archived else None)
        self.actions(hub, cards)
        return self.counts


def nsih_board(by=None):
    """The NSIH board, made the first time with its lists and the team as members."""
    from ..models import Campus, Project

    campus = Campus.objects.filter(code="NSIH").first()
    if campus is None:
        return None
    board = Board.objects.filter(campus=campus, name="NSIH").first()
    if board is None:
        board = logic.create_board("NSIH", by, kind=Board.Kind.CAMPUS, campus=campus,
                                   project=Project.objects.filter(campus=campus, parent=None).order_by("pk").first(),
                                   description="National Security Industrial Hub, Bloomfield, Indiana. The workstream "
                                               "lists from the team's Properties-NSIH Trello board.")
        for n, (name, feed) in enumerate(NSIH_BOARD_LISTS, 1):
            BoardList.objects.create(board=board, name=name, position=n * logic.GAP, feed=feed)
        board.members.add(*team_users().values())
    return board


@transaction.atomic
def import_nsih(data, by=None):
    board = nsih_board(by)
    if board is None:
        raise ValueError("No NSIH campus.")
    imp = NsihImporter(data, board, by)
    imp.run()
    return imp


# ---------------------------------------------------------------------------
# Properties-General → "ACMI general", an archived "Properties-General archive" board, and a few cards on the NSIH
# and MEIH boards (docs/TRELLO_MAPPING.md, "Properties-General"; Kellen's answers of 2026-10-27: the NSIH and
# Hostetler date cards wait for the PSA and related documents, whose history goes onto the NSIH board's cards).
# ---------------------------------------------------------------------------

GENERAL = "ACMI general"
GENERAL_ARCHIVE = "Properties-General archive"
NIRON_LIST = "Project North Star – Niron"
# Trello list → (board, hub list, True to put its cards there archived). Lists not named here are skipped.
GENERAL_LISTS = {
    "Press / Postings": (GENERAL, "Press / postings", False),
    "Administrative": (GENERAL, "Administrative", False),
    "Other Markets": (GENERAL, "Other markets", False),
    "Airport Commerce": (GENERAL, "Airport Commerce", False),
    "Project North Star - Niron Magnetics": (GENERAL, NIRON_LIST, False),
    "NSIH (Bloomfield, IN)": ("NSIH", "Administration", True),
    "MEIH - ARA": ("MEIH", "ARA Facility – deal", True),
    **{name: (GENERAL_ARCHIVE, name, False) for name in [
        "Avio", "Critical Dates - Avio", "Strategic", "RRAD", "Propulsion Park (San Marcos, TX) - Tasks [David]",
        "Propulsion Park (San Marcos, TX) - Critical Dates [David]", "Precision Park (Lafayette, IN) - Tasks [David]",
        "Precision Park (Lafayette, IN) - Critical Dates [David]", "King Ranch - Aurora, CO", "TexAmericas",
        "U.S. Army Enterprise Strategic Capital Partnership", "Prospecting", "Tenant Followups"]},
}
# The NSIH date lists: not imported until the PSA and related documents are uploaded (Kellen). Their comments and
# attachments go onto the NSIH board card for the agreement they come from.
GENERAL_DATE_LISTS = ("Critical Dates - NSIH", "Critical Dates - Hostetler / NSIH",
                      "NSIH (Bloomfield, IN) - Critical Dates [David]")
# Skipped like their own boards (Kellen, 2026-10-26).
GENERAL_SKIPPED = ("Exploration Park (Houston, TX)", "Exploration Park (Houston, TX) - Critical Dates [Kellen]",
                   "Exploration Park - Archive", "Project Volare", "Volare (Hurt, VA)")
GENERAL_CARDS = {
    "Spinner Modifications": ("opportunity", "Spinner"),
    "Airport Commerce: Spinner Questions": ("opportunity", "Spinner"),
    "Spinner Facility Design": ("opportunity", "Spinner"),
    "Supernova Modifications": ("opportunity", "Supernova"),
    "Vendor: The Schreifer Group": ("firm", "The Schreifer Group"),
    "ARA - Lease Negotiation": ("opportunity", "Applied Research Associates"),
}


def date_card_home(list_name, card_name):
    """The NSIH board card a skipped date card's history goes onto."""
    if list_name == "Critical Dates - Hostetler / NSIH":
        return "Land PSA - Hostetler"
    if "Ground Lease" in card_name:
        return "Prometheus - Ground Lease"
    if "Project Control" in card_name or "Invoice" in card_name:
        return "Prometheus - DMA"
    return "Land PSA - NFP"


class GeneralImporter(NsihImporter):
    skip_labels = {"High Value", "Medium Value", "Low Value"}

    def __init__(self, data, boards, by=None):
        super().__init__(data, boards[GENERAL], by)
        self.boards = boards
        self._label_caches = {}

    def use(self, board):
        """Point label matching and list lookups at `board`."""
        self.board = board
        self._board_labels = self._label_caches.setdefault(board.pk, {})

    def run(self):
        hub, cards, redirect = {}, {c["id"]: c for c in self.data["cards"]}, {}
        list_pos = {i: l.get("pos") or 0 for i, l in self.lists.items()}
        homes = {}
        for tc in sorted(self.data["cards"], key=lambda c: (list_pos.get(c["idList"], 0), c.get("pos") or 0)):
            tlist = self.lists.get(tc["idList"], {}).get("name", "")
            if tc.get("cardRole") == "mirror" or tc.get("mirrorSourceId") or tc["name"].startswith("https://trello.com/c/"):
                self.counts["mirror and link cards skipped"] += 1
                continue
            if tlist in GENERAL_DATE_LISTS:
                home = date_card_home(tlist, tc["name"])
                if home not in homes:
                    self.use(self.boards["NSIH"])
                    card = Card.objects.filter(list__board=self.board, title=home).order_by("pk").first()
                    homes[home] = card
                    if card is not None:
                        field = card.record_field()
                        hub[f"home:{home}"] = (card, {COMMENT_FIELD[field]: card.record()} if field in COMMENT_FIELD else {})
                if homes[home] is not None:
                    redirect[tc["id"]] = f"home:{home}"
                    self.attachments(homes[home], tc)
                self.counts["NSIH date cards not imported (waiting for the PSA)"] += 1
                continue
            if tlist in GENERAL_SKIPPED:
                self.counts["Exploration Park and Volare cards skipped"] += 1
                continue
            if tlist not in GENERAL_LISTS:
                self.counts[f"cards skipped (list {tlist})"] += 1
                continue
            board_name, list_name, all_archived = GENERAL_LISTS[tlist]
            self.use(self.boards[board_name])
            kind, rec_name = GENERAL_CARDS.get(tc["name"], (None, None))
            if tlist == "Project North Star - Niron Magnetics":
                kind, rec_name = "opportunity", "Niron Magnetics"
            self.place(hub, tc, list_name, kind, rec_name, archived=True if all_archived else None)
            self.counts[f"cards onto {board_name}"] += 1
        self.actions(hub, cards, redirect)
        return self.counts

    def target_list(self, name):
        lst = self.board.lists.filter(name=name).first()
        if lst is None:
            if self.board.name not in (GENERAL, GENERAL_ARCHIVE):
                raise ValueError(f"The {self.board.name} board has no list called {name!r}.")
            last = self.board.lists.order_by("-position").first()
            lst = BoardList.objects.create(board=self.board, name=name[:120], archived=(name == NIRON_LIST),
                                           position=(last.position if last else 0) + logic.GAP)
            self.counts["lists created"] += 1
        return lst


def general_boards(by=None):
    """{name: board}: the two team boards (made the first time) and the NSIH and MEIH boards."""
    boards = {}
    for name, archived, description in [
        (GENERAL, False, "The team's general lists from the Properties-General Trello board: press and postings, "
                         "administrative, other markets, Airport Commerce."),
        (GENERAL_ARCHIVE, True, "Old campuses and projects from the Properties-General Trello board (Avio, Strategic, "
                                "RRAD, Propulsion Park, Precision Park, King Ranch, TexAmericas…), kept for their history."),
    ]:
        board = Board.objects.filter(name=name, kind=Board.Kind.TEAM).first()
        if board is None:
            board = logic.create_board(name, by, kind=Board.Kind.TEAM, description=description)
            board.members.add(*team_users().values())
            if archived:
                board.archived = True
                board.save(update_fields=["archived"])
        boards[name] = board
    boards["NSIH"], boards["MEIH"] = nsih_board(by), meih_board()
    return boards


@transaction.atomic
def import_general(data, by=None):
    boards = general_boards(by)
    if boards["NSIH"] is None or boards["MEIH"] is None:
        raise ValueError("Import the NSIH and MEIH boards first.")
    imp = GeneralImporter(data, boards, by)
    imp.run()
    return imp


# ---------------------------------------------------------------------------
# Properties-Campus Pursuits → "Campus pursuits" and Properties-Capitalization → "Capital partners", two team boards
# (docs/TRELLO_MAPPING.md; Kellen took the five recommended answers on 2026-10-27).
# ---------------------------------------------------------------------------

PURSUITS = "Campus pursuits"
PURSUITS_LISTS = {  # Trello list → (hub list, True to put its cards there archived)
    "Strategic Project Priority - High": ("High priority", False),
    "Strategic Project Priority - Medium": ("Medium priority", False),
    "Strategic Project Priority - Low": ("Low priority", False),
    "Holding / Overflow": ("Holding / overflow", False),
    "Strategic Initiatives": ("Holding / overflow", True),
    "2025 Strategic Project Ranking Overall": ("Holding / overflow", True),
}
# Card name (as it starts) → the deal it's about, found by the deal's company.
PURSUITS_DEALS = [
    ("TBD - Elbit America", "Elbit America / 26Defense"), ("Niron RFP", "Niron Magnetics"),
    ("Thales - TBD location", "Thales"), ("SAIC - South Carolina", "SAIC"),
    ("MD - Johns Hopkins / APL", "Johns Hopkins University"), ("Long Wall (Indiana Munitions Campus)", "Long Wall"),
    ("Minnesota / North Wind", "North Wind"), ("Minnesota - Northwind", "North Wind"), ("North Wind", "North Wind"),
    ("Project:\xa0Saab", "Saab"), ("Project: Saab", "Saab"), ("Vulcan Forms & Specter Aerospace", "Specter Aerospace"),
    ("Castelion", "Castelion"),
]

CAPITAL = "Capital partners"
CAPITAL_LISTS = {
    "Intro Conversations": ("Intro conversations", False),
    "Acitive Discussions": ("Active discussions", False),
    "Term Sheet / Negotiation": ("Term sheet / negotiation", False),
    "Committed / Closed": ("Committed / closed", False),
    "Passed / Not a Fit": ("Passed / not a fit", False),
}
FORTRESS = "Fortress Investment Group"
CAPITAL_FIRMS = {  # card name → directory firm
    "BGO": "BGO", "Wellington": "Wellington", "Montgomery Street Partners": "Montgomery Street Partners, Inc.",
    "Koch Real Estate Advisors": "Koch Real Estate", "Temerity": "Temerity", "Silver Peak": "Silver Peak",
    "Bain Capital": "Bain Capital", "OneIM": "OneIM", "CTL": "CTL Capital", "Davis Companies": "Davis Companies",
    "L&B Capital": "L&B Capital", "Blue Owl": "Blue Owl", "Cerberus": "Cerberus", FORTRESS: FORTRESS, "JRE": "JRE",
}
CAPITAL_NEW_FIRMS = ["Wellington", "Temerity", "Silver Peak", "Davis Companies", "L&B Capital", "Blue Owl", "JRE"]


def team_board(name, lists, by=None, description=""):
    """A team board with these lists (made the first time, with the team as members)."""
    board = Board.objects.filter(name=name, kind=Board.Kind.TEAM).first()
    if board is None:
        board = logic.create_board(name, by, kind=Board.Kind.TEAM, description=description)
        for n, list_name in enumerate(lists, 1):
            BoardList.objects.create(board=board, name=list_name, position=n * logic.GAP)
        board.members.add(*team_users().values())
    return board


class TeamBoardImporter(NsihImporter):
    """Trello lists onto a team board's lists (`list_map`); `link(card)` gives a card's (kind, record name)."""
    skip_labels = {"High Value", "Medium Value", "Low Value"}
    list_map = {}

    def link(self, tc):
        return None, None

    def run(self):
        hub, cards = {}, {c["id"]: c for c in self.data["cards"]}
        list_pos = {i: l.get("pos") or 0 for i, l in self.lists.items()}
        for tc in sorted(self.data["cards"], key=lambda c: (list_pos.get(c["idList"], 0), c.get("pos") or 0)):
            tlist = self.lists.get(tc["idList"], {}).get("name", "")
            if tc.get("cardRole") == "mirror" or tc.get("mirrorSourceId") or tc["name"].startswith("https://trello.com/c/"):
                self.counts["mirror and link cards skipped"] += 1
                continue
            if tlist not in self.list_map:
                self.counts[f"cards skipped (list {tlist})"] += 1
                continue
            list_name, all_archived = self.list_map[tlist]
            kind, rec_name = self.link(tc)
            self.place(hub, tc, list_name, kind, rec_name, archived=True if all_archived else None)
        self.actions(hub, cards)
        return self.counts


class PursuitsImporter(TeamBoardImporter):
    list_map = PURSUITS_LISTS

    def link(self, tc):
        deal = next((company for start, company in PURSUITS_DEALS if tc["name"].startswith(start)), None)
        return ("opportunity", deal) if deal else (None, None)


class CapitalImporter(TeamBoardImporter):
    list_map = CAPITAL_LISTS

    def link(self, tc):
        firm = CAPITAL_FIRMS.get(tc["name"].strip())
        return ("firm", firm) if firm else (None, None)

    def run(self):
        from ..models import DataRoom, Firm

        # Kellen: the directory's "Fortress" is Fortress Investment Group, the Fortress data room's partner.
        if not Firm.objects.filter(name=FORTRESS).exists() and Firm.objects.filter(name="Fortress").update(name=FORTRESS):
            self.counts["Fortress renamed Fortress Investment Group"] += 1
        for name in CAPITAL_NEW_FIRMS:
            if not Firm.objects.filter(name__iexact=name).exists():
                Firm.objects.create(name=name, kind="investor")
                self.counts["firms added to the directory"] += 1
        fortress = Firm.objects.filter(name=FORTRESS).first()
        if fortress and DataRoom.objects.filter(name="Fortress", partner=None).update(partner=fortress):
            self.counts["Fortress data room partner set"] += 1
        return super().run()


@transaction.atomic
def import_pursuits(data, by=None):
    board = team_board(PURSUITS, ["High priority", "Medium priority", "Low priority", "Holding / overflow"], by,
                       "Where ACMI might build next, ranked: from the Properties-Campus Pursuits Trello board.")
    imp = PursuitsImporter(data, board, by)
    imp.run()
    return imp


@transaction.atomic
def import_capital(data, by=None):
    board = team_board(CAPITAL,[v[0] for v in CAPITAL_LISTS.values()], by,
                       "Equity and debt partners, by stage: from the Properties-Capitalization Trello board. A "
                       "partner's offer on a project goes in the capital tracker.")
    imp = CapitalImporter(data, board, by)
    imp.run()
    return imp
