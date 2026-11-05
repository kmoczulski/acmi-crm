"""The LOI / PSA terms tracker's pages (crm/contracts/terms.py is the engine): a contract's rounds side by side with the
dates and deposits they produce, adding and editing a round with a live preview, marking the agreed round, and loading
a team "LOI Tracker" workbook (crm/contracts/terms_import.py)."""
from django.contrib import messages
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import Contract, TermsRound
from ..permissions import is_manager, refuse
from . import terms, terms_import, timeline
from .forms import TermsImportForm, TermsRoundForm

MAX_SHOWN = 4  # rounds side by side by default; more are one click away


def _contract(pk):
    return get_object_or_404(Contract.objects.select_related("site", "campus", "owner"), pk=pk)


def _back(contract):
    return redirect(reverse("crm:contract_terms", args=[contract.pk]))


def terms_panel(contract):
    """What the contract page shows of the tracker: the agreed round (else the latest) and its headline numbers. None
    when there is nothing to show and the contract isn't a land purchase (LOI, PSA, option)."""
    rounds = list(contract.term_rounds.all())
    if not rounds and contract.kind not in (Contract.Kind.LOI, Contract.Kind.PSA, Contract.Kind.OPTION):
        return None
    pick = next((r for r in rounds if r.agreed), None) or (rounds[-1] if rounds else None)
    return {"count": len(rounds), "round": pick, "agreed": bool(pick and pick.agreed),
            "head": pick.schedule()["head"] if pick else None}


def next_round_defaults(rounds, today):
    """What the next round probably is: ACMI's first offer, then the seller's redline, then our counter, and so on."""
    if not rounds:
        return {"label": "ACMI initial offer", "sent_by": TermsRound.Side.US, "dated": today}
    side = TermsRound.Side.THEM if rounds[-1].sent_by == TermsRound.Side.US else TermsRound.Side.US
    base = "Seller redline" if side == TermsRound.Side.THEM else "ACMI counter"
    n = sum(1 for r in rounds if r.label.startswith(base)) + 1
    return {"label": base if n == 1 else f"{base} {n}", "sent_by": side, "dated": today}


def contract_terms(request, pk):
    """The rounds side by side: where each side stands, the dates and deposits each proposal produces, and what the
    inputs are."""
    contract = _contract(pk)
    today = timezone.localdate()
    rounds = list(contract.term_rounds.select_related("document", "created_by"))
    wanted = set(request.GET.getlist("r"))
    shown = [r for r in rounds if str(r.pk) in wanted] if wanted else rounds[-MAX_SHOWN:]
    cols = []
    for r in shown:
        res = r.schedule()
        cols.append({"round": r, "res": res, "head": res["head"]})
    results = [c["res"] for c in cols]
    # Where the two sides stand: the latest round from each, if both have made one.
    us = next((c for c in reversed(cols) if c["round"].sent_by == TermsRound.Side.US), None)
    them = next((c for c in reversed(cols) if c["round"].sent_by == TermsRound.Side.THEM), None)
    versions = [{"id": str(c["round"].pk), "label": c["round"].label, "who": c["round"].get_sent_by_display(),
                 "rows": terms.lane_rows(c["res"])} for c in cols]
    whens = [r["when"] for v in versions for r in v["rows"]]
    lanes, ticks = timeline.lanes(versions, (min(whens), max(whens)) if whens else (None, None), today)
    return render(request, "crm/contract_terms.html", {
        "contract": contract, "rounds": rounds, "cols": cols, "today": today,
        "picked": [r.pk for r in shown], "events": terms.compare_events(results),
        "phase_names": {ph["n"]: ph["name"] for res in results for ph in res["phases"]},
        "grid": terms.compare_terms([r.terms for r in shown]),
        "gap": terms.gap(us["head"], them["head"]) if us and them else [], "us": us, "them": them,
        "lanes": lanes, "ticks": terms.thin_ticks(ticks), "agreed": next((r for r in rounds if r.agreed), None),
        "import_form": TermsImportForm(), "can_manage": is_manager(request.user),
        "warnings": [(c["round"], w) for c in cols for w in c["res"]["warnings"]],
    })


