"""Boards: card ordering, moves, board templates, record links and the card's activity log.

Positions are integers 1,000 apart so a card can drop between two others without touching the rest; when a gap
closes the list is renumbered. A card is a view of a CRM record wherever one exists (`Card.RECORD_FIELDS`).
"""
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from ..models import (
    Approval, Board, BoardLabel, BoardList, CapitalSource, Card, CardEvent, Company, Contract, Firm, LandSite, Meeting,
    Opportunity, Task,
)

GAP = 1000

# Board templates: the lists a new board starts with, each a name or (name, feed). "project" is the team's MEIH
# Trello layout, with its sites, dates and tasks filled from the records (crm/boards/bound.py).
TEMPLATES = {
    "blank": ("Blank", []),
    "project": ("Project board", [("Land sites", "sites"), ("Critical dates", "critical_dates"), "Deal and design",
                                  "Contacts and consultants", ("Open tasks", "open_tasks")]),
    "deal": ("Deal steps", ["To do", "Doing", "Waiting on them", "Done"]),
}
# Every new board starts with the team's Trello categories (Task.Label) as labels.
DEFAULT_LABELS = [("Deal info", "blue"), ("Legal", "purple"), ("Civil", "orange"), ("Architecture", "sky"),
                  ("Program / procurement", "green"), ("Land use", "yellow"), ("Critical date", "red")]

RECORD_MODELS = {
    "company": Company, "opportunity": Opportunity, "site": LandSite, "contract": Contract, "firm": Firm,
    "approval": Approval, "capital_source": CapitalSource, "meeting": Meeting, "task": Task,
}
# A record's URL (as search returns it) -> the card field it links to.
URL_FIELDS = [
    (re.compile(r"^/companies/(\d+)/"), "company"),
    (re.compile(r"^/opportunities/(\d+)/"), "opportunity"),
    (re.compile(r"^/land/sites/(\d+)/"), "site"),
    (re.compile(r"^/contracts/(\d+)/"), "contract"),
    (re.compile(r"^/directory/(\d+)/"), "firm"),
    (re.compile(r"^/entitlements/(\d+)/"), "approval"),
    (re.compile(r"^/capital/(\d+)/"), "capital_source"),
    (re.compile(r"^/tasks/(\d+)/"), "task"),
    (re.compile(r"^/meetings/\?.*\bopen=(\d+)"), "meeting"),
]


def log(card, user, kind, text):
    return CardEvent.objects.create(card=card, user=user, kind=kind, text=text[:300])


# ---------------------------------------------------------------------------
# Boards and lists
# ---------------------------------------------------------------------------


@transaction.atomic
def create_board(name, user, kind=Board.Kind.CUSTOM, template="blank", campus=None, project=None, colour="navy",
                 description=""):
    last = Board.objects.order_by("-position").first()
    board = Board.objects.create(name=name.strip()[:120], kind=kind, campus=campus, project=project, colour=colour,
                                 description=description, position=(last.position + 1) if last else 1, created_by=user)
    if user is not None:
        board.members.add(user)
    for n, spec in enumerate(TEMPLATES.get(template, TEMPLATES["blank"])[1], 1):
        list_name, feed = spec if isinstance(spec, tuple) else (spec, "")
        BoardList.objects.create(board=board, name=list_name, position=n * GAP, feed=feed)
    for n, (label, colour_) in enumerate(DEFAULT_LABELS, 1):
        BoardLabel.objects.create(board=board, name=label, colour=colour_, position=n)
    return board


def add_list(board, name):
    last = board.lists.order_by("-position").first()
    return BoardList.objects.create(board=board, name=name.strip()[:120], position=(last.position + GAP) if last else GAP)


def move_list(lst, index):
    """Put a list at `index` among the board's open lists."""
    others = [x for x in lst.board.lists.filter(archived=False).order_by("position", "id") if x.pk != lst.pk]
    index = max(0, min(index, len(others)))
    others.insert(index, lst)
    for n, x in enumerate(others, 1):
        if x.position != n * GAP:
            x.position = n * GAP
            x.save(update_fields=["position"])


def sort_list(lst, by):
    """Renumber a list's open cards by due date, name or first label."""
    cards = list(lst.cards.filter(archived=False).prefetch_related("labels"))
    far = timezone.localdate() + timedelta(days=36500)
    if by == "due":
        cards.sort(key=lambda c: (c.due or far, c.title.lower()))
    elif by == "name":
        cards.sort(key=lambda c: c.title.lower())
    elif by == "label":
        cards.sort(key=lambda c: (next((l.position for l in c.labels.all()), 999), c.title.lower()))
    else:
        return
    for n, c in enumerate(cards, 1):
        c.position = n * GAP
    Card.objects.bulk_update(cards, ["position"])


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def next_position(lst):
    last = lst.cards.filter(archived=False).order_by("-position").first()
    return (last.position + GAP) if last else GAP


