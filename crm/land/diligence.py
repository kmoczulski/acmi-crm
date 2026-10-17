"""Due diligence on the sites ACMI is buying: the checklist (the same one the client reports use), who is on each
item, what it costs, what is late against the feasibility deadline, and Claude's cited summaries of the reports.

The deadline is the end of the feasibility period: the kept "Feasibility / due diligence ends" date on the site's
open PSA or option (crm/contracts), else the site's own milestone. Before it the team decides to proceed (the
deposit goes hard), extend, or terminate; that decision is kept on the contract.
"""
from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Max, Q
from django.utils import timezone

from ..models import Contract, ContractDate, DiligenceItem, Notification
from .assignments import CHECKLIST

# The items most land purchases need, from the full checklist (refs into assignments.CHECKLIST).
STANDARD = ["A-2", "A-3", "A-4", "A-5", "B-1", "B-2", "D-1", "D-4", "D-5", "D-6", "E-3", "F-2", "F-3", "G-3"]
LEADS = (7, 1, 0)  # reminder days ahead
SOON = 14  # feasibility this close with items still open: they're at risk

FOCUS = {  # what to pull out of a report, by checklist reference
    "A-2": "acreage, encroachments, easements and where they run, access, flood zone, setbacks, gaps or overlaps, and "
           "the Table A items covered",
    "A-3": "each Schedule B requirement and exception: what it is (easement, covenant, mineral reservation, lien), "
           "whether it limits building, and whether the survey or an endorsement can clear it",
    "A-4": "every easement and encumbrance, where it runs, who holds it, and what it rules out",
    "A-5": "existing leases (farm, hunting, cell, billboard): who, term, termination rights, notice needed",
    "B-1": "the zoning district, whether the planned use is permitted by right, by special exception or not at all, "
           "nonconformities and violations, and the approvals needed",
    "D-1": "recognized environmental conditions (RECs), controlled and historical RECs, de minimis conditions, data "
           "gaps, and whether a Phase II is recommended",
    "D-4": "jurisdictional wetlands and waters: acres, where they are, buffers, and the likely permit path (nationwide "
           "or individual Section 404)",
    "D-5": "listed species or habitat found or likely, and the survey windows or agency consultation needed",
    "D-6": "cultural resources found or likely, and whether SHPO consultation or a Phase I archaeological survey is needed",
    "E-3": "soils, bearing capacity, groundwater, rock, unsuitable fill, foundation recommendations and conditions that "
           "drive cost",
    "F-2": "capacity available, the timeline and cost to serve, and what ACMI must commit, by when",
    "F-3": "capacity committed, conditions, fees, extensions ACMI must build, and when the letter expires",
    "G-3": "access points, turn lanes or signals required, and who pays",
}
SUMMARY_SYSTEM = """You review due-diligence reports for ACMI Properties, which buys land for purpose-built \
manufacturing campuses for defense, aerospace and energetics companies. Summarize the report for the deal team, \
who must decide before the feasibility period ends whether to buy the land, renegotiate, or walk away.

- Lead with what could kill the deal, reprice it, or delay it.
- Then the findings and the report's recommendations, and what needs follow-up (by whom, if the report says).
- Cite the report for every finding, number and recommendation. If something the team would expect is missing \
from the report, say so.
- Brief and plain: "## " headings and "- " bullets. You are not giving legal, engineering or environmental advice."""


def start(site, which="standard", user=None, contract=None):
    """Add the checklist's items to a site: the standard set, or "full" for the whole client checklist. Items already on
    the site (same reference) are skipped. Returns how many were added."""
    have = set(site.diligence.exclude(ref="").values_list("ref", flat=True))
    pos = site.diligence.aggregate(m=Max("position"))["m"] or 0
    contract = contract or governing_contract(site)
    new = []
    for ref, block, item, how in CHECKLIST:
        if (which != "full" and ref not in STANDARD) or ref in have:
            continue
        pos += 1
        new.append(DiligenceItem(site=site, contract=contract, ref=ref, block=block, name=item, how=how, position=pos,
                                 updated_by=user))
    DiligenceItem.objects.bulk_create(new)
    return len(new)


