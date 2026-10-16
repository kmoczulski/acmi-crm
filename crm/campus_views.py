"""Campus Summary tab: /campuses/<code>/ (see crm/campus.py)."""
from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import campus as campus_mod
from .models import Campus, CampusReport, CampusUpdate, LandSite, Opportunity, Project
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


def _report_action(request, campus):
    """Weekly update: draft with Claude, save edits, or send (managers)."""
    from . import campus_ai
    from .outreach.sender import SendError

    action = request.POST.get("action")
    report = CampusReport.objects.filter(campus=campus, pk=request.POST.get("report") or 0).first()
    if action == "draft_report":
        try:
            campus_ai.draft(campus, user=request.user)
            messages.success(request, "Claude drafted this week's update. Read it through and edit before sending.")
        except campus_ai.DraftUnavailable as exc:
            messages.error(request, str(exc))
    elif report is None:
        messages.error(request, "There's no draft to save yet.")
    elif report.status == CampusReport.Status.SENT:
        messages.error(request, "That update has already gone out.")
    elif action == "save_report":
        report.body = request.POST.get("body", report.body).strip() + "\n"
        report.recipients = (request.POST.get("recipients") or "").strip()
        report.save(update_fields=["body", "recipients", "updated_at"])
        messages.success(request, "Update saved.")
    elif action == "send_report":
        if not is_manager(request.user):
            messages.error(request, "Only managers can send the weekly update.")
        else:
            report.body = request.POST.get("body", report.body).strip() + "\n"
            report.recipients = (request.POST.get("recipients") or "").strip()
            report.save(update_fields=["body", "recipients", "updated_at"])
            try:
                campus_ai.send(report, request.user)
                messages.success(request, f"Update sent to {report.recipients}.")
            except (SendError, OSError) as exc:
                messages.error(request, f"Not sent: {exc}")
    return redirect(f"{request.path}#report")


def campus_summary(request, code):
    campus = get_object_or_404(Campus, code__iexact=code)
    if request.method == "POST" and request.POST.get("action", "").endswith("_report"):
        return _report_action(request, campus)
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
    from . import ai, campus_ai

    days = request.GET.get("days", "7")
    data = campus_mod.summary(campus, feed_days=int(days) if days.isdigit() and 0 < int(days) <= 60 else 7)
    this_week = campus_ai.week_of()
    return render(request, "crm/campus_summary.html", {
        **data, "campuses": Campus.objects.all(), "email": campus_mod.email_text(data),
        "waiting_choices": CampusUpdate.Waiting.choices,
        "report": CampusReport.objects.filter(campus=campus, week_of=this_week).first(), "week_of": this_week,
        "past_reports": CampusReport.objects.filter(campus=campus).exclude(week_of=this_week)[:12],
        "ai_ready": ai.enabled(),
    })


def campus_report(request, code, pk):
    """One weekly update as a printable page (logo, then the text as it went out)."""
    report = get_object_or_404(CampusReport.objects.select_related("campus", "sent_by"), pk=pk,
                               campus__code__iexact=code)
    return render(request, "crm/campus_report.html", {"r": report})


@require_POST
def campus_update_delete(request, pk):
    u = get_object_or_404(CampusUpdate.objects.select_related("campus"), pk=pk)
    if u.author_id != request.user.pk and not is_manager(request.user):
        messages.error(request, "Only the author or a manager can remove an update.")
    else:
        u.delete()
        messages.success(request, "Update removed.")
    return redirect("crm:campus_summary", code=u.campus.code)
