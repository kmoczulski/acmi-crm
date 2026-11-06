"""Search from any page: the top bar box and Ctrl+K open one palette that finds records and pages.

Companies we're actually talking to come first (an open deal, then the most recent touch), so typing a few
letters of a tenant's name finds it without knowing which list it lives on.

People and organizations nobody has filed yet are found too, labelled "Not filed yet": senders waiting in Needs
filing and meeting attendees no record knows. So are the contacts kept on directory firms.

Emails come up the way meetings do (Kellen, 2026-11-06: "if I search a company their emails should pop up like their
meetings"): the mail logged on the companies and people the search found, mail whose subject has the words, and mail
filed on the directory firms it found.
"""
from urllib.parse import quote

from django.db.models import Exists, OuterRef, Q
from django.http import JsonResponse
from django.urls import reverse
from django.utils.timezone import localtime

from .models import (
    OPEN_STAGES, Activity, Campus, Card, Company, Contract, Firm, FirmEmail, LandSite, Meeting, Opportunity, Person,
    Project, UnmatchedEmail,
)

PER_GROUP = 6
EMAIL_HITS = 5
EMAIL_KINDS = (Activity.Kind.EMAIL_SENT, Activity.Kind.EMAIL_RECEIVED, Activity.Kind.REPLY)
EMAIL_BADGES = {"email_sent": "Sent", "email_received": "Received", "reply": "Reply"}

# Pages people jump to: (label, url name, query, extra words to match, section).
PAGES = [
    ("Dashboard", "crm:home", "", "home start", "Home"),
    ("My tasks", "crm:task_list", "", "to do todo", "Home"),
    ("Calendar", "crm:meeting_list", "", "meetings month granola", "Home"),
    ("Notifications", "crm:notification_list", "", "alerts bell", "Home"),
    ("Chat", "crm:chat", "", "google chat messages direct spaces", "Home"),
    ("Boards", "crm:boards", "", "trello cards lists kanban", "Home"),
    ("Tenant pipeline", "crm:opportunity_board", "", "deals opportunities board investor", "Tenants"),
    ("Tenant pipeline report", "crm:pipeline_report", "", "investor report pdf excel", "Tenants"),
    ("Companies", "crm:company_list", "", "tenants prospects accounts", "Tenants"),
    ("People", "crm:person_list", "", "contacts", "Tenants"),
    ("Rent roll", "crm:rent_roll", "", "leases", "Tenants"),
    ("Sites", "crm:land_sites_page", "", "land", "Land"),
    ("Map", "crm:land_search", "", "land search parcels", "Land"),
    ("Client searches", "crm:assignments", "", "assignments due diligence", "Land"),
    ("Entitlements", "crm:approval_list", "", "approvals zoning permits", "Land"),
    ("Campus summary", "crm:campus_index", "", "campus updates", "Campuses"),
    ("Projects", "crm:project_list", "", "campuses", "Campuses"),
    ("Legal", "crm:contract_list", "", "contracts leases psa loi nda critical dates documents", "Campuses"),
    ("Capital", "crm:capital_index", "", "financing debt equity stack", "Campuses"),
    ("Directory", "crm:directory", "", "lenders investors vendors consultants firms", "Directory"),
    ("Lists", "crm:list_index", "", "", "Directory"),
    ("Possible duplicates", "crm:duplicates", "", "merge", "Directory"),
    ("Import from a spreadsheet", "crm:sheet_import", "", "excel xlsx csv upload contacts companies", "Directory"),
    ("Send queue", "crm:outreach_home", "", "outreach email campaign", "Outreach"),
    ("Needs filing", "crm:needs_filing", "", "mail review inbox unmatched unknown senders", "Outreach"),
    ("News review", "crm:news_home", "", "news", "Outreach"),
    ("Email templates", "crm:template_list", "", "outreach", "Outreach"),
    ("Weekly report", "crm:weekly_report", "", "report", "Outreach"),
]

# The "+ New" menu: (label, url name, hint).
NEW_ITEMS = [
    ("Tenant deal", "crm:opportunity_create", "A company that may lease or buy"),
    ("Company", "crm:company_create", ""),
    ("Person", "crm:person_create", ""),
    ("Import from a spreadsheet", "crm:sheet_import", "Companies and contacts from Excel or CSV"),
    ("Task", "crm:task_list", "Opens My tasks with the new-task box"),
    ("Card", "crm:boards", "On a board of your choice"),
    ("Legal record", "crm:contract_create", "A lease, PSA, LOI or NDA: upload or start one"),
    ("Site", "crm:land_search", "Build one on the map"),
    ("Entitlement", "crm:approval_create", ""),
    ("Capital source", "crm:capital_source_create", ""),
]


