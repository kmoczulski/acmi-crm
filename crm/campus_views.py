"""Campus Summary tab: /campuses/<code>/ (see crm/campus.py)."""
from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import campus as campus_mod
from .models import Campus, CampusUpdate, LandSite, Opportunity, Project
from .permissions import is_manager


def campus_index(request):
    first = Campus.objects.first()
    if first is None:
        messages.info(request, "Add a campus on the Projects page first.")
        return redirect("crm:project_list")
    return redirect("crm:campus_summary", code=first.code)


def _target(campus, value):
    """('tenant', opportunity) / ('land', site) / ('project', project) / ('other', None) from the form's target."""
    kind, _, pk = (value or "other").partition(":")
    if kind == "opp":
        return CampusUpdate.Workstream.TENANT, {"opportunity": get_object_or_404(Opportunity, pk=pk, campus=campus)}
    if kind == "site":
        return CampusUpdate.Workstream.LAND, {"site": get_object_or_404(LandSite, pk=pk, campus=campus)}
    if kind == "project":
        return CampusUpdate.Workstream.PROJECT, {"project": get_object_or_404(Project, pk=pk, campus=campus)}
    return CampusUpdate.Workstream.OTHER, {}


def _date(value, default=None):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def campus_summary(request, code):
    campus = get_object_or_404(Campus, code__iexact=code)
    if request.method == "POST":
        text = (request.POST.get("text") or "").strip()
        if not text:
            messages.error(request, "Write the update first.")
        else:
            workstream, link = _target(campus, request.POST.get("target"))
            waiting = request.POST.get("waiting_on", "")
            CampusUpdate.objects.create(
                campus=campus, workstream=workstream, text=text[:2000], author=request.user,
                waiting_on=waiting if waiting in CampusUpdate.Waiting.values else "",
                promised_on=_date(request.POST.get("promised_on")),
                happened_on=_date(request.POST.get("happened_on"), default=date.today()), **link)
            messages.success(request, "Update added.")
        return redirect(f"{request.path}#{request.POST.get('anchor', '')}")
    days = request.GET.get("days", "7")
    data = campus_mod.summary(campus, feed_days=int(days) if days.isdigit() and 0 < int(days) <= 60 else 7)
    return render(request, "crm/campus_summary.html", {
        **data, "campuses": Campus.objects.all(), "email": campus_mod.email_text(data),
        "waiting_choices": CampusUpdate.Waiting.choices,
    })


@require_POST
def campus_update_delete(request, pk):
    u = get_object_or_404(CampusUpdate.objects.select_related("campus"), pk=pk)
    if u.author_id != request.user.pk and not is_manager(request.user):
        messages.error(request, "Only the author or a manager can remove an update.")
    else:
        u.delete()
        messages.success(request, "Update removed.")
    return redirect("crm:campus_summary", code=u.campus.code)
