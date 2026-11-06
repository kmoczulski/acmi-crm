"""Filing legal documents: each agreement is filed to a tenant, a deal and a project, so it can be found from the
company card, the project page and the Legal list.

Kellen, 2026-11-06: "I just executed a lease and it wasn't filed to the tenant or project. I want to be able to do
that so I can navigate from a company card quickly." Dropping a file used to start an agreement with no tenant and no
project, and the form had no tenant field, so the company page, the tenant overview PDF and the rent roll never saw it.

The places an agreement can be filed:
  company      the tenant (or other company) it is with
  opportunity  the tenant deal it belongs to; it implies the company
  project      a campus or a site's project (Project); it implies the campus
  site         a land site; it implies the campus and the site's project
  campus       a campus (MEIH, NSIH)
"""
import re

from django.db.models import Q

from ..models import Campus, Company, Contract, LandSite, Opportunity, OpportunityStage, Project

FIELDS = ("company", "opportunity", "project", "site", "campus")
# A lease or PDA is with a tenant: without a company it is not filed, whatever project it sits in.
TENANT_KINDS = (Contract.Kind.LEASE, Contract.Kind.PDA)
# What is filed to a company by its counterparty's name once Claude has read it (an NDA or LOI may name a company too).
MATCH_KINDS = (*TENANT_KINDS, Contract.Kind.NDA, Contract.Kind.LOI)
# An NDA is often signed before there is a project, so it is never "not filed" for lack of one.
NO_PROJECT_KINDS = (Contract.Kind.NDA,)
# The order a company's page lists them in: its lease first.
KIND_ORDER = ("lease", "pda", "loi", "psa", "option", "nda", "jv", "loan", "easement", "services", "other")
NO_PROJECT = "No project"

LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "corp", "corporation", "co", "company", "ltd", "limited", "lp", "llp", "plc", "pllc",
}


def project_label(c):
    """The project a contract belongs to, the way the rest of the hub names one (Kellen, 2026-11-05: "it should also be
    sorted by project"): the project it was filed to, else the land site's project (ARA Facility), else the site itself,
    else the campus (MEIH), else the campus or site of its deal, else "No project"."""
    if c.project is not None:
        return c.project.name
    site = c.site
    if site is not None:
        if getattr(site, "project", None) is not None:
            return site.project.name
        return site.name
    if c.campus is not None:
        return c.campus.code
    deal = c.opportunity
    if deal is not None:
        if deal.campus is not None:
            return deal.campus.code
        if deal.site:
            return deal.site
    return NO_PROJECT


def places(c):
    """Where an agreement is filed, for the chips: tenant, deal, project (and where it links), site, campus."""
    deal = c.opportunity
    tenant = c.company or (deal.company if deal is not None else None)
    project = c.project or (c.site.project if c.site is not None and c.site.project_id else None)
    campus = c.campus or (deal.campus if deal is not None else None)
    label = project_label(c)
    if project is not None:
        link = project.get_absolute_url()
    elif c.site is not None:
        link = c.site.get_absolute_url()
    elif campus is not None:
        from django.urls import reverse

        link = reverse("crm:campus_summary", args=[campus.code])
    else:
        link = ""
    return {"tenant": tenant, "deal": deal, "project": project, "site": c.site, "campus": campus,
            "project_name": "" if label == NO_PROJECT else label, "project_url": link}


def missing(c):
    """What an agreement still needs to count as filed: [] when it is, else "tenant" and / or "project"."""
    out = []
    if c.kind in TENANT_KINDS and not (c.company_id or c.opportunity_id):
        out.append("tenant")
    if c.kind not in NO_PROJECT_KINDS and project_label(c) == NO_PROJECT:
        out.append("project")
    return out


def summary(c):
    """One line for what was just filed: "Acme Aerospace · MEIH", or "" when it is filed nowhere."""
    p = places(c)
    return " · ".join(x for x in (p["tenant"].name if p["tenant"] else "", p["project_name"]) if x)


