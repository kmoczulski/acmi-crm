"""Where each approval stands and what's next: the usual permit matrix to start from, the next hearing or step,
missed steps, approvals about to lapse, open conditions, and the lines the dashboard, reminders and campus summary
show."""
from datetime import timedelta
from types import SimpleNamespace

from django.db.models import Q
from django.utils import timezone

from ..choices import Order
from ..models import Approval, ApprovalCondition, ApprovalStep, Notification

STEP_LEADS = (7, 1, 0)  # reminder days before a hearing or other step
EXPIRY_LEADS = (60, 30, 7, 1, 0)  # before an approval lapses
SOON = 14  # a step this close, or a condition due, is a flag on the campus summary
EXPIRY_FLAG = 60
STATUS_ORDER = Order("Approval.Status", ["planned", "preapp", "submitted", "review", "hearing"])  # the order an open approval moves in

# The usual approvals for a manufacturing building on a greenfield site, from the client reports' permit matrix,
# plus those a deal may need: (kind, title, ticked by default).
MATRIX = [
    ("rezoning", "Rezoning", False),
    ("special_use", "Special use permit", False),
    ("variance", "Variance", False),
    ("subdivision", "Subdivision / plat", False),
    ("site_plan", "Site plan approval", True),
    ("grading", "Erosion and sediment control plan", True),
    ("stormwater", "Stormwater management plan", True),
    ("wetlands", "Wetlands permit (Section 404 / 401)", False),
    ("essp", "DDESB explosives safety site plan", False),
    ("access", "Road access / entrance permit", True),
    ("utility", "Utility extension agreements", True),
    ("building", "Building permit", True),
    ("occupancy", "Certificate of occupancy", False),
]


def fmt(d):
    return f"{d:%b} {d.day}"


def start(kinds, *, project=None, site=None, user=None):
    """Add an approval for each ticked kind to a project or site. Kinds it already has are skipped. Returns the new
    approvals."""
    campus = (project.campus if project else None) or (site.campus if site else None)
    have = set(Approval.objects.filter(project=project, site=site).values_list("kind", flat=True))
    titles = {kind: title for kind, title, _ in MATRIX}
    return [Approval.objects.create(title=titles[k], kind=k, project=project, site=site, campus=campus, owner=user,
                                    created_by=user)
            for k in kinds if k in titles and k not in have]


def advance(approval, status):
    """Move an open approval's status forward to `status` (never back, never off a decision)."""
    if approval.status in STATUS_ORDER and status in STATUS_ORDER and \
            STATUS_ORDER.index(status) > STATUS_ORDER.index(approval.status):
        approval.status = status
        return True
    return False


def annotate(approvals, today=None):
    """Adds to each approval: .next (soonest step not done yet), .missed (past steps not marked done), .expiring_in
    (days until an approval in hand lapses), .open_conditions and .waiting_days. Expects steps and conditions
    prefetched for lists."""
    today = today or timezone.localdate()
    for a in approvals:
        pending = [s for s in a.steps.all() if not s.done]
        a.next = min((s for s in pending if s.date >= today), key=lambda s: (s.date, s.pk), default=None)
        if a.next:
            a.next.left = (a.next.date - today).days
        a.missed = [s for s in pending if s.date < today]
        a.expiring_in = ((a.expires_on - today).days if a.expires_on and a.status == Approval.Status.APPROVED
                         else None)
        a.open_conditions = [c for c in a.conditions.all() if c.confirmed and c.status == ApprovalCondition.Status.OPEN]
        a.waiting_days = (today - a.waiting_since).days if a.waiting_on and a.waiting_since else None
    return approvals


def with_related(qs):
    return qs.select_related("project", "site", "campus", "owner").prefetch_related("steps", "conditions")


def campus_lines(approvals, today):
    """(facts, flags) for a campus summary item: each approval's status and next step; flags for a hearing within
    two weeks, a step that passed without an outcome, an approval lapsing within 60 days, a denial, conditions due."""
    facts, flags = [], []
    for a in annotate(list(with_related(approvals).exclude(status=Approval.Status.WITHDRAWN)), today):
        fact = f"{a.title}: {a.get_status_display().lower()}"
        if a.next:
            fact += f", {a.next.label.lower()} {fmt(a.next.date)}"
        elif a.status == Approval.Status.APPROVED and a.approved_on:
            fact += f" {fmt(a.approved_on)}"
        if a.jurisdiction:
            fact += f" ({a.jurisdiction})"
        if a.waiting_on:
            fact += f"; {a.get_waiting_on_display().lower()}" + (f" for {a.waiting_days} days" if a.waiting_days else "")
        facts.append(fact)
        if a.next and (a.next.date - today).days <= SOON:
            left = (a.next.date - today).days
            flags.append(f"{a.title}: {a.next.label.lower()} {'today' if left == 0 else f'in {left} days'}")
        flags += [f"{a.title}: {s.label.lower()} was {fmt(s.date)}, outcome not recorded" for s in a.missed]
        if a.expiring_in is not None and a.expiring_in <= EXPIRY_FLAG:
            flags.append(f"{a.title} {'expired' if a.expiring_in < 0 else 'expires'} {fmt(a.expires_on)}")
        if a.status == Approval.Status.DENIED:
            flags.append(f"{a.title} was denied")
        flags += [f"{a.title}: condition due {fmt(c.due_on)}: {' '.join(c.text.split())[:70]}"
                  for c in a.open_conditions if c.due_on and (c.due_on - today).days <= SOON]
    return facts, flags


