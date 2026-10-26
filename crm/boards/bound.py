"""Bound lists and feed lists: the boards that are views of the CRM's records.

A **record board** (`Board.kind == "bound"`, `Board.record_type`) has one list per status of its record type
(`BoardList.maps_to`). Every record in the board's scope has a card there, in the list for its status: dropping a card
in another bound list changes the record through the same path the record's page uses (a deal's stage change sends
its notification; a site's status change goes in its log), and a status changed anywhere else moves the card
(`record_saved`, called from `crm/signals.py`).

A **feed list** (`BoardList.feed`) is read-only and filled from the records of the board's campus or project each
time the board opens: critical dates, open tasks, land sites, contracts. Nothing in it goes stale. A card stored in a
feed list for one of its records (an imported Trello card, with its comments) shows in the record's place, so the
history stays reachable; a stored card whose record has left the feed shows at the end of the list.
"""
import contextvars
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import (
    Approval, Board, BoardList, CapitalSource, Card, Contract, ContractDate, LandSite, Opportunity, OpportunityStage,
    Project, Task,
)
from . import logic

# Moves made by a drop on a bound list already put the card where it belongs; the save hook must not move it again.
_dropping = contextvars.ContextVar("boards_dropping", default=False)


@dataclass(frozen=True)
class RecordType:
    key: str  # the Card field that links the record
    model: type
    status_field: str
    choices: list
    board_name: str
    noun: str  # "deal", for "Link the card to a deal first"


RECORD_TYPES = {
    "opportunity": RecordType("opportunity", Opportunity, "stage", OpportunityStage.choices, "Tenant pipeline", "deal"),
    "site": RecordType("site", LandSite, "status", LandSite.Status.choices, "Sites", "site"),
    "capital_source": RecordType("capital_source", CapitalSource, "status", CapitalSource.Status.choices, "Capital",
                                 "capital source"),
    "approval": RecordType("approval", Approval, "status", Approval.Status.choices, "Entitlements", "entitlement"),
    "contract": RecordType("contract", Contract, "status", Contract.Status.choices, "Contracts", "contract"),
}
MODEL_TYPES = {rt.model: rt for rt in RECORD_TYPES.values()}

FEEDS = {
    "sites": "Land sites",
    "critical_dates": "Critical dates",
    "open_tasks": "Open tasks",
    "contracts": "Contracts",
}


# ---------------------------------------------------------------------------
# Scope: the records a board covers
# ---------------------------------------------------------------------------


def _scope(board):
    """(campus, [project ids]) the board covers. A project board covers its project and the projects under it."""
    projects = []
    if board.project_id:
        projects = [board.project_id, *Project.objects.filter(parent_id=board.project_id).values_list("pk", flat=True)]
    campus = board.campus or (board.project.campus if board.project_id else None)
    return campus, projects


def records_in_scope(board, rt):
    qs = rt.model.objects.all()
    campus, projects = _scope(board)
    if not campus and not projects:
        return qs
    if rt.key == "opportunity":
        return qs.filter(campus=campus) if campus else qs.none()
    if rt.key == "site":
        return qs.filter(Q(project_id__in=projects) if projects else Q(campus=campus))
    if rt.key == "capital_source":
        return qs.filter(project_id__in=projects) if projects else qs.filter(project__campus=campus)
    if rt.key == "approval":
        return qs.filter(Q(project_id__in=projects) if projects else Q(campus=campus))
    if rt.key == "contract":
        return qs.filter(Q(site__project_id__in=projects) if projects else Q(campus=campus))
    return qs


def in_scope(board, record):
    return records_in_scope(board, MODEL_TYPES[type(record)]).filter(pk=record.pk).exists()


# ---------------------------------------------------------------------------
# Record boards
# ---------------------------------------------------------------------------