def file_to(contract, *, company=None, opportunity=None, project=None, site=None, campus=None, clear=()):
    """Put an agreement where it belongs. What is picked wins; what a pick implies (a deal's company and campus, a
    site's campus and project, a project's campus) only fills what is blank. `clear` names places to take it off
    ("company" also takes off the deal, which belongs to a company). Returns the field names that changed."""
    changed = set()

    def put(field, value):
        if getattr(contract, field) != value:
            setattr(contract, field, value)
            changed.add(field)

    for name in clear:
        if name == "company":
            put("company", None)
            put("opportunity", None)
        elif name in FIELDS:
            put(name, None)
    if opportunity is not None:
        put("opportunity", opportunity)
        put("company", opportunity.company)
        if campus is None and not contract.campus_id and opportunity.campus_id:
            put("campus", opportunity.campus)
    elif company is not None:
        if contract.opportunity_id and contract.opportunity.company_id != company.pk:
            put("opportunity", None)  # a deal with another company is no longer this agreement's deal
        put("company", company)
    if site is not None:
        put("site", site)
        if campus is None and not contract.campus_id and site.campus_id:
            put("campus", site.campus)
        if project is None and not contract.project_id and site.project_id:
            put("project", site.project)
    if project is not None:
        put("project", project)
        home = project.campus or (project.parent.campus if project.parent_id else None)
        if campus is None and not contract.campus_id and home is not None:
            put("campus", home)
    if campus is not None:
        put("campus", campus)
    if changed:
        contract.save(update_fields=[*changed, "updated_at"])
    return sorted(changed)


# ---------------------------------------------------------------------------
# Matching a counterparty to a company
# ---------------------------------------------------------------------------


def words(name):
    """A company name as comparable words: lower case, no punctuation, "&" as "and", no "The" in front and no Inc /
    LLC / Corp at the end ("Acme Aerospace, Inc." and "ACME Aerospace" are the same)."""
    out = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and ")).split()
    while out and out[-1] in LEGAL_SUFFIXES:
        out.pop()
    if out and out[0] == "the":
        out.pop(0)
    return out


def match_company(name):
    """The one company whose name is `name` once legal suffixes are ignored; None when none or several are."""
    wanted = words(name)
    if not wanted:
        return None
    candidates = Company.objects.filter(name_key__contains=wanted[0][:12])[:2000]
    found = [c for c in candidates if words(c.name) == wanted]
    return found[0] if len(found) == 1 else None


def _single_deal(company, campus=None):
    deals = company.opportunities.exclude(stage=OpportunityStage.LOST).select_related("campus", "company")
    if campus is not None:
        deals = deals.filter(campus=campus)
    deals = list(deals[:2])
    return deals[0] if len(deals) == 1 else None


def _campus_project(campus):
    """The campus's own project, when there is exactly one."""
    found = list(Project.objects.filter(campus=campus, parent__isnull=True, active=True)[:2])
    return found[0] if len(found) == 1 else None


def auto_file(contract):
    """File what the document itself says, after Claude has read it: a tenant agreement goes to the company whose name
    is its counterparty (one exact match only), then to that company's only deal (at the campus, when it names one),
    and a campus with one project of its own files there. Returns what was filled; nothing already filed is moved."""
    changed = []
    if (not contract.company_id and not contract.opportunity_id and contract.kind in MATCH_KINDS
            and contract.counterparty.strip()):
        company = match_company(contract.counterparty)
        if company is not None:
            changed += file_to(contract, company=company)
    if contract.company_id and not contract.opportunity_id:
        deal = _single_deal(contract.company, contract.campus)
        if deal is not None:
            changed += file_to(contract, opportunity=deal)
    if contract.campus_id and not contract.project_id and not contract.site_id:
        project = _campus_project(contract.campus)
        if project is not None:
            changed += file_to(contract, project=project)
    return sorted(set(changed))


# ---------------------------------------------------------------------------
# What the pages list
# ---------------------------------------------------------------------------


def _decorate(contracts, today=None):
    from . import dates

    nxt = dates.next_dates(contracts, today)
    for c in contracts:
        c.next = nxt.get(c.pk)
        c.filed = places(c)
        c.missing = missing(c)
    return contracts


def _order(c):
    kind = KIND_ORDER.index(c.kind) if c.kind in KIND_ORDER else len(KIND_ORDER)
    return (not c.is_open, kind, -(c.effective_date or c.created_at.date()).toordinal())