def page_links():
    return [{"label": label, "url": reverse(name) + query, "words": words, "section": section}
            for label, name, query, words, section in PAGES]


def new_links():
    extra = {"Task": "#new-task", "Card": "?new=card"}
    return [{"label": label, "url": reverse(name) + extra.get(label, ""), "hint": hint}
            for label, name, hint in NEW_ITEMS]


def _hit(title, url, sub="", badge="", tone=""):
    return {"title": title, "url": url, "sub": sub, "badge": badge, "tone": tone}


def results(text, user=None):
    text = text.strip()
    if len(text) < 2:
        return []
    groups = []

    def add(label, items):
        if items:
            groups.append({"label": label, "items": items})

    open_deal = Opportunity.objects.filter(company=OuterRef("pk"), stage__in=OPEN_STAGES)
    companies = (
        Company.objects.filter(Q(name__icontains=text) | Q(domains__icontains=text))
        .annotate(in_deal=Exists(open_deal))
        .order_by("-in_deal", "-last_interaction_at", "name")[:PER_GROUP]
    )
    add("Companies", [
        _hit(c.name, c.get_absolute_url(), c.sector, "In a deal" if c.in_deal else c.get_status_display(),
             "good" if c.in_deal else "")
        for c in companies
    ])
    deals = (
        Opportunity.objects.filter(Q(name__icontains=text) | Q(company__name__icontains=text))
        .select_related("company", "campus").order_by("-stage_changed_at")[:PER_GROUP]
    )
    add("Tenant deals", [
        _hit(o.name, o.get_absolute_url(), o.campus.code if o.campus else o.site, o.get_stage_display(), f"stage-{o.stage}")
        for o in deals
    ])
    people = (
        Person.objects.filter(Q(full_name__icontains=text) | Q(email__icontains=text))
        .select_related("company").order_by("-last_interaction_at", "full_name")[:PER_GROUP]
    )
    add("People", [
        _hit(str(p), p.get_absolute_url(), " · ".join(x for x in (p.title, p.company.name if p.company else "") if x))
        for p in people
    ])
    add("Sites", [
        _hit(s.name, s.get_absolute_url(), s.campus.code if s.campus else "", s.get_status_display())
        for s in LandSite.objects.filter(name__icontains=text).select_related("campus")[:PER_GROUP]
    ])
    add("Projects and campuses", [
        _hit(p.name, p.get_absolute_url(), p.address) for p in Project.objects.filter(name__icontains=text)[:PER_GROUP]
    ] + [
        _hit(f"{c.code} · {c.name}", reverse("crm:campus_index") + f"#campus-{c.pk}", c.location)
        for c in Campus.objects.filter(Q(code__icontains=text) | Q(name__icontains=text))[:3]
    ])
    add("Legal", [
        _hit(c.title, c.get_absolute_url(), c.counterparty, c.get_status_display())
        for c in Contract.objects.filter(Q(title__icontains=text) | Q(counterparty__icontains=text)
                                         | Q(company__name__icontains=text)
                                         | Q(opportunity__company__name__icontains=text))[:PER_GROUP]
    ])
    firms = list(Firm.objects.filter(Q(name__icontains=text) | Q(domains__icontains=text))[:PER_GROUP])
    add("Directory", [_hit(f.name, f.get_absolute_url(), f.get_kind_display()) for f in firms])
    contacts = []
    low = text.lower()
    for f in Firm.objects.filter(contacts__icontains=text)[:PER_GROUP]:
        for c in f.contacts or []:
            if low in (c.get("name") or "").lower() or low in (c.get("email") or "").lower():
                contacts.append(_hit(c.get("name") or c.get("email"), f.get_absolute_url() + "#contacts",
                                     " · ".join(x for x in (c.get("title"), f.name) if x), "Directory contact"))
    add("Directory contacts", contacts[:PER_GROUP])
    add("Not filed yet", unfiled(text))
    add("Emails", email_hits(text, companies, people, firms, user))
    add("Meetings", [
        _hit(m.title, m.get_absolute_url(), f"{localtime(m.start):%b} {localtime(m.start).day}, {m.start.year}")
        for m in Meeting.objects.filter(title__icontains=text).order_by("-start")[:4]
    ])
    add("Cards", [
        _hit(c.title, c.get_absolute_url(), f"{c.list.board.name} › {c.list.name}", "Card")
        for c in Card.objects.filter(title__icontains=text, archived=False, list__archived=False, list__board__archived=False)
        .select_related("list__board").order_by("-updated_at")[:PER_GROUP]
    ])
    return groups


def _date(when):
    when = localtime(when)
    return f"{when:%b} {when.day}, {when.year}"


