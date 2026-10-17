"""Critical dates across contracts: what's coming up, daily reminders, the land site's deal timeline, and the lines
the campus summary shows."""
from datetime import timedelta
from types import SimpleNamespace

from django.db.models import Q
from django.utils import timezone

from ..models import Contract, ContractDate, Notification

LEADS = (30, 14, 7, 1, 0)  # days ahead a reminder goes out
SOON = 14  # a deadline this close (or missed) is a flag on the campus summary


def _fmt(d):
    return f"{d:%b} {d.day}"


def live(qs=None):
    """Confirmed, open dates on contracts still in play."""
    qs = ContractDate.objects.all() if qs is None else qs
    return qs.filter(confirmed=True, done=False, date__isnull=False, contract__status__in=Contract.OPEN_STATUSES)


def upcoming(days=30, today=None):
    """{"overdue": [...], "soon": [...]}: missed deadlines, then the next `days` days: contract dates, open diligence
    items and closing items. Each row has date, left, late, label, amount, url, what (the deal), who, tag."""
    from ..land import diligence
    from . import closing

    today = today or timezone.localdate()
    rows = []
    qs = live().filter(date__lte=today + timedelta(days=days)).select_related("contract", "contract__owner")
    for d in qs:
        rows.append(SimpleNamespace(date=d.date, label=d.label, amount=d.amount, url=d.contract.get_absolute_url()
                                    + "#dates", what=d.contract.title, who=d.contract.owner, tag=""))
    for i in diligence.due(days, today):
        rows.append(SimpleNamespace(date=i.due_on, label=i.name, amount=None, url=f"/land/sites/{i.site_id}/diligence/"
                                    f"#dd-{i.pk}", what=i.site.name, who=i.owner, tag="DD"))
    for i in closing.due(days, today):
        rows.append(SimpleNamespace(date=i.due_on, label=i.name, amount=None, url=i.contract.get_absolute_url()
                                    + "#closing", what=i.contract.title, who=i.contract.owner, tag="Closing"))
    rows.sort(key=lambda r: r.date)
    for r in rows:
        r.left = (r.date - today).days
        r.late = -r.left
    return {"overdue": [r for r in rows if r.left < 0], "soon": [r for r in rows if r.left >= 0], "days": days}


def next_dates(contracts, today=None):
    """{contract pk: its next open date (missed ones first)}."""
    out = {}
    for d in live().filter(contract__in=contracts).order_by("date", "id"):
        out.setdefault(d.contract_id, d)
    today = today or timezone.localdate()
    for d in out.values():
        d.left = (d.date - today).days
    return out


def reminders(user, today=None):
    """Deadlines on the contracts someone runs (or created, if nobody runs them): 30, 14, 7 and 1 days ahead, on the
    day, and once when missed. Called once a day per person from notify.due_sweep. Keyed by the date, so a date
    that moves gets fresh reminders."""
    from .. import notify

    today = today or timezone.localdate()
    mine = Q(contract__owner=user) | Q(contract__owner__isnull=True, contract__created_by=user)
    for d in live().filter(mine, date__lte=today + timedelta(days=LEADS[0])).select_related("contract"):
        left = (d.date - today).days
        if left < 0:
            bucket, when = "late", f"was due {_fmt(d.date)} ({-left} day{'s' if left != -1 else ''} ago)"
        else:
            bucket = min(lead for lead in LEADS if lead >= left)
            when = "today" if left == 0 else f"in {left} day{'s' if left != 1 else ''} ({_fmt(d.date)})"
        notify.send(user, Notification.Kind.DEADLINE, f"{d.contract.title}: {d.label} {when}",
                    d.contract.get_absolute_url() + "#dates", None, key=f"cdate:{d.pk}:{d.date:%Y%m%d}:{bucket}")


# ---------------------------------------------------------------------------
# The land site's deal timeline
# ---------------------------------------------------------------------------


def milestone_dates(contract):
    """{site milestone key: date} a land contract settles: when we sent the LOI or PSA (our first draft out), when it
    was signed, when feasibility ends, and the closing date once it has closed."""
    c = contract
    if c.kind not in (Contract.Kind.LOI, Contract.Kind.PSA, Contract.Kind.OPTION):
        return {}
    out = {}
    sent = [d.dated for d in c.documents.all() if d.is_draft and d.sent_by == "us" and d.dated]
    signed = c.effective_date if c.status in (Contract.Status.SIGNED, Contract.Status.CLOSED) else None
    dates = [d for d in c.dates.all() if d.confirmed and d.date]
    if c.kind == Contract.Kind.LOI:
        if sent:
            out["loi_sent"] = min(sent)
        if signed:
            out["loi_signed"] = signed
        return out
    if sent:
        out["psa_sent"] = min(sent)
    if signed:
        out["psa_signed"] = signed
    feasibility = [d.date for d in dates if d.kind == ContractDate.Kind.FEASIBILITY]
    if feasibility:
        out["feasibility_end"] = max(feasibility)
    closing = [d for d in dates if d.kind == ContractDate.Kind.CLOSING]
    if c.status == Contract.Status.CLOSED and closing:
        done = [d.date for d in closing if d.done]  # the day it actually closed, over a planned or outside date
        out["closed"] = max(done) if done else max(d.date for d in closing)
    return out


def sync_site(contract, user=None):
    """Put the contract's dates on its site's deal timeline. A contract for the whole site sets the site and every
    included parcel's deal; one for some owners (parcel_deals) sets only theirs. Sets or moves only the milestones it
    settles, never clears one, and statuses only move forward. Returns dates changed."""
    from ..land.tracker import apply_milestones

    if not contract.site_id:
        return 0
    dates = milestone_dates(contract)
    if not dates:
        return 0
    why = f"from {contract.title}"
    picked = list(contract.parcel_deals.select_related("parcel", "site"))
    if picked:
        return sum(apply_milestones(t, dates, user, why=why) for t in picked)
    changed = apply_milestones(contract.site, dates, user, why=why)
    for deal in contract.site.parcel_deals.filter(included=True).select_related("parcel", "site"):
        apply_milestones(deal, dates, user, why=why, log=False)  # the whole site: logged once, above
    return changed


# ---------------------------------------------------------------------------
# Campus summary
# ---------------------------------------------------------------------------


def campus_lines(contracts, today):
    """(facts, flags) for a campus summary item: each open contract's status, who has the ball, and its next date;
    deadlines within two weeks, or missed, become flags."""
    facts, flags = [], []
    for c in contracts:
        if not c.is_open:
            continue
        open_dates = [d for d in c.dates.all() if d.confirmed and not d.done and d.date]
        fact = f"{c.title} ({c.get_status_display().lower()}"
        if c.waiting_on:
            fact += f", ball with {dict(US_THEM).get(c.waiting_on, c.get_waiting_on_display().lower())}"
        fact += ")"
        if open_dates:
            nxt = min(open_dates, key=lambda d: d.date)
            fact += f"; next date: {nxt.label} {_fmt(nxt.date)}"
        facts.append(fact)
        for d in sorted(open_dates, key=lambda d: d.date):
            left = (d.date - today).days
            if left < 0:
                flags.append(f"{d.label} was due {_fmt(d.date)} ({c.title})")
            elif left <= SOON:
                flags.append(f"{d.label} {'today' if left == 0 else f'in {left} days'} ({c.title})")
    return facts, flags


US_THEM = (("us", "us"), ("them", "them"), ("third", "a third party"))
