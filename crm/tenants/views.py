"""Site fit for a tenant deal (/opportunities/<id>/fit/) and the rent roll of signed leases (/leases/)."""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ..land import qd
from ..models import Campus, Opportunity, TenantNeeds
from . import fit, leases

MAX_CASES = 12


def opportunity_fit(request, pk):
    opp = get_object_or_404(Opportunity.objects.select_related("company", "campus"), pk=pk)
    needs = TenantNeeds.objects.filter(opportunity=opp).first()
    if request.method == "POST":
        needs = needs or TenantNeeds(opportunity=opp)
        cases = []
        for i in range(MAX_CASES):
            hd = request.POST.get(f"hd_{i}", "")
            try:
                newqd = float((request.POST.get(f"newqd_{i}") or "0").replace(",", ""))
                sqft = float((request.POST.get(f"sqft_{i}") or "0").replace(",", ""))
            except ValueError:
                messages.error(request, f"Row {i + 1}: the explosive weight and square feet must be numbers.")
                return redirect(reverse("crm:opportunity_fit", args=[opp.pk]))
            if newqd > 0 and hd in qd.HAZARD_DIVISIONS:
                kind = request.POST.get(f"kind_{i}", "operating")
                cases.append({"name": (request.POST.get(f"name_{i}") or "").strip()[:80] or f"Building {i + 1}",
                              "hd": hd, "newqd": newqd, "sqft": sqft,
                              "kind": kind if kind in qd.BUILDING_KINDS else "operating"})
        needs.qd_cases = cases
        needs.needs_rail = request.POST.get("needs_rail") == "1"
        needs.needs_gas = request.POST.get("needs_gas") == "1"
        needs.notes = (request.POST.get("notes") or "").strip()
        needs.save()
        messages.success(request, "Saved the program's needs.")
        return redirect(reverse("crm:opportunity_fit", args=[opp.pk]))
    everywhere = request.GET.get("everywhere") == "1"
    rows = fit.rank(opp, needs, everywhere=everywhere)
    blds = fit.buildings(needs)
    cases = list(needs.qd_cases) if needs else []
    return render(request, "crm/opportunity_fit.html", {
        "opp": opp, "needs": needs, "rows": rows, "everywhere": everywhere, "buildings": blds,
        "cases": cases + [{}] * max(1, 3 - len(cases)), "hds": qd.HAZARD_DIVISIONS, "kinds": qd.BUILDING_KINDS,
    })


def rent_roll(request):
    campus = Campus.objects.filter(code__iexact=request.GET.get("campus", "")).first()
    return render(request, "crm/rent_roll.html", {
        "roll": leases.rent_roll(campus), "campus": campus, "campuses": Campus.objects.all(),
    })
