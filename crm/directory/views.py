"""The directory (/directory/): lenders and investors, consultants, lawyers, engineers, contractors and vendors, one
firm's page, moving a Company over, and filing a review-queue domain here."""
from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from ..capital import logic as capital_logic
from ..ai import enabled as ai_enabled
from ..models import Company, DomainSort, Firm, UnmatchedEmail
from ..permissions import is_manager, refuse
from . import logic, sort
from .forms import FirmForm

GROUPS = {"capital": "Lenders and investors", "services": "Consultants and vendors"}


def firm_list(request):
    """Every firm, filtered to the capital side, the services side or one type, or by name or domain."""
    group = request.GET.get("group", "")
    kind = request.GET.get("kind", "")
    q = (request.GET.get("q") or "").strip()
    form = FirmForm(request.POST or None, initial={"kind": "bank" if group == "capital" else "consultant"})
    if request.method == "POST":
        if form.is_valid():
            firm = form.save(commit=False)
            firm.created_by = request.user
            firm.save()
            moved = logic.file_queued(firm)
            messages.success(request, f"Added {firm.name}." + (
                f" Filed {moved} queued email{'s' if moved != 1 else ''} on it." if moved else ""))
            return redirect(firm)
        messages.error(request, "Not added: " + " ".join(" ".join(v) for v in form.errors.values()))
    qs = Firm.objects.prefetch_related("sources__project", "team_roles__project")
    if kind:
        qs = qs.filter(kind=kind)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(domains__icontains=q.lower()) | Q(contacts__icontains=q))
    firms = [f for f in qs if not group or f.is_capital == (group == "capital")]
    last = logic.last_emails()
    for f in firms:
        f.last_email = last.get(f.pk)
    return render(request, "crm/directory.html", {
        "firms": firms, "form": form, "group": group, "kind": kind, "q": q, "groups": GROUPS,
        "title": GROUPS.get(group, "Directory"), "kinds": Firm.Kind.choices,
    })


def firm_detail(request, pk):
    firm = get_object_or_404(Firm, pk=pk)
    form = FirmForm(instance=firm)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "details":
            form = FirmForm(request.POST, instance=firm)
            if form.is_valid():
                firm = form.save()
                moved = logic.file_queued(firm)
                messages.success(request, "Saved." + (
                    f" Filed {moved} email{'s' if moved != 1 else ''} from the review queue on it." if moved else ""))
                return redirect(firm)
            messages.error(request, "Not saved: " + " ".join(" ".join(v) for v in form.errors.values()))
        elif action == "contact_add":
            if (request.POST.get("name") or "").strip():
                firm.contacts = (firm.contacts or []) + [{
                    k: (request.POST.get(k) or "").strip()[:200] for k in ("name", "title", "email", "phone")}]
                firm.save()
            return redirect(firm.get_absolute_url() + "#contacts")
        elif action == "contact_delete":
            try:
                i = int(request.POST.get("i", ""))
            except ValueError:
                i = -1
            # The index comes from the page, which may be out of date (another tab removed someone first), so it
            # only counts when the contact still there has the name and email the page showed.
            contacts = firm.contacts or []
            seen = [(request.POST.get(k) or "").strip() for k in ("name", "email")]
            if 0 <= i < len(contacts) and [(contacts[i].get(k) or "").strip() for k in ("name", "email")] == seen:
                firm.contacts = contacts[:i] + contacts[i + 1:]
                firm.save()
            else:
                messages.error(request, "That list changed. Nothing was removed.")
            return redirect(firm.get_absolute_url() + "#contacts")
        elif action == "delete":
            if not is_manager(request.user):
                return refuse(request)
            if firm.sources.exists():
                messages.error(request, "Remove its capital sources first.")
                return redirect(firm)
            firm.delete()
            messages.success(request, f"Deleted {firm.name}.")
            return redirect("crm:directory")
        else:
            return redirect(firm)
    today = timezone.localdate()
    sources = capital_logic.annotate(list(capital_logic.with_related(firm.sources.all())), today, touch=False)
    return render(request, "crm/firm_detail.html", {
        "p": firm, "form": form, "sources": sources,
        "team": firm.team_roles.select_related("project").order_by("project__name"),
        "touches": capital_logic.touches(None, today, provider=firm, limit=60),
        "upcoming": logic.upcoming_meetings(firm),
        "group": "capital" if firm.is_capital else "services",
        "groups": GROUPS,
        "filings": firm.sorts.filter(status=DomainSort.Status.FILED),
    })


def firm_names(request):
    """Firm names for a typed-name field (?group=capital for a capital source's provider)."""
    return JsonResponse(logic.names(request.GET.get("group", "")), safe=False)


