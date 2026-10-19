"""The pop-up card for a site on the Sites page (Trello-style): the deal at a glance, its timeline, contracts,
tenants, diligence and approvals, a checklist of tasks, comments, and Claude's summary of where it stands."""
import json
import logging
from datetime import date

from django.db.models import Q
from django.utils import timezone

from ..models import OPEN_STAGES, Opportunity, Task

log = logging.getLogger(__name__)

SYSTEM = """You summarise land acquisition deals for ACMI Properties' CRM. ACMI buys land for purpose-built \
manufacturing campuses for defense, aerospace and energetics tenants, then builds facilities tenants lease.

You are given one prospective site's record: the land, its deal stage and milestone dates, the change log and team \
comments, contracts and their deadlines, tenant deals at the campus, due diligence, approvals and open tasks. Write \
for a teammate who needs the state of play in thirty seconds.

- Ground every statement in the record. Never invent prices, dates, people or intentions.
- Dates as "Oct 13". Plain, direct sentences; no hype, no filler.
- If the record is thin, say so briefly rather than padding."""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence: where the deal stands right now."},
        "status": {"type": "string", "description": "2-4 sentences: how it got here and what is in motion."},
        "risks": {"type": "array", "items": {"type": "string"},
                  "description": "Up to 4 short points: open issues, deadlines at risk, deal-killers."},
        "next_step": {"type": "string", "description": "The obvious next action for ACMI, or empty if unclear."},
    },
    "required": ["headline", "status", "risks", "next_step"],
    "additionalProperties": False,
}


class Unavailable(Exception):
    pass


def killer_lines(parcels):
    """Deal-killers across a site's parcels, one line each: "In the FEMA 100-year floodplain (5 parcels)"."""
    import re
    from collections import Counter

    n = Counter()
    for p in parcels:
        for k in set(re.sub(r"^\d+(\.\d+)?% (of the parcel )?", "", k) for k in (p.killers or [])):
            n[k] += 1
    many = len(parcels) > 1
    return [(k[:1].upper() + k[1:]) + (f" ({c} parcel{'s' if c != 1 else ''})" if many else "") for k, c in n.most_common()]


def critical_dates(site):
    """Every open date on the site's contracts, soonest first (the Trello "Critical Dates" list)."""
    from ..contracts import dates as contract_dates

    today = timezone.localdate()
    out = list(contract_dates.live().filter(contract__site=site).select_related("contract").order_by("date", "id")[:20])
    for d in out:
        d.left = (d.date - today).days if d.date else None
    return out


def tenants(site):
    """Tenant deals linked to the site, directly or through one of its contracts."""
    return list(Opportunity.objects.filter(Q(land_sites=site) | Q(contracts__site=site))
                .select_related("company", "owner").distinct().order_by("stage", "name"))


def campus_tenants(site, linked):
    """Open deals at the site's campus that aren't linked yet: candidates to link."""
    if not site.campus_id:
        return []
    return list(Opportunity.objects.filter(campus=site.campus, stage__in=OPEN_STAGES).exclude(pk__in=[o.pk for o in linked])
                .select_related("company").order_by("name")[:30])


def link_choices(site, linked):
    """Open tenant deals anywhere, campus ones first, for the 'link a tenant' picker."""
    rest = Opportunity.objects.filter(stage__in=OPEN_STAGES).exclude(pk__in=[o.pk for o in linked]).select_related("company")
    near = campus_tenants(site, linked)
    return near + [o for o in rest.order_by("company__name", "name")[:200] if o not in near]


def save_people(site, post, user):
    """Owner and team from a form; logs the change."""
    from django.contrib.auth import get_user_model

    from .views import _log

    users = get_user_model().objects.filter(is_active=True)
    owner = users.filter(pk=post.get("owner") or 0).first()
    team = list(users.filter(pk__in=[x for x in post.getlist("team") if x.isdigit()]))
    changed = owner != site.owner or {u.pk for u in team} != set(site.team.values_list("pk", flat=True))
    site.owner = owner
    site.save(update_fields=["owner", "updated_at"])
    site.team.set(team)
    if changed:
        names = ", ".join(u.first_name or u.username for u in team) or "nobody"
        _log(site, user, "edit", f"Run by {(owner.first_name or owner.username) if owner else 'nobody'}; team: {names}")