def _bucket(left, leads):
    return "late" if left < 0 else min(lead for lead in leads if lead >= left)


def _when(left, d):
    if left < 0:
        return f"was {fmt(d)}"
    return "today" if left == 0 else f"in {left} day{'s' if left != 1 else ''} ({fmt(d)})"


def reminders(user, today=None):
    """Hearings and other steps (7 and 1 days ahead, on the day, once when they pass unrecorded), approvals about to
    lapse (60, 30, 7 and 1 days ahead, on the day, once lapsed) and conditions with a due date, for whoever runs the
    approval (else whoever created it). Called from notify.due_sweep."""
    from .. import notify

    today = today or timezone.localdate()
    theirs = Q(approval__owner=user) | Q(approval__owner__isnull=True, approval__created_by=user)
    steps = (ApprovalStep.objects.filter(theirs, done=False, date__lte=today + timedelta(days=STEP_LEADS[0]))
             .exclude(approval__status=Approval.Status.WITHDRAWN).select_related("approval"))
    for s in steps:
        left = (s.date - today).days
        text = (f"{s.approval.title}: {s.label} was {fmt(s.date)}; record what happened" if left < 0
                else f"{s.approval.title}: {s.label} {_when(left, s.date)}")
        notify.send(user, Notification.Kind.DEADLINE, f"Entitlements, {text}", s.approval.get_absolute_url(), None,
                    key=f"step:{s.pk}:{s.date:%Y%m%d}:{_bucket(left, STEP_LEADS)}")
    mine = Q(owner=user) | Q(owner__isnull=True, created_by=user)
    lapsing = Approval.objects.filter(mine, status=Approval.Status.APPROVED, expires_on__isnull=False,
                                      expires_on__lte=today + timedelta(days=EXPIRY_LEADS[0]))
    for a in lapsing:
        left = (a.expires_on - today).days
        text = f"{a.title} expired {fmt(a.expires_on)}" if left < 0 else f"{a.title} expires {_when(left, a.expires_on)}"
        notify.send(user, Notification.Kind.DEADLINE, f"Entitlements, {text}", a.get_absolute_url(), None,
                    key=f"expiry:{a.pk}:{a.expires_on:%Y%m%d}:{_bucket(left, EXPIRY_LEADS)}")
    conditions = ApprovalCondition.objects.filter(theirs, confirmed=True, status=ApprovalCondition.Status.OPEN,
                                                  due_on__isnull=False,
                                                  due_on__lte=today + timedelta(days=STEP_LEADS[0])).select_related(
        "approval")
    for c in conditions:
        left = (c.due_on - today).days
        notify.send(user, Notification.Kind.DEADLINE,
                    f"Entitlements, {c.approval.title}: condition due {_when(left, c.due_on)}: {c.text[:120]}",
                    c.approval.get_absolute_url() + "#conditions", None,
                    key=f"cond:{c.pk}:{c.due_on:%Y%m%d}:{_bucket(left, STEP_LEADS)}")


def due_rows(days=14, today=None):
    """Steps, conditions and lapsing approvals within `days` (or missed), as deadline rows for the dashboard."""
    today = today or timezone.localdate()
    until = today + timedelta(days=days)
    rows = []
    for s in (ApprovalStep.objects.filter(done=False, date__lte=until).exclude(approval__status="withdrawn")
              .select_related("approval", "approval__owner")):
        rows.append(SimpleNamespace(date=s.date, label=s.label, amount=None, url=s.approval.get_absolute_url(),
                                    what=s.approval.title, who=s.approval.owner, tag="Entitlement"))
    for c in (ApprovalCondition.objects.filter(confirmed=True, status="open", due_on__isnull=False, due_on__lte=until)
              .select_related("approval", "approval__owner")):
        rows.append(SimpleNamespace(date=c.due_on, label=f"Condition: {c.text[:80]}", amount=None,
                                    url=c.approval.get_absolute_url() + "#conditions", what=c.approval.title,
                                    who=c.approval.owner, tag="Entitlement"))
    for a in Approval.objects.filter(status="approved", expires_on__isnull=False, expires_on__lte=until).select_related(
            "owner"):
        rows.append(SimpleNamespace(date=a.expires_on, label="Approval lapses", amount=None, url=a.get_absolute_url(),
                                    what=a.title, who=a.owner, tag="Entitlement"))
    return rows
