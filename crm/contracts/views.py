"""Contract pages: the list with every deadline, one contract's file (documents, dates, questions, abstracts), the
reader with defined terms and cross-references, redlines between drafts, and the review templates."""
import re
from datetime import date

from django.contrib import messages
from django.db.models import Count, Max
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from ..ai import enabled as ai_enabled
from ..entitlements import logic as entitlement_logic
from ..land import diligence
from ..models import (
    Campus, ClosingItem, Company, Contract, ContractAnswer, ContractDate, ContractDocument, ContractTemplate, LandSite,
    LeaseTerms, Opportunity, Project,
)
from ..permissions import is_manager, manager_required, refuse
from . import ai, closing, compare, dates, filing, intake, methods, timeline
from . import text as textmod
from ..tenants import leases
from .filing import NO_PROJECT, project_label  # the same names the list and its tests have always imported from here
from .forms import ContractForm, DateForm, DocumentForm, LeaseTermsForm, TemplateForm
from .terms_views import terms_panel

INLINE_TYPES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp")
COMPARE_QUESTION = ("What changed between these two documents? List each change in order with the section, what it "
                    "said before, what it says now, and what it means for ACMI. Flag anything that looks unintended.")


# ---------------------------------------------------------------------------
# List and create
# ---------------------------------------------------------------------------


_SECTIONS = (
    ("Land", ("loi", "psa", "option")),
    ("Leases", ("lease",)),
    ("PDAs / DMAs", ("pda",)),
    ("Financing and JV", ("loan", "jv")),
    ("Construction and design", ("services",)),
    ("Easements", ("easement",)),
    ("NDAs", ("nda",)),
)


def _by_project(contracts):
    """[{"project", "contracts"}] with projects A to Z and "No project" last; each keeps the order it came in."""
    groups = {}
    for c in contracts:
        groups.setdefault(project_label(c), []).append(c)
    names = sorted((n for n in groups if n != NO_PROJECT), key=str.lower) + ([NO_PROJECT] if NO_PROJECT in groups else [])
    return [{"project": n, "contracts": groups[n]} for n in names]


def _contract_sections(contracts, group="type"):
    """group="type": Land (PSA / LOI / option), leases, PDAs, then everything else, and inside each the contracts by
    project. group="project": a section per project, and inside each the contracts by type. Empty groups are dropped."""
    kind_to = {k: title for title, kinds in _SECTIONS for k in kinds}
    order = [title for title, _ in _SECTIONS] + ["Other"]
    if group == "project":
        rank = {title: i for i, title in enumerate(order)}
        return [{"title": g["project"], "contracts": g["contracts"],
                 "groups": [{"project": "", "contracts": sorted(
                     g["contracts"], key=lambda c: rank[kind_to.get(c.kind, "Other")])}]}
                for g in _by_project(contracts)]
    buckets = {title: [] for title in order}
    for c in contracts:
        buckets[kind_to.get(c.kind, "Other")].append(c)
    return [{"title": title, "contracts": buckets[title], "groups": _by_project(buckets[title])}
            for title in order if buckets[title]]


def contract_list(request):
    show = request.GET.get("show", "open")
    kind = request.GET.get("kind", "")
    campus = request.GET.get("campus", "")
    group = "project" if request.GET.get("group") == "project" else "type"
    tenant = Company.objects.filter(pk=request.GET.get("company") or 0).first()
    in_project = Project.objects.filter(pk=request.GET.get("project") or 0).first()
    qs = Contract.objects.select_related("site__project", "campus", "project", "company", "owner",
                                         "opportunity__company", "opportunity__campus"
                                         ).annotate(docs=Count("documents", distinct=True))
    if show == "open":
        qs = qs.filter(status__in=Contract.OPEN_STATUSES)
    if kind in Contract.Kind.values:
        qs = qs.filter(kind=kind)
    if campus:
        qs = qs.filter(campus__code__iexact=campus)
    if tenant:
        qs = qs.filter(filing.company_q(tenant))
    if in_project:
        qs = qs.filter(filing.project_q(in_project))
    contracts = list(qs)
    today = timezone.localdate()
    nxt = dates.next_dates(contracts, today)
    for c in contracts:
        c.next = nxt.get(c.pk)
        c.waiting_days = (today - c.waiting_since).days if c.waiting_on and c.waiting_since else None
        c.filed = filing.places(c)
        c.missing = filing.missing(c)
    contracts.sort(key=lambda c: (c.next is None, c.next.date if c.next else today, c.title.lower()))
    return render(request, "crm/contract_list.html", {
        "contracts": contracts, "sections": _contract_sections(contracts, group), "group": group,
        "deadlines": dates.upcoming(60, today), "show": show, "kind": kind,
        "kinds": Contract.Kind.choices, "campuses": Campus.objects.all(), "campus": campus,
        "tenant": tenant, "in_project": in_project, "ai_ready": ai_enabled(),
        # Agreements with no tenant (a lease, PDA or NDA) or no project: the ones to file.
        "unfiled": [c for c in contracts if c.missing],
        "drop_company": tenant.pk if tenant else "", "drop_project": in_project.pk if in_project else "",
    })


def critical_dates(request):
    """Critical dates calendar: month, week or day, filtered by campus or project."""
    from ..models import Campus, Project

    today = timezone.localdate()
    view = request.GET.get("view", "month")
    anchor = request.GET.get("when") or request.GET.get("month") or request.GET.get("crit")
    campus_id = request.GET.get("campus", "")
    project_id = request.GET.get("project", "")
    cal = dates.span_calendar(view, anchor, today, owner_ids=None)
    if campus_id or project_id:
        from ..models import Contract
        keep = set()
        if campus_id.isdigit():
            keep |= set(Contract.objects.filter(campus_id=int(campus_id)).values_list("pk", flat=True))
        if project_id.isdigit():
            keep |= set(Contract.objects.filter(site__project_id=int(project_id)).values_list("pk", flat=True))
        if keep:
            for week in cal.get("weeks", []):
                for day in week:
                    day["items"] = [r for r in day["items"] if getattr(getattr(r, "contract", None), "pk", None) in keep or not hasattr(r, "contract")]
            cal["count"] = sum(len(d["items"]) for w in cal["weeks"] for d in w)
    campuses = list(Campus.objects.all())
    projects = list(Project.objects.filter(active=True).select_related("campus"))
    ctx = {"cal": cal, "campuses": campuses, "projects": projects, "campus_id": campus_id, "project_id": project_id}
    if request.GET.get("partial") == "1":
        return render(request, "crm/partials/critical_calendar.html", {**ctx, "swap": True})
    return render(request, "crm/critical_dates.html", ctx)


