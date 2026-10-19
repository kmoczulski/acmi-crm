"""The Sites page pop-up card (/land/sites/<id>/card/): GET draws it; POSTs from inside it (comment, checklist,
stage, Claude summary) change the site and draw it again, so the card updates in place."""
from datetime import date, time

from django.contrib.auth import get_user_model
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render

from ..models import LandSite, Task
from . import site_card


def _date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _time(raw):
    try:
        return time.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def site_card_view(request, pk):
    from .views import _set_status

    site = get_object_or_404(LandSite.objects.select_related("campus", "project"), pk=pk)
    error = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "comment":
            text = (request.POST.get("text") or "").strip()
            if text:
                site_card.add_comment(site, request.user, text)
        elif action == "people":
            site_card.save_people(site, request.POST, request.user)
        elif action in ("tenant_link", "tenant_unlink"):
            site_card.link_tenant(site, request.POST.get("opportunity"), request.user, unlink=action == "tenant_unlink")
        elif action == "task_add":
            title = (request.POST.get("title") or "").strip()
            if title:
                assignee = get_user_model().objects.filter(pk=request.POST.get("assignee") or 0, is_active=True).first()
                site_card.add_task(site, request.user, title, _date(request.POST.get("due_date")),
                                   _time(request.POST.get("due_time")), assignee, request.POST.get("label", ""))
        elif action == "task_toggle":
            t = get_object_or_404(Task, pk=request.POST.get("task"), site=site)
            from django.utils import timezone

            t.done = not t.done
            t.done_at = timezone.now() if t.done else None
            t.save(update_fields=["done", "done_at"])
        elif action == "status":
            _set_status(site, request.POST.get("status"), request.user)
        elif action == "summary":
            try:
                site_card.generate(site)
            except site_card.Unavailable as exc:
                error = str(exc)
        else:
            return HttpResponseBadRequest("Unknown action")
        site.refresh_from_db()
    ctx = site_card.context(site)
    ctx.update({"error": error, "statuses": LandSite.Status.choices, "today": date.today(),
                "users": get_user_model().objects.filter(is_active=True).order_by("first_name", "username")})
    return render(request, "crm/partials/site_card.html", ctx)


def site_url(site):
    from django.urls import reverse

    return reverse("crm:land_sites_page") + f"?card={site.pk}"
