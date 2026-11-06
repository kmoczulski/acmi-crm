"""The File to picker's two endpoints: what it offers for a search, and filing one agreement (crm/contracts/filing.py)."""
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from ..models import Contract
from . import dates, filing


def _safe_next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next")
    ok = nxt and nxt.startswith("/") and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return nxt if ok else fallback


def file_options(request):
    """JSON for the picker: tenants and deals (from two letters), projects, land sites and campuses."""
    return JsonResponse({"options": [
        {"kind": kind, "pk": pk, "name": name, "detail": detail}
        for kind, pk, name, detail in filing.options(request.GET.get("q", ""))]})


@require_POST
def contract_file_to(request, pk):
    """File one agreement: the posted company, opportunity, project, site and campus ids (a deal brings its company
    and campus, a site its campus and project), and `clear` names places to take it off."""
    contract = get_object_or_404(Contract.objects.select_related(
        "site__project", "campus", "project", "company", "opportunity__company", "opportunity__campus"), pk=pk)
    clear = [name for name in request.POST.getlist("clear") if name in filing.FIELDS]
    picks = filing.picks(request.POST)
    changed = filing.file_to(contract, clear=clear, **picks)
    if "company" in picks and "opportunity" not in picks:
        # "File to this tenant" also finds the rest the way a dropped file does: the company's only deal, then the
        # campus's own project, when each is the only one.
        changed = sorted(set(changed) | set(filing.auto_file(contract)))
    if "site" in changed:
        dates.sync_site(contract, request.user)  # the site's deal timeline follows the agreement's dates
    where = filing.summary(contract)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"ok": True, "changed": changed, "filed": where, "missing": filing.missing(contract)})
    if not changed:
        messages.info(request, "Nothing changed.")
    elif where:
        messages.success(request, f"Filed to {where}.")
    else:
        messages.success(request, "Taken off its tenant and project.")
    return redirect(_safe_next(request, contract.get_absolute_url()))
