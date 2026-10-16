from django.contrib import messages
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from crm.forms import active_users
from crm.models import NewsItem, NewsScan
from crm.projects import company_projects

from . import review
from .scan_parser import CORE_SECTIONS


def news_home(request):
    return render(request, "crm/news_home.html", {
        "files": review.available_scans(),
        "folder": review.watch_folder(),
        "scans": NewsScan.objects.all()[:20],
    })


@require_POST
def news_import(request):
    path = request.POST.get("path", "")
    if path not in {f["path"] for f in review.available_scans()}:
        return HttpResponseBadRequest("Unknown scan file")
    try:
        scan = review.import_scan(path, user=request.user)
    except Exception as exc:  # unreadable file, changed scan format
        messages.error(request, f"Could not read that scan: {exc}")
        return redirect("crm:news_home")
    if scan.drafts_checked_at is None:
        review.check_drafts(scan, request.user)
    return redirect(scan)


def _ordered_sections(items):
    order = {name.lower(): i for i, name in enumerate(CORE_SECTIONS)}
    sections = {}
    for item in items:
        sections.setdefault(item.section or "Other", []).append(item)
    return sorted(sections.items(), key=lambda kv: order.get(kv[0].lower(), 99))


def news_review(request, pk):
    scan = get_object_or_404(NewsScan, pk=pk)
    items = list(scan.items.select_related("company", "owner"))
    board = [i for i in items if i.kind == NewsItem.Kind.BOARD]
    prospects = [i for i in items if i.kind == NewsItem.Kind.PROSPECT]
    history = review.history_for([i.company_id for i in items if i.company_id], scan)
    for i in items:
        i.history = history.get(i.company_id, [])[:4]
        i.projects = company_projects(i.company) if i.company_id else []
        i.people = review.item_recipients(i, request.user) if i.kind == NewsItem.Kind.BOARD else []
        i.research = i.kind == NewsItem.Kind.BOARD and review.research_needed(i, request.user)
    decided = sum(1 for i in board + prospects if i.decision)
    return render(request, "crm/news_review.html", {
        "scan": scan,
        "sections": _ordered_sections(board),
        "strong": [p for p in prospects if p.tier == "strong"],
        "watching": [p for p in prospects if p.tier != "strong"],
        "weak": [i for i in items if i.kind == NewsItem.Kind.WEAK],
        "counts": {
            "proceed": sum(i.decision == "proceed" for i in board), "decline": sum(i.decision == "decline" for i in board),
            "pursue": sum(i.decision == "pursue" for i in prospects), "pass": sum(i.decision == "pass" for i in prospects),
            "left": len(board) + len(prospects) - decided, "drafts": sum(bool(i.draft_uid) for i in board),
            "research": sum(i.research for i in board),
        },
        "users": active_users(),
        "locked": scan.status == NewsScan.Status.APPLIED,
    })


@require_POST
def news_item_save(request, pk):
    """Autosave from a review card: decision, note, owner, draft edits."""
    item = get_object_or_404(NewsItem.objects.select_related("scan"), pk=pk)
    if item.scan.status == NewsScan.Status.APPLIED:
        return JsonResponse({"ok": False, "error": "This scan has already been applied."}, status=409)
    data = request.POST
    if "decision" in data:
        allowed = {"", "proceed", "decline"} if item.kind == NewsItem.Kind.BOARD else {"", "pursue", "pass"}
        if data["decision"] not in allowed:
            return HttpResponseBadRequest("Bad decision")
        item.decision = data["decision"]
    if "note" in data:
        item.note = data["note"][:5000]
    if "owner" in data:
        item.owner_id = int(data["owner"]) if data["owner"].isdigit() else None
    if data.get("revert"):
        item.edited, item.edit_to, item.edit_subject, item.edit_html = False, "", "", ""
    elif any(k in data for k in ("edit_to", "edit_subject", "edit_html")):
        if not item.edited:
            item.edit_to, item.edit_subject, item.edit_html = item.draft_to, item.draft_subject, item.draft_html
        item.edit_to = data.get("edit_to", item.edit_to)
        item.edit_subject = data.get("edit_subject", item.edit_subject)[:500]
        if "edit_html" in data:
            from .drafts import sanitize_html

            item.edit_html = sanitize_html(data["edit_html"])
        item.edited = (item.edit_to, item.edit_subject, item.edit_html) != (item.draft_to, item.draft_subject,
                                                                             item.draft_html)
    item.save()
    return JsonResponse({"ok": True, "decision": item.decision, "edited": item.edited})


@require_POST
def news_bulk(request, pk):
    scan = get_object_or_404(NewsScan, pk=pk, status=NewsScan.Status.OPEN)
    action = request.POST.get("action")
    board = scan.items.filter(kind=NewsItem.Kind.BOARD)
    if action == "proceed_all":
        board.filter(decision="").update(decision=NewsItem.Decision.PROCEED)
    elif action == "clear":
        scan.items.update(decision="")
    return redirect(scan)


@require_POST
def news_check_drafts(request, pk):
    scan = get_object_or_404(NewsScan, pk=pk, status=NewsScan.Status.OPEN)
    review.check_drafts(scan, request.user)
    if scan.drafts_error:
        messages.error(request, scan.drafts_error)
    else:
        messages.success(request, "Gmail drafts re-checked.")
    return redirect(scan)


def news_plan(request, pk):
    scan = get_object_or_404(NewsScan, pk=pk)
    if request.method == "POST":
        if scan.status == NewsScan.Status.APPLIED:
            messages.info(request, "Already applied.")
        else:
            results = review.apply(scan, request.user)
            if results.get("gmail_errors"):
                messages.error(request, "Some Gmail steps failed: " + "; ".join(results["gmail_errors"][:3]))
            messages.success(request, "Applied.")
        return redirect("crm:news_plan", pk=scan.pk)
    return render(request, "crm/news_plan.html", {
        "scan": scan,
        "plan": review.build_plan(scan, request.user) if scan.status == NewsScan.Status.OPEN else None,
        "applied_items": scan.items.exclude(result={}).select_related("company") if scan.results else [],
    })