def governing_contract(site):
    """The agreement the site's diligence runs against: its open PSA or option, signed ones first."""
    qs = site.contracts.filter(kind__in=[Contract.Kind.PSA, Contract.Kind.OPTION], status__in=Contract.OPEN_STATUSES)
    return qs.filter(status=Contract.Status.SIGNED).order_by("-effective_date", "-pk").first() or qs.first()


def feasibility_end(site, contract=None):
    """(date, where it comes from): the contract's kept feasibility date, else the site's milestone."""
    if contract is not None:
        d = (contract.dates.filter(kind=ContractDate.Kind.FEASIBILITY, confirmed=True, date__isnull=False)
             .order_by("-date").first())
        if d:
            return d.date, contract.title
    raw = (site.milestones or {}).get("feasibility_end")
    return (date.fromisoformat(raw), "the site's timeline") if raw else (None, "")


def _sum(items, field):
    return sum((getattr(i, field) or Decimal(0) for i in items), Decimal(0))


def summary(site, today=None, contract=None):
    """Everything the diligence page shows: items grouped by block, the deadline, progress, money, and what's at risk.
    Each item gets .late (open and past due), .after_deadline (due after feasibility ends) and .days_left."""
    today = today or timezone.localdate()
    items = list(site.diligence.select_related("owner", "contract"))
    contract = contract if contract is not None else governing_contract(site)
    end, end_from = feasibility_end(site, contract)
    days_left = (end - today).days if end else None
    for i in items:
        i.days_left = (i.due_on - today).days if i.due_on else None
        i.late = bool(i.is_open and i.due_on and i.due_on < today)
        i.after_deadline = bool(i.is_open and end and i.due_on and i.due_on > end)
    live = [i for i in items if i.status != DiligenceItem.Status.WAIVED]
    still_open = [i for i in items if i.is_open]
    crunch = days_left is not None and days_left <= SOON
    at_risk = sorted((i for i in still_open if i.late or i.after_deadline or crunch),
                     key=lambda i: (not i.late, not i.after_deadline, i.due_on or end or today))  # worst first
    groups = OrderedDict()
    for i in items:
        groups.setdefault(i.block, []).append(i)
    committed = [i for i in items if i.status != DiligenceItem.Status.TODO and i.cost is None]
    return {
        "items": items, "groups": list(groups.items()), "contract": contract, "end": end, "end_from": end_from,
        "days_left": days_left, "days_ago": -days_left if days_left is not None and days_left < 0 else None,
        "count": len(live), "cleared": sum(i.status == "clear" for i in items),
        "issues": [i for i in items if i.status == DiligenceItem.Status.ISSUE], "open": still_open, "at_risk": at_risk,
        "budget": _sum(live, "budget"), "invoiced": _sum(items, "cost"), "committed": _sum(committed, "budget"),
        "today": today,
    }


def _fmt(d):
    return f"{d:%b} {d.day}"


def campus_lines(site, today):
    """(facts, flags) for the campus summary: diligence progress, issues found, late items, a feasibility crunch."""
    if not site.diligence.exists():
        return [], []
    s = summary(site, today)
    issues = s["issues"]
    facts = [f"due diligence: {s['cleared']} of {s['count']} cleared"
             + (f", {len(issues)} issue{'s' if len(issues) != 1 else ''}" if issues else "")]
    flags = [f"DD issue: {i.name}" + (f" ({' '.join(i.findings.split())[:80]})" if i.findings else "") for i in issues]
    flags += [f"DD late: {i.name} (due {_fmt(i.due_on)})" for i in s["open"] if i.late]
    if s["open"] and s["days_left"] is not None and 0 <= s["days_left"] <= SOON:
        flags.append(f"feasibility ends in {s['days_left']} days with {len(s['open'])} DD item"
                     f"{'s' if len(s['open']) != 1 else ''} open")
    return facts, flags