def _mail_on(user):
    """Mail in the hub is on for this person (their own mailbox or a shared one), as the top bar decides it."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    from .mail import connect, shared

    return connect.is_on(user) or bool(shared.shared_for(user))


def _mail_link(activity, user, mail_on):
    """Where an email hit opens: its conversation in Mail when it sits in this person's mailbox and Mail is on for them
    (the links the Follow-ups list uses), else where it is logged, the company's Activity tab or the person's page."""
    if mail_on and any(u.pk == user.pk for u in activity.team_members.all()):
        if activity.thread_id and str(activity.thread_id).isdigit():
            return f"{reverse('crm:inbox')}?label=ALL&t={int(activity.thread_id):x}"
        if activity.message_id:
            return f"{reverse('crm:inbox_open')}?m={quote(activity.message_id, safe='')}"
    if activity.company_id:
        return activity.company.get_absolute_url() + "?tab=activity"
    if activity.person_id:
        return activity.person.get_absolute_url()
    return reverse("crm:home")


def email_hits(text, companies, people, firms, user=None):
    """The Emails group, newest first and one row per conversation: mail logged on the companies and people the search
    found, mail whose subject has the words, and mail filed on the directory firms it found. Real conversation only,
    as the record pages show it (no calendar replies, bounces or out-of-office)."""
    from .views import recent_activity

    match = Q(summary__icontains=text)  # "Email from Ben Allen: Lease draft": the subject and the name
    if companies:
        match |= Q(company_id__in=[c.pk for c in companies])
    if people:
        match |= Q(person_id__in=[p.pk for p in people])
    rows = [(a.occurred_at, ("a", str(a.thread_id or a.message_id or a.pk)), a)
            for a in recent_activity(Activity.objects.filter(match, kind__in=EMAIL_KINDS), limit=EMAIL_HITS * 4,
                                     include_sent=True)]
    on_firm = Q(subject__icontains=text)
    if firms:
        on_firm |= Q(provider_id__in=[f.pk for f in firms])
    filed = FirmEmail.objects.filter(on_firm, kind__in=EMAIL_KINDS)
    rows += [(e.occurred_at, ("f", str(e.thread_id or e.message_id)), e)
             for e in filed.select_related("provider").order_by("-occurred_at")[:EMAIL_HITS * 4]]
    rows.sort(key=lambda r: r[0], reverse=True)
    hits, seen, mail_on = [], set(), None
    for when, key, row in rows:
        if key in seen:
            continue
        seen.add(key)
        if isinstance(row, FirmEmail):
            who = " · ".join(x for x in (row.provider.name, row.name or row.address, _date(when)) if x)
            hits.append(_hit((row.subject or "(no subject)").strip()[:140], row.provider.get_absolute_url(), who,
                             EMAIL_BADGES.get(row.kind, "")))
        else:
            if mail_on is None:
                mail_on = _mail_on(user)
            who = " · ".join(x for x in (row.company.name if row.company_id else "", str(row.person) if row.person_id else "",
                                         _date(when)) if x)
            hits.append(_hit((row.summary.split(": ", 1)[-1] or "(no subject)").strip()[:140],
                             _mail_link(row, user, mail_on), who, EMAIL_BADGES.get(row.kind, "")))
        if len(hits) == EMAIL_HITS:
            break
    return hits


def unfiled(text):
    """Senders waiting in Needs filing (one row per domain) and meeting attendees no record knows."""
    from .needs_filing import _domain

    hits, seen = [], set()
    rows = (UnmatchedEmail.objects.filter(Q(name__icontains=text) | Q(address__icontains=text) | Q(domain__icontains=text))
            .order_by("-occurred_at").values_list("domain", "name", "address")[:60])
    for domain, name, address in rows:
        if domain in seen:
            continue
        seen.add(domain)
        hits.append(_hit(name or address, reverse("crm:needs_filing") + f"?domain={domain}", f"{address} · emails waiting",
                         "Not filed yet", "unfiled-tone"))
    low = text.lower()
    for m in Meeting.objects.filter(unfiled__icontains=text).order_by("-start")[:PER_GROUP]:
        for a in m.unfiled:
            if (low in (a.get("name") or "").lower() or low in a["email"]) and a["email"] not in seen:
                seen.add(a["email"])
                when = localtime(m.start)
                hits.append(_hit(a.get("name") or a["email"], m.get_absolute_url(),
                                 f"{a['email']} · at {m.title[:40]}, {when:%b} {when.day}", "Not filed yet", "unfiled-tone"))
    return hits[:PER_GROUP]


def search(request):
    return JsonResponse({"groups": results(request.GET.get("q", ""), request.user)})
