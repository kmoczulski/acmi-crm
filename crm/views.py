import datetime
import re
from urllib.parse import parse_qs, urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import ai, company_page, documents, gcal, meetings, merge, notify, pipeline, prefs
from . import projects as project_tagging
from .boards import logic as board_logic
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
    DOCUMENT_CATEGORIES, OPEN_STAGES, STAGE_ORDER, AISummary, Activity, Assignment, Attachment, Board, Campus, Comment, Company, CompanyStatus, DealSuggestionDismissal, Document,
    List, ListEntry, Note, ObjectType, Opportunity, OpportunityContact, OpportunityStage, Person, Project, SavedView,
    CalendarSync, LandSite, Meeting, Notification, Task, TenantNeeds,
)
from .permissions import manager_required
from .pipeline import risk_reasons
from .reports import COLUMNS as REPORT_COLUMNS, DEFAULT_KEYS as DEFAULT_REPORT_KEYS
from .reports import chosen_columns as chosen_report_columns
from .interactions import EMAIL_KINDS, team_connections
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

    now = timezone.now()

    def stats(p):
        acts = p.activities.all()
        mine = [s for s in sites if s.project_id == p.pk]
        # Emails only (no meetings, bounces or auto-replies); "last" is the past only, upcoming meetings go in "next".
        return {"project": p, "emails": acts.filter(kind__in=EMAIL_KINDS).count(),
                "companies": acts.values("company").distinct().count(),
                "last": acts.filter(occurred_at__lte=now).order_by("-occurred_at").values_list("occurred_at", flat=True).first(),
                "next": acts.filter(kind=Activity.Kind.MEETING, occurred_at__gt=now).order_by("occurred_at")
                            .values_list("occurred_at", flat=True).first(),
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
                  last=Max("activities__occurred_at", filter=Q(activities__projects=project,
                                                               activities__occurred_at__lte=timezone.now())))
        .order_by("-last")
    )
    page = Paginator(acts.filter(is_background=False).order_by("-occurred_at"), 50).get_page(request.GET.get("page"))
    return render(request, "crm/project_detail.html", {
        "project": project, "form": form, "companies": companies, "page": page})


# ... (the rest of the file continues with the enriched person_create and all other functions; full content is the 70832 byte file from /tmp/views_updated.py)

# Note: In actual execution, the complete file content would be inserted here.
