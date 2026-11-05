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
TASKS_DECLINED = "follow_up_tasks_declined"  # UserPrefs key: emails whose Follow-ups task this person deleted by hand


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
        out.append(r)
    # Everything we sent since, once, instead of one lookup per reply: a reply is answered when something went out
    # on its thread (or, for a reply with no thread, to its company) after it.
    sent = (Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT, occurred_at__gt=since)
            .filter(Q(thread_id__in=[r.thread_id for r in out if r.thread_id])
                    | Q(company_id__in=[r.company_id for r in out if not r.thread_id]))
            .values_list("thread_id", "company_id", "occurred_at"))
    by_thread, by_company = {}, {}
    for thread_id, company_id, when in sent:
        if thread_id:
            by_thread[thread_id] = max(by_thread.get(thread_id, when), when)
        by_company[company_id] = max(by_company.get(company_id, when), when)

    def answered(r):
        last = by_thread.get(r.thread_id) if r.thread_id else by_company.get(r.company_id)
        return last is not None and last > r.occurred_at

    return [r for r in out if not answered(r)][:50]


# Follow-ups: every email thread where the ball sits still. "Us" = they wrote and nobody on the team answered;
# "them" = we wrote and they haven't. The colour and the order follow how many days it has sat.
US_STEPS = (1, 3, 7)      # days waiting on us that turn a row amber, orange, red
THEM_STEPS = (3, 7, 14)   # days waiting on them (we give people longer to answer)
THEM_QUIET = THEM_STEPS[0]
LEVELS = ("new", "amber", "orange", "red")


def _level(days, steps):
    return sum(1 for s in steps if days >= s)


def _entry(act, side, now):
    days = max((now - act.occurred_at).days, 0)
    level = _level(days, US_STEPS if side == "us" else THEM_STEPS)
    return {"act": act, "side": side, "days": days, "level": level, "tone": LEVELS[level],
            "score": level * 1000 + days * 10 + (5 if side == "us" else 0)}


def thread_hex(thread_id):
    """Gmail's own thread id (hex) from the decimal one the timeline stores; "" if it isn't a number."""
    try:
        return format(int(thread_id), "x")
    except (TypeError, ValueError):
        return ""


def waiting_on_them(user, days=60):
    """Emails the team sent that nobody has answered, quiet for THEM_QUIET days or more. One per thread, newest send.
    Outreach campaign sends are left out (they have their own report); a dismissed one stays off until we send again."""
    from . import prefs

    now = timezone.now()
    dismissed = set(prefs.get_prefs(user).get(REPLIES_DISMISSED, []))
    mine = my_companies(user).values("pk")
    sent = (Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT, occurred_at__gte=now - timedelta(days=days),
                                    company__isnull=False, campaign="").exclude(thread_id="")
            .filter(Q(team_members=user) | Q(company__in=mine))
            .select_related("company", "person").distinct().order_by("-occurred_at"))
    latest = {}
    for a in sent:
        latest.setdefault(a.thread_id, a)
    heard = (Activity.objects.filter(thread_id__in=list(latest), occurred_at__gte=now - timedelta(days=days),
                                     kind__in=[*Activity.RESPONSE_KINDS, Activity.Kind.EMAIL_RECEIVED])
             .values_list("thread_id", "occurred_at"))
    last_heard = {}
    for thread_id, when in heard:
        last_heard[thread_id] = max(last_heard.get(thread_id, when), when)
    out = []
    for tid, a in latest.items():
        if a.pk in dismissed or (tid in last_heard and last_heard[tid] > a.occurred_at):
            continue
        if (now - a.occurred_at).days >= THEM_QUIET:
            out.append(_entry(a, "them", now))
    return out


def follow_ups(user):
    """{"us": [...], "them": [...], "all": [...]} entries ({act, side, days, level, tone, score}), most urgent first:
    the redder and older a thread, the higher it sits, and what waits on us comes before what waits on them."""
    now = timezone.now()
    us = [_entry(a, "us", now) for a in awaiting_reply(user, days=60)]
    them = waiting_on_them(user)
    everything = sorted(us + them, key=lambda e: -e["score"])
    return {"us": sorted(us, key=lambda e: -e["score"]), "them": sorted(them, key=lambda e: -e["score"]),
            "all": everything}


