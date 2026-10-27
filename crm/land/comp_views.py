"""Sale comps pages: the comps table and map (/land/comps/, ?format=csv to download), adding and editing a team comp,
and a comp's broker flyer. Team pages only (capital partners never reach land search)."""
import csv
from datetime import date

from django import forms
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import LandCounty, LandSite, Parcel, SaleComp
from . import comps

INLINE_TYPES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp")


class CompForm(forms.ModelForm):
    class Meta:
        model = SaleComp
        fields = ["sale_date", "price", "acres", "county", "place", "buyer", "seller", "arms_length", "conveyance",
                  "deed", "improved", "parcels_count", "lat", "lon", "parcel", "site", "source_note", "notes",
                  "internal_only", "flyer"]
        widgets = {
            "sale_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "arms_length": forms.NullBooleanSelect(),
            "parcel": forms.HiddenInput(), "site": forms.HiddenInput(),
        }
        labels = {"price": "Price ($)", "lat": "Latitude", "lon": "Longitude", "arms_length": "Arms-length sale?",
                  "parcels_count": "Parcels in the sale", "source_note": "Source (deed, broker, flyer...)",
                  "notes": "Notes (team only)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["county"].queryset = LandCounty.objects.order_by("state", "name")
        self.fields["county"].required = False

    def clean(self):
        data = super().clean()
        if data.get("price") is not None and data["price"] <= 0:
            self.add_error("price", "Enter the sale price.")
        if data.get("acres") is not None and data["acres"] <= 0:
            self.add_error("acres", "Enter the acres sold.")
        if data.get("sale_date") and data["sale_date"] > date.today():
            self.add_error("sale_date", "A sale date can't be in the future.")
        if (data.get("lat") is None) != (data.get("lon") is None):
            self.add_error("lon", "Give both latitude and longitude, or neither.")
        if not (data.get("county") or data.get("lat") is not None or data.get("parcel")):
            self.add_error("county", "Pick the county (or give a location) so the comp shows up near other land.")
        return data


def _rows(found):
    return [{"c": c, "url": reverse("crm:land_comp_edit", args=[c.pk])} for c in found]


def comps_page(request):
    kwargs, center, county = comps.from_params(request.GET)
    found = comps.find(**kwargs) if (center or county or request.GET.get("all")) else []
    if request.GET.get("format") == "csv":
        return _csv(found)
    summary = comps.summarize(found)
    texas = (center and center["county"] and center["county"].state == "TX") or (county and county.state == "TX")
    pins = [{"lat": c.lat, "lon": c.lon, "ppa": round(c.price_per_acre), "acres": round(c.acres, 1),
             "date": c.sale_date.isoformat(), "url": reverse("crm:land_comp_edit", args=[c.pk]),
             "source": c.source} for c in found if c.lat is not None]
    return render(request, "crm/land_comps.html", {
        "rows": _rows(found), "summary": summary, "center": center, "county": county, "texas": texas,
        "texas_note": comps.TEXAS_NOTE, "pins": pins, "params": request.GET, "query": request.GET.urlencode(),
        "counties": LandCounty.objects.order_by("state", "name"),
        "sites": LandSite.objects.exclude(status=LandSite.Status.DROPPED).order_by("name"),
        "searched": bool(center or county or request.GET.get("all")), "min_comps": comps.MIN_COMPS,
    })


def _csv(found):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="Sale_Comps_{date.today():%Y-%m-%d}.csv"'
    w = csv.writer(resp)
    w.writerow(["Sale date", "County", "State", "Place", "Acres", "Price", "Price per acre", "Miles", "Buyer", "Seller",
                "Arms-length", "How conveyed", "Deed", "With buildings", "Parcels in sale", "Source", "Source note",
                "Latitude", "Longitude"])
    for c in found:
        w.writerow([c.sale_date.isoformat(), c.county.name if c.county else "", c.county.state if c.county else "",
                    c.place, round(c.acres, 2), c.price, round(c.price_per_acre), "" if c.miles is None else round(c.miles, 2),
                    c.buyer, c.seller, {True: "yes", False: "no"}.get(c.arms_length, "not known"), c.conveyance,
                    c.deed, "yes" if c.improved else "", c.parcels_count, c.get_source_display(), c.source_note,
                    "" if c.lat is None else round(c.lat, 6), "" if c.lon is None else round(c.lon, 6)])
    return resp


def comp_edit(request, pk=None):
    comp = get_object_or_404(SaleComp, pk=pk) if pk else None
    county_record = comp is not None and comp.source == SaleComp.Source.COUNTY
    if request.method == "POST":
        if county_record:
            # County comps come from the records: only the team's notes, site link and flyer can change.
            comp.notes = request.POST.get("notes", comp.notes)
            comp.internal_only = bool(request.POST.get("internal_only"))
            site = LandSite.objects.filter(pk=request.POST.get("site") or 0).first()
            comp.site = site
            if request.FILES.get("flyer"):
                comp.flyer = request.FILES["flyer"]
            comp.save()
            messages.success(request, "Saved.")
            return redirect("crm:land_comp_edit", pk=comp.pk)
        form = CompForm(request.POST, request.FILES, instance=comp)
        if form.is_valid():
            c = form.save(commit=False)
            c.source = SaleComp.Source.TEAM
            if c.parcel and c.lat is None:
                c.lat, c.lon = c.parcel.lat, c.parcel.lon
            if c.parcel and not c.county:
                c.county = c.parcel.county
            if not c.created_by_id:
                c.created_by = request.user
            c.save()
            messages.success(request, f"Saved the comp: {c.acres:,.1f} ac at ${c.price_per_acre:,.0f}/ac.")
            return redirect(request.POST.get("next") or reverse("crm:land_comps") + _back_query(c))
    else:
        initial = {}
        parcel = Parcel.objects.select_related("county").filter(pk=request.GET.get("parcel") or 0).first() \
            if str(request.GET.get("parcel") or "").isdigit() else None
        site = LandSite.objects.filter(pk=request.GET.get("site") or 0).first() \
            if str(request.GET.get("site") or "").isdigit() else None
        if parcel and not comp:
            initial = {"parcel": parcel.pk, "county": parcel.county_id, "acres": round(parcel.acres, 2),
                       "lat": round(parcel.lat, 6), "lon": round(parcel.lon, 6), "seller": parcel.owner_name,
                       "place": parcel.situs_address or parcel.legal_desc[:200]}
        if site and not comp:
            c = comps.site_center(site)
            initial.update({"site": site.pk, **({"county": c[3].pk, "acres": round(c[2], 2), "lat": round(c[0], 6),
                                                 "lon": round(c[1], 6), "place": site.name} if c and not parcel else {})})
        form = CompForm(instance=comp, initial=initial)
    return render(request, "crm/land_comp_form.html", {
        "form": form, "comp": comp, "county_record": county_record,
        "sites": LandSite.objects.exclude(status=LandSite.Status.DROPPED).order_by("name"),
        "texas_note": comps.TEXAS_NOTE, "next": request.GET.get("next", ""),
    })


def _back_query(c):
    if c.parcel_id:
        return f"?parcel={c.parcel_id}"
    if c.site_id:
        return f"?site={c.site_id}"
    return f"?county={c.county.fips}" if c.county else "?all=1"


@require_POST
def comp_delete(request, pk):
    comp = get_object_or_404(SaleComp, pk=pk)
    if comp.source == SaleComp.Source.COUNTY:
        messages.error(request, "County comps come from the county's records and come back on the next refresh. "
                                "Mark it 'keep off outside reports' instead.")
        return redirect("crm:land_comp_edit", pk=comp.pk)
    back = _back_query(comp)
    comp.delete()
    messages.success(request, "Deleted the comp.")
    return redirect(reverse("crm:land_comps") + back)


def comp_flyer(request, pk):
    comp = get_object_or_404(SaleComp, pk=pk)
    if not comp.flyer:
        raise Http404
    try:
        fh = comp.flyer.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("That file is no longer on the server.")
    name = comp.flyer.name.rsplit("/", 1)[-1]
    return FileResponse(fh, as_attachment=not name.lower().endswith(INLINE_TYPES), filename=name)
