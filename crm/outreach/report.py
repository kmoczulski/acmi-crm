"""Weekly outreach report (replaces send_weekly_report.py), built from the CRM.

Week = Monday to Sunday. "Outreach emails" are campaign sends (workbook-era or
from the CRM's send queue): emails with a campaign set.
"""
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from django.template.loader import render_to_string
from django.utils import timezone

from crm.models import Activity, Company, NewsScan, Priority

RESPONSE_ORDER = ["Responded", "Meeting Scheduled", "Referred", "Follow Up", "Not Interested",
                  "Out of Office Reply", "Automated Reply", "Undeliverable", "No Reply"]
POSITIVE = {"Responded", "Meeting Scheduled", "Referred", "Follow Up"}
TIERS = [Priority.HIGH, Priority.MID, Priority.LOW]


def week_bounds(day=None):
    """(start, end) datetimes for the Monday-Sunday week containing `day` (default: last full week)."""
    if day is None:
        day = timezone.localdate() - timedelta(days=7)
    monday = day - timedelta(days=day.weekday())
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(monday, time.min), tz)
    return start, start + timedelta(days=7)


def build(day=None):
    start, end = week_bounds(day)
    sends = (Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT, occurred_at__gte=start, occurred_at__lt=end)
             .exclude(campaign="").select_related("person", "company"))
    rows = list(sends)
    by_person = defaultdict(list)
    for a in rows:
        by_person[a.person_id or ("c", a.company_id)].append(a)

    def tier_of(a):
        return (a.person.priority if a.person and a.person.priority else
                (a.company.priority if a.company else "")) or "Other"

    emails_by_tier = Counter(tier_of(a) for a in rows)
    companies_by_tier = defaultdict(set)
    for a in rows:
        if a.company_id:
            companies_by_tier[tier_of(a)].add(a.company_id)

    # Each person's status: their latest response since their first send this week, else No Reply.
    status_of, status_tier = {}, {}
    for key, acts in by_person.items():
        first = min(a.occurred_at for a in acts)
        person = acts[0].person
        status = "No Reply"
        if person and person.last_response and person.last_response_at and person.last_response_at >= first:
            status = person.last_response
        status_of[key], status_tier[key] = status, tier_of(acts[0])
    summary = []
    for status in RESPONSE_ORDER:
        counts = {t: sum(1 for k, s in status_of.items() if s == status and status_tier[k] == t) for t in TIERS}
        total = sum(1 for s in status_of.values() if s == status)
        if total:
            summary.append({"status": status, "total": total, **{t.lower(): counts[t] for t in TIERS}})

    respondents = {}
    for key, status in status_of.items():
        if status in ("No Reply", "Out of Office Reply", "Automated Reply"):
            continue
        a = by_person[key][0]
        name = a.company.name if a.company else (a.person.email if a.person else "?")
        entry = respondents.setdefault((name, status), {"company": a.company, "name": name, "status": status,
                                                        "people": [], "tier": tier_of(a)})
        if a.person:
            entry["people"].append(a.person)

    company_rows = defaultdict(list)
    per_company = defaultdict(list)
    for a in rows:
        if a.company_id:
            per_company[a.company_id].append(a)
    companies = Company.objects.in_bulk(list(per_company))
    for cid, acts in per_company.items():
        statuses = Counter(status_of.get(a.person_id or ("c", cid), "No Reply") for a in acts)
        company_rows[tier_of(acts[0])].append({
            "company": companies[cid], "emails": len(acts), "people": len({a.person_id for a in acts}),
            "status": statuses.most_common(1)[0][0],
        })
    for tier in company_rows:
        company_rows[tier].sort(key=lambda r: (r["status"] == "No Reply", r["company"].name))

    replies = (Activity.objects.filter(kind=Activity.Kind.REPLY, occurred_at__gte=start, occurred_at__lt=end,
                                       outcome__in=POSITIVE)
               .select_related("person", "company").prefetch_related("projects").order_by("-occurred_at"))
    campaigns = Counter(a.campaign for a in rows)
    return {
        "start": start, "end": end - timedelta(days=1),
        "campaigns": [c for c, _ in campaigns.most_common()],
        "emails": len(rows),
        "companies": len({a.company_id for a in rows if a.company_id}),
        "tiers": [{"tier": t, "emails": emails_by_tier.get(t, 0), "companies": len(companies_by_tier.get(t, ()))}
                  for t in TIERS],
        "summary": summary,
        "response_rate": round(100 * sum(1 for s in status_of.values() if s in POSITIVE) / len(status_of), 1)
        if status_of else 0,
        "respondents": sorted(respondents.values(), key=lambda r: (r["status"] not in POSITIVE, r["name"])),
        "company_rows": [(t, company_rows.get(t, [])) for t in TIERS],
        "positive_replies": list(replies[:25]),
        "news_scans": NewsScan.objects.filter(applied_at__gte=start, applied_at__lt=end),
        "new_companies": Company.objects.filter(created_at__gte=start, created_at__lt=end).count(),
    }


def render_email(report, request=None):
    return render_to_string("crm/report_email.html", {"r": report}, request=request)


def send(report, user, recipients):
    """Email the report from the user's Gmail."""
    from crm.models import Mailbox, OutreachSettings

    from .sender import Smtp, SendError, build_message

    mailbox = Mailbox.objects.filter(user=user).first()
    if mailbox is None:
        raise SendError("Connect your mailbox on the Mail page first.")
    settings = OutreachSettings.load()
    subject = f"Weekly Outreach Report – {report['start']:%b %d} to {report['end']:%b %d, %Y}"
    html_body = render_email(report)
    plain = (f"Weekly outreach report {report['start']:%b %d}–{report['end']:%b %d}: {report['emails']} emails "
             f"to {report['companies']} companies; response rate {report['response_rate']}%.")
    msg = build_message(mailbox, ", ".join(recipients), subject, html_body, plain, [], settings)
    with Smtp(mailbox) as smtp:
        smtp.send(msg)