def add_card(lst, title, user, **fields):
    card = Card.objects.create(list=lst, title=title.strip()[:300], position=next_position(lst), created_by=user, **fields)
    log(card, user, "created", f"added this card to {lst.name}")
    return card


@transaction.atomic
def place(card, lst, index, user=None):
    """Put `card` into `lst` at `index` (0-based among the list's open cards, not counting the card itself).

    Picks a position between the neighbours; renumbers the whole list when the gap has closed. Logs a move when
    the list changed. Returns the new position.
    """
    siblings = list(lst.cards.filter(archived=False).exclude(pk=card.pk).order_by("position", "id"))
    index = max(0, min(index, len(siblings)))
    before = siblings[index - 1].position if index > 0 else None
    after = siblings[index].position if index < len(siblings) else None
    if before is None and after is None:
        pos = GAP
    elif before is None:
        pos = after - GAP if after - GAP > 0 else None
    elif after is None:
        pos = before + GAP
    else:
        pos = (before + after) // 2 if after - before > 1 else None
    moved = card.list_id != lst.pk
    old_name = card.list.name if moved else ""
    if pos is None:  # no room left: renumber everything in order
        siblings.insert(index, card)
        for n, c in enumerate(siblings, 1):
            c.position = n * GAP
        Card.objects.bulk_update([c for c in siblings if c.pk != card.pk], ["position"])
        pos = card.position
    card.list = lst
    card.position = pos
    card.save(update_fields=["list", "position", "updated_at"])
    if moved:
        log(card, user, "moved", f"moved this card from {old_name} to {lst.name}")
    return pos


def copy_card(card, user, title=None):
    new = Card.objects.create(
        list=card.list, position=next_position(card.list), title=(title or card.title)[:300], description=card.description,
        start=card.start, due=card.due, due_time=card.due_time, cover=card.cover, created_by=user,
        **{f: getattr(card, f) for f in Card.RECORD_FIELDS},
    )
    new.labels.set(card.labels.all())
    new.members.set(card.members.all())
    log(new, user, "created", f"copied from “{card.title}”")
    return new


def link_from_url(url):
    """A record URL (what search returns) -> (field, pk), or (None, None)."""
    for pattern, field in URL_FIELDS:
        m = pattern.match(url or "")
        if m:
            return field, int(m.group(1))
    return None, None


def set_record(card, field, pk, user=None):
    """Link the card to one record (clearing any other). field=None unlinks."""
    for f in Card.RECORD_FIELDS:
        setattr(card, f + "_id", None)
    if field:
        obj = RECORD_MODELS[field].objects.get(pk=pk)
        setattr(card, field, obj)
        log(card, user, "linked", f"linked this card to {Card.RECORD_LABELS[field].lower()} {obj}")
    else:
        log(card, user, "linked", "removed the record link")
    card.save()
    return card.record()


def record_facts(card):
    """What the card dialog shows about its record: a few facts a glance needs, each (label, value, url)."""
    r = card.record()
    if r is None:
        return []
    field = card.record_field()
    facts = []

    def add(label, value):
        if value not in (None, "", []):
            facts.append((label, value))

    owner = getattr(r, "owner", None)
    if field == "opportunity":
        add("Stage", r.get_stage_display())
        add("Company", r.company.name)
        add("Last touch", r.company.last_interaction_at)
        add("Next step", r.next_step)
    elif field == "company":
        add("Stage", r.get_status_display())
        add("Last touch", r.last_interaction_at)
        add("Sector", r.sector)
    elif field == "site":
        add("Status", r.get_status_display())
        add("Where", r.home_label)
    elif field == "contract":
        add("Status", r.get_status_display())
        add("Counterparty", r.counterparty)
        add("Waiting on", _waiting(r))
        nxt = r.dates.filter(done=False, date__isnull=False).order_by("date").first()
        if nxt:
            add("Next date", f"{nxt.label} · {nxt.date:%b} {nxt.date.day}")
    elif field == "firm":
        add("Type", r.get_kind_display())
    elif field == "approval":
        add("Status", r.get_status_display())
        add("Waiting on", _waiting(r))
        add("Next step", r.next_step)
    elif field == "capital_source":
        add("Status", r.get_status_display())
        add("Waiting on", _waiting(r))
        add("Project", r.project.name)
    elif field == "meeting":
        add("When", r.start)
    elif field == "task":
        add("Due", r.due_date)
        add("Done", "Yes" if r.done else "No")
        owner = r.assignee
    if owner is not None:
        add("Owner", owner.get_full_name() or owner.username)
    return facts


def _waiting(r):
    """"Them since Oct 3" for a record's ball-in-court field, or ""."""
    if not r.waiting_on:
        return ""
    since = f" since {r.waiting_since:%b} {r.waiting_since.day}" if r.waiting_since else ""
    return r.get_waiting_on_display() + since


def record_cards(record):
    """Open cards that stand for this record, with their board and list."""
    return record.cards.filter(archived=False, list__archived=False, list__board__archived=False).select_related("list__board")