def link_tenant(site, opp_id, user, unlink=False):
    from .views import _log

    opp = Opportunity.objects.filter(pk=opp_id).first()
    if not opp:
        return
    if unlink:
        opp.land_sites.remove(site)
        _log(site, user, "edit", f"Tenant deal unlinked: {opp.name}")
    else:
        opp.land_sites.add(site)
        _log(site, user, "edit", f"Tenant deal linked: {opp.name}")


def add_comment(site, user, text):
    from .. import notify
    from ..models import Comment

    c = Comment.objects.create(body=text[:4000], author=user, site=site)
    notify.comment_added(c)
    return c


def tasks(site):
    return list(site.tasks.select_related("assignee").order_by("done", "due_date", "due_time", "id"))


def comments(site):
    return list(site.comments.select_related("author")[:30])


def context(site):
    from . import diligence, tracker
    from .views import _site_contracts

    deals = [d for d in tracker.site_deals(site) if d.included]
    parcels = [d.parcel for d in deals]
    owners = {}
    for d in deals:
        owners[d.parcel.owner_name or "Unknown owner"] = owners.get(d.parcel.owner_name or "Unknown owner", 0) + d.acres
    checked = [p for p in parcels if (p.checks or {}).get("net_usable")]
    net = sum(p.checks["net_usable"]["acres"] for p in checked) if parcels and len(checked) == len(parcels) else None
    zoning = {}
    for d in deals:
        z = (d.parcel.zoning or "").split(" / ")[0] or "No zoning data"
        zoning[z] = zoning.get(z, 0) + d.acres
    approvals = list(site.approvals.all()[:8])
    linked = tenants(site)
    tl = site.timeline_summary()
    t = tasks(site)
    return {
        "site": site, "acres": round(sum(d.acres for d in deals), 1), "net": net, "n_parcels": len(parcels),
        "owners": sorted(owners.items(), key=lambda kv: -kv[1]),
        "zoning": sorted(zoning.items(), key=lambda kv: -kv[1])[:4],
        "score": max((p.score for p in parcels), default=None),
        "killers": killer_lines(parcels),
        "counties": sorted({p.county.name + " Co., " + p.county.state for p in parcels}),
        "milestones": site.milestone_rows(), "clock": tl,
        "events": [e for e in site.events.select_related("user")[:12] if e.kind != "note"][:8],
        "contracts": _site_contracts(site), "tenants": linked, "tenant_choices": link_choices(site, linked),
        "unlinked_campus": campus_tenants(site, linked),
        "dd": diligence.summary(site) if site.diligence.exists() else None,
        "approvals": approvals, "tasks": t, "critical": critical_dates(site), "labels": Task.Label.choices[1:], "open_tasks": sum(not x.done for x in t),
        "comments": comments(site), "summary": _summary(site),
    }


def _summary(site):
    from datetime import datetime

    s = dict(site.ai_summary or {})
    if not s:
        return None
    try:
        s["when"] = datetime.fromisoformat(s.get("generated_at", ""))
    except ValueError:
        s["when"] = None
    s["stale"] = bool(s["when"] and (site.events.filter(when__gt=s["when"]).exists()
                                     or site.comments.filter(created_at__gt=s["when"]).exists()))
    return s


