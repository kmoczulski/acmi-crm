"""Scheduling pages (docs/SCHEDULING.md): the Schedule page and the small JSON calls behind it.

Every call goes through the signed-in person's own Google Calendar permission, so the hub shows only what they could
see in Google Calendar and a meeting is always made on their own calendar.
"""
import logging
import re
import uuid
from datetime import date

import requests
from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm import gcal, settings_nav
from crm.mail.views import team_only
from crm.models import Company, Person

from . import connect, service, slots

log = logging.getLogger(__name__)
NEEDS_PERMISSION = ("The hub isn't allowed to put meetings on your calendar yet. Press “Allow scheduling” at the top of "
                    "the page, or use “Open in Google Calendar”.")


def _json_error(message, status=400, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def _prefill(request):
    """Who and what the page starts with, from the link that opened it (?person=, ?company=, ?with=, ?title=)."""
    emails, title, company, contacts = [], (request.GET.get("title") or "").strip()[:200], None, []
    person_id, company_id = request.GET.get("person", ""), request.GET.get("company", "")
    person = Person.objects.select_related("company").filter(pk=person_id).first() if person_id.isdigit() else None
    if person:
        company = person.company
        if person.email:
            emails.append(person.email)
        title = title or (f"Meeting with {person.full_name}" if person.full_name else "")
    if not company and company_id.isdigit():
        company = Company.objects.filter(pk=company_id).first()
    if company:
        title = title or f"ACMI / {company.name}"
        contacts = [{"email": p.email, "name": p.full_name or p.email, "title": p.title}
                    for p in company.people.exclude(email__isnull=True).exclude(email="").order_by("full_name")[:12]]
    try:
        extra = service.clean_emails(request.GET.get("with", ""), service.MAX_INVITED)
    except service.ScheduleError:
        extra = []
    emails += [e for e in extra if e not in emails]
    return {"emails": emails, "title": title, "company": company, "contacts": contacts, "pick": _pick(request),
            "from_email": request.GET.get("from") == "email"}


def _pick(request):
    """A time to open the meeting form at, from ?when=YYYY-MM-DDTHH:MM (an email where someone settled on a time): the
    person still confirms it. None for anything that is not a real date and time."""
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T([01]\d|2[0-3]):([0-5]\d)", request.GET.get("when", ""))
    if not match:
        return None
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return None
    return {"date": match.group(1), "time": f"{match.group(2)}:{match.group(3)}"}


@team_only
def home(request):
    user = request.user
    ctx = {"google_ready": settings.GOOGLE_SIGNIN_ENABLED, "connected": gcal.connected(user),
           "has_email": bool(user.email), "can_schedule": connect.can_schedule(user)}
    if ctx["google_ready"] and ctx["connected"] and ctx["has_email"]:
        pre = _prefill(request)
        names = service.names_for(pre["emails"])
        ctx.update({
            "company": pre["company"], "request_id": uuid.uuid4().hex,
            "config": {
                "me": user.email.lower(), "team": service.team(user),
                "invited": [{"email": e, "name": names.get(e) or e} for e in pre["emails"]],
                "contacts": pre["contacts"], "title": pre["title"],
                "company": pre["company"].name if pre["company"] else "",
                "minutes": service.minutes_from(request.GET.get("minutes")), "durations": list(slots.DURATIONS),
                "hours": list(slots.DEFAULT_HOURS), "canSchedule": ctx["can_schedule"],
                "today": timezone.localdate().isoformat(),
                "start": service.week_start(request.GET.get("start") or (pre["pick"] or {}).get("date")).isoformat(),
                "pick": pre["pick"], "fromEmail": pre["from_email"],
                "urls": {"availability": reverse("crm:schedule_availability"), "people": reverse("crm:schedule_people"),
                         "create": reverse("crm:schedule_create"), "open": reverse("crm:schedule_open"),
                         "calendar": reverse("crm:meeting_list")},
            },
        })
    return render(request, "crm/schedule.html", ctx)


@team_only
def availability_json(request):
    """A week of who is busy when: ?emails=a,b&start=YYYY-MM-DD&days=5|7&from=8&to=18&minutes=30."""
    g = request.GET
    try:
        data = service.availability(request.user, g.get("emails", ""), g.get("start"), 7 if g.get("days") == "7" else 5,
                                    service.hours_from(g.get("from"), g.get("to")), service.minutes_from(g.get("minutes")))
    except (service.ScheduleError, gcal.CalendarError) as exc:
        return _json_error(str(exc))
    except requests.RequestException:
        log.warning("Availability failed for %s", request.user, exc_info=True)
        return _json_error("Couldn't reach Google Calendar. Try again.", 502)
    return JsonResponse(data)


@team_only
def people(request):
    return JsonResponse({"results": service.search_people(request.user, request.GET.get("q", "")),
                         "team": service.team(request.user)})


@team_only
@require_POST
def create(request):
    if not connect.can_schedule(request.user):
        return _json_error(NEEDS_PERMISSION, 403, needs_permission=True)
    try:
        return JsonResponse(service.create_meeting(request.user, request.POST))
    except gcal.ScopeMissing:
        connect.forget(request.user)
        return _json_error(NEEDS_PERMISSION, 403, needs_permission=True)
    except (service.ScheduleError, gcal.CalendarError) as exc:
        return _json_error(str(exc))
    except requests.RequestException:
        log.warning("Creating a meeting failed for %s", request.user, exc_info=True)
        return _json_error("Couldn't reach Google Calendar. Press Send again: it won't invite anyone twice.", 502)


@team_only
def open_in_google(request):
    """Google Calendar's own new-event screen, filled in, for someone who hasn't allowed scheduling (or doesn't want to)."""
    try:
        fields = service.meeting_fields(request.user, request.GET)
    except service.ScheduleError as exc:
        messages.error(request, str(exc))
        return redirect("crm:schedule")
    return redirect(service.google_link(fields))


@team_only
@require_POST
def allow(request):
    if not settings.GOOGLE_SIGNIN_ENABLED or not gcal.connected(request.user):
        messages.error(request, "Connect Google Calendar first (Calendar page).")
        return redirect("crm:schedule")
    settings_nav.remember(request)
    request.session[connect.SESSION_KEY] = True
    return redirect(connect.connect_url(request))


@team_only
def allowed(request):
    """Where Google sends the person back after Allow scheduling."""
    if not request.session.pop(connect.SESSION_KEY, False):
        return settings_nav.back(request, "crm:schedule")
    try:
        connect.finish(request.user)
    except (connect.ScheduleConnectError, requests.RequestException) as exc:
        text = str(exc) if isinstance(exc, connect.ScheduleConnectError) else "Couldn't reach Google. Try again."
        messages.error(request, text)
        return settings_nav.back(request, "crm:schedule")
    messages.success(request, "Done. Meetings you schedule here go on your Google Calendar and Google emails the invitations.")
    return settings_nav.back(request, "crm:schedule")