@transaction.atomic
def create_record_board(record_type, user, name="", campus=None, project=None, colour="navy"):
    """A board with one bound list per status of `record_type`, and a card for every record in its scope."""
    rt = RECORD_TYPES[record_type]
    board = logic.create_board(name or rt.board_name, user, kind=Board.Kind.BOUND, campus=campus, project=project,
                               colour=colour)
    board.record_type = record_type
    board.save(update_fields=["record_type"])
    for n, (value, label) in enumerate(rt.choices, 1):
        BoardList.objects.create(board=board, name=label, position=n * logic.GAP, maps_to=value)
    sync_board(board, user)
    return board


def bound_list_for(board, status):
    return board.lists.filter(maps_to=status, archived=False).order_by("position", "id").first()


@transaction.atomic
def sync_board(board, user=None):
    """Give every record in the board's scope a card in the list for its status; move cards that sit in the wrong
    bound list. Archived cards stay archived. Returns {"created": n, "moved": n}."""
    rt = RECORD_TYPES.get(board.record_type)
    counts = {"created": 0, "moved": 0}
    if rt is None or board.kind != Board.Kind.BOUND:
        return counts
    lists = {l.maps_to: l for l in board.lists.filter(archived=False).exclude(maps_to="").order_by("-position")}
    cards = {}
    for c in Card.objects.filter(list__board=board).exclude(**{rt.key: None}).select_related("list").order_by("archived", "id"):
        cards.setdefault(getattr(c, rt.key + "_id"), c)
    for record in records_in_scope(board, rt).order_by("pk"):
        target = lists.get(getattr(record, rt.status_field))
        card = cards.get(record.pk)
        if card is None:
            if target is not None:
                logic.add_card(target, str(record), user, **{rt.key: record})
                counts["created"] += 1
        elif not card.archived and target is not None and card.list_id != target.pk:
            logic.place(card, target, 10**6, user)
            counts["moved"] += 1
    return counts


def record_saved(record, skip=None):
    """After a record's save: put its card on every record board that covers it into the list for its status.
    `skip` is a card the caller is placing itself (a drop)."""
    rt = MODEL_TYPES.get(type(record))
    if rt is None or _dropping.get():
        return
    boards = list(Board.objects.filter(kind=Board.Kind.BOUND, record_type=rt.key, archived=False)
                  .select_related("campus", "project__campus"))
    if not boards:
        return
    status = getattr(record, rt.status_field)
    for board in boards:
        card = Card.objects.filter(list__board=board, **{rt.key: record}).order_by("archived", "id").first()
        if card is not None and (card.archived or card.pk == skip):
            continue
        target = bound_list_for(board, status)
        if target is None or not in_scope(board, record):
            continue
        if card is None:
            logic.add_card(target, str(record), None, **{rt.key: record})
        elif card.list_id != target.pk:
            logic.place(card, target, 10**6, None)
            logic.log(card, None, "moved", f"followed the {rt.noun} to {target.name}")


def drop_on_bound(card, lst, user):
    """A card dropped on a bound list: change its record's status the way the record's page does.

    Returns an error message when the move can't happen, else None. The card itself is placed by the caller."""
    board = lst.board
    rt = RECORD_TYPES.get(board.record_type)
    if rt is None or not lst.maps_to:
        return None
    record = getattr(card, rt.key)
    if record is None:
        return f"Cards in {lst.name} are {rt.noun}s. Link this card to a {rt.noun} first."
    if getattr(record, rt.status_field) == lst.maps_to:
        return None
    token = _dropping.set(True)
    try:
        if rt.key == "opportunity":
            from .. import notify

            old = record.get_stage_display()
            record.stage = lst.maps_to
            record.save()
            notify.deal_stage_changed(record, old, user)
        elif rt.key == "site":
            from ..land.views import _set_status

            _set_status(record, lst.maps_to, user, why="moved on a board")
        else:
            setattr(record, rt.status_field, lst.maps_to)
            record.save()
    finally:
        _dropping.reset(token)
    # Other record boards that show the same record follow it.
    record_saved(record, skip=card.pk)
    return None


