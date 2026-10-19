"""Site brief pages: one site (/land/sites/<id>/brief/), several side by side (/land/briefs/?ids=1,2,3), the aerial
images they print, and PDF downloads of both (?format=pdf)."""
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string

from ..ai import enabled as ai_enabled
from ..models import LandSite
from . import brief_options, briefs, flyer, listing, score


def _build(site, refresh=False):
    try:
        return briefs.build(site, refresh=refresh)
    except ValueError:
        return None


def _pdf(request, built, filename, page_hide=frozenset()):
    """A PDF download of these briefs (cover page when there are several, notes page last), or back to the page."""
    try:
        for b in built:
            briefs.embed(b)
        html = render_to_string("crm/land_briefs_pdf.html", {
            "briefs": built, "definitions": briefs.definitions(built), "method": score.methodology(), "zoning": briefs.zoning_sections(built), "css": briefs.pdf_css(), "page_hide": page_hide,
            "cover_rows": brief_options.COVER_ROWS,
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
        return _pdf(request, [brief], briefs.export_name(site.name), brief_options.page_hidden(request))
    return render(request, "crm/land_site_brief.html", {"brief": brief, "site": site, "page_hide": brief_options.page_hidden(request),
                                                         "definitions": briefs.definitions([brief]) if brief else [],
                                                         "method": score.methodology(),
                                                         "zoning": briefs.zoning_sections([brief]) if brief else {}})


def briefs_page(request):
    """Comparison cover page plus a brief per site. ?ids=1,2,3 (default: every site that isn't dropped)."""
    # ?ids=1,2,3 or the site picker's repeated ?ids=1&ids=2
    ids = [int(i) for raw in request.GET.getlist("ids") for i in raw.split(",") if i.strip().isdigit()]
    sites = LandSite.objects.filter(pk__in=ids) if ids else LandSite.objects.exclude(status=LandSite.Status.DROPPED)
    if request.method == "POST":  # "Re-run live checks"
        for s in sites:
            _build(s, refresh=True)
        return redirect(request.get_full_path())
    pairs = [(s, _build(s)) for s in sites]
    built = [b for _, b in pairs if b]
    skipped = [s for s, b in pairs if not b]  # a site with no parcels mapped can't be briefed
    built.sort(key=lambda b: b["ranking"])
    if built and request.GET.get("format") == "pdf":
        return _pdf(request, built, briefs.export_name(), brief_options.page_hidden(request))
    picked = {b["site"].pk for b in built}
    picker = [(s, s.pk in picked) for s in
              LandSite.objects.select_related("campus", "project").exclude(status=LandSite.Status.DROPPED)]
    return render(request, "crm/land_briefs.html", {"briefs": built, "ids": ",".join(str(b["site"].pk) for b in built),
                                                     "picker": picker, "skipped": skipped, "page_hide": brief_options.page_hidden(request),
                                                     "cover_rows": brief_options.COVER_ROWS, "pages": brief_options.PAGES,
                                                     "hide_param": ",".join(sorted(brief_options.page_hidden(request))),
                                                     "definitions": briefs.definitions(built), "method": score.methodology(),
                                                     "zoning": briefs.zoning_sections(built)})


def site_customize(request, pk):
    """Switch sections of this site's brief on or off and add sections of your own text."""
    from .views import _log

    site = get_object_or_404(LandSite, pk=pk)
    if request.method == "POST":
        site.brief_options = brief_options.parse(request.POST)
        site.save(update_fields=["brief_options", "updated_at"])
        _log(site, request.user, "edit", "Brief layout edited")
        messages.success(request, "Brief layout saved.")
        return redirect("crm:land_site_brief", pk=site.pk)
    hide, extras = brief_options.hidden(site), brief_options.extras(site)
    slots = [extras[i] if i < len(extras) else {"title": "", "body": ""} for i in range(max(len(extras) + 1, 2))]
    return render(request, "crm/land_site_customize.html", {
        "site": site, "slots": list(enumerate(slots)),
        "sections": [(k, label, page, k not in hide) for k, label, page in brief_options.SECTIONS]})


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
    if request.method == "POST" and request.POST.get("action") == "flyer":
        return _flyer_upload(request, site)
    if request.method == "POST":
        site.listing = listing.parse(request.POST)
        site.save(update_fields=["listing", "updated_at"])
        _log(site, request.user, "edit", "Listing details edited")
        messages.success(request, "Listing details saved.")
        return redirect("crm:land_site_brief", pk=site.pk)
    return render(request, "crm/land_site_listing.html", {
        "site": site, "l": listing.form_rows(site.listing), "text_fields": listing.TEXT_FIELDS,
        "list_fields": listing.LIST_FIELDS, "statuses": listing.CLAIM_STATUSES, "ai_ready": ai_enabled(),
        "flyer_name": site.flyer.name.rsplit("/", 1)[-1] if site.flyer else ""})


def _flyer_upload(request, site):
    """Keep the flyer on the site and, with Claude set up, read it into the listing (blank fields only)."""
    from ..contracts.ai import Unavailable
    from .views import _log

    upload = request.FILES.get("flyer")
    if not upload:
        messages.error(request, "Choose the flyer file first.")
        return redirect("crm:land_site_listing", pk=site.pk)
    if upload.size > flyer.MAX_BYTES:
        messages.error(request, "Flyers up to 25 MB.")
        return redirect("crm:land_site_listing", pk=site.pk)
    if not upload.name.lower().endswith(flyer.SUFFIXES):
        messages.error(request, flyer.WRONG_TYPE)
        return redirect("crm:land_site_listing", pk=site.pk)
    data = upload.read()
    upload.seek(0)
    if site.flyer:
        site.flyer.delete(save=False)
    site.flyer.save(upload.name, upload, save=False)
    site.save(update_fields=["flyer", "updated_at"])
    _log(site, request.user, "edit", f"Broker flyer uploaded ({upload.name})")
    if not ai_enabled():
        messages.success(request, "Flyer saved. Add ANTHROPIC_API_KEY to .env to have Claude read it.")
        return redirect("crm:land_site_listing", pk=site.pk)
    try:
        filled = flyer.read(site, data, upload.name, request.user)
    except Unavailable as exc:
        messages.error(request, f"Flyer saved, but Claude couldn't read it: {exc}")
        return redirect("crm:land_site_listing", pk=site.pk)
    _log(site, request.user, "edit", "Listing details filled from the flyer by Claude")
    messages.success(request, ("Claude filled in: " + ", ".join(filled) + ". Check them below, then save.")
                     if filled else "Claude found nothing new in the flyer: the fields were already filled in.")
    return redirect("crm:land_site_listing", pk=site.pk)


def site_flyer(request, pk):
    """The site's broker flyer, for signed-in users (PDFs and images open in the browser)."""
    site = get_object_or_404(LandSite, pk=pk)
    if not site.flyer:
        raise Http404
    try:
        fh = site.flyer.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("That file is no longer on the server.")
    name = site.flyer.name.rsplit("/", 1)[-1]
    return FileResponse(fh, as_attachment=not name.lower().endswith(flyer.SUFFIXES), filename=name)
