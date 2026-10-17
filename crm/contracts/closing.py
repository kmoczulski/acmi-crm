"""The closing checklist on a purchase: each side's deliverables, the money, recording and the clean-up after.
Marking the contract closed fills in the closing date, and the site timeline sync (dates.sync_site) then moves the
site and its parcels' deals to Acquired."""
from datetime import timedelta

from django.db.models import Max, Q
from django.utils import timezone

from ..models import ClosingItem, Contract, ContractDate, Notification
from . import dates

LEADS = (7, 1, 0)
POST_CLOSING_DAYS = 30  # default due date for the clean-up after closing

DEFAULTS = [  # (side, item)
    ("buyer", "Title commitment reviewed; objections sent and resolved"),
    ("buyer", "Final ALTA survey certified to us, the lender and the title company"),
    ("buyer", "Buying entity formed, in good standing and authorized to close"),
    ("buyer", "Closing funds ready; wire instructions confirmed by phone with the title company"),
    ("buyer", "Insurance bound from the closing date"),
    ("seller", "Deed signed and notarized"),
    ("seller", "Owner's title affidavit and FIRPTA certificate"),
    ("seller", "Seller's authority documents (resolutions, good standing)"),
    ("seller", "Payoff letters and lien releases"),
    ("seller", "Existing leases ended or assigned; possession delivered"),
    ("title", "Pro forma title policy and endorsements agreed"),
    ("title", "Settlement statement approved by both sides"),
    ("lender", "Loan documents signed and lender conditions met (if financed)"),
    ("post", "Deed and any easements recorded (book and page noted)"),
    ("post", "Owner's title policy issued"),
    ("post", "Transfer and recordation taxes paid; rollback taxes settled"),
    ("post", "Tax bills, utilities and insurance moved to the buying entity"),
    ("post", "Closing binder saved to the shared drive"),
]


def planned(contract):
    """The kept closing date the checklist runs to: the earliest one not done yet (an outside date comes later)."""
    return (contract.dates.filter(kind=ContractDate.Kind.CLOSING, confirmed=True, done=False, date__isnull=False)
            .order_by("date", "pk").first())


def closing_date(contract):
    d = planned(contract)
    return d.date if d else None


def start(contract):
    """Add the standard closing items, due on the closing date (the clean-up 30 days after). Returns how many."""
    have = set(contract.closing_items.values_list("name", flat=True))
    pos = contract.closing_items.aggregate(m=Max("position"))["m"] or 0
    when = closing_date(contract)
    new = []
    for side, name in DEFAULTS:
        if name in have:
            continue
        pos += 1
        due = (when + timedelta(days=POST_CLOSING_DAYS) if side == "post" else when) if when else None
        new.append(ClosingItem(contract=contract, side=side, name=name, due_on=due, position=pos))
    ClosingItem.objects.bulk_create(new)
    return len(new)


def progress(contract, today=None):
    """{"done", "total", "late", "groups"} for the checklist (items marked not needed don't count)."""
    today = today or timezone.localdate()
    items = list(contract.closing_items.select_related("done_by"))
    for i in items:
        i.late = i.status == ClosingItem.Status.OPEN and i.due_on is not None and i.due_on < today
    groups = [(label, [i for i in items if i.side == value]) for value, label in ClosingItem.Side.choices]
    live = [i for i in items if i.status != ClosingItem.Status.NA]
    return {"items": items, "groups": [(label, rows) for label, rows in groups if rows],
            "done": sum(i.status == ClosingItem.Status.DONE for i in live), "total": len(live),
            "late": [i for i in items if i.late],
            "pre_open": [i for i in live if i.side != "post" and i.status == ClosingItem.Status.OPEN]}


def close(contract, on, user=None):
    """Mark the purchase closed on `on`: the planned closing date becomes that day (and done; an outside date is left
    alone), the status Closed, and the site and its parcels' deals move to Acquired through the timeline sync."""
    d = (planned(contract)
         or contract.dates.filter(kind=ContractDate.Kind.CLOSING, confirmed=True, done=False).first()
         or ContractDate(contract=contract, kind=ContractDate.Kind.CLOSING, label="Closing", created_by=user))
    d.date, d.done, d.done_at, d.done_by = on, True, timezone.now(), user
    d.save()
    contract.status = Contract.Status.CLOSED
    contract.save(update_fields=["status", "updated_at"])
    return dates.sync_site(contract, user)


def reminders(user, today=None):
    """Open closing items on the contracts someone runs: 7 and 1 days ahead, on the day, once when late."""
    from .. import notify

    today = today or timezone.localdate()
    mine = Q(contract__owner=user) | Q(contract__owner__isnull=True, contract__created_by=user)
    qs = ClosingItem.objects.filter(mine, status=ClosingItem.Status.OPEN, due_on__isnull=False,
                                    due_on__lte=today + timedelta(days=LEADS[0]),
                                    contract__status__in=Contract.OPEN_STATUSES).select_related("contract")
    for i in qs:
        left = (i.due_on - today).days
        if left < 0:
            bucket, when = "late", f"was due {i.due_on:%b} {i.due_on.day}"
        else:
            bucket = min(lead for lead in LEADS if lead >= left)
            when = "due today" if left == 0 else f"due in {left} day{'s' if left != 1 else ''}"
        notify.send(user, Notification.Kind.DEADLINE, f"Closing, {i.contract.title}: {i.name} {when}",
                    i.contract.get_absolute_url() + "#closing", None,
                    key=f"close:{i.pk}:{i.due_on:%Y%m%d}:{bucket}")


def due(days=14, today=None):
    """Open closing items due within `days` (or late) on open contracts, for the dashboard."""
    today = today or timezone.localdate()
    return list(ClosingItem.objects.filter(status=ClosingItem.Status.OPEN, due_on__isnull=False,
                                           due_on__lte=today + timedelta(days=days),
                                           contract__status__in=Contract.OPEN_STATUSES)
                .select_related("contract", "contract__owner").order_by("due_on", "pk"))
