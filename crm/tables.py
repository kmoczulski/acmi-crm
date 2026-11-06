"""Filtering, sorting and columns for the Companies and People tables.

A table's whole state lives in its query string, which is also what a
SavedView stores. That keeps views shareable as plain links.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count, F, Min, Q
from django.utils import timezone

from .models import OPEN_STAGES, Company, CompanyStatus, Opportunity, Person, Priority


@dataclass
class Column:
    key: str
    label: str
    sort: str = ""  # ORM ordering field; empty = not sortable
    default: bool = False


COMPANY_COLUMNS = [
    Column("name", "Company", "name", True),
    Column("status", "Stage", "status", True),
    Column("priority", "Priority", "priority", True),
    Column("sector", "Sector", "sector", True),
    Column("owner", "Owner", "owner__first_name", True),
    Column("people_count", "People", "people_count", True),
    Column("touch_count", "Touches", "touch_count", True),
    Column("last_interaction", "Last touch", "last_interaction_at", True),
    Column("last_reply", "Last reply", "last_reply_at", True),
    Column("last_response", "Last response", "last_response_at", False),
    Column("next_task", "Next task", "next_task_due", True),
    Column("flags", "Flags", "", False),
    Column("fit_score", "Fit", "fit_score", True),
    Column("connection", "Who knows them", "strongest_connection_user__first_name", True),
    Column("deal", "Best deal", "", False),
    Column("campus", "Campus", "campus_plan", False),
    Column("projects", "Projects", "", False),
    Column("hq_location", "HQ", "hq_location", False),
    Column("domains", "Domains", "", False),
]

PERSON_COLUMNS = [
    Column("name", "Name", "full_name", True),
    Column("email", "Email", "email", True),
    Column("blasts", "Email blasts", "", True),
    Column("title", "Title", "title", True),
    Column("company", "Company", "company__name", True),
    Column("priority", "Priority", "priority", True),
    Column("owner", "Owner", "owner__first_name", True),
    Column("touch_count", "Touches", "touch_count", True),
    Column("last_interaction", "Last touch", "last_interaction_at", True),
    Column("last_reply", "Last reply", "last_reply_at", True),
    Column("last_response", "Last response", "last_response_at", False),
    Column("flags", "Flags", "", False),
    Column("next_task", "Next task", "next_task_due", False),
]

TOUCH_FILTERS = {
    "": "Any",
    "never": "Never touched",
    "7": "Touched in last 7 days",
    "30": "Touched in last 30 days",
    "gt30": "Not touched in 30+ days",
    "gt90": "Not touched in 90+ days",
}

RESPONSE_FILTERS = [
    "No Reply", "Responded", "Not Interested", "Undeliverable", "Out of Office Reply", "Automated Reply",
]


class Table:
    columns = []
    default_sort = "name"

    def __init__(self, params, user):
        self.params = params
        self.user = user

    # Subclasses: base_queryset(), search(qs, text)

    def get(self, key, default=""):
        return self.params.get(key, default).strip()

    def getlist(self, key):
        return [v for v in self.params.getlist(key) if v]

    @property
    def visible_columns(self):
        keys = self.getlist("cols")
        if not keys:
            return [c for c in self.columns if c.default]
        by_key = {c.key: c for c in self.columns}
        return [by_key[k] for k in keys if k in by_key]

    @property
    def sort(self):
        value = self.get("sort", self.default_sort)
        allowed = {c.sort for c in self.columns if c.sort}
        return value if value.lstrip("-") in allowed else self.default_sort

    def filtered(self):
        qs = self.base_queryset()
        if text := self.get("q"):
            qs = self.search(qs, text)
        if priorities := self.getlist("priority"):
            qs = qs.filter(priority__in=priorities)
        owner = self.get("owner")
        if owner == "me":
            qs = qs.filter(owner=self.user)
        elif owner == "none":
            qs = qs.filter(owner__isnull=True)
        elif owner.isdigit():
            qs = qs.filter(owner_id=int(owner))
        flag = self.get("flag")
        if flag == "none":
            qs = qs.filter(flags=[])
        elif flag == "not_excluded":
            qs = qs.exclude(flags__icontains='"Exclude"')
        elif flag:
            qs = qs.filter(flags__icontains=f'"{flag}"')
        if response := self.get("response"):
            qs = qs.filter(last_response=response)
        qs = self.touch_filter(qs, self.get("touch"))
        return qs

    def touch_filter(self, qs, value):
        now = timezone.now()
        if value == "never":
            return qs.filter(last_interaction_at__isnull=True)
        if value.isdigit():
            return qs.filter(last_interaction_at__gte=now - timedelta(days=int(value)))
        if value.startswith("gt") and value[2:].isdigit():
            return qs.filter(last_interaction_at__lt=now - timedelta(days=int(value[2:])))
        return qs

    def ordered(self):
        sort = self.sort
        field = sort.lstrip("-")
        expr = F(field).desc(nulls_last=True) if sort.startswith("-") else F(field).asc(nulls_last=True)
        return self.filtered().order_by(expr, "pk")


ACTIVE_DAYS = 30
ACTIVE_STATUSES = [CompanyStatus.ENGAGED, CompanyStatus.MEETING, CompanyStatus.OPPORTUNITY, CompanyStatus.TENANT]


class CompanyTable(Table):
    columns = COMPANY_COLUMNS
    default_sort = "name"

    def base_queryset(self):
        return Company.objects.select_related("owner", "strongest_connection_user").prefetch_related("campus_interest", "opportunities").annotate(
            people_count=Count("people", distinct=True),
            next_task_due=Min("tasks__due_date", filter=Q(tasks__done=False)),
        )

    def search(self, qs, text):
        return qs.filter(Q(name__icontains=text) | Q(sector__icontains=text) | Q(domains__icontains=text))

    def filtered(self):
        qs = super().filtered()
        # Scope chips on Companies: "active" = in a deal, at a later outreach stage or touched in the last 30 days.
        scope = self.get("scope")
        in_deal = Q(pk__in=Opportunity.objects.filter(stage__in=OPEN_STAGES).values("company_id"))
        if scope == "deal":
            qs = qs.filter(in_deal)
        elif scope == "active":
            qs = qs.filter(
                in_deal | Q(status__in=ACTIVE_STATUSES)
                | Q(last_interaction_at__gte=timezone.now() - timedelta(days=ACTIVE_DAYS))
            )
        if statuses := self.getlist("status"):
            qs = qs.filter(status__in=statuses)
        if sector := self.get("sector"):
            qs = qs.filter(sector__icontains=sector)
        fit = self.get("fit")
        if fit == "none":
            qs = qs.filter(fit_score__isnull=True)
        elif fit.isdigit():
            qs = qs.filter(fit_score__gte=int(fit))
        project = self.get("project")
        if project.isdigit():
            qs = qs.filter(activities__projects=int(project)).distinct()
        campus = self.get("campus")
        if campus in ("own", "none"):
            qs = qs.filter(campus_plan=campus)
        elif campus == "unknown":
            qs = qs.filter(campus_plan="")
        elif campus:
            qs = qs.filter(campus_interest__code=campus)
        return qs


class PersonTable(Table):
    columns = PERSON_COLUMNS
    default_sort = "full_name"

    def base_queryset(self):
        return Person.objects.select_related("owner", "company").annotate(
            next_task_due=Min("tasks__due_date", filter=Q(tasks__done=False)),
        )

    def search(self, qs, text):
        return qs.filter(
            Q(full_name__icontains=text) | Q(email__icontains=text) | Q(title__icontains=text)
            | Q(company__name__icontains=text)
        )

    def filtered(self):
        qs = super().filtered()
        if company := self.get("company"):
            qs = qs.filter(company__name__icontains=company)
        # Email blasts: the same people the outreach queue skips for Exclude or a bad email.
        excluded = Q(flags__icontains='"Exclude"') | Q(company__flags__icontains='"Exclude"')
        blasts = self.get("blasts")
        if blasts == "excluded":
            qs = qs.filter(excluded)
        elif blasts == "bad":
            qs = qs.filter(email_valid=False)
        elif blasts == "in":
            qs = qs.exclude(excluded).exclude(email_valid=False).exclude(email__isnull=True).exclude(email="")
        return qs


FILTER_CHOICES = {
    "priorities": Priority.choices,
    "statuses": CompanyStatus.choices,
    "touch": TOUCH_FILTERS.items(),
    "responses": RESPONSE_FILTERS,
}
