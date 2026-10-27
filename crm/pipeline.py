"""Pipeline numbers for Home, the Opportunities board and the investor report.

Everything here reads the database; nothing writes except `create_from_suggestion`.
"""
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Max, Q
from django.utils import timezone

from .models import (
    OPEN_STAGES, STAGE_ORDER, STAGE_PROBABILITY, Activity, Attachment, Campus, Company, DealSuggestionDismissal,
    FieldHistory, Opportunity, OpportunityStage, Task,
)

STALE_DAYS = 14
# Email attachments that mean a deal is real, and the stage each one implies.
DEAL_DOCS = OrderedDict([
    ("Lease / term sheet", OpportunityStage.LEASE_NEGOTIATION),
    ("PDA", OpportunityStage.LOI_NEGOTIATION),
    ("LOI", OpportunityStage.LOI_NEGOTIATION),
    ("Contract", OpportunityStage.LOI_NEGOTIATION),
    ("NDA", OpportunityStage.QUALIFIED),
    ("Proposal", OpportunityStage.QUALIFIED),
])
NEGATIVE_REPLIES = {"Not Interested", "Undeliverable", "Out of Office Reply", "Automated Reply"}


def open_deals():
    return Opportunity.objects.filter(stage__in=OPEN_STAGES)


def _sum(opps, attr="value"):
    return sum((getattr(o, attr) or 0 for o in opps), Decimal(0))


def risk_reasons(opp, today=None):
    """Why an open deal needs attention; empty list = fine."""
    today = today or timezone.localdate()
    reasons = []
    if not opp.next_step:
        reasons.append("No next step")
    elif opp.next_step_due and opp.next_step_due < today:
        reasons.append(f"Next step overdue since {opp.next_step_due:%b} {opp.next_step_due.day}")
    last = opp.company.last_interaction_at
    if last is None:
        reasons.append("Never contacted")
    elif (timezone.now() - last).days > STALE_DAYS:
        reasons.append(f"No contact in {(timezone.now() - last).days} days")
    return reasons


def summary(qs=None):
    """Totals by stage and by campus for open deals, plus signed."""
    deals = list((qs if qs is not None else Opportunity.objects.all()).select_related("campus", "company", "owner"))
    open_ = [o for o in deals if o.stage in OPEN_STAGES]
    signed = [o for o in deals if o.stage == OpportunityStage.SIGNED]
    by_stage = []
    for value, label in OpportunityStage.choices:
        items = [o for o in deals if o.stage == value]
        by_stage.append({
            "value": value, "label": label, "count": len(items), "total": _sum(items),
            "weighted": sum((o.weighted_value for o in items), Decimal(0)), "probability": STAGE_PROBABILITY[value],
        })
    by_campus = OrderedDict()
    for o in open_:
        key = o.campus.code if o.campus_id else "Other / TBD"
        row = by_campus.setdefault(key, {"label": key, "count": 0, "total": Decimal(0), "weighted": Decimal(0),
                                         "sf": 0, "acres": Decimal(0)})
        row["count"] += 1
        row["total"] += o.value or 0
        row["weighted"] += o.weighted_value
        row["sf"] += o.sq_ft_need or 0
        row["acres"] += o.acreage_need or 0
    return {
        "open_count": len(open_),
        "open_total": _sum(open_),
        "weighted_total": sum((o.weighted_value for o in open_), Decimal(0)),
        "signed_count": len(signed),
        "signed_total": _sum(signed),
        "unpriced": sum(1 for o in open_ if not o.value),
        "by_stage": by_stage,
        "by_campus": sorted(by_campus.values(), key=lambda r: (r["label"] == "Other / TBD", r["label"])),
    }


def at_risk(qs=None, limit=None):
    today = timezone.localdate()
    rows = []
    deals = (qs if qs is not None else open_deals()).filter(stage__in=OPEN_STAGES).select_related("company", "owner", "campus")
    for o in deals:
        reasons = risk_reasons(o, today)
        if reasons:
            rows.append({"opp": o, "reasons": reasons})
    # Furthest-along deals first: those hurt most when they stall.
    rows.sort(key=lambda r: (-STAGE_ORDER[r["opp"].stage], -(r["opp"].value or 0)))
    return rows[:limit] if limit else rows


def changes_since(days=7):
    """Deal stage moves and new deals in the last `days` days."""
    since = timezone.now() - timedelta(days=days)
    ct = ContentType.objects.get_for_model(Opportunity)
    moves = list(FieldHistory.objects.filter(content_type=ct, field="stage", changed_at__gte=since)
                 .select_related("changed_by").order_by("-changed_at")[:30])
    opps = Opportunity.objects.in_bulk([m.object_id for m in moves])
    for m in moves:
        m.opp = opps.get(m.object_id)
    new = list(Opportunity.objects.filter(created_at__gte=since).exclude(source_key__startswith="trello:")
               .exclude(source_key__startswith="tracker:").select_related("company")[:20])
    return {"moves": [m for m in moves if m.opp], "new": new}