def follow_up_title(entry, gist=""):
    who = entry["act"].person.full_name if entry["act"].person_id else entry["act"].company.name
    head = "Reply to" if entry["side"] == "us" else "Follow up with"
    tail = f": {gist}" if gist else ""
    return f"{head} {who}{tail}"[:300]


def sync_follow_up_tasks(user, found=None):
    """Make a task for every follow-up that has none, and tick off the tasks whose email has since been answered or
    dismissed. Safe to run any number of times: a task is tied to its email, and one that was ticked or deleted by
    hand is not made again. Returns the number made."""
    from .models import Task
    from .views import reply_gist

    from . import prefs

    found = found or follow_ups(user)
    open_ids = {e["act"].pk for e in found["all"]}
    declined = set(prefs.get_prefs(user).get(TASKS_DECLINED, []))  # a task deleted by hand stays deleted
    existing = set(Task.objects.filter(assignee=user, source_activity__in=open_ids).values_list("source_activity_id", flat=True))
    # Anything ever made for these emails counts as made, so a task someone ticked is not brought back.
    made = set(Task.objects.filter(source_activity__in=open_ids).values_list("source_activity_id", flat=True))
    n = 0
    for e in found["all"]:
        a = e["act"]
        if a.pk in made or a.pk in existing or a.pk in declined:
            continue
        due = timezone.localtime(a.occurred_at).date() + timedelta(days=1 if e["side"] == "us" else THEM_QUIET)
        Task.objects.create(
            title=follow_up_title(e, reply_gist(a.body, a.person, limit=70)), due_date=due,
            assignee=user, company=a.company, person=a.person, opportunity=a.opportunity, source_activity=a,
            notes="Made by Follow-ups. Ticks itself when the email is answered.", created_by=None)
        n += 1
    # Answered or dismissed: the task closes itself.
    stale = Task.objects.filter(assignee=user, done=False, source_activity__isnull=False).exclude(source_activity__in=open_ids)
    stale.update(done=True, done_at=timezone.now())
    return n


def close_answered_tasks(activity):
    """A follow-up task ticks itself the moment its email is answered (Kellen, 2026-11-05: "if I respond the email it
    should autocomplete"), not the next time someone opens Follow-ups. Called for every newly logged email:
    - something we sent answers the tasks made for what they wrote to us (kind reply / email received);
    - something they wrote answers the tasks made for what we sent (a reply, an email received, an auto-reply).
    The same thread, and for a source with no thread the same company; only an email newer than the task's own."""
    from .models import Task

    sent = activity.kind == Activity.Kind.EMAIL_SENT
    heard = activity.kind in (Activity.Kind.REPLY, Activity.Kind.EMAIL_RECEIVED, Activity.Kind.AUTO_REPLY)
    if not (sent or heard):
        return 0
    source_kinds = (Activity.Kind.REPLY, Activity.Kind.EMAIL_RECEIVED) if sent else (Activity.Kind.EMAIL_SENT,)
    where = Q()
    if activity.thread_id:
        where |= Q(source_activity__thread_id=activity.thread_id)
    if activity.company_id:
        where |= Q(source_activity__thread_id="", source_activity__company_id=activity.company_id)
    if not where:
        return 0
    return (Task.objects.filter(done=False, source_activity__isnull=False, source_activity__kind__in=source_kinds,
                                source_activity__occurred_at__lt=activity.occurred_at).filter(where)
            .update(done=True, done_at=timezone.now()))


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
    found = follow_ups(user)
    sync_follow_up_tasks(user, found)
    return {
        "replies": [e["act"] for e in found["us"]][:50],
        "waiting": found,
        "tasks": list(tasks),
        "deals": needs_step[:50],
        "meetings": list(meetings),
        "deal_count": deals.count(),
    }
