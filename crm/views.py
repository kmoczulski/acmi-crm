import re
from urllib.parse import parse_qs, urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import ai, company_page, documents, gcal, meetings, merge, notify, pipeline
from . import projects as project_tagging
from .build import logic as build_logic
from .capital import logic as capital_logic
from .contracts import dates as contract_dates
from .entitlements import logic as entitlement_logic
from .tenants import fit as tenant_fit
from .forms import (
    CompanyForm, DocumentForm, ListForm, NoteForm, OpportunityContactForm, OpportunityForm, PersonForm, ProjectForm,
    TaskForm, active_users,
)
from .models import (
    DOCUMENT_CATEGORIES, OPEN_STAGES, STAGE_ORDER, AISummary, Activity, Attachment, Campus, Comment, Company, CompanyStatus, DealSuggestionDismissal, Document,
    List, ListEntry, Note, ObjectType, Opportunity, OpportunityContact, OpportunityStage, Person, Project, SavedView,
    CalendarSync, Meeting, Notification, Task, TenantNeeds,
)
from .permissions import manager_required
from .pipeline import risk_reasons
from .interactions import team_connections
from .mail.parse import new_text
from .tables import FILTER_CHOICES, CompanyTable, PersonTable

TIMELINE_PAGE = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_next(request, fallback):
    """The posted ?next= path if it stays on this site ('/\\evil.com' and '//evil.com' don't), else fallback."""
    nxt = request.POST.get("next") or request.GET.get("next")
    ok = nxt and nxt.startswith("/") and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return nxt if ok else fallback


def _record_from_post(request):
    """Resolve company/person/opportunity ids posted from a record page."""
    links = {}
    for field, model in (("company", Company), ("person", Person), ("opportunity", Opportunity)):
        value = request.POST.get(field)
        if value:
            links[field] = get_object_or_404(model, pk=value)
    if "person" in links and "company" not in links and links["person"].company_id:
        links["company"] = links["person"].company
    if "opportunity" in links and "company" not in links:
        links["company"] = links["opportunity"].company
    return links


def _query_dict(qs_string):
    from django.http import QueryDict

    return QueryDict(qs_string or "", mutable=False)


def _neighbours(table_cls, request, pk):
    """Prev/next record ids and position within the table the user came from."""
    origin = request.GET.get("from")
    if origin is None:
        return None
    table = table_cls(_query_dict(origin), request.user)
    ids = list(table.ordered().values_list("pk", flat=True))
    try:
        i = ids.index(pk)
    except ValueError:
        return None
    suffix = "?" + urlencode({"from": origin})
    return {
        "position": i + 1,
        "total": len(ids),
        "prev": ids[i - 1] if i > 0 else None,
        "next": ids[i + 1] if i + 1 < len(ids) else None,
        "suffix": suffix,
        "back": "?" + origin,
    }


def _timeline(qs, request):
    show_bg = request.GET.get("bg") == "1"
    now = timezone.now()
    if not show_bg:
        qs = qs.filter(is_background=False)
    qs = qs.select_related("actor", "person", "company").prefetch_related("team_members", "projects")
    upcoming = list(qs.filter(occurred_at__gt=now).order_by("occurred_at")[:20])
    page = Paginator(qs.filter(occurred_at__lte=now), TIMELINE_PAGE).get_page(request.GET.get("tpage"))
    return {"upcoming": upcoming, "page": page, "show_bg": show_bg}


def _table_context(request, table, object_type):
    page = Paginator(table.ordered(), 50).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    base_qs = params.urlencode()
    views = SavedView.objects.filter(object_type=object_type).filter(Q(owner=request.user) | Q(shared=True))
    model = table.base_queryset().model
    flag_options = sorted({f for flags in model.objects.values_list("flags", flat=True) for f in flags or []})
    return {
        "object_type": object_type,
        "flag_options": flag_options,
        "campuses": Campus.objects.all(),
        "projects": Project.objects.filter(active=True),
        "page": page,
        "table": table,
        "columns": table.visible_columns,
        "all_columns": table.columns,
        "current_qs": base_qs,
        "from_param": urlencode({"from": base_qs}),
        "saved_views": views,
        "users": active_users(),
        "choices": FILTER_CHOICES,
        "selected": {k: request.GET.getlist(k) for k in request.GET},
    }


POSITIVE_OUTCOMES = ["Responded", "Meeting Scheduled", "Referred", "Follow Up"]
GREETING = re.compile(r"^(hi|hello|hey|good (morning|afternoon)|thanks|thank you)\b[^\n.!,]*[,.!]?\s*", re.I)


SIGNOFF = re.compile(r"^(--|_{3,}|best|thanks|thank you|regards|sincerely|cheers|sent from|v/r|respectfully)\b", re.I)
SIGNATURE_LINE = re.compile(r"(\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4})|@|www\.|https?://|^(mobile|cell|office|phone|tel)\b", re.I)


NAME_LINE = re.compile(r"[A-Z][a-z'’.-]+( [A-Z]\.?)?( [A-Z][a-z'’.-]+){1,2},?")


def reply_gist(body, person=None, limit=180):
    """The opening of what they wrote: above the quoted thread, minus greeting, sign-off and signature."""
    names = {n.lower() for n in ((person.first_name, person.full_name) if person else ()) if n}
    last = (person.last_name or "").lower() if person else ""
    kept = []
    for line in new_text(body or "").replace("\ufffd", "'").splitlines():
        line = line.strip()
        if not line:
            continue
        if SIGNOFF.match(line) or SIGNATURE_LINE.search(line) or line.lower().rstrip(",.") in names:
            break
        if kept and last and len(line) < 80 and last in line.lower():
            break  # "Will Durant, PMP CEO & President"
        if kept and NAME_LINE.fullmatch(line):
            break  # a bare "First Last" line starts the signature
        kept.append(line)
    text = GREETING.sub("", " ".join(kept))
    # "Done, thanks." style one-liners keep their words; only trailing sign-offs are dropped.
    return text[:limit].rsplit(" ", 1)[0] + "…" if len(text) > limit else text


def _merge_candidates(company):
    """Likely twins first (same core name or a shared domain), for the merge picker."""
    core = merge.core_name(company.name)
    same = Company.objects.exclude(pk=company.pk)
    likely = [c.name for c in same.filter(name__icontains=core[:6])[:20] if merge.core_name(c.name) == core]
    for d in company.domains:
        likely += [c.name for c in same.filter(domains__icontains=f'"{d}"')[:10]]
    return list(dict.fromkeys(likely))