def company_move(request, pk):
    """A Company that's really a consultant, vendor or lender: Managers move it into the directory, which takes it
    (and its people) out of outreach for good."""
    if not is_manager(request.user):
        return refuse(request)
    company = get_object_or_404(Company, pk=pk)
    preview = logic.move_preview(company)
    if request.method == "POST":
        try:
            firm, stats = logic.move_company(company, request.POST.get("kind", ""), request.user)
        except logic.MoveBlocked as e:
            messages.error(request, f"Not moved: it has {e}. Move or remove those first.")
            return redirect("crm:company_move", pk)
        counts = ", ".join(f"{n} {word}{'s' if n != 1 else ''}" for word, n in
                           (("contact", stats["contacts"]), ("email", stats["emails"]), ("note", stats["notes"])))
        messages.success(request, f"Moved {firm.name} to the directory: {counts}. The Company is deleted.")
        return redirect(firm)
    return render(request, "crm/company_move.html", {"company": company, "pv": preview, "kinds": Firm.Kind.choices,
                                                     "picked": request.GET.get("kind", "")})


@require_POST
def file_domain(request):
    """From the mail review queue: this domain is a consultant, vendor or lender, not a prospect."""
    domain = (request.POST.get("domain") or "").strip().lower()
    if not domain or not UnmatchedEmail.objects.filter(domain=domain).exists():
        messages.error(request, "That domain isn't in the review queue.")
        return redirect("crm:needs_filing")
    firm, moved = logic.file_domain(domain, request.POST.get("kind", ""), request.POST.get("name", ""), request.user)
    messages.success(request, f"{domain} → {firm.name} in the directory: {moved} email{'s' if moved != 1 else ''} "
                              "filed there.")
    return redirect("crm:needs_filing")


# ---------------------------------------------------------------------------
# Claude's sorting (sort.py): filings to undo, suggestions to take, Companies that look like vendors
# ---------------------------------------------------------------------------


def suggestions(request):
    """What Claude has filed from email lately (with Undo) and the Companies it thinks are vendors."""
    if request.method == "POST":
        if not is_manager(request.user):
            return refuse(request)
        try:
            out = sort.review_companies(limit=20)
        except sort.Unavailable as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"Claude checked {out['checked']} compan{'ies' if out['checked'] != 1 else 'y'}: "
                                      f"{out['flagged']} look{'s' if out['flagged'] == 1 else ''} like vendors.")
        return redirect("crm:firm_suggestions")
    return render(request, "crm/directory_suggestions.html", {
        "flags": sort.company_flags(), "filings": sort.recent_filings(days=30),
        "left": len(sort.company_candidates()), "can_manage": is_manager(request.user), "ai_ready": ai_enabled(),
    })


@require_POST
def sort_action(request, pk):
    """file (take Claude's suggestion for a queued domain), undo (an automatic filing) or dismiss."""
    s = get_object_or_404(DomainSort, pk=pk)
    action = request.POST.get("action", "")
    back = "crm:needs_filing" if request.POST.get("back") == "mail" else "crm:firm_suggestions"
    if action == "file" and s.company_id is None and s.status == DomainSort.Status.SUGGESTED:
        if not sort.waiting(s.domain):
            messages.error(request, f"{s.domain} isn't waiting in Needs filing any more.")
            return redirect(back)
        firm, moved = sort.file(s, request.user)
        messages.success(request, f"{s.domain} → {firm.name} in the directory: {moved} email{'s' if moved != 1 else ''}"
                                  " filed there.")
    elif action == "undo":
        if sort.undo(s, request.user):
            messages.success(request, f"Undone: {s.domain} is back in Needs filing.")
    elif action == "dismiss":
        if s.company_id and not is_manager(request.user):
            return refuse(request)
        s.status, s.decided_by, s.decided_at = DomainSort.Status.DISMISSED, request.user, timezone.now()
        s.save()
    return redirect(back)


# ---------------------------------------------------------------------------
# A name in an email (crm/static/crm/who.js): who it is, and adding them to the directory
# ---------------------------------------------------------------------------


def who(request):
    """The small card a name in an email opens: who the address is in the hub, else a form to add them here."""
    info = logic.who(request.GET.get("email", ""), request.GET.get("name", ""))
    return render(request, "crm/partials/who_card.html", {
        "w": info, "kinds": Firm.Kind.choices, "firm_names": logic.names() if not info["team"] else [],
        "picked_kind": info["domain_firm"].kind if info["domain_firm"] else ""})


@require_POST
def who_add(request):
    """Add the person to the directory as a contact at a firm (logic.add_contact)."""
    fetched = request.headers.get("X-Requested-With") == "fetch"
    try:
        firm, created, domain_added, moved = logic.add_contact(
            request.POST.get("email", ""), request.POST.get("name", ""), request.POST.get("kind", ""),
            request.POST.get("firm", ""), request.POST.get("title", ""), request.POST.get("phone", ""), request.user)
    except logic.ContactError as exc:
        if fetched:
            return JsonResponse({"error": str(exc)}, status=400)
        messages.error(request, str(exc))
    else:
        text = f"Added to {firm.name}{' (new in the directory)' if created else ''}."
        if domain_added:
            text += f" Email from {firm.domains[-1]} files there from now on"
            text += f"; {moved} waiting email{'s' if moved != 1 else ''} moved." if moved else "."
        if fetched:
            return JsonResponse({"ok": True, "message": text, "firm": firm.name, "url": firm.get_absolute_url()})
        messages.success(request, text)
    nxt = request.POST.get("next", "")
    ok = nxt.startswith("/") and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()})
    return redirect(nxt if ok else "crm:directory")
