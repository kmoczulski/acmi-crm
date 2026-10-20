"""Who gets which outreach email (replaces build_eligible_queue in send_emails_v4.py).

Rules come from OutreachSettings; history comes from the CRM's computed fields,
which include personal Gmail as well as past campaign sends.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from crm.choices import Group
from crm.models import (
    CompanyStatus, EmailTemplate, NewsDraft, NewsItem, OutreachSettings, Person, Priority,
)
from crm.names import needs_name

LATE_STAGES = Group("CompanyStatus", [CompanyStatus.OPPORTUNITY, CompanyStatus.TENANT, CompanyStatus.NOT_INTERESTED,
                                      CompanyStatus.DO_NOT_CONTACT])
SKIP_LABELS = {
    "no_email": "No usable email",
    "bounced": "Email bounced",
    "excluded": "Flagged Exclude",
    "late_stage": "Company at a late stage (opportunity, tenant, not interested)",
    "news": "Getting news outreach instead",
    "priority": "Priority not included",
    "owner": "Different owner",
    "no_name": "No known name",
    "cooldown": "Emailed within the cooldown",
    "max_touches": "Hit the max touches",
    "replied": "Replied recently",
    "responded": "Last response: Responded",
    "not_interested": "Last response: Not interested",
    "undeliverable": "Last response: Undeliverable",
    "no_template": "No template for their priority/sector",
}


@dataclass
class QueueEntry:
    person: Person
    template: EmailTemplate
    greeting: str


def match_template(templates, priority, sector):
    """Same order as send_emails_v4: exact priority + sector, then priority + any sector, then any + any."""
    sector = (sector or "").lower()

    def any_priority(t):
        return not t.priority

    def any_sector(t):
        return t.sector_match.strip() in ("", "*")

    for t in templates:
        s = t.sector_match.strip().lower()
        if t.priority == priority and not any_sector(t) and sector and (s in sector or sector in s):
            return t
    for t in templates:
        if t.priority == priority and any_sector(t):
            return t
    for t in templates:
        if any_priority(t) and any_sector(t):
            return t
    return None


def greeting_for(person):
    if needs_name(person):
        return "To whom it may concern"  # templates supply the comma: "{greeting},"
    return (person.first_name or person.full_name).split()[0]


def _news_companies(days=30):
    since = timezone.now() - timedelta(days=days)
    ids = set(NewsDraft.objects.filter(article_date__gte=since.date()).exclude(company=None)
              .values_list("company_id", flat=True))
    ids |= set(NewsItem.objects.filter(decision=NewsItem.Decision.PROCEED, scan__imported_at__gte=since)
               .exclude(company=None).values_list("company_id", flat=True))
    return ids


def build(settings=None, limit=None):
    """(queue, skipped Counter). Ordered by priority (High first), then longest since last touch."""
    settings = settings or OutreachSettings.load()
    templates = list(EmailTemplate.objects.filter(active=True, campaign=settings.current_campaign)
                     .order_by("template_id"))
    included = {Priority.HIGH: settings.include_high, Priority.MID: settings.include_mid,
                Priority.LOW: settings.include_low, Priority.TEST: settings.include_test}
    now = timezone.now()
    news = _news_companies() if settings.prioritize_news else set()
    skipped = Counter()
    queue = []
    people = (Person.objects.exclude(email=None).exclude(email="").select_related("company", "owner")
              .order_by("pk"))
    for p in people.iterator(chunk_size=2000):
        company = p.company
        priority = p.priority or (company.priority if company else "")
        if "@" not in p.email or "." not in p.email.split("@")[-1]:
            skipped["no_email"] += 1
        elif p.email_valid is False:
            skipped["bounced"] += 1
        elif p.is_excluded or (company and company.is_excluded):
            skipped["excluded"] += 1
        elif settings.skip_late_stages and company and company.status in LATE_STAGES:
            skipped["late_stage"] += 1
        elif company and company.pk in news:
            skipped["news"] += 1
        elif priority in included and not included[priority]:
            skipped["priority"] += 1
        elif settings.owner_filter_id and (p.owner_id or (company.owner_id if company else None)) != settings.owner_filter_id:
            skipped["owner"] += 1
        elif settings.exclude_tbd_names and needs_name(p):
            skipped["no_name"] += 1
        elif p.last_interaction_at and p.last_interaction_at >= now - timedelta(days=settings.cooldown_days):
            skipped["cooldown"] += 1
        elif p.touch_count >= settings.max_touches:
            skipped["max_touches"] += 1
        elif (settings.skip_recent_replies_days and p.last_reply_at
              and p.last_reply_at >= now - timedelta(days=settings.skip_recent_replies_days)):
            skipped["replied"] += 1
        elif settings.exclude_responded and p.last_response == "Responded":
            skipped["responded"] += 1
        elif settings.exclude_not_interested and p.last_response == "Not Interested":
            skipped["not_interested"] += 1
        elif settings.exclude_undeliverable and p.last_response == "Undeliverable":
            skipped["undeliverable"] += 1
        else:
            template = match_template(templates, priority, (company.sector if company else ""))
            if template is None:
                skipped["no_template"] += 1
            else:
                queue.append(QueueEntry(person=p, template=template, greeting=greeting_for(p)))
    rank = {Priority.HIGH: 0, Priority.MID: 1, Priority.LOW: 2}
    queue.sort(key=lambda e: (rank.get(e.person.priority or (e.person.company.priority if e.person.company else ""), 3),
                              e.person.last_interaction_at or timezone.make_aware(datetime(2000, 1, 1))))
    return (queue[:limit] if limit else queue), skipped


def sent_today(user):
    """Outreach emails this user's batches sent since midnight (for the daily cap)."""
    from crm.models import SendItem

    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return SendItem.objects.filter(batch__created_by=user, status=SendItem.Status.SENT, sent_at__gte=start).count()