def _summary_context(company=None, person=None):
    summary = AISummary.objects.filter(company=company, person=person).first()
    return {
        "ai_enabled": ai.enabled(),
        "ai_summary": summary,
        "ai_stale": ai.stale_count(summary, company=company, person=person),
    }


@require_POST
def summary_refresh(request, kind, pk):
    record = get_object_or_404(Company if kind == "company" else Person, pk=pk)
    try:
        ai.generate(**{kind: record})
    except ai.SummaryUnavailable as exc:
        messages.error(request, str(exc))
    return redirect(_safe_next(request, record.get_absolute_url()))


def duplicates(request):
    groups = merge.suggestions()
    labels = dict(CompanyStatus.choices)
    for g in groups:
        for c in g["companies"]:
            c["status_label"] = labels.get(c["status"], c["status"])
    return render(request, "crm/duplicates.html", {"groups": groups})


@require_POST
@manager_required
def duplicates_merge(request):
    keep = get_object_or_404(Company, pk=request.POST.get("keep"))
    ids = [int(i) for i in request.POST.getlist("merge") if i.isdigit()]
    others = list(Company.objects.filter(pk__in=ids).exclude(pk=keep.pk))
    if not others:
        messages.error(request, "Tick at least one other company to merge into the one you keep.")
    else:
        names = ", ".join(o.name for o in others)
        counts = merge.merge(keep, others, user=request.user)
        moved = ", ".join(f"{v} {k}" for k, v in counts.items() if v and k != "companies merged")
        messages.success(request, f"Merged {names} into {keep.name}." + (f" Moved {moved}." if moved else ""))
    return redirect(_safe_next(request, reverse("crm:duplicates")))


@require_POST
def duplicates_dismiss(request):
    ids = [int(i) for i in request.POST.get("key", "").split(",") if i.isdigit()]
    if len(ids) > 1:
        merge.dismiss(ids, user=request.user)
        messages.success(request, "Marked as not duplicates.")
    return redirect("crm:duplicates")


@require_POST
@manager_required
def company_merge_into(request, pk):
    """From a company page: fold another company (picked by name) into this one."""
    keep = get_object_or_404(Company, pk=pk)
    other = Company.objects.filter(name__iexact=request.POST.get("other", "").strip()).exclude(pk=pk).first()
    if other is None:
        messages.error(request, "No other company with that exact name.")
    else:
        merge.merge(keep, [other], user=request.user)
        messages.success(request, f"Merged {other.name} into {keep.name}.")
    return redirect(keep)


def project_list(request):
    """Campuses, each with its own correspondence, the projects inside it and its prospective land sites."""
    from .forms import CampusForm
    from .models import LandSite

    form = ProjectForm(request.POST if request.POST.get("form") == "project" else None, prefix="p")
    campus_form = CampusForm(request.POST if request.POST.get("form") == "campus" else None, prefix="c")
    if request.method == "POST" and request.POST.get("form") == "project" and form.is_valid():
        project = form.save()
        counts = project_tagging.tag()
        messages.success(request, f"Added {project.name}. Re-tagged correspondence: {counts.get('project', 0)} project emails.")
        return redirect(project)
    if request.method == "POST" and request.POST.get("form") == "campus" and campus_form.is_valid():
        campus = campus_form.save()
        Project.objects.get_or_create(name=campus.code, defaults={
            "campus": campus, "keywords": [campus.code, campus.name], "address": campus.location})
        project_tagging.tag()
        messages.success(request, f"Added campus {campus.code}.")
        return redirect("crm:project_list")

    def stats(p):
        acts = p.activities.all()
        mine = [s for s in sites if s.project_id == p.pk]
        return {"project": p, "emails": acts.count(), "companies": acts.values("company").distinct().count(),
                "last": acts.order_by("-occurred_at").values_list("occurred_at", flat=True).first(),
                "sites": [s for s in mine if s.status != "dropped"],
                "archived": [s for s in mine if s.status == "dropped"]}

    sites = LandSite.by_status(LandSite.objects.select_related("campus", "project").prefetch_related("parcels"))
    projects = list(Project.objects.select_related("parent", "campus"))
    groups, used = [], set()
    for campus in Campus.objects.all():
        home = next((p for p in projects if p.campus_id == campus.pk and p.parent_id is None), None)
        children = [p for p in projects if p is not home and (p.campus_id == campus.pk or (home and p.parent_id == home.pk))]
        used.update(p.pk for p in children + ([home] if home else []))
        campus_sites = [s for s in sites if s.campus_id == campus.pk and not s.project_id
                        or (s.project_id and s.project_id == (home.pk if home else None))]
        children_stats = [stats(p) for p in children]
        groups.append({
            "campus": campus, "home": stats(home) if home else None, "projects": children_stats,
            "sites": [s for s in campus_sites if s.status != "dropped"],
            # Archive: every dropped site in the campus, its projects included.
            "archived": [s for s in campus_sites if s.status == "dropped"]
                        + [s for r in children_stats for s in r["archived"]],
        })
    other = [stats(p) for p in projects if p.pk not in used]
    loose_sites = [s for s in sites if not s.campus_id and not s.project_id and s.status != "dropped"]
    return render(request, "crm/project_list.html", {
        "groups": groups, "other": other, "loose_sites": loose_sites, "form": form, "campus_form": campus_form})


def campus_edit(request, pk):
    from .forms import CampusForm

    campus = get_object_or_404(Campus, pk=pk)
    old_code, old_name = campus.code, campus.name
    form = CampusForm(request.POST or None, instance=campus)
    if request.method == "POST" and form.is_valid():
        campus = form.save()
        # The campus's own project is named for its code: keep the two together.
        home = Project.objects.filter(campus=campus, parent__isnull=True, name=old_code).first()
        if home and campus.code != old_code:
            home.name = campus.code
            home.keywords = [campus.code if k == old_code else campus.name if k == old_name else k for k in home.keywords]
            home.save()
            project_tagging.tag()
        messages.success(request, f"Saved {campus.code}.")
        return redirect("crm:project_list")
    return render(request, "crm/form.html", {
        "form": form, "title": f"Edit campus {old_code}", "cancel": reverse("crm:project_list")})


