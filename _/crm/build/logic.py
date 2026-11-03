"""Where a project's build stands: the standard schedule and budget to start from, budget math (approved change
orders add to a line's budget), schedule slip against the baseline, and the lines the campus summary and dashboard
show."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Max
from django.utils import timezone

from ..models import Bid, BudgetLine, BuildMilestone, ChangeOrder, Draw, TeamMember
from ..templatetags.crm_tags import money

SCHEDULE = [  # (phase, milestone), in order
    ("design", "Test fit / concept"),
    ("design", "Schematic design"),
    ("design", "Design development"),
    ("design", "Construction documents (permit set)"),
    ("procurement", "GC RFP issued"),
    ("procurement", "GC proposals due"),
    ("procurement", "GC interviews"),
    ("procurement", "GC selected"),
    ("procurement", "GMP / construction contract signed"),
    ("construction", "Notice to proceed"),
    ("construction", "Site work starts"),
    ("construction", "Pad ready"),
    ("construction", "Foundations complete"),
    ("construction", "Steel / vertical starts"),
    ("construction", "Dry-in"),
    ("construction", "Substantial completion"),
    ("construction", "Certificate of occupancy"),
    ("closeout", "Tenant move-in"),
    ("closeout", "Rent commencement"),
    ("closeout", "Final completion and closeout"),
]
BUDGET = [  # (group, code, line)
    ("land", "01", "Land purchase"),
    ("land", "02", "Closing costs and title"),
    ("hard", "10", "Sitework and utilities"),
    ("hard", "20", "Building shell"),
    ("hard", "30", "MEP"),
    ("hard", "40", "Tenant improvements and process"),
    ("hard", "50", "General conditions and GC fee"),
    ("soft", "60", "Architecture and engineering"),
    ("soft", "61", "Permits and impact fees"),
    ("soft", "62", "Legal"),
    ("soft", "63", "Insurance and builder's risk"),
    ("soft", "64", "Testing and inspections"),
    ("soft", "65", "Developer fee"),
    ("finance", "70", "Construction interest and loan fees"),
    ("contingency", "90", "Hard cost contingency"),
    ("contingency", "91", "Soft cost contingency"),
]
GC_SELECTED = "GC selected"
SLIP_FLAG = 14  # days behind the baseline that make a campus summary flag
DRAW_WAIT = 30  # days a submitted draw can wait for funding before it's flagged
OPEN_BIDS = (Bid.Status.RECEIVED, Bid.Status.INTERVIEWED, Bid.Status.SHORTLISTED)


def fmt(d):
    return f"{d:%b} {d.day}"


def start_schedule(project):
    have = set(project.milestones.values_list("name", flat=True))
    pos = project.milestones.aggregate(m=Max("position"))["m"] or 0
    new = []
    for phase, name in SCHEDULE:
        if name not in have:
            pos += 1
            new.append(BuildMilestone(project=project, phase=phase, name=name, position=pos))
    BuildMilestone.objects.bulk_create(new)
    return len(new)


def start_budget(project):
    have = {(line.code, line.name) for line in project.budget.all()}
    pos = project.budget.aggregate(m=Max("position"))["m"] or 0
    new = []
    for group, code, name in BUDGET:
        if (code, name) not in have:
            pos += 1
            new.append(BudgetLine(project=project, group=group, code=code, name=name, position=pos))
    BudgetLine.objects.bulk_create(new)
    return len(new)


def budget(project):
    """Lines with .changes (approved change orders), .current, .forecast_total and .variance, grouped, with totals.
    Approved change orders on no line count in the totals as "Not on a line"."""
    zero = Decimal(0)
    lines = list(project.budget.prefetch_related("change_orders"))
    for line in lines:
        line.changes = sum((c.amount for c in line.change_orders.all() if c.status == ChangeOrder.Status.APPROVED), zero)
        line.current = line.original + line.changes
        line.forecast_total = line.forecast if line.forecast is not None else line.current
        line.variance = line.forecast_total - line.current
    loose = sum((c.amount for c in project.change_orders.filter(status=ChangeOrder.Status.APPROVED, line=None)), zero)
    totals = {k: sum((getattr(line, k) or zero for line in lines), zero)
              for k in ("original", "changes", "current", "committed", "paid", "forecast_total", "variance")}
    for k in ("changes", "current", "forecast_total"):
        totals[k] += loose
    groups = [(label, [line for line in lines if line.group == value]) for value, label in BudgetLine.Group.choices]
    return {"lines": lines, "groups": [(label, rows) for label, rows in groups if rows], "totals": totals,
            "loose": loose}


def schedule(project, today=None):
    """Milestones by phase, each with .late (forecast passed without an actual date) and .slip; the next one not
    done; and the worst slip among those still ahead."""
    today = today or timezone.localdate()
    items = list(project.milestones.all())
    for m in items:
        m.late = bool(not m.actual and m.forecast and m.forecast < today)
    ahead = [m for m in items if not m.actual and (m.forecast or m.baseline)]
    nxt = min(ahead, key=lambda m: (m.forecast or m.baseline, m.position), default=None)
    slips = [m.slip for m in ahead if m.slip is not None]
    groups = [(label, [m for m in items if m.phase == value]) for value, label in BuildMilestone.Phase.choices]
    return {"items": items, "groups": [(label, rows) for label, rows in groups if rows], "next": nxt,
            "worst_slip": max(slips) if slips else None, "late": [m for m in items if m.late]}


def status(project, today=None):
    """Everything the build page's strip, the project page panel and the campus summary show."""
    today = today or timezone.localdate()
    bids = list(project.bids.all())
    cos = list(project.change_orders.all())
    draws = list(project.draws.all())
    gc_member = project.team.filter(role=TeamMember.Role.GC).exclude(status=TeamMember.Status.PROPOSED).first()
    selected = next((b for b in bids if b.status == Bid.Status.SELECTED), None)
    pending = [c for c in cos if c.status == ChangeOrder.Status.PENDING]
    waiting = [d for d in draws if d.status == Draw.Status.SUBMITTED and d.submitted_on
               and (today - d.submitted_on).days > DRAW_WAIT]
    return {
        "schedule": schedule(project, today), "budget": budget(project), "bids": bids,
        "bids_in": [b for b in bids if b.status in OPEN_BIDS], "selected": selected,
        "gc": gc_member.firm if gc_member else (selected.firm if selected else ""),
        "pending": pending, "pending_total": sum((c.amount for c in pending), Decimal(0)),
        "approved_days": sum(c.days for c in cos if c.status == ChangeOrder.Status.APPROVED),
        "funded": sum((d.funded or Decimal(0) for d in draws if d.status == Draw.Status.FUNDED), Decimal(0)),
        "retainage": sum((d.retainage or Decimal(0) for d in draws), Decimal(0)),
        "draws_waiting": waiting, "today": today,
        "empty": not (bids or cos or draws or project.milestones.exists() or project.budget.exists()
                      or project.team.exists()),
    }


