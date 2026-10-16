"""Client land-search assignments: internal pages and the private guest share-link pages."""
import json
from datetime import date

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..models import Assignment, AssignmentSite, LandSite
from . import assignments as A


def assignment_list(request):
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:200]
        if not name:
            messages.error(request, "Give the assignment a name.")
        else:
            a = Assignment.objects.create(name=name, client=(request.POST.get("client") or "").strip()[:200],
                                          created_by=request.user)
            return redirect(a)
    rows = Assignment.objects.prefetch_related("entries")
    return render(request, "crm/assignment_list.html", {"assignments": rows})


def _share_url(request, a):
    from django.urls import reverse

    return request.build_absolute_uri(reverse("crm:guest_home", args=[a.share_token])) if a.share_token else ""


def assignment_detail(request, pk):
    a = get_object_or_404(Assignment, pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "details":
            a.name = (request.POST.get("name") or a.name).strip()[:200]
            a.client = (request.POST.get("client") or "").strip()[:200]
            a.program = (request.POST.get("program") or "").strip()[:300]
            a.demand_driver = (request.POST.get("demand_driver") or "").strip()[:200]
            floor = A._num(request.POST.get("acreage_floor"))
            a.acreage_floor = floor
            a.guest_intro = request.POST.get("guest_intro", "").strip()
            a.notes = request.POST.get("notes", "").strip()
            a.save()
            messages.success(request, "Saved.")
        elif action == "add_site":
            site = LandSite.objects.filter(pk=request.POST.get("site") or 0).first()
            if site:
                AssignmentSite.objects.get_or_create(assignment=a, site=site)
                messages.success(request, f"Added {site.name}.")
        elif action == "remove_site":
            AssignmentSite.objects.filter(assignment=a, pk=request.POST.get("entry") or 0).delete()
        elif action == "entry":
            e = AssignmentSite.objects.filter(assignment=a, pk=request.POST.get("entry") or 0).first()
            if e:
                pin = request.POST.get("rank_override", "").strip()
                e.rank_override = int(pin) if pin.isdigit() and int(pin) > 0 else None
                e.show_to_guest = bool(request.POST.get("show_to_guest"))
                e.save(update_fields=["rank_override", "show_to_guest", "updated_at"])
        elif action in ("share_on", "share_regen"):
            if action == "share_regen" or not a.share_token:
                a.share_token = A.new_token()
            a.share_enabled = True
            raw = (request.POST.get("share_expires") or "").strip()
            try:
                a.share_expires = date.fromisoformat(raw) if raw else None
            except ValueError:
                a.share_expires = None
            a.save()
            messages.success(request, "Share link is on." if action == "share_on" else "New link made; the old one no longer works.")
        elif action == "share_off":
            a.share_enabled = False
            a.save(update_fields=["share_enabled", "updated_at"])
            messages.success(request, "Share link switched off.")
        return redirect(a)

    ranked = A.rank(a)
    used = a.entries.values_list("site_id", flat=True)
    return render(request, "crm/assignment_detail.html", {
        "a": a, "ranked": ranked, "rule": A.RANKING_RULE, "share_url": _share_url(request, a),
        "available": LandSite.objects.exclude(pk__in=used).exclude(status="dropped").order_by("name"),
        "categories": A.CATEGORIES, "today": date.today(),
    })


def _entry(a, entry_pk):
    return get_object_or_404(AssignmentSite.objects.select_related("site", "assignment"), pk=entry_pk, assignment=a)


def _map_json(site):
    """Site outline as a GeoJSON dict for the report map (None if it cannot be drawn)."""
    from shapely.geometry import mapping

    from . import trim

    try:
        geom = trim.site_geometry(site)
        return mapping(geom) if geom is not None and not geom.is_empty else None
    except Exception:  # noqa: BLE001
        return None


def assignment_report(request, pk, entry_pk):
    """Internal view of one site's report, exactly as the client sees it plus the edit link."""
    a = get_object_or_404(Assignment, pk=pk)
    e = _entry(a, entry_pk)
    ctx = A.build(e, a)
    return render(request, "crm/assignment_report.html", {
        "a": a, "c": ctx, "map_geojson": _map_json(e.site), "guest": False, "internal_notes": e.site.notes,
    })


def assignment_edit(request, pk, entry_pk):
    a = get_object_or_404(Assignment, pk=pk)
    e = _entry(a, entry_pk)
    if request.method == "POST":
        e.report = A.parse_form(request.POST)
        e.save(update_fields=["report", "updated_at"])
        messages.success(request, "Report saved.")
        return redirect("crm:assignment_report", pk=a.pk, entry_pk=e.pk)
    c = A.build(e, a)
    rep = c["report"]
    cats = {x["key"]: x for x in c["cats"]}
    pre_by_label = dict(c["overview"])
    utilities = list(rep["utilities"]) or [{"utility": u} for u in A.UTILITIES]
    permits = list(rep["permits"]) or [{"approval": p} for p in A.PERMITS]
    rom = list(rep["rom"]) or [{"category": cat, "incentive": inc} for cat, inc in A.ROM]
    pad = lambda rows, n: rows + [{}] * max(0, n - len(rows))  # noqa: E731
    return render(request, "crm/assignment_edit.html", {
        "a": a, "e": e, "c": c, "rep": rep, "cats": [(k, l, cats[k]) for k, l in A.CATEGORIES], "ratings": A.RATINGS,
        "overview": [(k, l, rep["overview"].get(k, ""), pre_by_label.get(l, "")) for k, l in A.OVERVIEW],
        "sections": [(k, l, rep["sections"].get(k, "")) for k, l in A.SECTIONS],
        "utilities": list(enumerate(pad(utilities, len(A.UTILITIES) + 2))),
        "permits": list(enumerate(pad(permits, len(A.PERMITS) + 2))),
        "rom": list(enumerate(pad(rom, len(A.ROM) + 2))),
        "actions": list(enumerate(pad(list(rep["actions"]), 6))),
        "dates": list(enumerate(pad(list(rep["dates"]), 4))),
        "statuses": A.STATUSES, "incentive": A.INCENTIVE, "net": rep["net"],
    })


# ---- guest pages: no login, reached only through the share token ----

def _guest_response(request, template, ctx):
    resp = render(request, template, ctx)
    resp["X-Robots-Tag"] = "noindex, nofollow"
    resp["Cache-Control"] = "no-store"
    resp["Referrer-Policy"] = "no-referrer"
    return resp


def _touch(a):
    Assignment.objects.filter(pk=a.pk).update(guest_views=a.guest_views + 1, guest_last_view=timezone.now())


def guest_home(request, token):
    a = A.guest_assignment(token)
    if not a:
        raise Http404
    _touch(a)
    return _guest_response(request, "crm/guest_assignment.html", {
        "a": a, "ranked": A.rank(a, guest=True), "rule": A.RANKING_RULE, "categories": A.CATEGORIES, "token": token,
    })


def guest_report(request, token, entry_pk):
    a = A.guest_assignment(token)
    if not a:
        raise Http404
    e = get_object_or_404(AssignmentSite.objects.select_related("site"), pk=entry_pk, assignment=a, show_to_guest=True)
    ranked = A.rank(a, guest=True)
    position = next((i for i, c, _ in ranked if c["entry"].pk == e.pk), None)
    ctx = A.build(e, a)
    return _guest_response(request, "crm/guest_report.html", {
        "a": a, "c": ctx, "map_geojson": _map_json(e.site), "guest": True, "token": token,
        "position": position, "total": len(ranked),
    })