def contract_create(request):
    initial = {"owner": request.user}
    site = LandSite.objects.select_related("campus").filter(pk=request.GET.get("site") or 0).first()
    deal = Opportunity.objects.select_related("company").filter(pk=request.GET.get("opportunity") or 0).first()
    company = Company.objects.filter(pk=request.GET.get("company") or 0).first()
    project = Project.objects.select_related("campus", "parent__campus").filter(pk=request.GET.get("project") or 0).first()
    if site:
        initial.update(site=site, campus=site.campus, kind=Contract.Kind.PSA, title=f"{site.name} PSA")
    elif deal:
        initial.update(opportunity=deal, campus=deal.campus, kind=Contract.Kind.LOI, counterparty=deal.company.name,
                       title=f"{deal.company.name} LOI")
    elif company:
        initial.update(company=company.pk, kind=Contract.Kind.LEASE, counterparty=company.name,
                       title=f"{company.name} lease")
    if project:
        initial.update(project=project, campus=initial.get("campus") or project.campus
                       or (project.parent.campus if project.parent_id else None))
    if request.GET.get("kind") in Contract.Kind.values:
        initial["kind"] = request.GET["kind"]
    form = ContractForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        contract = form.save(commit=False)
        contract.created_by = request.user
        contract.save()
        form.save_m2m()
        messages.success(request, "Added. Upload the signed document to read it with Claude.")
        return redirect(contract)
    return render(request, "crm/form.html", {"form": form, "title": "New legal record",
                                             "cancel": reverse("crm:contract_list")})


def _drop_file(request, contract, new_contract=False):
    """One dropped file onto a contract: a document read on arrival, then Claude fills in what it is. Returns the
    JSON for the page's upload list."""
    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"ok": False, "error": "No file."}, status=400)
    if upload.size > DocumentForm.MAX_BYTES:
        return JsonResponse({"ok": False, "error": f"{upload.name} is over {DocumentForm.MAX_MB} MB."}, status=400)
    hint = intake.guess(upload.name)
    doc = ContractDocument(contract=contract, added_by=request.user, kind=hint["kind"], title=hint["title"],
                           dated=hint["dated"], file=upload,
                           position=(contract.documents.aggregate(m=Max("position"))["m"] or 0) + 1)
    _read_upload(doc, upload)
    same = contract.documents.filter(text=doc.text).exclude(text="").first() if doc.text else None
    if same:
        return JsonResponse({"ok": False, "error": f"Already in the file as {same.title}."}, status=200)
    doc.save()
    filled, note = [], ""
    if ai_enabled():
        try:
            filled = intake.read(doc, contract, request.user, new_contract=new_contract)
        except ai.Unavailable as exc:
            note = str(exc)
    contract.save(update_fields=["updated_at"])
    if new_contract:
        filing.auto_file(contract)  # now that the document has named its parties: the tenant, its deal, the campus project
    dates.sync_site(contract, request.user)
    return JsonResponse({
        "ok": True, "doc": doc.pk, "title": doc.title, "kind": doc.get_kind_display(),
        "dated": f"{doc.dated:%b} {doc.dated.day}, {doc.dated.year}" if doc.dated else "",
        "by": doc.made_by if doc.is_draft else "", "pages": doc.pages, "filled": filled, "note": note,
        "scan": doc.text_status == ContractDocument.TextStatus.SCAN, "url": contract.get_absolute_url(),
        "reader": reverse("crm:contract_reader", args=[contract.pk, doc.pk]) if doc.text else "",
        "filed": filing.summary(contract), "missing": filing.missing(contract),
    })


@require_POST
def contract_drop(request, pk):
    """A file dropped onto a contract's page (one request per file)."""
    return _drop_file(request, get_object_or_404(Contract, pk=pk))


@require_POST
def contract_drop_new(request):
    """A document dropped onto Legal (or a company's, project's or site's page): a new record, filed where the drop
    said (company, deal, project, site, campus), then named and filled in from the file."""
    upload = request.FILES.get("file")
    if not upload:
        return JsonResponse({"ok": False, "error": "No file."}, status=400)
    contract = Contract.objects.create(
        title=intake.guess(upload.name)["title"][:200], kind=Contract.Kind.OTHER, status=Contract.Status.NEGOTIATING,
        owner=request.user, created_by=request.user)
    filing.file_to(contract, **filing.picks(request.POST))
    response = _drop_file(request, contract, new_contract=True)
    if response.status_code != 200:
        contract.delete()
    return response


