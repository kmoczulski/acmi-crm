"""Weekly outreach report (replaces send_weekly_report.py), built from the CRM.

Week = Monday to Sunday. It covers prospecting outreach only:
- campaign sends (workbook-era or from the CRM's send queue): emails with a campaign set, and
- news outreach: the news-review drafts that were approved and then sent from Gmail.
Companies that are already ACMI's (tenants, signed LOI / lease / DMA, a signed or closed contract) are left out,
and a response only counts when it came back on an outreach email's own thread.
"""
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from django.template.loader import render_to_string
from django.utils import timezone

from django.db.models import Q

from crm.choices import Group
from crm.models import Activity, Company, CompanyStatus, Contract, NewsItem, NewsScan, OpportunityStage, Priority

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


SIGNED_STAGES = Group("OpportunityStage", [OpportunityStage.LOI_SIGNED, OpportunityStage.LEASE_NEGOTIATION,
                                           OpportunityStage.SIGNED])


def already_ours():
    """Companies past prospecting: tenants, a signed LOI / lease / DMA, or a signed or closed contract."""
    return set(Company.objects.filter(
        Q(status__in=Group("CompanyStatus", [CompanyStatus.TENANT])) | Q(opportunities__stage__in=SIGNED_STAGES)
        | Q(contracts__status__in=Group("Contract.Status", [Contract.Status.SIGNED, Contract.Status.CLOSED]))
    ).values_list("pk", flat=True))


def _news_filter(since):
    """Sent emails that are approved news-review drafts: same company, the draft's subject."""
    q = Q(pk__in=[])
    for company_id, subject in (NewsItem.objects.filter(decision=NewsItem.Decision.PROCEED, company__isnull=False,
                                                        scan__imported_at__gte=since)
                                .exclude(draft_subject="").values_list("company_id", "draft_subject")):
        q |= Q(company_id=company_id, summary__icontains=subject.strip()[:200])
    return q


def outreach_sends(start, end):
    """(campaign and news outreach emails sent in [start, end), how many went to companies already ACMI's)."""
    sent = Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT, occurred_at__gte=start, occurred_at__lt=end)
    rows = list(sent.filter(~Q(campaign="") | _news_filter(start - timedelta(days=60)))
                .select_related("person", "company").distinct())
    ours = already_ours()
    kept = [a for a in rows if a.company_id not in ours]
    for a in kept:
        a.stream = "Campaign" if a.campaign else "News"
    return kept, len(rows) - len(kept)


def _thread_status(rows):
    """{person or company key: latest response outcome on the outreach emails' own threads (after the send)}."""
    threads = {a.thread_id: a for a in rows if a.thread_id}
    out = {}
    if threads:
        replies = (Activity.objects.filter(kind__in=Activity.RESPONSE_KINDS, thread_id__in=list(threads))
                   .order_by("occurred_at", "id"))
        for r in replies:
            send = threads[r.thread_id]
            if r.occurred_at >= send.occurred_at and r.outcome:
                out[send.person_id or ("c", send.company_id)] = r.outcome
    return out


def build(day=None):
    start, end = week_bounds(day)
    rows, left_out = outreach_sends(start, end)
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

    # Each person's status: the latest response on their outreach threads, else what the workbook recorded on the
    # send itself, else No Reply. Replies about other business (a tenant's building, a signed deal) don't count.
    on_thread = _thread_status(rows)
    status_of, status_tier = {}, {}
    for key, acts in by_person.items():
        status = on_thread.get(key) or next((a.outcome for a in acts if a.outcome and a.outcome != "No Reply"
                                             and a.source == Activity.Source.IMPORT), "No Reply")
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

    # Positive replies this week on any outreach email's thread (the email may have gone out in an earlier week).
    outreach_threads = (Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT).exclude(thread_id="")
                        .filter(~Q(campaign="") | _news_filter(start - timedelta(days=120))).values("thread_id"))
    replies = (Activity.objects.filter(kind=Activity.Kind.REPLY, occurred_at__gte=start, occurred_at__lt=end,
                                       outcome__in=POSITIVE, thread_id__in=outreach_threads)
               .exclude(company_id__in=already_ours())
               .select_related("person", "company").prefetch_related("projects").order_by("-occurred_at"))
    campaigns = Counter(a.campaign for a in rows if a.campaign)
    return {
        "start": start, "end": end - timedelta(days=1),
        "campaigns": [c for c, _ in campaigns.most_common()],
        "emails": len(rows), "news_emails": sum(1 for a in rows if a.stream == "News"), "left_out": left_out,
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