def campus_lines(project, today):
    """(facts, flags) for the campus summary's project line: next milestone, GC, budget, change orders; flags for a
    milestone that passed without a date, a slip of two weeks or more, a forecast over budget, a slow draw."""
    s = status(project, today)
    if s["empty"]:
        return [], []
    facts, flags = [], []
    sched, cash = s["schedule"], s["budget"]
    nxt = sched["next"]
    if nxt:
        facts.append(f"next milestone: {nxt.name} {fmt(nxt.forecast or nxt.baseline)}"
                     + (f" ({nxt.slip} days behind baseline)" if nxt.slip and nxt.slip > 0 else ""))
        if nxt.slip and nxt.slip >= SLIP_FLAG:
            flags.append(f"{nxt.name} {nxt.slip} days behind baseline")
    if s["gc"]:
        facts.append(f"GC: {s['gc']}")
    elif s["bids"]:
        short = [b.firm for b in s["bids"] if b.status == Bid.Status.SHORTLISTED]
        facts.append(f"GC selection: {len(s['bids_in'])} proposal{'s' if len(s['bids_in']) != 1 else ''} in"
                     + (f", shortlist {', '.join(short)}" if short else ""))
    if cash["lines"]:
        t = cash["totals"]
        facts.append(f"budget {money(t['current'])}, forecast {money(t['forecast_total'])}")
        if t["variance"] > 0:
            flags.append(f"forecast {money(t['variance'])} over budget")
    if s["pending"]:
        facts.append(f"{len(s['pending'])} change order{'s' if len(s['pending']) != 1 else ''} pending "
                     f"({money(s['pending_total'])})")
    flags += [f"{m.name} was forecast for {fmt(m.forecast)}; record it or re-forecast" for m in sched["late"]]
    flags += [f"draw {d.number} submitted {fmt(d.submitted_on)}, not funded yet" for d in s["draws_waiting"]]
    return facts, flags


def due_rows(days=14, today=None):
    """Milestones forecast within `days` that haven't happened (or passed without a date) and GC interviews, as
    dashboard deadline rows."""
    today = today or timezone.localdate()
    until = today + timedelta(days=days)
    rows = []
    for m in BuildMilestone.objects.filter(actual__isnull=True, forecast__isnull=False, forecast__lte=until) \
            .select_related("project"):
        rows.append(SimpleNamespace(date=m.forecast, label=m.name, amount=None, what=m.project.name, who=None,
                                    url=f"/projects/{m.project_id}/build/#schedule", tag="Build"))
    for b in Bid.objects.filter(interview_on__gte=today, interview_on__lte=until, status__in=OPEN_BIDS) \
            .select_related("project"):
        rows.append(SimpleNamespace(date=b.interview_on, label=f"GC interview: {b.firm}", amount=None,
                                    what=b.project.name, who=None, url=f"/projects/{b.project_id}/build/#bids",
                                    tag="Build"))
    return rows