def reminders(user, today=None):
    """Items someone chases (or, with nobody on them, the contract's owner): 7 and 1 days before they're due, on the
    day, and once when late. Only items not yet received; keyed by the due date. Called from notify.due_sweep."""
    from .. import notify

    today = today or timezone.localdate()
    mine = Q(owner=user) | Q(owner__isnull=True, contract__owner=user)
    qs = DiligenceItem.objects.filter(mine, status__in=["todo", "ordered"], due_on__isnull=False,
                                      due_on__lte=today + timedelta(days=LEADS[0])).select_related("site")
    for i in qs:
        left = (i.due_on - today).days
        if left < 0:
            bucket, when = "late", f"was due {_fmt(i.due_on)}"
        else:
            bucket = min(lead for lead in LEADS if lead >= left)
            when = "due today" if left == 0 else f"due in {left} day{'s' if left != 1 else ''} ({_fmt(i.due_on)})"
        notify.send(user, Notification.Kind.DEADLINE, f"Due diligence, {i.site.name}: {i.name} {when}",
                    f"/land/sites/{i.site_id}/diligence/#dd-{i.pk}", None, key=f"dd:{i.pk}:{i.due_on:%Y%m%d}:{bucket}")


def due(days=14, today=None):
    """Open items due within `days` (or late), for the dashboard's deadlines."""
    today = today or timezone.localdate()
    return list(DiligenceItem.objects.filter(status__in=["todo", "ordered"], due_on__isnull=False,
                                             due_on__lte=today + timedelta(days=days))
                .select_related("site", "owner").order_by("due_on", "pk"))


# ---------------------------------------------------------------------------
# Claude: a cited summary of the uploaded report
# ---------------------------------------------------------------------------


def summarize(item, user=None):
    """Have Claude read the item's report and keep a cited summary on it. Raises contracts.ai.Unavailable."""
    import base64

    from ..contracts import ai

    if item.text_status == "ok" and item.text:
        source = {"type": "text", "media_type": "text/plain", "data": item.text}
    elif item.text_status == "scan" and item.file and item.file.name.lower().endswith(".pdf"):
        if item.file.size > ai.MAX_PDF_BYTES:
            raise ai.Unavailable("That scan is over 25 MB; Claude can't read it in one go.")
        with item.file.open("rb") as fh:
            source = {"type": "base64", "media_type": "application/pdf",
                      "data": base64.standard_b64encode(fh.read()).decode()}
    else:
        raise ai.Unavailable("Upload the report (PDF or Word) first.")
    if len(item.text) > ai.MAX_CHARS:
        raise ai.Unavailable("That report is too long to read at once.")
    focus = FOCUS.get(item.ref, "the main findings, recommendations, costs and deadlines, and anything that affects "
                                "buying or building on the land")
    context = f"{item.block}: {item.name}" + (f" by {item.consultant}" if item.consultant else "")
    prompt = (f"SITE: {item.site.name}" + (f" ({item.site.campus.code} campus)" if item.site.campus_id else "")
              + f"\nREPORT: {item.name}\nPull out {focus}.")
    content = [{"type": "document", "source": source, "title": item.name, "context": context,
                "citations": {"enabled": True}}, {"type": "text", "text": prompt}]
    message = ai._stream(SUMMARY_SYSTEM, content, 12000, {"effort": ai.EFFORT})
    if message.stop_reason == "max_tokens":
        raise ai.Unavailable("The summary ran too long and was cut off. Try again.")
    item.summary = ai.blocks(message, [item])
    item.summary_model, item.summary_at, item.updated_by = message.model or ai.MODEL, timezone.now(), user
    item.save(update_fields=["summary", "summary_model", "summary_at", "updated_by", "updated_at"])
    return item