def project_detail(request, pk):
    from .models import LandSite

    project = get_object_or_404(Project.objects.select_related("parent", "campus"), pk=pk)
    form = ProjectForm(request.POST or None, instance=project)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            counts = project_tagging.tag()
            messages.success(request, f"Saved. Re-tagged correspondence: {counts.get('project', 0)} project emails.")
            return redirect(project)
        messages.error(request, "Fix the errors below.")
    acts = project.activities.select_related("company", "person").prefetch_related("team_members", "projects")
    companies = (
        Company.objects.filter(activities__projects=project)
        .annotate(n=Count("activities", filter=Q(activities__projects=project), distinct=True),
                  last=Max("activities__occurred_at", filter=Q(activities__projects=project)))
        .order_by("-last")
    )
    page = Paginator(acts.filter(is_background=False).order_by("-occurred_at"), 50).get_page(request.GET.get("page"))
    return render(request, "crm/project_detail.html", {
        "project": project, "form": form, "companies": companies, "page": page, "title": "project",
        "approvals": entitlement_logic.annotate(list(entitlement_logic.with_related(project.approvals.all()))),
        "matrix": entitlement_logic.MATRIX, "new_approval_url": reverse("crm:approval_create") + f"?project={project.pk}",
        "build": build_logic.status(project),
        "capital": capital_logic.project_rollup(project),
        "land_sites": project.land_sites.prefetch_related("parcels").exclude(status="dropped"),
        "other_sites": LandSite.objects.exclude(project=project).exclude(status="dropped").order_by("name"),
    })


def documents_for(company=None, person=None):
    """Files exchanged with this company/person, newest first, one row per file name."""
    qs = Attachment.objects.select_related("activity", "activity__person", "activity__actor")
    qs = qs.filter(activity__company=company) if company is not None else qs.filter(activity__person=person)
    docs = {}
    for a in qs.order_by("-activity__occurred_at"):
        key = a.filename.lower()
        if key not in docs:
            docs[key] = {"file": a, "times": 0, "sent": a.activity.kind == Activity.Kind.EMAIL_SENT}
        docs[key]["times"] += 1
    return list(docs.values())


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


CALENDAR_NOISE = r": (Accepted|Declined|Tentatively accepted|Tentative|Updated invitation|Invitation|Canceled event):"


def recent_activity(qs=None, limit=25, include_sent=False):
    """Real conversation only: no automatic stage moves, calendar RSVPs, bounces or out-of-office.

    Home also leaves out outbound email (thousands of campaign sends); record pages keep it.
    """
    qs = Activity.objects.all() if qs is None else qs
    if not include_sent:
        qs = qs.exclude(kind=Activity.Kind.EMAIL_SENT)
    return (
        qs.filter(is_background=False, occurred_at__lte=timezone.now())
        .exclude(kind=Activity.Kind.STAGE_CHANGE, opportunity__isnull=True)  # company stage moves are noise here
        .exclude(summary__regex=CALENDAR_NOISE)
        .exclude(kind__in=[Activity.Kind.BOUNCE, Activity.Kind.AUTO_REPLY])
        .select_related("company", "person", "actor", "opportunity").prefetch_related("projects")[:limit]
    )


def week_calendar(request):
    """This week's meetings, Monday to Friday (weekend days only when something is on), for the Dashboard."""
    from datetime import timedelta

    today = timezone.localdate()
    try:
        offset = int(request.GET.get("week", 0))
    except ValueError:
        offset = 0
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    days = [monday + timedelta(days=i) for i in range(7)]
    everyone = request.GET.get("cal") == "all"
    qs = Meeting.objects.filter(start__date__gte=days[0], start__date__lte=days[-1]).prefetch_related("companies")
    if not everyone:
        qs = qs.filter(team=request.user).distinct()
    by_day = {}
    for m in qs.order_by("start"):
        by_day.setdefault(timezone.localtime(m.start).date(), []).append(m)
    shown = [d for d in days if d.weekday() < 5 or by_day.get(d)]
    return {
        "days": [{"date": d, "meetings": by_day.get(d, []), "today": d == today, "past": d < today} for d in shown],
        "offset": offset, "everyone": everyone, "monday": monday, "sunday": days[-1],
        "count": sum(len(v) for v in by_day.values()),
    }


def home(request):
    today = timezone.localdate()
    my_tasks = Task.objects.filter(assignee=request.user, done=False).select_related("company", "person")
    replies = list(
        Activity.objects.filter(kind=Activity.Kind.REPLY, outcome__in=POSITIVE_OUTCOMES, person__isnull=False)
        .select_related("person", "company").prefetch_related("team_members", "projects")
        .order_by("-occurred_at")[:40]
    )
    for r in replies:
        r.gist = reply_gist(r.body, r.person)
    # Recent activity: last 7 days; "Show 7 more days" widens the window a week at a time.
    try:
        activity_days = min(max(int(request.GET.get("days", 7)), 7), 365)
    except ValueError:
        activity_days = 7
    recent = recent_activity(
        Activity.objects.filter(occurred_at__gte=timezone.now() - timezone.timedelta(days=activity_days)), limit=500,
    )
    day = pipeline.my_day(request.user)
    for r in day["replies"]:
        r.gist = reply_gist(r.body, r.person, limit=120)
    stage_counts = dict(Company.objects.values_list("status").annotate(n=Count("id")))
    risk = pipeline.at_risk()
    return render(request, "crm/home.html", {
        "overdue": [t for t in my_tasks if t.due_date and t.due_date < today],
        "upcoming": [t for t in my_tasks if not t.due_date or t.due_date >= today][:15],
        "recent": recent,
        "activity_days": activity_days,
        "replies": replies,
        "day": day,
        "stage_counts": [(value, label, stage_counts.get(value, 0)) for value, label in CompanyStatus.choices],
        "summary": pipeline.summary(),
        "at_risk": risk[:40],
        "at_risk_count": len(risk),
        "changes": pipeline.changes_since(7),
        "suggestions": pipeline.suggestions(limit=40),
        "week": week_calendar(request),
        "deadlines": contract_dates.upcoming(14, today),
        "critical": contract_dates.month_calendar(request.GET.get("crit"), today),
    })


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


def company_list(request):
    table = CompanyTable(request.GET, request.user)
    return render(request, "crm/company_list.html", _table_context(request, table, ObjectType.COMPANY))


def company_create(request):
    form = CompanyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        company = form.save(commit=False)
        company.created_by = request.user
        company.save()
        form.save_m2m()
        Activity.objects.create(
            kind=Activity.Kind.RECORD_CREATED, occurred_at=timezone.now(), actor=request.user, company=company,
            summary="Company created", is_background=True,
        )
        return redirect(company)
    return render(request, "crm/form.html", {"form": form, "title": "New company", "cancel": reverse("crm:company_list")})


