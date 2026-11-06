"""Record cards: a company, a deal or a person at a glance, opened over whatever page you were on.

nav.js opens one when a company, deal or person link is clicked plainly (modifier clicks and links in headers,
breadcrumbs and the record nav still go to the page); it fetches <record url>card/ and shows the fragment in the
#peek dialog, with "Open page" for the full record. Links inside a card open the next card, with Back.
Kellen, 2026-10-27: "Should it not be their card? ... It should be much more fluid and easy to navigate."
"""
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from . import company_page
from .contracts import filing as legal_filing
from .models import AISummary, Activity, Company, Opportunity, OpportunityStage, Person
from .pipeline import risk_reasons
from .views import recent_activity, task_rows

RECENT = 6
LEGAL_ROWS = 5


def _page(request, record):
    """The record's own page, keeping the list position (?from=...) the clicked link carried."""
    qs = request.META.get("QUERY_STRING", "")
    return record.get_absolute_url() + (f"?{qs}" if qs else "")


def company_card(request, pk):
    company = get_object_or_404(Company.objects.select_related("owner"), pk=pk)
    deals = list(company.opportunities.select_related("campus").order_by("stage"))
    open_deals = [o for o in deals if o.is_open]
    now = timezone.now()
    meetings = company.meetings.order_by("start")
    legal = legal_filing.company_agreements(company)
    return render(request, "crm/cards/company.html", {
        "company": company, "page": _page(request, company),
        # Legal: its lease and other agreements, a click from the card; the page's Legal tab files a new one.
        "legal": legal[:LEGAL_ROWS], "legal_count": len(legal), "legal_url": company.get_absolute_url() + "?tab=legal",
        "legal_maybe": len(legal_filing.company_suggestions(company)),
        "people": list(company.people.order_by("-last_interaction_at", "full_name")[:6]),
        "people_count": company.people.count(),
        "open_deals": open_deals, "pipeline": sum(o.value or 0 for o in open_deals),
        "next_meeting": meetings.filter(start__gt=now).first(),
        "last_meeting": meetings.filter(start__lte=now).last(),
        "recent": recent_activity(Activity.objects.filter(company=company), limit=RECENT, include_sent=True),
        "comments": company.comments.select_related("author")[:3],
        "tasks": task_rows(company.tasks.filter(done=False))[:5],
        "summary": AISummary.objects.filter(company=company).first(),
        "about": company_page.gist(company.description, 280),  # the description as one plain line
    })


def opportunity_card(request, pk):
    opp = get_object_or_404(Opportunity.objects.select_related("company", "campus", "owner"), pk=pk)
    return render(request, "crm/cards/deal.html", {
        "opp": opp, "page": _page(request, opp),
        "contacts": opp.contacts.select_related("person"),
        "agreements": legal_filing.deal_agreements(opp), "sites": opp.land_sites.all(),
        "tasks": task_rows(opp.tasks.filter(done=False))[:5],
        "recent": recent_activity(Activity.objects.filter(Q(opportunity=opp) | Q(company=opp.company)), limit=RECENT,
                                  include_sent=True),
        "comments": opp.comments.select_related("author")[:3],
        "risk": risk_reasons(opp) if opp.is_open else [],
        "days_in_stage": (timezone.now() - opp.stage_changed_at).days,
        "stages": OpportunityStage.choices,
    })


def person_card(request, pk):
    person = get_object_or_404(Person.objects.select_related("company", "owner"), pk=pk)
    return render(request, "crm/cards/person.html", {
        "person": person, "page": _page(request, person),
        "deals": Opportunity.objects.filter(contacts__person=person).select_related("campus").distinct(),
        "recent": recent_activity(Activity.objects.filter(person=person), limit=RECENT, include_sent=True),
        "tasks": task_rows(person.tasks.filter(done=False))[:5],
    })