# ---------------------------------------------------------------------------
# Feed lists
# ---------------------------------------------------------------------------


@dataclass
class FeedItem:
    title: str
    url: str
    kind: str  # shown on the item, so the record type never travels by colour alone
    sub: str = ""
    due: date = None
    done: bool = False
    card: Card = None  # a stored card for this record (comments, checklists), shown in its place
    key: tuple = field(default=())

    @property
    def due_state(self):
        if self.done or not self.due:
            return ""
        days = (self.due - timezone.localdate()).days
        return "late" if days < 0 else "today" if days == 0 else "soon" if days <= 7 else ""


def _sites(board):
    rt = RECORD_TYPES["site"]
    qs = records_in_scope(board, rt).exclude(status=LandSite.Status.DROPPED).select_related("campus", "project")
    def rank(site):  # STATUS_ORDER is furthest along first
        try:
            return LandSite.STATUS_ORDER.index(site.status)
        except ValueError:
            return 99

    sites = sorted(qs, key=lambda s: (rank(s), s.name.lower()))
    return [FeedItem(s.name, s.get_absolute_url(), "Site", s.get_status_display(), key=("site", s.pk)) for s in sites]


def _contract_scope(board):
    campus, projects = _scope(board)
    if projects:
        return Q(contract__site__project_id__in=projects)
    return Q(contract__campus=campus) if campus else Q()


def _critical_dates(board):
    qs = (ContractDate.objects.filter(_contract_scope(board), done=False, date__isnull=False)
          .exclude(contract__status__in=[Contract.Status.TERMINATED, Contract.Status.CLOSED])
          .select_related("contract").order_by("date", "pk"))
    return [FeedItem(d.label, d.contract.get_absolute_url(), "Critical date", d.contract.title, due=d.date,
                     key=("contract", d.contract_id)) for d in qs]


def _contracts(board):
    rt = RECORD_TYPES["contract"]
    qs = records_in_scope(board, rt).exclude(status=Contract.Status.TERMINATED).order_by("title")
    return [FeedItem(c.title, c.get_absolute_url(), "Contract", c.get_status_display(), key=("contract", c.pk))
            for c in qs]


def _open_tasks(board):
    campus, projects = _scope(board)
    on_board = Q(cards__list__board=board) | Q(checklist_items__checklist__card__list__board=board)
    if projects:
        here = Q(site__project_id__in=projects)
    elif campus:
        here = Q(site__campus=campus) | Q(opportunity__campus=campus)
    else:
        here = Q(pk__in=[])
    qs = (Task.objects.filter(here | on_board, done=False).select_related("assignee").distinct()
          .order_by("due_date", "pk"))
    return [FeedItem(t.title, t.get_absolute_url(), "Task",
                     (t.assignee.get_full_name() or t.assignee.username) if t.assignee else "Unassigned",
                     due=t.due_date, key=("task", t.pk)) for t in qs[:200]]


FEED_BUILDERS = {"sites": _sites, "critical_dates": _critical_dates, "open_tasks": _open_tasks, "contracts": _contracts}


def feed_items(lst, stored_cards):
    """The feed's items, each carrying the stored card for its record if there is one; stored cards whose record is
    no longer in the feed follow at the end (as items of their own) so their history stays on the board."""
    builder = FEED_BUILDERS.get(lst.feed)
    items = builder(lst.board) if builder else []
    by_record = {}
    for c in stored_cards:
        f = c.record_field()
        if f:
            by_record.setdefault((f, getattr(c, f + "_id")), c)
    used = set()
    for item in items:
        card = by_record.get(item.key)
        if card is not None and card.pk not in used:
            item.card = card
            used.add(card.pk)
    leftovers = [FeedItem(c.title, c.get_absolute_url(), c.record_kind or "Card", card=c) for c in stored_cards
                 if c.pk not in used]
    return items + leftovers
