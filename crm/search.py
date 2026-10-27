"""Search from any page: the top bar box and Ctrl+K open one palette that finds records and pages.

Companies we're actually talking to come first (an open deal, then the most recent touch), so typing a few
letters of a tenant's name finds it without knowing which list it lives on.

People and organizations nobody has filed yet are found too, labelled "Not filed yet": senders waiting in Needs
filing and meeting attendees no record knows. So are the contacts kept on directory firms.
"""
from django.db.models import Exists, OuterRef, Q
from django.http import JsonResponse
from django.urls import reverse
from django.utils.timezone import localtime

from .models import (
    OPEN_STAGES, Campus, Card, Company, Contract, Firm, LandSite, Meeting, Opportunity, Person, Project, UnmatchedEmail,
)

PER_GROUP = 6

# Pages people jump to: (label, url name, query, extra words to match, section).
PAGES = [
    ("Dashboard", "crm:home", "", "home start", "Home"),
    ("My tasks", "crm:task_list", "", "to do todo", "Home"),
    ("Calendar", "crm:meeting_list", "", "meetings month granola", "Home"),
    ("Notifications", "crm:notification_list", "", "alerts bell", "Home"),
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
    ("Contracts", "crm:contract_list", "", "psa loi lease critical dates", "Campuses"),
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
    ("Contract", "crm:contract_create", "Upload or start one"),
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


def results(text):
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
    add("Contracts", [
        _hit(c.title, c.get_absolute_url(), c.counterparty, c.get_status_display())
        for c in Contract.objects.filter(Q(title__icontains=text) | Q(counterparty__icontains=text))[:PER_GROUP]
    ])
    add("Directory", [
        _hit(f.name, f.get_absolute_url(), f.get_kind_display())
        for f in Firm.objects.filter(Q(name__icontains=text) | Q(domains__icontains=text))[:PER_GROUP]
    ])
    contacts = []
    low = text.lower()
    for f in Firm.objects.filter(contacts__icontains=text)[:PER_GROUP]:
        for c in f.contacts or []:
            if low in (c.get("name") or "").lower() or low in (c.get("email") or "").lower():
                contacts.append(_hit(c.get("name") or c.get("email"), f.get_absolute_url() + "#contacts",
                                     " · ".join(x for x in (c.get("title"), f.name) if x), "Directory contact"))
    add("Directory contacts", contacts[:PER_GROUP])
    add("Not filed yet", unfiled(text))
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
    return JsonResponse({"groups": results(request.GET.get("q", ""))})
