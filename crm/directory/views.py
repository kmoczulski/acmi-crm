"""The directory (/directory/): lenders and investors, consultants, lawyers, engineers, contractors and vendors, one
firm's page, moving a Company over, and filing a review-queue domain here."""
from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..capital import logic as capital_logic
from ..models import Company, Firm, UnmatchedEmail
from ..permissions import is_manager, refuse
from . import logic
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
            firm.contacts = [c for n, c in enumerate(firm.contacts or []) if n != i]
            firm.save()
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
    return render(request, "crm/company_move.html", {"company": company, "pv": preview, "kinds": Firm.Kind.choices})


@require_POST
def file_domain(request):
    """From the mail review queue: this domain is a consultant, vendor or lender, not a prospect."""
    domain = (request.POST.get("domain") or "").strip().lower()
    if not domain or not UnmatchedEmail.objects.filter(domain=domain).exists():
        messages.error(request, "That domain isn't in the review queue.")
        return redirect("crm:mail")
    firm, moved = logic.file_domain(domain, request.POST.get("kind", ""), request.POST.get("name", ""), request.user)
    messages.success(request, f"{domain} → {firm.name} in the directory: {moved} email{'s' if moved != 1 else ''} "
                              "filed there.")
    return redirect("crm:mail")