def company_detail(request, pk):
    company = get_object_or_404(Company.objects.select_related("owner"), pk=pk)
    form = CompanyForm(request.POST or None, instance=company)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect(request.get_full_path())
        messages.error(request, "Fix the errors below.")

    tab = request.GET.get("tab", "overview")
    activities = Activity.objects.filter(company=company)
    open_tasks = company.tasks.filter(done=False).select_related("assignee")
    latest_news = activities.filter(kind=Activity.Kind.NEWS_TRIGGER).first()
    ctx = {
        "record": company,
        "company": company,
        "form": form,
        "tab": tab,
        "nav": _neighbours(CompanyTable, request, company.pk),
        "people": company.people.select_related("owner").order_by("-last_interaction_at", "full_name"),
        "opportunities": company.opportunities.select_related("campus", "owner").order_by("stage"),
        "open_tasks": open_tasks,
        "done_tasks": company.tasks.filter(done=True)[:10],
        "notes": company.notes_set.select_related("author")[:20],
        "latest_news": latest_news,
        "recent_activity": recent_activity(activities, limit=8, include_sent=True),
        "list_entries": company.list_entries.select_related("list"),
        "available_lists": List.objects.filter(object_type=ObjectType.COMPANY).exclude(entries__company=company),
        "task_form": TaskForm(initial={"assignee": request.user}),
        "note_form": NoteForm(),
        "record_field": "company",
        "fit_definition": company.get_fit_score_display().split(": ", 1)[-1] if company.fit_score else "",
        "team": team_connections(company=company),
        "documents": documents_for(company=company),
        "added_documents": company.documents.select_related("added_by", "opportunity"),
        "comments": company.comments.select_related("author", "opportunity", "task")[:50],
        "comment_target": ("company", company.pk),
        "document_categories": DOCUMENT_CATEGORIES,
        **_summary_context(company=company),
        "merge_candidates": _merge_candidates(company),
        "company_projects": project_tagging.company_projects(company),
        **company_page.meeting_context(company, request, tab),
        "users": active_users(),
    }
    ctx["open_deals"] = [o for o in ctx["opportunities"] if o.is_open]
    ctx["other_tasks"] = [t for t in open_tasks if t.pk not in ctx["meeting_task_ids"]]
    ctx["open_action_count"] = len(ctx["open_items"]) + len(ctx["other_tasks"])
    ctx["done_tasks"] = [t for t in ctx["done_tasks"] if t.pk not in ctx["meeting_task_ids"]]
    ctx["done_action_count"] = len(ctx["done_items"]) + len(ctx["done_tasks"])
    if tab == "activity":
        filtered, ctx["activity_kind"] = company_page.filter_activity(activities, request.GET.get("kind"))
        ctx["timeline"] = _timeline(filtered, request)
        ctx["activity_chips"] = company_page.activity_chips(activities)
    return render(request, "crm/company_detail.html", ctx)


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


def person_list(request):
    table = PersonTable(request.GET, request.user)
    return render(request, "crm/person_list.html", _table_context(request, table, ObjectType.PERSON))


def person_create(request):
    initial = {"company": request.GET.get("company")} if request.GET.get("company") else None
    form = PersonForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        person = form.save(commit=False)
        person.created_by = request.user
        person.save()
        return redirect(person)
    return render(request, "crm/form.html", {"form": form, "title": "New person", "cancel": reverse("crm:person_list")})


def person_detail(request, pk):
    person = get_object_or_404(Person.objects.select_related("company", "owner"), pk=pk)
    form = PersonForm(request.POST or None, instance=person)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect(request.get_full_path())
        messages.error(request, "Fix the errors below.")
    tab = request.GET.get("tab", "overview")
    activities = Activity.objects.filter(person=person)
    ctx = {
        "record": person,
        "person": person,
        "form": form,
        "tab": tab,
        "nav": _neighbours(PersonTable, request, person.pk),
        "open_tasks": person.tasks.filter(done=False).select_related("assignee"),
        "done_tasks": person.tasks.filter(done=True)[:10],
        "notes": person.notes_set.select_related("author")[:20],
        "recent_activity": activities.filter(is_background=False, occurred_at__lte=timezone.now()).select_related("actor").prefetch_related("team_members", "projects")[:8],
        "opportunities": person.opportunities.select_related("campus"),
        "list_entries": person.list_entries.select_related("list"),
        "available_lists": List.objects.filter(object_type=ObjectType.PERSON).exclude(entries__person=person),
        "task_form": TaskForm(initial={"assignee": request.user}),
        "note_form": NoteForm(),
        "record_field": "person",
        "team": team_connections(person=person),
        "documents": documents_for(person=person),
        **_summary_context(person=person),
    }
    if tab == "activity":
        ctx["timeline"] = _timeline(activities, request)
    return render(request, "crm/person_detail.html", ctx)


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------


def _filtered_deals(request):
    qs = Opportunity.objects.select_related("company", "campus", "owner").prefetch_related("team")
    campus = request.GET.get("campus")
    if campus == "none":
        qs = qs.filter(campus__isnull=True)
    elif campus:
        qs = qs.filter(campus__code=campus)
    owner = request.GET.get("owner")
    if owner == "me":
        qs = qs.filter(Q(owner=request.user) | Q(team=request.user)).distinct()
    elif owner and owner.isdigit():
        qs = qs.filter(owner_id=int(owner))
    if q := request.GET.get("q", "").strip():
        qs = qs.filter(Q(name__icontains=q) | Q(company__name__icontains=q))
    return qs


def opportunity_board(request):
    qs = _filtered_deals(request)
    view = request.GET.get("view", "board")
    stage_filter = request.GET.get("stage")
    risk_only = request.GET.get("risk") == "1"
    today = timezone.localdate()
    deals = list(qs)
    for o in deals:
        o.risk = risk_reasons(o, today) if o.is_open else []
    if risk_only:
        view = "table"
        deals = [o for o in deals if o.risk]
    if stage_filter in OpportunityStage.values:
        deals = [o for o in deals if o.stage == stage_filter]
        view = "table" if view == "board" and request.GET.get("view") != "board" else view
    show_closed = request.GET.get("closed") == "1"
    deals.sort(key=lambda o: (-STAGE_ORDER[o.stage], -(o.value or 0), o.name))
    columns = []
    for value, label in OpportunityStage.choices:
        items = [o for o in deals if o.stage == value]
        columns.append({
            "value": value, "label": label, "items": items, "total": sum(o.value or 0 for o in items),
            "collapsed": value in (OpportunityStage.ON_HOLD, OpportunityStage.LOST) and not show_closed,
        })
    return render(request, "crm/opportunity_board.html", {
        "columns": columns, "deals": deals, "view": view, "campuses": Campus.objects.all(),
        "campus": request.GET.get("campus"), "risk_only": risk_only, "stage_filter": stage_filter,
        "summary": pipeline.summary(qs), "users": active_users(),
        "suggestions": pipeline.suggestions(limit=10) if view == "board" else [],
    })


