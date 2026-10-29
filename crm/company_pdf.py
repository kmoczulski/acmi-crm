"""A company as a one-page tenant overview PDF (/companies/<id>/overview.pdf), in the site briefs' layout and logo.

Kellen, 2026-10-28, after Fortress's diligence request: "there should be the ability to export a company card to PDF
utilizing the ACMI Properties logo and formatting we are already doing for the land site comparison docs", and "some of
this stuff would be myself entering data - like if we have a signed LOI or lease the entity would be pulled from that".
So the written overview is the company's description, and the tenant facts come from the records: the signing entity
from the LOI / lease / PDA, the guarantor from the lease terms, the NDA and financials from contracts and documents, the
size, power and dates from the deals. Anything not entered yet prints as Open and is listed under "Still needed".
"""
import re

from django.db.models import F, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone

from .land import briefs
from .models import Company, Contract, Document, OpportunityStage

SIGNED = (Contract.Status.SIGNED, Contract.Status.CLOSED)
# The agreements that name the tenant's signing entity, strongest first.
ENTITY_KINDS = (Contract.Kind.LEASE, Contract.Kind.PDA, Contract.Kind.LOI)


def _contracts(company):
    return (Contract.objects.filter(Q(company=company) | Q(opportunity__company=company))
            .exclude(status=Contract.Status.TERMINATED).select_related("opportunity", "campus").distinct())


def _rank(contract):
    """Signed before unsigned, then lease before PDA before LOI, then the latest."""
    kind = ENTITY_KINDS.index(contract.kind) if contract.kind in ENTITY_KINDS else len(ENTITY_KINDS)
    return (contract.status not in SIGNED, kind, -(contract.effective_date or contract.created_at.date()).toordinal())


def _label(contract):
    day = contract.effective_date
    when = f" {day:%b} {day.day}, {day.year}" if day and contract.status in SIGNED else ""
    return f"{contract.get_kind_display()}, {contract.get_status_display().lower()}{when}"


def tenant_rows(company, contracts, documents):
    """(item, value, where it came from) for the Tenant & financials table; value None when nobody has entered it."""
    # Only a signed (or closed) agreement names the entity and guarantor: a draft LOI started from a deal carries the
    # company's CRM name as counterparty, which isn't a legal entity anyone has confirmed. A draft shows as the source.
    agreements = sorted((c for c in contracts if c.kind in ENTITY_KINDS and c.counterparty.strip()), key=_rank)
    entity = next((c for c in agreements if c.status in SIGNED), None)
    draft = agreements[0] if agreements and not entity else None
    leases = [c for c in contracts if c.kind == Contract.Kind.LEASE and c.status in SIGNED and hasattr(c, "lease_terms")]
    guarantor = next((c for c in sorted(leases, key=_rank) if c.lease_terms.guarantor.strip()), None)
    nda = sorted((c for c in contracts if c.kind == Contract.Kind.NDA), key=_rank)
    nda_docs = [d for d in documents if d.category == "NDA"]
    financials = [d for d in documents if d.category == "Financials"]
    if nda and nda[0].status in SIGNED:
        nda_row = ("Signed", _label(nda[0]))
    elif nda_docs:
        nda_row = ("On file", nda_docs[0].title)
    elif nda:
        nda_row = (nda[0].get_status_display(), _label(nda[0]))
    else:
        nda_row = (None, "")
    return [
        ("Signing entity", entity.counterparty if entity else None,
         _label(entity) if entity else f"{_label(draft)}: {draft.counterparty} (not signed)" if draft else ""),
        ("Guarantor", guarantor.lease_terms.guarantor if guarantor else None, _label(guarantor) if guarantor else ""),
        ("NDA", *nda_row),
        ("Financials on file", "; ".join(d.title for d in financials[:4]) or None,
         f"{len(financials)} document{'s' if len(financials) != 1 else ''}" if financials else ""),
    ]


