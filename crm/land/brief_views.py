"""Site brief pages: one site (/land/sites/<id>/brief/), several side by side (/land/briefs/?ids=1,2,3), the aerial
images they print, and PDF downloads of both (?format=pdf)."""
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string

from ..models import LandSite
from . import briefs, listing, score


def _build(site, refresh=False):
    try:
        return briefs.build(site, refresh=refresh)
    except ValueError:
        return None


def _pdf(request, built, filename):
    """A PDF download of these briefs (cover page when there are several, notes page last), or back to the page."""
    try:
        for b in built:
            briefs.embed(b)
        html = render_to_string("crm/land_briefs_pdf.html", {
            "briefs": built, "definitions": briefs.definitions(built), "method": score.methodology(), "zoning": briefs.zoning_sections(built), "css": briefs.pdf_css(),
            "logo_data": briefs.logo_data_uri(), "title": filename.rsplit(".", 1)[0].replace("_", " ")})
        data = briefs.html_to_pdf(html)
    except briefs.PdfUnavailable as exc:
        messages.error(request, f"Couldn't make the PDF: {exc}. Print the page and choose Save as PDF instead.")
        params = request.GET.copy()
        params.pop("format", None)
        return redirect(request.path + ("?" + params.urlencode() if params else ""))
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def site_brief(request, pk):
    site = get_object_or_404(LandSite, pk=pk)
    if request.method == "POST":  # "Re-run live checks" (a POST, so a link elsewhere can't set off the agency calls)
        _build(site, refresh=True)
        return redirect(request.get_full_path())
    brief = _build(site)
    if brief and request.GET.get("format") == "pdf":
        return _pdf(request, [brief], briefs.export_name(site.name))
    return render(request, "crm/land_site_brief.html", {"brief": brief, "site": site,
                                                         "definitions": briefs.definitions([brief]) if brief else [],
                                                         "method": score.methodology(),
                                                         "zoning": briefs.zoning_sections([brief]) if brief else {}})


def briefs_page(request):
    """Comparison cover page plus a brief per site. ?ids=1,2,3 (default: every site that isn't dropped)."""
    ids = [int(i) for i in request.GET.get("ids", "").split(",") if i.strip().isdigit()]
    sites = LandSite.objects.filter(pk__in=ids) if ids else LandSite.objects.exclude(status=LandSite.Status.DROPPED)
    if request.method == "POST":  # "Re-run live checks"
        for s in sites:
            _build(s, refresh=True)
        return redirect(request.get_full_path())
    built = [b for b in (_build(s) for s in sites) if b]
    built.sort(key=lambda b: b["ranking"])
    if built and request.GET.get("format") == "pdf":
        return _pdf(request, built, briefs.export_name())
    return render(request, "crm/land_briefs.html", {"briefs": built, "ids": ",".join(str(b["site"].pk) for b in built),
                                                     "definitions": briefs.definitions(built), "method": score.methodology(),
                                                     "zoning": briefs.zoning_sections(built)})


def site_aerial(request, pk):
    site = get_object_or_404(LandSite, pk=pk)
    mode = "context" if request.GET.get("mode") == "context" else "site"
    geoms, outline = briefs.site_geometries(site)
    if outline is None:
        raise Http404("No parcels")
    size = briefs.CONTEXT_SIZE if mode == "context" else briefs.SITE_SIZE
    data = briefs.render_aerial(geoms, outline, mode=mode, size=size)
    resp = HttpResponse(data, content_type="image/jpeg")
    resp["Cache-Control"] = "private, max-age=600"
    return resp


def site_listing(request, pk):
    """Edit the flyer / listing facts for a site; anything left blank prints as N/A on the brief."""
    from .views import _log

    site = get_object_or_404(LandSite, pk=pk)
    if request.method == "POST":
        site.listing = listing.parse(request.POST)
        site.save(update_fields=["listing", "updated_at"])
        _log(site, request.user, "edit", "Listing details edited")
        messages.success(request, "Listing details saved.")
        return redirect("crm:land_site_brief", pk=site.pk)
    return render(request, "crm/land_site_listing.html", {
        "site": site, "l": listing.form_rows(site.listing), "text_fields": listing.TEXT_FIELDS,
        "list_fields": listing.LIST_FIELDS, "statuses": listing.CLAIM_STATUSES})