@require_POST
def suggestion_create(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    opp = pipeline.create_from_suggestion(company, request.user)
    messages.success(request, f"Created {opp.name}. Fill in the site, size and value.")
    return redirect(opp)


@require_POST
def suggestion_dismiss(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    DealSuggestionDismissal.objects.get_or_create(company=company, defaults={"dismissed_by": request.user})
    messages.success(request, f"{company.name} will not be suggested again.")
    return redirect(_safe_next(request, reverse("crm:opportunity_board")))


def opportunity_create(request):
    initial = {"company": request.GET.get("company"), "owner": request.user, "team": [request.user]}
    form = OpportunityForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        opp = form.save()
        Activity.objects.create(
            kind=Activity.Kind.RECORD_CREATED, occurred_at=timezone.now(), actor=request.user, opportunity=opp,
            company=opp.company, summary=f"Opportunity created: {opp.name}",
        )
        return redirect(opp)
    return render(request, "crm/form.html", {
        "form": form, "title": "New opportunity", "cancel": reverse("crm:opportunity_board"),
    })


def opportunity_detail(request, pk):
    opp = get_object_or_404(Opportunity.objects.select_related("company", "campus", "owner"), pk=pk)
    old_label = opp.get_stage_display()
    form = OpportunityForm(request.POST or None, instance=opp)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            if old_label != opp.get_stage_display():
                notify.deal_stage_changed(opp, old_label, request.user)
            messages.success(request, "Saved.")
            return redirect(opp)
        messages.error(request, "Fix the errors below.")
    activities = Activity.objects.filter(Q(opportunity=opp) | Q(company=opp.company))
    contacts = opp.contacts.select_related("person")
    needs = TenantNeeds.objects.filter(opportunity=opp).first()
    agreements = list(opp.contracts.all())
    nxt = contract_dates.next_dates(agreements)
    for c in agreements:
        c.next = nxt.get(c.pk)
    return render(request, "crm/opportunity_detail.html", {
        "needs": needs, "qd_buildings": tenant_fit.buildings(needs), "agreements": agreements,
        "record": opp,
        "opp": opp,
        "form": form,
        "timeline": _timeline(activities, request),
        "open_tasks": opp.tasks.filter(done=False).select_related("assignee"),
        "notes": opp.notes_set.select_related("author")[:20],
        "task_form": TaskForm(initial={"assignee": request.user}),
        "note_form": NoteForm(),
        "record_field": "opportunity",
        "days_in_stage": (timezone.now() - opp.stage_changed_at).days,
        "risk": risk_reasons(opp) if opp.is_open else [],
        "contacts": contacts,
        "contact_form": OpportunityContactForm(opportunity=opp),
        "documents": documents_for(company=opp.company),
        "added_documents": Document.objects.filter(Q(opportunity=opp) | Q(company=opp.company)).select_related("added_by", "opportunity"),
        "document_form": DocumentForm(),
        "team": opp.team.all(),
        "document_categories": DOCUMENT_CATEGORIES,
        "today": timezone.localdate(),
        "comments": opp.comments.select_related("author", "task")[:50],
        "comment_target": ("opportunity", opp.pk),
    })


@require_POST
def opportunity_contact_add(request, pk):
    opp = get_object_or_404(Opportunity, pk=pk)
    form = OpportunityContactForm(request.POST, opportunity=opp)
    if form.is_valid():
        OpportunityContact.objects.update_or_create(
            opportunity=opp, person=form.cleaned_data["person"],
            defaults={"role": form.cleaned_data["role"], "note": form.cleaned_data["note"]},
        )
        messages.success(request, "Stakeholder saved.")
    else:
        messages.error(request, "Pick a person and a role.")
    return redirect(opp)


@require_POST
def opportunity_contact_remove(request, pk):
    contact = get_object_or_404(OpportunityContact, pk=pk)
    contact.delete()
    return redirect(contact.opportunity)


@require_POST
def opportunity_toggle_report(request, pk):
    opp = get_object_or_404(Opportunity, pk=pk)
    opp.investor_report = not opp.investor_report
    opp.save()
    messages.success(request, f"{opp.name} {'added to' if opp.investor_report else 'removed from'} the investor report.")
    return redirect(_safe_next(request, opp.get_absolute_url()))


@require_POST
def opportunity_set_stage(request, pk):
    opp = get_object_or_404(Opportunity, pk=pk)
    stage = request.POST.get("stage")
    if stage not in OpportunityStage.values:
        return HttpResponseBadRequest("Unknown stage")
    old_label = opp.get_stage_display()
    opp.stage = stage
    opp.save()
    if old_label != opp.get_stage_display():
        notify.deal_stage_changed(opp, old_label, request.user)
    return JsonResponse({"ok": True, "stage": stage})


# ---------------------------------------------------------------------------
# Tasks and notes
# ---------------------------------------------------------------------------


def task_list(request):
    scope = request.GET.get("scope", "mine")
    state = request.GET.get("state", "open")
    qs = Task.objects.select_related("assignee", "company", "person", "opportunity").annotate(n_comments=Count("comments"))
    if scope == "mine":
        qs = qs.filter(assignee=request.user)
    qs = qs.filter(done=(state == "done"))
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    return render(request, "crm/task_list.html", {
        "page": page, "scope": scope, "state": state, "task_form": TaskForm(initial={"assignee": request.user}),
    })


@require_POST
def task_add(request):
    form = TaskForm(request.POST)
    if form.is_valid():
        task = form.save(commit=False)
        for field, obj in _record_from_post(request).items():
            setattr(task, field, obj)
        task.created_by = request.user
        task.save()
        notify.task_assigned(task, request.user)
        messages.success(request, "Task added.")
    else:
        messages.error(request, "Task needs a title.")
    return redirect(_safe_next(request, reverse("crm:task_list")))


def task_detail(request, pk):
    """A task as a Trello-style card: details, repeat, comments."""
    task = get_object_or_404(Task.objects.select_related("assignee", "company", "person", "opportunity", "meeting"), pk=pk)
    old_assignee = task.assignee_id
    form = TaskForm(request.POST or None, instance=task, full=True)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            if task.assignee_id != old_assignee:
                notify.task_assigned(task, request.user)
            messages.success(request, "Saved.")
            return redirect(task)
        messages.error(request, "Fix the errors below.")
    return render(request, "crm/task_detail.html", {
        "task": task, "form": form, "comments": task.comments.select_related("author"),
        "comment_target": ("task", task.pk), "users": active_users(),
    })


@require_POST
def task_toggle(request, pk):
    task = get_object_or_404(Task, pk=pk)
    task.done = not task.done
    task.done_at = timezone.now() if task.done else None
    task.save()
    if task.done:
        Activity.objects.create(
            kind=Activity.Kind.TASK, occurred_at=timezone.now(), actor=request.user, company=task.company,
            person=task.person, opportunity=task.opportunity, summary=f"Completed task: {task.title}",
            is_background=True,
        )
        nxt = task.next_due()
        if nxt:
            Task.objects.create(
                title=task.title, notes=task.notes, due_date=nxt, due_time=task.due_time, assignee=task.assignee, company=task.company,
                person=task.person, opportunity=task.opportunity, repeat=task.repeat, repeat_until=task.repeat_until,
                created_by=task.created_by,
            )
            messages.success(request, f"Done. Next one is due {nxt:%a %b} {nxt.day}.")
    return redirect(_safe_next(request, reverse("crm:task_list")))


@require_POST
def comment_add(request):
    body = request.POST.get("body", "").strip()
    target = {}
    from .models import LandSite

    for field, model in (("task", Task), ("opportunity", Opportunity), ("site", LandSite), ("company", Company)):
        if value := request.POST.get(field):
            target[field] = get_object_or_404(model, pk=value)
    if not body or not target:
        messages.error(request, "Write something first.")
        return redirect(_safe_next(request, "/"))
    if "task" in target:
        target.setdefault("company", target["task"].company)
        target.setdefault("opportunity", target["task"].opportunity)
    if "opportunity" in target and target["opportunity"]:
        target.setdefault("company", target["opportunity"].company)
    comment = Comment.objects.create(body=body, author=request.user, **{k: v for k, v in target.items() if v})
    notify.comment_added(comment)
    return redirect(_safe_next(request, "/") + "#comments")


@require_POST
def comment_delete(request, pk):
    comment = get_object_or_404(Comment, pk=pk, author=request.user)
    comment.delete()
    return redirect(_safe_next(request, "/") + "#comments")


def notification_list(request):
    qs = Notification.objects.filter(user=request.user).select_related("actor")
    show = request.GET.get("show", "unread")
    if show == "unread":
        qs = qs.filter(read_at__isnull=True)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "crm/notifications.html", {"page": page, "show": show})


def notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    if n.read_at is None:
        n.read_at = timezone.now()
        n.save(update_fields=["read_at"])
    return redirect(n.url if n.url.startswith("/") else reverse("crm:notification_list"))


def notification_poll(request):
    """For the browser's live alerts (static/crm/alerts.js): unread count and anything newer than ?after=."""
    from django.http import JsonResponse

    from . import notify

    notify.timed_sweep(request.user)  # tasks whose time has come
    unread = Notification.objects.filter(user=request.user, read_at__isnull=True)
    try:
        after = int(request.GET.get("after") or 0)
    except ValueError:
        after = 0
    latest = Notification.objects.filter(user=request.user).order_by("-id").values_list("id", flat=True).first() or 0
    items = [{"id": n.pk, "kind": n.get_kind_display(), "text": n.text,
              "url": reverse("crm:notification_open", args=[n.pk])}
             for n in unread.filter(id__gt=after).order_by("id")[:10]] if after else []
    return JsonResponse({"unread": unread.count(), "latest": latest, "items": items})


@require_POST
def notification_read_all(request):
    Notification.objects.filter(user=request.user, read_at__isnull=True).update(read_at=timezone.now())
    return redirect(_safe_next(request, reverse("crm:notification_list")))


@require_POST
def note_add(request):
    form = NoteForm(request.POST)
    links = _record_from_post(request)
    if form.is_valid() and links:
        note = form.save(commit=False)
        note.author = request.user
        for field, obj in links.items():
            setattr(note, field, obj)
        note.save()
        Activity.objects.create(
            kind=Activity.Kind.NOTE, occurred_at=note.created_at, actor=request.user, summary=note.body[:200],
            body=note.body, source_id=f"note:{note.pk}", **links,
        )
        messages.success(request, "Note added.")
    return redirect(_safe_next(request, "/"))


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def list_index(request):
    form = ListForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        lst = form.save(commit=False)
        lst.created_by = request.user
        lst.save()
        return redirect(lst)
    lists = List.objects.annotate(n=Count("entries"))
    return render(request, "crm/list_index.html", {"lists": lists, "form": form})


def list_detail(request, pk):
    lst = get_object_or_404(List, pk=pk)
    entries = lst.entries.select_related("company", "person", "opportunity", "opportunity__company", "person__company")
    view = request.GET.get("view", "board")
    columns = [{"stage": s, "items": [e for e in entries if e.stage == s]} for s in lst.stages]
    unstaged = [e for e in entries if e.stage not in lst.stages]
    if unstaged:
        columns.insert(0, {"stage": "", "items": unstaged})
    return render(request, "crm/list_detail.html", {
        "list": lst, "entries": entries, "columns": columns, "view": view,
    })


@require_POST
def list_add_entry(request):
    lst = get_object_or_404(List, pk=request.POST.get("list"))
    field = lst.object_type
    obj_id = request.POST.get(field)
    if not obj_id:
        raise Http404
    model = {"company": Company, "person": Person, "opportunity": Opportunity}[field]
    obj = get_object_or_404(model, pk=obj_id)
    ListEntry.objects.get_or_create(
        list=lst, **{field: obj}, defaults={"stage": lst.stages[0] if lst.stages else "", "added_by": request.user}
    )
    messages.success(request, f"Added to {lst.name}.")
    return redirect(_safe_next(request, lst.get_absolute_url()))


@require_POST
def list_entry_stage(request, pk):
    entry = get_object_or_404(ListEntry, pk=pk)
    stage = request.POST.get("stage", "")
    if stage and stage not in entry.list.stages:
        return HttpResponseBadRequest("Unknown stage")
    entry.stage = stage
    entry.save(update_fields=["stage"])
    return JsonResponse({"ok": True})


@require_POST
def list_entry_remove(request, pk):
    entry = get_object_or_404(ListEntry, pk=pk)
    entry.delete()
    return redirect(_safe_next(request, entry.list.get_absolute_url()))


# ---------------------------------------------------------------------------
# Saved views
# ---------------------------------------------------------------------------


@require_POST
def saved_view_save(request):
    object_type = request.POST.get("object_type")
    if object_type not in (ObjectType.COMPANY, ObjectType.PERSON):
        return HttpResponseBadRequest("Unknown table")
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "Give the view a name.")
    else:
        query = request.POST.get("query", "")
        # Drop paging so a view always opens on page 1.
        params = {k: v for k, v in parse_qs(query).items() if k != "page"}
        SavedView.objects.update_or_create(
            owner=request.user, object_type=object_type, name=name,
            defaults={"query": urlencode(params, doseq=True), "shared": request.POST.get("shared") == "on"},
        )
        messages.success(request, f"Saved view “{name}”.")
    target = "crm:company_list" if object_type == ObjectType.COMPANY else "crm:person_list"
    return redirect(reverse(target) + "?" + request.POST.get("query", ""))