def contract_terms_edit(request, pk, rid=None):
    """Add a round (starting from the last one, or from ?from=<round>) or edit one."""
    contract = _contract(pk)
    instance = get_object_or_404(TermsRound, pk=rid, contract=contract) if rid else None
    rounds = list(contract.term_rounds.all())
    source = initial = None
    if instance is None:
        source = next((r for r in rounds if str(r.pk) == request.GET.get("from")), None) or (rounds[-1] if rounds else None)
        initial = next_round_defaults(rounds, timezone.localdate())
    if request.method == "POST":
        form = TermsRoundForm(request.POST, contract=contract, instance=instance)
        if form.is_valid():
            r = instance or TermsRound(contract=contract, created_by=request.user)
            r.label, r.sent_by, r.dated = form.cleaned_data["label"], form.cleaned_data["sent_by"], form.cleaned_data["dated"]
            r.document, r.notes, r.terms = form.cleaned_data["document"], form.cleaned_data["notes"], form.terms()
            if instance is None:
                r.position = (contract.term_rounds.aggregate(m=Max("position"))["m"] or 0) + 1
            r.save()
            contract.save(update_fields=["updated_at"])
            messages.success(request, f"Saved {r.label}.")
            return _back(contract)
    else:
        form = TermsRoundForm(contract=contract, instance=instance, source=source, initial=initial)
    return render(request, "crm/contract_terms_edit.html", {
        "contract": contract, "form": form, "round": instance, "source": source if instance is None else None,
    })


@require_POST
def contract_terms_preview(request, pk):
    """The schedule for the form as it stands (the edit page asks as you type), as an HTML fragment."""
    contract = _contract(pk)
    data = request.POST.copy()
    if not data.get("label"):
        data["label"] = "Preview"
    form = TermsRoundForm(data, contract=contract)
    if not form.is_valid():
        problems = [f"{form.fields[name].label if name in form.fields else name}: {' '.join(errs)}"
                    for name, errs in form.errors.items()]
        return render(request, "crm/partials/terms_preview.html", {"problems": problems[:4]})
    res = terms.schedule(form.terms(), form.cleaned_data["dated"])
    return render(request, "crm/partials/terms_preview.html", {"res": res})


@require_POST
def contract_terms_delete(request, pk, rid):
    contract = _contract(pk)
    r = get_object_or_404(TermsRound, pk=rid, contract=contract)
    if not (is_manager(request.user) or r.created_by_id == request.user.pk):
        return refuse(request)
    r.delete()
    messages.success(request, f"Deleted {r.label}.")
    return _back(contract)


@require_POST
def contract_terms_agree(request, pk, rid):
    """Mark the round both sides settled on (one per contract), or take the mark off."""
    contract = _contract(pk)
    r = get_object_or_404(TermsRound, pk=rid, contract=contract)
    with transaction.atomic():
        if r.agreed:
            r.agreed = False
            messages.success(request, f"{r.label} is no longer marked agreed.")
        else:
            contract.term_rounds.exclude(pk=r.pk).update(agreed=False)
            r.agreed = True
            messages.success(request, f"{r.label} is the agreed round.")
        r.save(update_fields=["agreed", "updated_at"])
    return _back(contract)


@require_POST
def contract_terms_import(request, pk):
    """Rounds from a team LOI Tracker workbook: one column per round becomes one round."""
    contract = _contract(pk)
    form = TermsImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, " ".join(" ".join(v) for v in form.errors.values()))
        return _back(contract)
    upload = form.cleaned_data["file"]
    try:
        found = terms_import.read_workbook(upload)
    except terms_import.ImportProblem as exc:
        messages.error(request, str(exc))
        return _back(contract)
    pos = contract.term_rounds.aggregate(m=Max("position"))["m"] or 0
    for row in found["rounds"]:
        pos += 1
        TermsRound.objects.create(
            contract=contract, label=row["label"], sent_by=row["sent_by"], dated=row["dated"], terms=row["terms"],
            notes=f"Imported from {upload.name}.", position=pos, created_by=request.user)
    contract.save(update_fields=["updated_at"])
    count = len(found["rounds"])
    messages.success(request, f"Added {count} round{'s' if count != 1 else ''} from {upload.name}. Check the numbers "
                              "against the workbook." + (f" Not read: {'; '.join(found['skipped'][:6])}."
                                                         if found["skipped"] else ""))
    return _back(contract)