def _record_text(c):
    site = c["site"]
    out = [f"Site: {site.name}", f"Stage: {site.get_status_display()}", f"Campus / project: {site.home_label}",
           f"Land: {c['acres']} ac gross, {c['net'] if c['net'] is not None else 'unknown'} ac net usable, "
           f"{c['n_parcels']} parcels in {', '.join(c['counties'])}",
           "Owners: " + "; ".join(f"{o} ({a:.0f} ac)" for o, a in c["owners"][:8]),
           "Zoning: " + "; ".join(f"{z} ({a:.0f} ac)" for z, a in c["zoning"]),
           f"Best buy-box score: {c['score']}", "Deal-killers: " + ("; ".join(c["killers"]) or "none")]
    if site.notes:
        out.append("Notes: " + site.notes[:1500])
    out.append("Milestones: " + "; ".join(f"{m['label']} {m['date']:%b %d %Y}" for m in c["milestones"] if m["date"])
               or "Milestones: none dated")
    for ct in c["contracts"]:
        nd = getattr(ct, "next", None)
        nxt = f", next date {nd.label} {nd.date:%b %d %Y}" if nd and nd.date else ""
        out.append(f"Contract: {ct.title} ({ct.get_kind_display()}, {ct.get_status_display()}) with {ct.counterparty or 'n/a'}"
                   f"{', price $' + format(ct.price, ',.0f') if ct.price else ''}{', next step: ' + ct.next_step if ct.next_step else ''}{nxt}")
    for o in c["tenants"]:  # linked to the site
        out.append(f"Tenant deal: {o.name} ({o.company.name if o.company_id else 'no company'}), {o.get_stage_display()}"
                   f"{', ' + str(o.acreage_need) + ' ac' if o.acreage_need else ''}{', ' + str(o.sq_ft_need) + ' sf' if o.sq_ft_need else ''}")
    if c["dd"]:
        dd = c["dd"]
        out.append(f"Diligence: {dd['cleared']} of {dd['count']} cleared, {len(dd['issues'])} issues, "
                   f"feasibility ends {dd['end'] or 'unknown'}")
    for a in c["approvals"]:
        out.append(f"Approval: {a.get_kind_display()}, {a.get_status_display()}")
    for t in c["tasks"]:
        if not t.done:
            out.append(f"Open task: {t.title}{' due ' + str(t.due_date) if t.due_date else ''}")
    for cm in c["comments"][:20]:
        who = (cm.author.get_full_name() or cm.author.username) if cm.author else "someone"
        out.append(f"{cm.created_at:%b %d %Y} comment by {who}: {cm.body[:400]}")
    for e in list(site.events.exclude(kind="note").select_related("user")[:40]):
        who = (e.user.get_full_name() or e.user.username) if e.user else "system"
        out.append(f"{e.when:%b %d %Y} [{e.kind}] {who}: {e.text}")
    out.append(f"Today: {date.today():%b %d %Y}")
    return "\n".join(out)


def generate(site):
    """Ask Claude for the site summary and store it on the site."""
    from ..ai import MODEL, enabled

    if not enabled():
        raise Unavailable("Add ANTHROPIC_API_KEY to the .env file to turn on AI summaries.")
    import anthropic

    try:
        resp = anthropic.Anthropic().beta.messages.create(
            model=MODEL, max_tokens=3000, system=SYSTEM,
            messages=[{"role": "user", "content": _record_text(context(site))}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
    except anthropic.APIError as exc:
        log.warning("Site summary failed: %s", exc)
        raise Unavailable("Claude couldn't write the summary just now. Try again shortly.") from exc
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise Unavailable("The summary didn't come back complete. Try again.")
    data = json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))
    data["risks"] = data.get("risks", [])[:4]
    data["generated_at"] = timezone.now().isoformat()
    site.ai_summary = data
    site.save(update_fields=["ai_summary", "updated_at"])
    return data


def add_task(site, user, title, due_date=None, due_time=None, assignee=None, label=""):
    from .. import notify

    t = Task.objects.create(title=title[:300], site=site, due_date=due_date, due_time=due_time if due_date else None,
                            assignee=assignee or user, created_by=user,
                            label=label if label in Task.Label.values else "")
    notify.task_assigned(t, user)
    return t