def deal_rows(company):
    """The company's deals, signed and on hold included (not cold / lost), with what Fortress asks about each: where,
    stage, size, power, dates, energetics."""
    rows = []
    deals = company.opportunities.exclude(stage=OpportunityStage.LOST).select_related("campus", "needs")
    for o in deals.order_by("stage", "name"):
        needs = getattr(o, "needs", None)
        special = []
        if needs:
            if needs.qd_cases:
                special.append(f"{len(needs.qd_cases)} explosives (QD) case{'s' if len(needs.qd_cases) != 1 else ''}")
            special += [x for x, on in (("rail", needs.needs_rail), ("natural gas", needs.needs_gas)) if on]
        rows.append({"deal": o, "where": o.campus.name if o.campus else o.site, "special": special,
                     "needs_notes": needs.notes if needs else ""})
    return rows


def lease_rows(contracts):
    out = []
    for c in sorted(contracts, key=_rank):
        if c.kind != Contract.Kind.LEASE or not hasattr(c, "lease_terms"):
            continue
        t = c.lease_terms
        out.append({"contract": c, "terms": t, "label": _label(c)})
    return out


def contacts(company, limit=6):
    """Named contacts, most recently in touch first, each name once: an unnamed record would print its email address
    in a document that leaves ACMI, and duplicate person records would list the same person twice."""
    seen, out = set(), []
    for p in company.people.exclude(full_name="").order_by(F("last_interaction_at").desc(nulls_last=True), "full_name"):
        key = " ".join(p.full_name.lower().split())
        if key and key not in seen:
            seen.add(key)
            out.append(p)
            if len(out) == limit:
                break
    return out


def build(company):
    contracts = list(_contracts(company).select_related("lease_terms"))
    documents = list(Document.objects.filter(Q(company=company) | Q(opportunity__company=company)).distinct())
    tenant = tenant_rows(company, contracts, documents)
    deals = deal_rows(company)
    missing = [item for item, value, _ in tenant if value is None]
    if not company.description.strip():
        missing.insert(0, "Company overview (the description on the company page)")
    for d in deals:
        o = d["deal"]
        gaps = [label for label, value in (("size", o.sq_ft_need), ("acres", o.acreage_need),
                                           ("power", o.power_need_mw), ("delivery date", o.est_delivery or o.timeline))
                if not value]
        if gaps:
            missing.append(f"{o.name}: {', '.join(gaps)}")
    now = timezone.now()
    return {
        "company": company, "tenant": tenant, "deals": deals, "leases": lease_rows(contracts), "missing": missing,
        "people": contacts(company),
        "next_meeting": company.meetings.filter(start__gt=now).order_by("start").first(),
    }


def export_name(company, day=None):
    """ACMI_Tenant_Overview_<Company_Name_Words>_2026.10.28.pdf, the site briefs' naming."""
    words = "_".join(w for w in re.split(r"[^A-Za-z0-9]+", company.name) if w)
    return f"ACMI_Tenant_Overview_{words}_{(day or timezone.localdate()):%Y.%m.%d}.pdf"


def company_overview_pdf(request, pk):
    """The PDF download; ?print=1, or no Chrome on the server, gives the same page to print instead."""
    company = get_object_or_404(Company, pk=pk)
    filename = export_name(company)
    ctx = {**build(company), "css": briefs.pdf_css(), "title": filename.rsplit(".", 1)[0].replace("_", " ")}
    if request.GET.get("print"):
        return render(request, "crm/company_overview_pdf.html", {**ctx, "screen": True})
    try:
        data = briefs.html_to_pdf(render_to_string("crm/company_overview_pdf.html",
                                                   {**ctx, "logo_data": briefs.logo_data_uri()}))
    except briefs.PdfUnavailable as exc:
        return render(request, "crm/company_overview_pdf.html", {
            **ctx, "screen": True, "note": f"Couldn't make the PDF ({exc}). Print this page and choose Save as PDF."})
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