@require_POST
def saved_view_delete(request, pk):
    view = get_object_or_404(SavedView, pk=pk, owner=request.user)
    target = "crm:company_list" if view.object_type == ObjectType.COMPANY else "crm:person_list"
    view.delete()
    return redirect(target)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@require_POST
@manager_required
def person_delete(request, pk):
    person = get_object_or_404(Person, pk=pk)
    label = str(person) if (person.full_name or person.email) else "Unnamed person"
    company = person.company
    person.delete()
    if company:
        Activity.objects.create(
            kind=Activity.Kind.FIELD_CHANGE, occurred_at=timezone.now(), actor=request.user, company=company,
            summary=f"Person deleted: {label}", is_background=True,
        )
    messages.success(request, f"Deleted {label}.")
    return redirect(company or reverse("crm:person_list"))


@require_POST
@manager_required
def company_delete(request, pk):
    company = get_object_or_404(Company, pk=pk)
    name = company.name
    people = request.POST.get("people") == "delete"
    if people:
        company.people.all().delete()
    company.delete()
    messages.success(request, f"Deleted {name}{' and its people' if people else ''}.")
    return redirect("crm:company_list")


@require_POST
@manager_required
def opportunity_delete(request, pk):
    opp = get_object_or_404(Opportunity, pk=pk)
    company = opp.company
    Activity.objects.create(
        kind=Activity.Kind.FIELD_CHANGE, occurred_at=timezone.now(), actor=request.user, company=company,
        summary=f"Opportunity deleted: {opp.name}", is_background=True,
    )
    opp.delete()
    messages.success(request, f"Deleted opportunity {opp.name}.")
    return redirect(company)