_RELATED = ("campus", "project", "site__project", "company", "owner", "opportunity__company", "opportunity__campus",
            "lease_terms")


def company_q(company):
    """The agreements filed to a company, directly or through one of its deals."""
    return Q(company=company) | Q(opportunity__company=company)


def project_q(project):
    """The agreements filed to a project: directly, through one of its land sites, and, for a campus-level project, its
    child projects' and the campus's own."""
    q = Q(project=project) | Q(site__project=project)
    if project.parent_id is None:
        q |= Q(project__parent=project) | Q(site__project__parent=project)
        if project.campus_id:
            q |= Q(campus_id=project.campus_id)
    return q


def company_agreements(company, today=None):
    """Every agreement filed to a company, directly or through one of its deals: open ones first, its lease first."""
    qs = Contract.objects.filter(company_q(company)).select_related(*_RELATED)
    return _decorate(sorted(qs.distinct(), key=_order), today)


def company_suggestions(company):
    """Agreements filed to no company whose counterparty reads like this company's name: the lease that was dropped
    on the Legal page and never filed. A suggestion, filed with one click, never automatic."""
    wanted = words(company.name)
    if not wanted:
        return []
    qs = (Contract.objects.filter(company__isnull=True, opportunity__isnull=True, counterparty__icontains=wanted[0])
          .select_related(*_RELATED))
    out = []
    for c in qs:
        cp = words(c.counterparty)
        if cp == wanted or (len(" ".join(wanted)) >= 4 and _contains(cp, wanted)):
            out.append(c)
    return _decorate(sorted(out, key=_order))


def _contains(haystack, needle):
    n = len(needle)
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def project_agreements(project, today=None):
    """Every agreement filed to a project (see project_q)."""
    qs = Contract.objects.filter(project_q(project)).select_related(*_RELATED)
    return _decorate(sorted(qs.distinct(), key=_order), today)


def deal_agreements(deal, today=None):
    qs = Contract.objects.filter(opportunity=deal).select_related(*_RELATED)
    return _decorate(sorted(qs, key=_order), today)


def options(text, limit=5):
    """What the File to picker offers for a search: [(kind, pk, name, detail)]. With no search it offers the projects
    and campuses, which are few enough to pick from."""
    text = (text or "").strip()
    out = []
    if text:
        if len(text) < 2:
            return []
        for c in Company.objects.filter(name__icontains=text).order_by("name")[:limit]:
            out.append(("company", c.pk, c.name, "Tenant / company"))
        deals = Opportunity.objects.filter(Q(name__icontains=text) | Q(company__name__icontains=text))
        for o in deals.select_related("company", "campus").order_by("company__name", "name")[:limit]:
            out.append(("opportunity", o.pk, f"{o.company.name} · {o.name}",
                        "Deal" + (f" · {o.campus.code}" if o.campus_id else "")))
    projects = Project.objects.filter(active=True)
    campuses = Campus.objects.all()
    sites = LandSite.objects.none()
    if text:
        projects = projects.filter(name__icontains=text)
        campuses = campuses.filter(Q(code__icontains=text) | Q(name__icontains=text))
        sites = LandSite.objects.filter(name__icontains=text).select_related("campus")
    for p in projects.order_by("name")[: limit if text else 14]:
        out.append(("project", p.pk, p.name, "Project"))
    for s in sites.order_by("name")[:limit]:
        out.append(("site", s.pk, s.name, "Land site" + (f" · {s.campus.code}" if s.campus_id else "")))
    for c in campuses.order_by("code")[: limit if text else 14]:
        out.append(("campus", c.pk, f"{c.code} · {c.name}", "Campus"))
    return out


def target(kind, pk):
    """The record a picker choice names, or None."""
    model = {"company": Company, "opportunity": Opportunity, "project": Project, "site": LandSite,
             "campus": Campus}.get(kind)
    if model is None or not str(pk).isdigit():
        return None
    return model.objects.filter(pk=pk).first()


def picks(data):
    """The records a drop or the picker named, from posted ids: {"company": Company, ...}; unknown ids are ignored."""
    out = {}
    for field in FIELDS:
        record = target(field, (data.get(field) or "").strip())
        if record is not None:
            out[field] = record
    return out