# ---------------------------------------------------------------------------
# Deals suggested from email attachments
# ---------------------------------------------------------------------------


def suggestions(limit=None):
    """Companies with an LOI/PDA/NDA/lease/proposal in their email but no deal in the CRM."""
    since = timezone.now() - timedelta(days=365)
    with_deal = Opportunity.objects.values("company")
    dismissed = DealSuggestionDismissal.objects.values("company")
    files = (Attachment.objects.filter(category__in=DEAL_DOCS, activity__occurred_at__gte=since,
                                       activity__company__isnull=False)
             .exclude(activity__company__in=with_deal).exclude(activity__company__in=dismissed)
             .select_related("activity", "activity__company", "activity__person")
             .order_by("-activity__occurred_at"))
    by_company = OrderedDict()
    for f in files:
        company = f.activity.company
        row = by_company.setdefault(company.pk, {"company": company, "files": [], "stage": None})
        if len(row["files"]) < 3 and f.filename not in [x.filename for x in row["files"]]:
            row["files"].append(f)
        stage = DEAL_DOCS[f.category]
        if row["stage"] is None or STAGE_ORDER[stage] > STAGE_ORDER[row["stage"]]:
            row["stage"] = stage
    rows = list(by_company.values())
    for r in rows:
        r["stage_label"] = OpportunityStage(r["stage"]).label
    return rows[:limit] if limit else rows


def create_from_suggestion(company, user):
    row = next((r for r in suggestions() if r["company"].pk == company.pk), None)
    stage = row["stage"] if row else OpportunityStage.QUALIFIED
    owner = company.owner or company.strongest_connection_user or user
    files = ", ".join(f.filename for f in row["files"]) if row else ""
    opp = Opportunity.objects.create(
        name=f"{company.name} · TBD", company=company, stage=stage, owner=owner,
        next_step="Confirm site, size and timing", notes=f"Created from email documents: {files}" if files else "",
    )
    opp.team.add(owner)
    if company.owner_id is None:
        company.owner = owner
        company.save()
    Activity.objects.create(
        kind=Activity.Kind.RECORD_CREATED, occurred_at=timezone.now(), actor=user, company=company, opportunity=opp,
        summary=f"Opportunity created from documents: {opp.name}",
    )
    return opp


# ---------------------------------------------------------------------------
# My day
# ---------------------------------------------------------------------------


def my_companies(user):
    """Companies a user is responsible for: owned, or on the team of one of its deals."""
    return Company.objects.filter(
        Q(owner=user) | Q(opportunities__owner=user) | Q(opportunities__team=user)
    ).distinct()


REPLIES_DISMISSED = "my_day_replies_dismissed"  # UserPrefs key: reply Activity ids this person said need no answer


def awaiting_reply(user, days=21):
    """Replies to this user (or on their companies) that nobody on the team has answered yet, less the ones they
    dismissed (a later reply on the same thread is a new Activity, so it shows again)."""
    from . import prefs

    since = timezone.now() - timedelta(days=days)
    dismissed = set(prefs.get_prefs(user).get(REPLIES_DISMISSED, []))
    mine = my_companies(user).values("pk")
    replies = (Activity.objects.filter(kind=Activity.Kind.REPLY, occurred_at__gte=since, company__isnull=False)
               .exclude(outcome__in=NEGATIVE_REPLIES)
               .filter(Q(team_members=user) | Q(company__in=mine))
               .select_related("company", "person").distinct().order_by("-occurred_at"))
    out, seen = [], set()
    for r in replies:
        key = r.thread_id or f"c{r.company_id}"
        if key in seen:
            continue
        seen.add(key)
        if r.pk in dismissed:
            continue
        answered = Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT, occurred_at__gt=r.occurred_at)
        answered = answered.filter(thread_id=r.thread_id) if r.thread_id else answered.filter(company=r.company_id)
        if not answered.exists():
            out.append(r)
    return out[:50]


def my_day(user):
    today = timezone.localdate()
    tasks = Task.objects.filter(assignee=user, done=False, due_date__lte=today).select_related("company", "person", "opportunity")
    deals = (open_deals().filter(Q(owner=user) | Q(team=user)).distinct()
             .select_related("company", "campus"))
    needs_step = [{"opp": o, "reasons": risk_reasons(o, today)} for o in deals]
    needs_step = [r for r in needs_step if r["reasons"]]
    needs_step.sort(key=lambda r: -STAGE_ORDER[r["opp"].stage])
    mine = my_companies(user).values("pk")
    meetings = (Activity.objects.filter(kind=Activity.Kind.MEETING, occurred_at__gte=timezone.now(),
                                        occurred_at__lte=timezone.now() + timedelta(days=7))
                .filter(Q(team_members=user) | Q(company__in=mine)).select_related("company").distinct()
                .order_by("occurred_at")[:10])
    return {
        "replies": awaiting_reply(user),
        "tasks": list(tasks),
        "deals": needs_step[:50],
        "meetings": list(meetings),
        "deal_count": deals.count(),
    }