# ---------------------------------------------------------------------------
# Documents added by hand (Drive links or uploads)
# ---------------------------------------------------------------------------


@require_POST
def document_add(request):
    links = _record_from_post(request)
    if "company" not in links:
        raise Http404
    form = DocumentForm(request.POST, request.FILES)
    if form.is_valid():
        documents.save_document(form, request.user, company=links["company"], opportunity=links.get("opportunity"))
        messages.success(request, "Document added.")
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect(_safe_next(request, links["company"].get_absolute_url()))


INLINE_TYPES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp")


def document_file(request, pk):
    """An uploaded document, streamed to signed-in users. PDFs and images open in the browser; anything else
    downloads, so an uploaded web page can't run as part of the CRM."""
    doc = get_object_or_404(Document, pk=pk)
    if not doc.file:
        raise Http404
    try:
        fh = doc.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("That file is no longer on the server.")
    name = doc.file.name.rsplit("/", 1)[-1]
    return FileResponse(fh, as_attachment=not name.lower().endswith(INLINE_TYPES), filename=name)


@require_POST
def document_delete(request, pk):
    doc = get_object_or_404(Document, pk=pk)
    if doc.file:
        doc.file.delete(save=False)
    doc.delete()
    messages.success(request, f"Removed {doc.title}.")
    return redirect(_safe_next(request, doc.company.get_absolute_url() if doc.company else "/"))


@require_POST
def company_bulk_owner(request):
    """Companies table: set the owner on the ticked rows, or hand each to whoever knows them best."""
    ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
    companies = Company.objects.filter(pk__in=ids)
    how = request.POST.get("owner", "")
    changed = 0
    for c in companies:
        if how == "connection":
            new = c.strongest_connection_user_id
        elif how == "none":
            new = None
        elif how.isdigit():
            new = int(how)
        else:
            continue
        if new != c.owner_id and (new or how == "none"):
            c.owner_id = new
            c.save()
            changed += 1
    messages.success(request, f"Owner updated on {changed} of {len(ids)} compan{'y' if len(ids) == 1 else 'ies'}.")
    return redirect(_safe_next(request, reverse("crm:company_list")))


# ---------------------------------------------------------------------------
# Investor / lender pipeline report (replaces the Fortress pipeline tracker)
# ---------------------------------------------------------------------------

REPORT_SECTIONS = [
    ("Signed agreements", [OpportunityStage.SIGNED, OpportunityStage.LOI_SIGNED]),
    ("Active discussions", [OpportunityStage.LEASE_NEGOTIATION, OpportunityStage.LOI_NEGOTIATION, OpportunityStage.QUALIFIED]),
    ("Prospects", [OpportunityStage.PROSPECT]),
]


