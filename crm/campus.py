"""The campus Summary tab: every workstream on a campus with its latest status, who the ball is with and how long it
has been, plus a daily feed and the weekly update as email text.

Lines are assembled from what the CRM already holds (a deal's stage and next step, the last email each way, meetings,
land milestones and per-owner deals, contracts and their critical dates, project email tags) and from CampusUpdate rows
the team adds for things that happened off-system. Nothing here is written by AI.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta

from django.utils import timezone

from .contracts import dates as contract_dates
from .land import diligence, tracker
from .models import Activity, CampusUpdate, LandSite, LandSiteEvent, Meeting, OpportunityStage, Project

QUIET_DAYS = 7          # nothing new in this many days: "no activity this week"
NO_REPLY_DAYS = 10      # we wrote last, this long ago: "no reply in N days"
INBOUND = (Activity.Kind.EMAIL_RECEIVED, Activity.Kind.REPLY)
OUTBOUND = (Activity.Kind.EMAIL_SENT,)
STAGE_ORDER = [OpportunityStage.SIGNED, OpportunityStage.LEASE_NEGOTIATION, OpportunityStage.LOI_SIGNED,
               OpportunityStage.LOI_NEGOTIATION, OpportunityStage.QUALIFIED, OpportunityStage.PROSPECT,
               OpportunityStage.ON_HOLD]


def _day(value):
    if value is None:
        return None
    return timezone.localtime(value).date() if isinstance(value, datetime) else value


def _ago(day, today):
    n = (today - day).days
    return "today" if n == 0 else "yesterday" if n == 1 else f"{n} days ago" if n < 21 else f"{round(n / 7)} weeks ago"


def _fmt_day(day):
    return f"{day:%b} {day.day}"


def _waiting_note(update, today):
    """'Waiting on them since Oct 2, promised Sep 11 (5 weeks ago)' from an update, or ''."""
    if not update or not update.waiting_on:
        return ""
    note = f"{dict(CampusUpdate.Waiting.choices)[update.waiting_on]} since {_fmt_day(update.happened_on)}"
    if update.promised_on:
        if update.promised_on < today:
            note += f", promised {_fmt_day(update.promised_on)} ({_ago(update.promised_on, today)})"
        else:
            note += f", promised for {_fmt_day(update.promised_on)}"
    return note


def _tenant(opp, today):
    acts = Activity.objects.filter(company=opp.company)
    last_in = acts.filter(kind__in=INBOUND).order_by("-occurred_at").first()
    last_out = acts.filter(kind__in=OUTBOUND).order_by("-occurred_at").first()
    now = timezone.now()
    meetings = Meeting.objects.filter(companies=opp.company)
    last_meeting = meetings.filter(start__lte=now).order_by("-start").first()
    next_meeting = meetings.filter(start__gt=now).order_by("start").first()
    update = opp.campus_updates.first()
    flags, facts = [], []
    if last_out and (not last_in or last_out.occurred_at > last_in.occurred_at):
        quiet = (today - _day(last_out.occurred_at)).days
        if quiet >= NO_REPLY_DAYS:
            flags.append(f"no reply in {quiet} days (we wrote {_fmt_day(_day(last_out.occurred_at))})")
    if last_in:
        facts.append(f"last heard from them {_fmt_day(_day(last_in.occurred_at))}")
    if last_meeting:
        facts.append(f"met {_fmt_day(_day(last_meeting.start))}")
    if next_meeting:
        facts.append(f"next meeting {_fmt_day(_day(next_meeting.start))}")
    if opp.next_step:
        due = f" (due {_fmt_day(opp.next_step_due)})" if opp.next_step_due else ""
        facts.append(f"next step: {opp.next_step}{due}")
        if opp.next_step_due and opp.next_step_due < today:
            flags.append(f"next step overdue since {_fmt_day(opp.next_step_due)}")
    # A stage move counts as activity only when it happened after the deal was created: imported deals carry the
    # import time as their stage date, which would make every quiet deal look fresh.
    moved = opp.stage_changed_at if opp.stage_changed_at - opp.created_at > timedelta(minutes=5) else None
    touches = [d for d in (_day(last_in and last_in.occurred_at), _day(last_out and last_out.occurred_at),
                           _day(last_meeting and last_meeting.start), update and update.happened_on,
                           _day(moved)) if d]
    last_touch = max(touches) if touches else None
    no_reply = any(f.startswith("no reply") for f in flags)  # already says how long it has been quiet
    if not no_reply and (not last_touch or (today - last_touch).days >= QUIET_DAYS):
        flags.append(f"no activity in {(today - last_touch).days} days" if last_touch else "no activity recorded")
    contract_facts, contract_flags = contract_dates.campus_lines(opp.contracts.prefetch_related("dates"), today)
    facts[:0], flags[:0] = contract_facts, contract_flags  # the agreement and its deadlines lead
    return {"kind": "tenant", "obj": opp, "name": opp.company.name, "url": opp.get_absolute_url(),
            "status": opp.get_stage_display(), "update": update, "facts": facts, "flags": flags,
            "waiting": _waiting_note(update, today), "last_touch": last_touch}


def _land(site, today):
    deals = [d for d in tracker.site_deals(site) if d.included]
    by_owner = defaultdict(list)
    for d in deals:
        by_owner[(d.parcel.owner_name or "Unknown owner").strip().title()].append(d)
    owners = []
    for owner, ds in sorted(by_owner.items(), key=lambda kv: -sum(d.acres for d in kv[1])):
        stage = min((d.status for d in ds), key=lambda s: LandSite.STATUS_ORDER.index(s))  # furthest along
        dates = [v for d in ds for v in (d.milestones or {}).values() if v]
        owners.append({"owner": owner, "status": dict(LandSite.Status.choices).get(stage, stage),
                       "acres": round(sum(d.acres for d in ds), 1), "last": max(dates) if dates else ""})
    event = site.events.first()
    update = site.campus_updates.first()
    touches = [d for d in (_day(event and event.when), update and update.happened_on) if d]
    last_touch = max(touches) if touches else None
    flags = []
    if not last_touch or (today - last_touch).days >= QUIET_DAYS:
        flags.append(f"no activity in {(today - last_touch).days} days" if last_touch else "no activity recorded")
    facts = [f"{event.text} ({_fmt_day(_day(event.when))})"] if event else []
    contract_facts, contract_flags = contract_dates.campus_lines(site.contracts.prefetch_related("dates"), today)
    dd_facts, dd_flags = diligence.campus_lines(site, today)
    facts[:0], flags[:0] = contract_facts + dd_facts, contract_flags + dd_flags  # the agreement, deadlines, DD lead
    return {"kind": "land", "obj": site, "name": site.name, "url": site.get_absolute_url(),
            "manage_url": f"/land/sites/{site.pk}/manage/", "status": site.get_status_display(), "update": update,
            "facts": facts, "flags": flags, "owners": owners if len(owners) > 1 else [],
            "waiting": _waiting_note(update, today), "last_touch": last_touch}


def _project(project, today):
    last_email = project.activities.order_by("-occurred_at").first()
    update = project.campus_updates.first()
    facts = [f"last email {_fmt_day(_day(last_email.occurred_at))}: {last_email.summary[:90]}"] if last_email else []
    touches = [d for d in (_day(last_email and last_email.occurred_at), update and update.happened_on) if d]
    last_touch = max(touches) if touches else None
    flags = []
    if not last_touch or (today - last_touch).days >= QUIET_DAYS:
        flags.append(f"no activity in {(today - last_touch).days} days" if last_touch else "no activity recorded")
    return {"kind": "project", "obj": project, "name": project.name, "url": project.get_absolute_url(),
            "status": "", "update": update, "facts": facts, "flags": flags,
            "waiting": _waiting_note(update, today), "last_touch": last_touch}


def summary(campus, today=None, feed_days=7):
    """Everything the Summary tab shows for one campus."""
    today = today or timezone.localdate()
    opps = (campus.opportunities.exclude(stage=OpportunityStage.LOST).select_related("company", "owner")
            .prefetch_related("campus_updates"))
    tenants = sorted((_tenant(o, today) for o in opps),
                     key=lambda i: (STAGE_ORDER.index(i["obj"].stage) if i["obj"].stage in STAGE_ORDER else 99,
                                    i["name"].lower()))
    sites = campus.land_sites.exclude(status=LandSite.Status.DROPPED).prefetch_related("events", "campus_updates")
    land = [_land(s, today) for s in LandSite.by_status(sites)]
    projects = [_project(p, today) for p in
                Project.objects.filter(campus=campus, active=True).exclude(name=campus.code).order_by("name")]
    other = list(campus.updates.filter(workstream=CampusUpdate.Workstream.OTHER, opportunity=None, site=None,
                                       project=None)[:10])
    return {"campus": campus, "today": today, "tenants": tenants, "land": land, "projects": projects, "other": other,
            "feed": feed(campus, today, feed_days), "feed_days": feed_days}


def feed(campus, today, days=7):
    """What changed on the campus, newest first, grouped by day: emails and meetings with its tenants, land site
    events, deal stage moves, and updates."""
    start = timezone.make_aware(datetime.combine(today - timedelta(days=days - 1), time.min))
    end = timezone.make_aware(datetime.combine(today, time.max))  # calendar imports hold future meetings too
    company_ids = list(campus.opportunities.values_list("company_id", flat=True))
    rows = []
    for a in (Activity.objects.filter(company_id__in=company_ids, occurred_at__gte=start, occurred_at__lte=end,
                                      is_background=False,
                                      kind__in=INBOUND + OUTBOUND + (Activity.Kind.MEETING, Activity.Kind.NOTE,
                                                                     Activity.Kind.CALL, Activity.Kind.STAGE_CHANGE))
              .select_related("company").order_by("-occurred_at")[:200]):
        rows.append({"when": a.occurred_at, "what": f"{a.company.name}: {a.summary}"[:200], "kind": a.get_kind_display()})
    for e in LandSiteEvent.objects.filter(site__campus=campus, when__gte=start).select_related("site")[:200]:
        rows.append({"when": e.when, "what": f"{e.site.name}: {e.text}", "kind": "Land"})
    for u in campus.updates.filter(created_at__gte=start).select_related("opportunity__company", "site", "project"):
        subject = (u.opportunity and u.opportunity.company.name) or (u.site and u.site.name) or \
            (u.project and u.project.name) or u.get_workstream_display()
        rows.append({"when": u.created_at, "what": f"{subject}: {u.text}", "kind": "Update"})
    rows.sort(key=lambda r: r["when"], reverse=True)
    by_day = defaultdict(list)
    for r in rows:
        by_day[_day(r["when"])].append(r)
    return [{"day": d, "rows": by_day[d]} for d in sorted(by_day, reverse=True)]


def _line(item):
    text = item["update"].text if item["update"] else ""
    parts = [text] if text else []
    if not text:
        parts += item["facts"][:2]
    if item["waiting"]:
        parts.append(item["waiting"])
    parts += item["flags"]
    return "; ".join(p for p in parts if p) or "no updates"


def email_text(data):
    """The weekly update in the team's usual bullet format, ready to paste into an email."""
    c = data["campus"]
    out = [f"{c.code} update, week of {data['today'] - timedelta(days=data['today'].weekday()):%b %d}", ""]
    if data["tenants"]:
        out.append("Tenants")
        out += [f"  - {i['name']} ({i['status']}): {_line(i)}" for i in data["tenants"]]
    if data["land"]:
        out.append("Land")
        for i in data["land"]:
            out.append(f"  - {i['name']} ({i['status']}): {_line(i)}")
            out += [f"      - {o['owner']}: {o['status']}" + (f", last milestone {o['last']}" if o["last"] else "")
                    for o in i["owners"]]
    if data["projects"]:
        out.append("Projects")
        out += [f"  - {i['name']}: {_line(i)}" for i in data["projects"]]
    if data["other"]:
        out.append("Other")
        out += [f"  - {u.text}" for u in data["other"]]
    return "\n".join(out) + "\n"