@manager_required
@require_POST
def contract_delete(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    for doc in contract.documents.all():
        if doc.file:
            doc.file.delete(save=False)
    contract.delete()
    messages.success(request, f"Deleted {contract.title}.")
    return redirect("crm:contract_list")


# ---------------------------------------------------------------------------
# One contract
# ---------------------------------------------------------------------------


def _reader_url(contract_pk, doc_pk, start=None, end=None):
    url = reverse("crm:contract_reader", args=[contract_pk, doc_pk])
    return f"{url}?hl={start}-{end}#hl" if start is not None and end is not None else url


def _cite_href(contract):
    def href(c):
        if c.get("start") is not None:
            return _reader_url(contract.pk, c["doc"], c["start"], c["end"])
        return _reader_url(contract.pk, c["doc"]) + (f"#page-{c['page']}" if c.get("page") else "")
    return href


def _read_upload(doc, upload):
    """Pull the text out of an uploaded file onto the document."""
    data = upload.read()
    upload.seek(0)
    doc.text, doc.page_starts, doc.text_status, doc.tracked = textmod.extract_full(data, upload.name)
    doc.pages = len(doc.page_starts)


def _owners(contract):
    """The site's parcel deals grouped by owner, for "which owners does this cover"."""
    if not contract.site_id:
        return []
    from ..land import tracker

    picked = set(contract.parcel_deals.values_list("pk", flat=True))
    groups = {}
    for deal in tracker.site_deals(contract.site):
        if deal.pk is None:
            continue
        owner = (deal.parcel.owner_name or "Unknown owner").strip().title()
        g = groups.setdefault(owner, {"owner": owner, "deals": [], "acres": 0.0})
        g["deals"].append(deal)
        g["acres"] += deal.acres or 0
    for g in groups.values():
        g["ids"] = ",".join(str(d.pk) for d in g["deals"])
        g["checked"] = any(d.pk in picked for d in g["deals"])
    return sorted(groups.values(), key=lambda g: -g["acres"])


def contract_detail(request, pk):
    contract = get_object_or_404(Contract.objects.select_related("site__project", "campus", "project", "company", "owner",
                                                                 "opportunity__company", "opportunity__campus"), pk=pk)
    if request.method == "POST":
        return _post(request, contract)
    docs = ai.chain(contract)
    old = ai.superseded(docs)
    for d in docs:
        d.superseded_by = old.get(d.pk)
    answers = list(contract.answers.select_related("asked_by", "template")[:30])
    href = _cite_href(contract)
    for a in answers:
        a.html, a.sources = ai.render(a, href)
        a.html = mark_safe(a.html)
    all_dates = list(contract.dates.select_related("source"))
    today = timezone.localdate()
    for d in all_dates:
        d.how = timeline.describe(timeline.term(d)) if d.anchor and d.offset is not None else ""
        d.left = d.days_left(today)
        d.late = -d.left if d.left is not None else None
        if d.source_id and d.source_start is not None:
            d.source_href = _reader_url(contract.pk, d.source_id, d.source_start, d.source_end)
    readable = ai.default_docs(contract)
    drafts = [d for d in docs if d.is_draft]
    with_text = [d for d in docs if d.text]
    for prev, d in zip(with_text, with_text[1:]):
        d.prev = prev  # any version against the one before it, signed or not
    purchase = contract.kind in (Contract.Kind.PSA, Contract.Kind.OPTION)
    return render(request, "crm/contract_detail.html", {
        "lease": _lease_context(contract, today) if contract.kind == Contract.Kind.LEASE else None,
        "approvals": (entitlement_logic.annotate(list(entitlement_logic.with_related(contract.approvals.all())), today)
                      if purchase else []),
        "new_approval_url": reverse("crm:approval_create") + f"?contract={contract.pk}",
        "dd": diligence.summary(contract.site, today, contract=contract) if purchase and contract.site_id else None,
        "closing": closing.progress(contract, today) if purchase else None, "today": today,
        "closing_sides": ClosingItem.Side.choices, "closing_statuses": ClosingItem.Status.choices,
        "contract": contract, "form": ContractForm(instance=contract), "doc_form": DocumentForm(),
        "date_form": DateForm(), "signed": [d for d in docs if not d.is_draft], "drafts": drafts,
        "docs": docs, "dates": [d for d in all_dates if d.confirmed], "proposed": [d for d in all_dates if not d.confirmed],
        "answers": [a for a in answers if not a.is_abstract], "abstracts": [a for a in answers if a.is_abstract],
        "templates": methods.for_kind(contract.kind), "ai_ready": ai_enabled(), "reads": readable,
        "owners": _owners(contract), "kinds": ContractDocument.Kind.choices, "senders": ContractDocument.Sender.choices,
        "date_kinds": ContractDate.Kind.choices, "can_manage": is_manager(request.user),
        "compare_pair": drafts[-2:] if len(drafts) >= 2 else [d for d in docs if d.text][-2:],
        "timeline": timeline_strip(contract, today), "units": ContractDate.Unit.choices, "terms": terms_panel(contract),
        "executed": executed_doc(contract, docs),
        "filed": filing.places(contract), "unfiled": filing.missing(contract),
        "comment_counts": dict(contract.comments.filter(parent__isnull=True, resolved=False).order_by()
                               .values_list("document").annotate(n=Count("id")).values_list("document", "n")),
    })


def _lease_context(contract, today):
    """The lease's terms, what they add up to today, and links to the language Claude found each term in."""
    terms = leases.terms_for(contract)
    sources = []
    for field, src in ((terms.sources if terms else None) or {}).items():
        if src.get("doc") and src.get("start") is not None:
            href = _reader_url(contract.pk, src["doc"], src["start"], src["end"])
        else:
            href = ""
        sources.append({"label": LeaseTerms._meta.get_field(field).verbose_name, "quote": src.get("quote", ""),
                        "note": src.get("note", ""), "href": href})
    return {"terms": terms, "form": LeaseTermsForm(instance=terms or LeaseTerms(contract=contract)),
            "rent_now": leases.rent_now(terms, today), "notice": leases.notice_deadline(terms),
            "years_left": leases.years_left(terms, today), "sources": sources}


def _recalculated(moved, left):
    """What "Saved." adds when a new effective date moved the critical dates that count from it."""
    parts = []
    if moved:
        n = len(moved)
        parts.append(f"Recalculated {n} critical date{'s' if n != 1 else ''} from the new effective date.")
    if left:
        parts.append(f"{left} {'stays where it is' if left == 1 else 'stay where they are'} (done, or moved by hand).")
    return " " + " ".join(parts) if parts else ""


def _post(request, contract):
    action = request.POST.get("action", "")
    anchor = ""
    user = request.user
    if action == "details":
        # Read before the form is built: is_valid() copies the posted values onto the contract, so afterwards this
        # would already be the new date and nothing would look changed.
        old_effective = contract.effective_date
        form = ContractForm(request.POST, instance=contract)
        if form.is_valid():
            form.save()
            # Owners picked on a site it no longer belongs to stop getting its dates.
            contract.parcel_deals.remove(*contract.parcel_deals.exclude(site_id=contract.site_id))
            note = ""
            if contract.effective_date and contract.effective_date != old_effective:
                note = _recalculated(*timeline.recompute_dates(contract, old_effective, user))
            dates.sync_site(contract, user)  # after the dates have moved, so the site's timeline gets the new ones
            messages.success(request, "Saved." + note)
        else:
            messages.error(request, "Not saved: " + "; ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items()))
    elif action == "upload":
        form = DocumentForm(request.POST, request.FILES)
        anchor = "documents"
        if form.is_valid():
            doc = form.save(commit=False)
            doc.contract, doc.added_by = contract, user
            doc.position = (contract.documents.aggregate(m=Max("position"))["m"] or 0) + 1
            if "file" in request.FILES:
                _read_upload(doc, request.FILES["file"])
            doc.save()
            contract.save(update_fields=["updated_at"])
            dates.sync_site(contract, user)
            status = {"ok": f"{doc.pages} page{'s' if doc.pages != 1 else ''} read.",
                      "scan": "It's a scan with no text layer: Claude can read the PDF, but the reader can't.",
                      "unsupported": "That file type can't be read; upload a PDF or Word file to ask about it.",
                      "error": "The file couldn't be read (damaged or password-protected)."}.get(doc.text_status, "")
            messages.success(request, f"Added {doc.title}. {status}".strip())
        else:
            messages.error(request, "Not added: " + " ".join(" ".join(v) for v in form.errors.values()))
    elif action == "doc_edit":
        doc = get_object_or_404(ContractDocument, pk=request.POST.get("doc"), contract=contract)
        form = DocumentForm(request.POST, request.FILES, instance=doc)
        anchor = "documents"
        if form.is_valid():
            doc = form.save(commit=False)
            if "file" in request.FILES:
                _read_upload(doc, request.FILES["file"])
            doc.save()
            dates.sync_site(contract, user)
            messages.success(request, f"Saved {doc.title}.")
        else:
            messages.error(request, "Not saved: " + " ".join(" ".join(v) for v in form.errors.values()))
    elif action == "doc_delete":
        if not is_manager(user):
            return refuse(request)
        doc = get_object_or_404(ContractDocument, pk=request.POST.get("doc"), contract=contract)
        if doc.file:
            doc.file.delete(save=False)
        doc.delete()
        anchor = "documents"
        messages.success(request, f"Removed {doc.title}.")
    elif action == "parcels":
        allowed = {d.pk for g in _owners(contract) for d in g["deals"]}
        picked = {int(x) for v in request.POST.getlist("deals") for x in v.split(",") if x.isdigit()} & allowed
        contract.parcel_deals.set(picked)
        dates.sync_site(contract, user)
        messages.success(request, "Saved which owners it covers.")
    elif action in ("date_add", "date_edit"):
        anchor = "dates"
        instance = (get_object_or_404(ContractDate, pk=request.POST.get("date_id"), contract=contract)
                    if action == "date_edit" else ContractDate(contract=contract, created_by=user))
        form = DateForm(request.POST, instance=instance)
        if form.is_valid():
            d = form.save(commit=False)
            d.confirmed = True
            timeline.fill_rule(d)
            d.save()
            dates.sync_site(contract, user)
            messages.success(request, f"Saved {d.label}.")
        else:
            messages.error(request, "Not saved: " + " ".join(" ".join(v) for v in form.errors.values()))
    elif action == "date_done":
        anchor = "dates"
        d = get_object_or_404(ContractDate, pk=request.POST.get("date_id"), contract=contract)
        d.done = not d.done
        d.done_at, d.done_by = (timezone.now(), user) if d.done else (None, None)
        d.save(update_fields=["done", "done_at", "done_by"])
        dates.sync_site(contract, user)
    elif action == "date_keep":
        anchor = "dates"
        ids = request.POST.getlist("date_id")
        kept = contract.dates.filter(confirmed=False, pk__in=[i for i in ids if i.isdigit()]).update(confirmed=True)
        dates.sync_site(contract, user)
        messages.success(request, f"Kept {kept} date{'s' if kept != 1 else ''}.")
    elif action == "date_delete":
        anchor = "dates"
        d = get_object_or_404(ContractDate, pk=request.POST.get("date_id"), contract=contract)
        if d.confirmed and not (is_manager(user) or d.created_by_id == user.pk):
            return refuse(request)
        d.delete()
    elif action == "date_discard":
        anchor = "dates"
        contract.dates.filter(confirmed=False).delete()
    elif action == "ask":
        question = (request.POST.get("question") or "").strip()[:4000]
        ids = [int(i) for i in request.POST.getlist("docs") if i.isdigit()]
        docs = [d for d in ai.chain(contract) if d.pk in ids and ai.readable(d)] if ids else None
        if question:
            try:
                answer = ai.ask(contract, question, user, docs=docs)
                anchor = f"answer-{answer.pk}"
            except ai.Unavailable as exc:
                messages.error(request, str(exc))
    elif action == "template":
        template = get_object_or_404(ContractTemplate, pk=request.POST.get("template"))
        try:
            answer = ai.run_template(contract, template, user)
            return redirect("crm:contract_answer", pk=contract.pk, answer_pk=answer.pk)
        except ai.Unavailable as exc:
            messages.error(request, str(exc))
    elif action == "find_dates":
        anchor = "dates"
        try:
            found = ai.find_dates(contract, user)
            messages.success(request, f"Claude found {len(found)} date{'s' if len(found) != 1 else ''}. "
                                      "Check each against its source, then keep the right ones."
                             if found else "Claude found no dates that aren't already listed.")
        except ai.Unavailable as exc:
            messages.error(request, str(exc))
    elif action == "lease_terms":
        anchor = "lease"
        form = LeaseTermsForm(request.POST, instance=leases.terms_for(contract) or LeaseTerms(contract=contract))
        if form.is_valid():
            terms = form.save(commit=False)
            terms.updated_by = user
            terms.save()
            leases.sync_dates(contract, terms, user)
            messages.success(request, "Saved the lease terms; expiration, renewal notice and rent start are on the "
                                      "critical dates.")
        else:
            messages.error(request, "Not saved: " + " ".join(" ".join(v) for v in form.errors.values()))
    elif action == "lease_read":
        anchor = "lease"
        try:
            filled = leases.read_terms(contract, user)
            messages.success(request, f"Claude filled in {', '.join(filled)}. Check each against its source."
                             if filled else "Nothing new: the terms are already filled in or not in the lease.")
        except ai.Unavailable as exc:
            messages.error(request, str(exc))
    elif action == "closing_start":
        anchor = "closing"
        n = closing.start(contract)
        messages.success(request, f"Added {n} closing item{'s' if n != 1 else ''}." if n else "Already there.")
    elif action in ("closing_add", "closing_edit"):
        anchor = "closing"
        item = (get_object_or_404(ClosingItem, pk=request.POST.get("item"), contract=contract)
                if action == "closing_edit" else ClosingItem(contract=contract, position=(
                    contract.closing_items.aggregate(m=Max("position"))["m"] or 0) + 1))
        name = (request.POST.get("name") or "").strip()[:200]
        if not name:
            messages.error(request, "Give the closing item a name.")
        else:
            item.name = name
            item.side = request.POST.get("side") if request.POST.get("side") in ClosingItem.Side.values else item.side
            item.responsible = (request.POST.get("responsible") or "").strip()[:120]
            item.notes = (request.POST.get("notes") or "").strip()[:300]
            try:
                item.due_on = date.fromisoformat(request.POST["due_on"]) if request.POST.get("due_on") else None
            except ValueError:
                item.due_on = None
            item.save()
    elif action == "closing_status":
        anchor = "closing"
        item = get_object_or_404(ClosingItem, pk=request.POST.get("item"), contract=contract)
        status = request.POST.get("status", "")
        if status in ClosingItem.Status.values:
            item.status = status
            item.done_on, item.done_by = ((timezone.localdate(), user) if status == ClosingItem.Status.DONE
                                          else (None, None))
            item.save(update_fields=["status", "done_on", "done_by"])
    elif action == "closing_delete":
        anchor = "closing"
        if not is_manager(user):
            return refuse(request)
        get_object_or_404(ClosingItem, pk=request.POST.get("item"), contract=contract).delete()
    elif action == "close":
        anchor = "closing"
        try:
            on = date.fromisoformat(request.POST.get("closed_on") or "")
        except ValueError:
            on = timezone.localdate()
        closing.close(contract, on, user)
        messages.success(request, f"Closed on {on:%b} {on.day}, {on.year}." + (
            " The site and its parcels are marked Acquired." if contract.site_id else ""))
    elif action in ("pin", "answer_delete"):
        answer = get_object_or_404(ContractAnswer, pk=request.POST.get("answer"), contract=contract)
        if action == "pin":
            answer.pinned = not answer.pinned
            answer.save(update_fields=["pinned"])
            anchor = f"answer-{answer.pk}"
        elif is_manager(user) or answer.asked_by_id == user.pk:
            answer.delete()
        else:
            return refuse(request)
    return redirect(contract.get_absolute_url() + (f"#{anchor}" if anchor else ""))


def contract_answer(request, pk, answer_pk):
    """One answer or abstract on its own page, printable, with every source quoted."""
    contract = get_object_or_404(Contract, pk=pk)
    answer = get_object_or_404(ContractAnswer.objects.select_related("asked_by", "template"), pk=answer_pk,
                               contract=contract)
    body, sources = ai.render(answer, _cite_href(contract))
    return render(request, "crm/contract_answer.html", {"contract": contract, "answer": answer,
                                                        "body": mark_safe(body), "sources": sources})


def contract_file(request, doc_pk):
    """A contract document's file, for signed-in users. PDFs open in the browser; anything else downloads."""
    doc = get_object_or_404(ContractDocument, pk=doc_pk)
    if not doc.file:
        raise Http404
    try:
        fh = doc.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("That file is no longer on the server.")
    name = doc.file.name.rsplit("/", 1)[-1]
    response = FileResponse(fh, as_attachment=not name.lower().endswith(INLINE_TYPES), filename=name)
    # The reader's "Original PDF" view shows it in a frame on our own pages (the site-wide policy forbids framing).
    response["Content-Security-Policy"] = "frame-ancestors 'self'"
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


# ---------------------------------------------------------------------------
# Reader: defined terms, cross-references, amendments
# ---------------------------------------------------------------------------


def _base_for(doc, signed):
    """The agreement a later document points back to: the latest restated agreement before it, else the original."""
    before = signed[:signed.index(doc)] if doc in signed else signed
    for d in reversed(before):
        if d.kind == ContractDocument.Kind.RESTATED:
            return d
    return next((d for d in before if d.kind == ContractDocument.Kind.ORIGINAL), None)


def glossary(docs, contract_pk):
    """{term: [{"title", "text", "href"}]}: each defined term's definitions across the documents, in order; the last
    one is the one in force."""
    out = {}
    for d in docs:
        if not d.text:
            continue
        for term, start, end in textmod.definitions(d.text):
            out.setdefault(term, []).append({"title": d.title, "text": d.text[start:end][:1500],
                                             "href": _reader_url(contract_pk, d.pk, start, end)})
    return out


def _users():
    """Everyone who can be @mentioned, for the comment box's picker."""
    from ..forms import active_users

    return [{"handle": (u.first_name or u.username).split()[0].lower() if (u.first_name or u.username) else u.username,
             "name": u.get_full_name() or u.username} for u in active_users()]


def doc_comments(doc):
    """The document's comment threads, oldest pin first: [{"c": comment, "replies": [...]}]."""
    roots = list(doc.comments.filter(parent__isnull=True).select_related("author").prefetch_related(
        "replies__author").order_by("resolved", "start", "created_at"))
    for c in roots:
        c.thread = sorted(c.replies.all(), key=lambda r: r.created_at)
    return roots


def contract_reader(request, pk, doc_pk):
    contract = get_object_or_404(Contract, pk=pk)
    docs = ai.chain(contract)
    doc = next((d for d in docs if d.pk == doc_pk), None)  # the chain's own instance: compared by identity below
    if doc is None:
        raise Http404
    signed = [d for d in docs if not d.is_draft]
    scope = [doc] if doc.is_draft else signed
    terms = glossary(scope, contract.pk)
    sections = {d.pk: textmod.sections(d.text) for d in scope if d.text}
    own = sections.get(doc.pk, {})
    base = None if doc.is_draft else _base_for(doc, signed)
    amendment_like = doc.kind not in (ContractDocument.Kind.ORIGINAL, ContractDocument.Kind.RESTATED,
                                      ContractDocument.Kind.DRAFT)

    def target(num, of_base):
        order = [base, doc] if (amendment_like and (of_base or num not in own)) else [doc]
        for d in order:
            if d is not None and num in sections.get(d.pk, {}):
                return d, sections[d.pk][num]
        return None

    links = {}
    for start, end, num, of_base in textmod.xrefs(doc.text):
        hit = target(num, of_base)
        if hit:
            d, (s, e) = hit
            links[start] = (end, _reader_url(contract.pk, d.pk, s, e),
                            {"num": num, "title": d.title, "span": reverse("crm:contract_span", args=[contract.pk, d.pk])
                             + f"?start={s}&end={min(e, s + textmod.SECTION_CHARS)}"})
    anchors = {start: f"s-{num}" for num, (start, _) in own.items()}
    badges = {}
    if not doc.is_draft:
        for later in signed[signed.index(doc) + 1:]:
            if later.text and _base_for(later, signed) is doc:
                for num, (s, e) in textmod.amended_sections(later.text).items():
                    if num in own:
                        badges.setdefault(own[num][0], []).append(
                            (f"§ {num} changed by {later.title}", _reader_url(contract.pk, later.pk, s, e)))
    highlight = None
    m = re.fullmatch(r"(\d+)-(\d+)", request.GET.get("hl", ""))
    if m:
        highlight = (int(m.group(1)), int(m.group(2)))
    comments = doc_comments(doc)
    marks = [(c.start, c.end, c.pk) for c in comments if c.start is not None and c.end and not c.resolved]
    body = textmod.reader_html(doc.text, doc.page_starts, terms=terms.keys(), links=links, anchors=anchors,
                               badges=badges, highlight=highlight, tracked=doc.tracked, marks=marks) if doc.text else ""
    outline = sorted(((num, s) for num, (s, _) in own.items()), key=lambda x: x[1])
    authors = sorted({c.get("author") or "Unknown" for c in doc.tracked or []})
    is_pdf = bool(doc.file) and doc.file.name.lower().endswith(".pdf")
    return render(request, "crm/contract_reader.html", {
        "contract": contract, "doc": doc, "docs": docs, "body": mark_safe(body), "glossary": terms,
        "outline": [(num, doc.text[s:s + 90].split("\n", 1)[0].strip()) for num, s in outline][:400],
        "base": base, "changed": sum(len(v) for v in badges.values()), "comments": comments,
        "open_comments": sum(1 for c in comments if not c.resolved), "users": _users(),
        "authors": [(a, n) for n, a in enumerate(authors)], "is_pdf": is_pdf,
        "view": "pdf" if request.GET.get("view") == "pdf" and is_pdf else "page",
        "prev": next((d for d in reversed(docs[:docs.index(doc)]) if d.text), None),
        "can_manage": is_manager(request.user), "executed": executed_doc(contract, docs),
    })


def _pin(doc, start, end, quote, hint=0):
    """The range a comment is pinned to: the offsets the page sent when the text there matches the quote, else the
    quote found nearest after `hint`, else just the quote."""
    text = doc.text or ""
    sent = start is not None and end is not None and 0 <= start < end <= len(text)
    if sent and (not quote or ai._norm(text[start:end]) == ai._norm(quote)):
        return start, end
    words = (quote or "").split()
    if words:
        probe = re.compile(r"\s+".join(re.escape(w) for w in words[:30]), re.I)
        m = probe.search(text, max(0, hint - 200)) or probe.search(text)
        if m:
            return m.start(), m.end()
    return (start, end) if sent else (None, None)  # a selection across pages carries the page numbers too


def _int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@require_POST
def contract_comment(request, pk):
    """Add, answer, resolve or delete a comment pinned to a contract document. Mentions notify (crm/notify.py)."""
    from .. import notify
    from ..models import Comment

    contract = get_object_or_404(Contract, pk=pk)
    action = request.POST.get("action", "add")
    back = request.POST.get("next") or contract.get_absolute_url()
    if not back.startswith("/") or back.startswith("//"):  # a path on this site, never another host
        back = contract.get_absolute_url()
    back = back.split("#")[0]
    if action in ("add", "reply"):
        body = (request.POST.get("body") or "").strip()[:4000]
        if not body:
            messages.error(request, "Write something first.")
            return redirect(back)
        if action == "reply":
            parent = get_object_or_404(Comment, pk=request.POST.get("parent"), contract=contract, parent__isnull=True)
            c = Comment.objects.create(body=body, author=request.user, contract=contract, document=parent.document,
                                       parent=parent)
            if parent.resolved:
                parent.resolved = False
                parent.save(update_fields=["resolved"])
            anchor = parent.pk
        else:
            doc = get_object_or_404(ContractDocument, pk=request.POST.get("doc"), contract=contract)
            quote = (request.POST.get("quote") or "").strip()[:2000]
            start, end = _pin(doc, _int(request.POST.get("start")), _int(request.POST.get("end")), quote,
                              _int(request.POST.get("hint")) or 0)
            c = Comment.objects.create(body=body, author=request.user, contract=contract, document=doc, start=start,
                                       end=end, quote=doc.text[start:end][:2000] if start is not None else quote)
            anchor = c.pk
        notify.comment_added(c)
        return redirect(f"{back}#c-{anchor}")
    c = get_object_or_404(Comment, pk=request.POST.get("comment"), contract=contract)
    if action == "resolve":
        c.resolved = not c.resolved
        c.save(update_fields=["resolved"])
        return redirect(f"{back}#c-{(c.parent or c).pk}")
    if action == "delete":
        if not (c.author_id == request.user.pk or is_manager(request.user)):
            return refuse(request)
        c.delete()
    return redirect(back)


def contract_span(request, pk, doc_pk):
    """A stretch of a document's text (a cross-referenced section) for the reader's side panel."""
    doc = get_object_or_404(ContractDocument, pk=doc_pk, contract_id=pk)
    try:
        start = max(0, int(request.GET.get("start", 0)))
        end = min(len(doc.text), int(request.GET.get("end", start)), start + textmod.SECTION_CHARS)
    except ValueError:
        raise Http404
    return JsonResponse({"title": doc.title, "page": doc.page_at(start), "text": doc.text[start:end],
                         "href": _reader_url(pk, doc.pk, start, end)})


# ---------------------------------------------------------------------------
# Redline
# ---------------------------------------------------------------------------


AUTHOR_COLORS = 6  # rl-a0 … rl-a5 in app.css


def _rounds(docs, a, b):
    """The versions from `a` to `b` whose changes a redline shows: the drafts between two drafts, else just the two."""
    i, j = docs.index(a), docs.index(b)
    if i < j and a.is_draft and b.is_draft:
        return [d for d in docs[i:j + 1] if d.is_draft and d.text]
    return [a, b]


def redline_for(contract, docs, a, b):
    """(html, stats, authors) for the redline from a to b: each change credited to the round that made it (a Word
    file's own tracked changes win), authors as [(name, css class, documents)] for the legend."""
    run = _rounds(docs, a, b)
    origin, removed = compare.blame([d.text for d in run]) if len(run) > 2 else (None, None)
    slots = {}

    def slot(name):
        return slots.setdefault(name, f"rl-a{len(slots) % AUTHOR_COLORS}")

    def who(rnd, start, end):
        for c in b.tracked or []:
            if c.get("author") and (c["start"] < end and start < c["end"] or c["start"] == start == end
                                    or start <= c["start"] <= end):
                name = c["author"]
                return f"{name} (tracked in {b.title}{', ' + c['date'] if c.get('date') else ''})", slot(name)
        doc = run[rnd] if rnd is not None and rnd < len(run) else b
        name = doc.made_by or "Unknown"
        when = f", {doc.dated:%b} {doc.dated.day}" if doc.dated else ""
        return f"{name} ({doc.title}{when})", slot(name)

    body, stats = compare.redline(a.text, b.text, origin=origin, removed=removed, who=who, page_starts=b.page_starts)
    if body is None:
        return None, stats, []
    names = {}
    for item in stats["items"]:
        name = item["who"].split(" (", 1)[0]
        names.setdefault(name, {"name": name, "cls": item["cls"], "count": 0})["count"] += 1
    return body, stats, list(names.values())


def contract_compare(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    docs = [d for d in ai.chain(contract) if d.text]
    by_pk = {d.pk: d for d in docs}
    a = by_pk.get(_int(request.GET.get("a") or request.POST.get("a")) or 0)
    b = by_pk.get(_int(request.GET.get("b") or request.POST.get("b")) or 0)
    if not (a and b) and len(docs) >= 2:
        drafts = [d for d in docs if d.is_draft]
        a, b = (drafts[-2], drafts[-1]) if len(drafts) >= 2 else (docs[-2], docs[-1])
    only_b = by_pk.get(_int(request.GET.get("b") or request.POST.get("b")) or 0)
    view = request.GET.get("view") or request.POST.get("view") or ""
    if only_b and only_b.tracked and (view == "tracked" or (not view and not request.GET.get("a"))):
        b, view = only_b, "tracked"  # the newer file's own Word redline, author by author
        a = a if a and a != b else next((d for d in reversed(docs[:docs.index(b)])), None)
        body, stats, authors = tracked_redline(b)
    else:
        view = "compare"
        body, stats, authors = (redline_for(contract, docs, a, b) if a and b and a != b else (None, {}, []))
    here = reverse("crm:contract_compare", args=[contract.pk]) + (
        f"?b={b.pk}&view=tracked" if view == "tracked" else f"?a={a.pk}&b={b.pk}&view=compare" if a and b else "")
    if request.method == "POST" and request.POST.get("action") == "annotate" and body:
        try:
            made = ai.annotate_changes(contract, a, b, stats["items"], request.user)
            messages.success(request, f"Claude left {len(made)} note{'s' if len(made) != 1 else ''} on the changes.")
        except ai.Unavailable as exc:
            messages.error(request, str(exc))
        return redirect(here)
    items = stats.get("items", [])
    threads = doc_comments(b) if b else []
    for item in items:
        item["comments"] = [c for c in threads if c.start is not None and (
            item["start"] <= c.start <= max(item["end"], item["start"]))]
    pinned = {c.pk for item in items for c in item["comments"]}
    return render(request, "crm/contract_compare.html", {
        "contract": contract, "docs": docs, "a": a, "b": b, "body": mark_safe(body or ""), "stats": stats,
        "items": items, "authors": authors, "other_comments": [c for c in threads if c.pk not in pinned],
        "notes": sum(1 for c in threads if c.by_claude), "question": COMPARE_QUESTION, "ai_ready": ai_enabled(),
        "users": _users(), "here": here, "can_manage": is_manager(request.user), "view": view,
    })


def tracked_redline(doc):
    """(html, stats, authors) for a Word file's own tracked changes: the document as its author marked it up, each
    change (a deletion and the insertion next to it, by one person) numbered in the sidebar."""
    slots, items, marks = {}, [], []
    for c in sorted(doc.tracked or [], key=lambda c: (c["start"], c["type"] != "del")):
        name = c.get("author") or "Unknown"
        cls = slots.setdefault(name, f"rl-a{len(slots) % AUTHOR_COLORS}")
        last = items[-1] if items else None
        if last and last["author"] == name and c["start"] <= last["end"] + 1:
            last["end"] = max(last["end"], c["end"])
        else:
            last = {"n": len(items) + 1, "author": name, "start": c["start"], "end": c["end"], "deleted": "",
                    "inserted": "", "cls": cls,
                    "who": name + (f" ({c['date']})" if c.get("date") else "") + " · tracked in Word"}
            items.append(last)
        if c["type"] == "del":
            last["deleted"] = (last["deleted"] + " " + c.get("text", "")).strip()
        else:
            last["inserted"] = (last["inserted"] + " " + doc.text[c["start"]:c["end"]]).strip()
        marks.append({**c, "n": last["n"], "cls": cls})
    body = textmod.reader_html(doc.text, doc.page_starts, tracked=marks)
    counts = {}
    for i in items:
        counts.setdefault(i["author"], {"name": i["author"], "cls": i["cls"], "count": 0})["count"] += 1
    stats = {"changes": len(items), "items": items,
             "inserted": sum(len(i["inserted"].split()) for i in items),
             "deleted": sum(len(i["deleted"].split()) for i in items)}
    return body, stats, list(counts.values())


# ---------------------------------------------------------------------------
# Closing timeline
# ---------------------------------------------------------------------------


def _versions(contract, docs):
    """Every timeline there is to show: each version Claude has read, oldest first, then the contract's kept dates."""
    out = []
    for d in docs:
        if d.timeline:
            out.append({"id": str(d.pk), "label": d.title, "who": d.made_by, "dated": d.dated, "doc": d,
                        "terms": timeline.from_document(d), "kept": False})
    kept = timeline.from_dates(contract)
    if kept:
        out.append({"id": "kept", "label": "Executed agreement" if contract.status in dates.EXECUTED
                    else "Critical dates (kept)", "who": "", "dated": contract.effective_date, "doc": None,
                    "terms": kept, "kept": True})
    return out


def _effective(request, contract, today):
    try:
        return date.fromisoformat(request.GET.get("eff") or request.POST.get("eff") or "")
    except ValueError:
        return contract.effective_date or today


def executed_doc(contract, docs=None):
    """The final executed agreement (the latest signed original or restatement), once the contract is signed."""
    if contract.status not in dates.EXECUTED:
        return None
    docs = ai.chain(contract) if docs is None else docs
    return next((d for d in reversed(docs) if d.kind in (ContractDocument.Kind.ORIGINAL,
                                                         ContractDocument.Kind.RESTATED)), None)


def timeline_strip(contract, today):
    """The contract page's timeline: the kept dates (as the contract stands), else the newest version read."""
    versions = _versions(contract, ai.chain(contract))
    if not versions:
        return None
    v = versions[-1]
    eff = contract.effective_date or today
    rows = timeline.compute(v["terms"], eff, prefer_rules=not v["kept"], today=today)
    lanes, ticks = timeline.lanes([{**v, "rows": rows}], (min((r["when"] for r in rows if r["when"]), default=None),
                                                          max((r["when"] for r in rows if r["when"]), default=None)),
                                  today)
    upcoming = [r for r in rows if r["when"] and r["when"] >= today and not r.get("done")][:4]
    return {"version": v, "rows": rows, "lanes": lanes, "ticks": ticks, "eff": eff, "upcoming": upcoming}


def contract_timeline(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    today = timezone.localdate()
    docs = ai.chain(contract)
    if request.method == "POST":
        return _timeline_post(request, contract, docs, today)
    eff = _effective(request, contract, today)
    versions = _versions(contract, docs)
    picked = request.GET.getlist("v") or [v["id"] for v in versions[-2:]]
    shown = [v for v in versions if v["id"] in picked]
    for v in shown:
        v["rows"] = timeline.compute(v["terms"], eff, prefer_rules=not v["kept"] or eff != contract.effective_date,
                                     today=today)
        for r in v["rows"]:
            if r.get("doc") and r.get("start") is not None:
                r["href"] = _reader_url(contract.pk, r["doc"], r["start"], r["end"])
    table, span = timeline.compare(shown)
    lanes, ticks = timeline.lanes(shown, span, today)
    options = []
    if contract.status in dates.EXECUTED or any(v["kept"] for v in shown):
        for d in contract.dates.filter(confirmed=True, kind=ContractDate.Kind.EXTENSION):
            if d.done:
                on = next((h["exercised"] for h in reversed(d.history or []) if h.get("exercised")), None)
                options.append({"ext": d, "exercised": date.fromisoformat(on) if on else None})
            else:
                options.append(timeline.proposal(d, today, target_pk=request.GET.get(f"t{d.pk}")))
    main = shown[-1] if shown else None
    months = []
    for r in (main["rows"] if main else []):
        label = f"{r['when']:%B %Y}" if r["when"] else "Not fixed yet"
        if not months or months[-1]["label"] != label:
            months.append({"label": label, "rows": []})
        months[-1]["rows"].append(r)
    return render(request, "crm/contract_timeline.html", {
        "main": main, "months": months, "executed": executed_doc(contract, docs),
        "contract": contract, "eff": eff, "versions": versions, "shown": shown, "picked": picked, "table": table,
        "lanes": lanes, "ticks": ticks, "options": options, "today": today, "ai_ready": ai_enabled(),
        "unread": [d for d in docs if ai.readable(d) and not d.timeline], "kinds": ContractDate.Kind.choices,
        "pushable": [d for d in contract.dates.filter(confirmed=True, done=False, date__isnull=False)],
        "what_if": eff != contract.effective_date,
    })


def _timeline_post(request, contract, docs, today):
    action = request.POST.get("action", "")
    back = reverse("crm:contract_timeline", args=[contract.pk])
    query = request.POST.get("query", "")
    if query.startswith("?"):
        back += query
    user = request.user
    if action == "read":
        doc = get_object_or_404(ContractDocument, pk=request.POST.get("doc"), contract=contract)
        try:
            found = ai.read_timeline(doc, user)
            messages.success(request, f"Read {len(found)} date{'s' if len(found) != 1 else ''} from {doc.title}.")
        except ai.Unavailable as exc:
            messages.error(request, str(exc))
    elif action == "keep":
        doc = get_object_or_404(ContractDocument, pk=request.POST.get("doc"), contract=contract)
        eff = _effective(request, contract, today)
        have = set(contract.dates.exclude(key="").values_list("key", flat=True))
        contract.dates.filter(confirmed=False).delete()
        made = 0
        for r in timeline.compute(timeline.from_document(doc), eff, today=today):
            if r["key"] in have:
                continue
            ContractDate.objects.create(
                contract=contract, kind=r["kind"], label=r["label"], date=r["when"], rule=r.get("rule") or r["how"],
                amount=r.get("amount"), quote=r.get("quote") or "", source_id=r.get("doc"),
                source_start=r.get("start"), source_end=r.get("end"), confirmed=False, from_claude=True,
                created_by=user, key=r["key"], anchor=r.get("anchor") or "", offset=r.get("offset"),
                unit=(r.get("unit") or "") if r.get("offset") is not None else "", extends=r.get("extends") or "",
                extend_by=r.get("extend_by"), extend_unit=(r.get("extend_unit") or "") if r.get("extend_by") else "",
                critical=r["kind"] != ContractDate.Kind.PAYMENT)
            made += 1
        messages.success(request, f"{made} date{'s' if made != 1 else ''} from {doc.title} wait under Critical dates: "
                                  "check them and keep the right ones.")
        return redirect(contract.get_absolute_url() + "#dates")
    elif action in ("exercise", "push"):
        d = get_object_or_404(ContractDate, pk=request.POST.get("date_id"), contract=contract, confirmed=True)
        prop = timeline.proposal(d, today, target_pk=request.POST.get("target"))
        if prop["target"] is None or prop["target"].pk == d.pk and action == "exercise":
            messages.error(request, "Pick the deadline this extension pushes out.")
            return redirect(back)
        try:
            new = date.fromisoformat(request.POST.get("new_date") or "")
        except ValueError:
            new = prop["new"]
        try:
            notice_on = date.fromisoformat(request.POST.get("notice_on") or "")
        except ValueError:
            notice_on = today
        target = prop["target"]
        if new is None:
            messages.error(request, "Pick the new date.")
            return redirect(back)
        also_ids = {int(i) for i in request.POST.getlist("also") if i.isdigit()}
        also = [x for x, _, _ in prop["also"] if x.pk in also_ids]
        note = (request.POST.get("note") or "").strip()[:300]
        if action == "exercise" and not d.extends:
            d.extends = target.key  # remembered for next time
            d.save(update_fields=["extends"])
        if action == "exercise":
            why = f"{d.label} exercised, notice {notice_on:%b} {notice_on.day}, {notice_on.year}" + (
                f": {note}" if note else "")
            moved = timeline.push(target, new, user, why, also=also, ext=d, notice_on=notice_on)
        else:
            moved = timeline.push(target, new, user, note or "Date moved", also=also)
        said = "; ".join(f"{x.label} {old:%b} {old.day} → {nw:%b} {nw.day}" if old else f"{x.label} → {nw:%b} {nw.day}"
                         for x, old, nw in moved)
        messages.success(request, (f"Confirmed: {d.label}. " if action == "exercise" else "")
                         + (f"Moved {said}." if moved else "No dates changed."))
    return redirect(back)


# ---------------------------------------------------------------------------
# Review templates
# ---------------------------------------------------------------------------


def template_list(request):
    methods.ensure_builtins()
    templates = list(ContractTemplate.objects.annotate(runs=Count("answers")))
    return render(request, "crm/contract_templates.html", {
        "builtin": [t for t in templates if t.builtin], "team": [t for t in templates if not t.builtin],
        "kind_labels": dict(Contract.Kind.choices),
    })


def template_edit(request, pk=None):
    """Make or edit a team template. Built-ins can't be edited here; "Copy" makes a team version to change."""
    template = get_object_or_404(ContractTemplate, pk=pk) if pk else ContractTemplate(created_by=request.user)
    copy_of = None
    if template.builtin:
        copy_of = template
        template = ContractTemplate(name=f"{copy_of.name} (ACMI)", description=copy_of.description,
                                    kinds=copy_of.kinds, points=copy_of.points, instructions=copy_of.instructions,
                                    created_by=request.user)
    if request.method == "POST" and request.POST.get("action") == "delete":
        if not template.pk:
            raise Http404
        if not is_manager(request.user):
            return refuse(request)
        template.delete()
        messages.success(request, f"Deleted {template.name}.")
        return redirect("crm:contract_templates")
    form = TemplateForm(request.POST or None, instance=template,
                        initial={"points_text": methods.points_text(template.points), "kinds": template.kinds})
    if request.method == "POST" and form.is_valid():
        template = form.save(commit=False)
        template.kinds = form.cleaned_data["kinds"]
        template.points = methods.parse_points(form.cleaned_data["points_text"])
        template.key = ""
        template.save()
        messages.success(request, f"Saved {template.name}.")
        return redirect("crm:contract_templates")
    return render(request, "crm/contract_template_edit.html", {"form": form, "template": template,
                                                                "copy_of": copy_of})