def _report_data(request):
    qs = Opportunity.objects.select_related("company", "campus", "owner")
    show_all = request.GET.get("all") == "1"
    if not show_all:
        qs = qs.filter(investor_report=True)
    deals = sorted(qs, key=lambda o: (-STAGE_ORDER[o.stage], o.site_label, -(o.value or 0)))
    sections = []
    for title, stages in REPORT_SECTIONS:
        rows = [o for o in deals if o.stage in stages]
        sections.append({
            "title": title, "rows": rows, "sf": sum(o.sq_ft_need or 0 for o in rows),
            "cost": sum(o.value or 0 for o in rows), "weighted": sum(o.weighted_value for o in rows),
        })
    sites = {}
    for o in deals:
        if o.stage not in OPEN_STAGES + [OpportunityStage.SIGNED]:
            continue
        row = sites.setdefault(o.site_label, {"site": o.site_label, "campus": o.campus, "tenants": 0, "sf": 0,
                                              "cost": 0, "weighted": 0, "acres": 0, "priced_sf": 0, "priced_cost": 0})
        row["tenants"] += 1
        row["sf"] += o.sq_ft_need or 0
        row["cost"] += o.value or 0
        row["weighted"] += o.weighted_value
        row["acres"] += o.acreage_need or 0
        if o.value and o.sq_ft_need:  # $/sf only from deals that have both
            row["priced_sf"] += o.sq_ft_need
            row["priced_cost"] += o.value
    for row in sites.values():
        row["psf"] = row["priced_cost"] / row["priced_sf"] if row["priced_sf"] else None
    return {"sections": sections, "sites": sorted(sites.values(), key=lambda r: (r["campus"] is None, r["site"])),
            "show_all": show_all, "as_of": timezone.localdate()}


def pipeline_report(request):
    return render(request, "crm/report_pipeline.html", _report_data(request))


def pipeline_report_xlsx(request):
    from .reports import pipeline_workbook

    data = _report_data(request)
    content = pipeline_workbook(data)
    response = HttpResponse(content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="ACMI_Properties_Pipeline_{data["as_of"]:%Y.%m.%d}.xlsx"'
    return response


# ---------------------------------------------------------------------------
# Meetings calendar (Granola + Google Calendar)
# ---------------------------------------------------------------------------


def meeting_list(request):
    import calendar as cal
    from datetime import date

    today = timezone.localdate()
    try:
        year, month = (int(x) for x in request.GET.get("month", "").split("-"))
        first = date(year, month, 1)
    except ValueError:
        first = today.replace(day=1)
    weeks = cal.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)
    qs = Meeting.objects.filter(start__date__gte=weeks[0][0], start__date__lte=weeks[-1][-1]).prefetch_related("companies")
    who = request.GET.get("who", "all")
    if who == "me":
        qs = qs.filter(team=request.user)
    elif who == "outside":
        qs = qs.filter(companies__isnull=False).distinct()
    by_day = {}
    for m in qs:
        by_day.setdefault(timezone.localtime(m.start).date(), []).append(m)
    prev_month = (first.replace(day=1) - timezone.timedelta(days=1)).replace(day=1)
    next_month = (first.replace(day=28) + timezone.timedelta(days=7)).replace(day=1)
    upcoming = Meeting.objects.filter(start__gte=timezone.now()).prefetch_related("companies")[:8]
    return render(request, "crm/meetings.html", {
        "weeks": [[{"date": d, "meetings": by_day.get(d, []), "other": d.month != first.month, "today": d == today}
                   for d in week] for week in weeks],
        "first": first, "prev": prev_month, "next": next_month, "who": who, "upcoming": upcoming,
        "open_id": request.GET.get("open", "") if request.GET.get("open", "").isdigit() else "",
        "google_ready": settings.GOOGLE_SIGNIN_ENABLED,
        "calendar_connected": gcal.connected(request.user),
        "calendar_state": CalendarSync.objects.filter(user=request.user).first(),
    })


@require_POST
def calendar_sync_now(request):
    try:
        stats = gcal.sync_user(request.user)
        removed = f", {stats['removed']} cancelled removed" if stats["removed"] else ""
        messages.success(request, f"Calendar synced: {stats['calendar']} meetings in the window{removed}.")
    except gcal.CalendarError as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # network trouble: show it, don't 500
        messages.error(request, f"Calendar sync failed: {exc}")
    return redirect(reverse("crm:meeting_list"))


def meeting_panel(request, pk):
    """The pop-up: Granola summary, action items (each can become a task), attendees, companies."""
    m = get_object_or_404(Meeting.objects.prefetch_related("companies", "team"), pk=pk)
    tasks = Task.objects.in_bulk([i["task_id"] for i in m.action_items if i.get("task_id")])
    items = [{**i, "index": n, "task": tasks.get(i.get("task_id"))} for n, i in enumerate(m.action_items)]
    for i in items:
        i["done"] = bool(i["task"] and i["task"].done)
    return render(request, "crm/partials/meeting_panel.html", {
        "m": m, "items": items, "summary_html": meetings.render_markdown(m.summary), "users": active_users(),
        "next": m.get_absolute_url(),  # the pop-up is fetched, so its forms return to the calendar, not the fragment
    })


@manager_required
def meeting_transcript(request, pk):
    """The saved transcript as a .txt download (Managers only: transcripts can hold NDA material)."""
    from . import transcripts

    m = get_object_or_404(Meeting.objects.prefetch_related("companies"), pk=pk)
    t = getattr(m, "transcript", None)
    if t is None:
        raise Http404("No transcript saved for this meeting.")
    resp = HttpResponse(transcripts.as_text(t), content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{transcripts.file_name(m)}"'
    return resp


@require_POST
def meeting_make_task(request, pk):
    m = get_object_or_404(Meeting, pk=pk)
    index = int(request.POST.get("index", -1))
    if not 0 <= index < len(m.action_items):
        raise Http404
    item = m.action_items[index]
    if not item.get("task_id"):
        owner = None
        if item.get("owner"):
            first = item["owner"].split()[0].split(",")[0].lower()
            owner = next((u for u in active_users() if (u.first_name or "").lower() == first or u.username == first), None)
        assignee = get_object_or_404(type(request.user), pk=request.POST["assignee"]) if request.POST.get("assignee") else (owner or request.user)
        due = request.POST.get("due_date") or None
        due_time = request.POST.get("due_time") or None
        company = m.companies.first()
        task = Task.objects.create(
            title=item["title"][:300], notes=(item.get("detail", "") + f"\n\nFrom meeting: {m.title} ({m.start:%b %d}) {m.url}").strip(),
            due_date=due, due_time=due_time if due else None, assignee=assignee, company=company, meeting=m, created_by=request.user,
        )
        items = list(m.action_items)
        items[index] = {**item, "task_id": task.pk}
        m.action_items = items
        m.save(update_fields=["action_items"])
        notify.task_assigned(task, request.user)
        messages.success(request, f"Task created for {assignee.get_full_name() or assignee.username}: {task.title}")
    return redirect(_safe_next(request, m.get_absolute_url()))
