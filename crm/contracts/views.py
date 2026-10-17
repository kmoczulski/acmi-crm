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
    Campus, ClosingItem, Contract, ContractAnswer, ContractDate, ContractDocument, ContractTemplate, LandSite,
    Opportunity,
)
from ..permissions import is_manager, manager_required, refuse
from . import ai, closing, compare, dates, methods
from . import text as textmod
from .forms import ContractForm, DateForm, DocumentForm, TemplateForm

INLINE_TYPES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp")
COMPARE_QUESTION = ("What changed between these two documents? List each change in order with the section, what it "
                    "said before, what it says now, and what it means for ACMI. Flag anything that looks unintended.")


# ---------------------------------------------------------------------------
# List and create
# ---------------------------------------------------------------------------


def contract_list(request):
    show = request.GET.get("show", "open")
    kind = request.GET.get("kind", "")
    campus = request.GET.get("campus", "")
    qs = Contract.objects.select_related("site", "campus", "owner", "opportunity__company").annotate(
        docs=Count("documents", distinct=True))
    if show == "open":
        qs = qs.filter(status__in=Contract.OPEN_STATUSES)
    if kind in Contract.Kind.values:
        qs = qs.filter(kind=kind)
    if campus:
        qs = qs.filter(campus__code__iexact=campus)
    contracts = list(qs)
    today = timezone.localdate()
    nxt = dates.next_dates(contracts, today)
    for c in contracts:
        c.next = nxt.get(c.pk)
        c.waiting_days = (today - c.waiting_since).days if c.waiting_on and c.waiting_since else None
    contracts.sort(key=lambda c: (c.next is None, c.next.date if c.next else today, c.title.lower()))
    return render(request, "crm/contract_list.html", {
        "contracts": contracts, "deadlines": dates.upcoming(60, today), "show": show, "kind": kind,
        "kinds": Contract.Kind.choices, "campuses": Campus.objects.all(), "campus": campus,
    })


def contract_create(request):
    initial = {"owner": request.user}
    site = LandSite.objects.select_related("campus").filter(pk=request.GET.get("site") or 0).first()
    deal = Opportunity.objects.select_related("company").filter(pk=request.GET.get("opportunity") or 0).first()
    if site:
        initial.update(site=site, campus=site.campus, kind=Contract.Kind.PSA, title=f"{site.name} PSA")
    elif deal:
        initial.update(opportunity=deal, campus=deal.campus, kind=Contract.Kind.LOI, counterparty=deal.company.name,
                       title=f"{deal.company.name} LOI")
    form = ContractForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        contract = form.save(commit=False)
        contract.created_by = request.user
        contract.save()
        form.save_m2m()
        messages.success(request, "Contract added. Upload the agreement to read it with Claude.")
        return redirect(contract)
    return render(request, "crm/form.html", {"form": form, "title": "New contract",
                                             "cancel": reverse("crm:contract_list")})


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
    doc.text, doc.page_starts, doc.text_status = textmod.extract(data, upload.name)
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
    contract = get_object_or_404(Contract.objects.select_related("site", "campus", "owner", "opportunity__company"),
                                 pk=pk)
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
        d.left = d.days_left(today)
        d.late = -d.left if d.left is not None else None
        if d.source_id and d.source_start is not None:
            d.source_href = _reader_url(contract.pk, d.source_id, d.source_start, d.source_end)
    readable = ai.default_docs(contract)
    drafts = [d for d in docs if d.is_draft]
    for prev, d in zip(drafts, drafts[1:]):
        d.prev = prev if prev.text and d.text else None
    purchase = contract.kind in (Contract.Kind.PSA, Contract.Kind.OPTION)
    return render(request, "crm/contract_detail.html", {
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
    })


def _post(request, contract):
    action = request.POST.get("action", "")
    anchor = ""
    user = request.user
    if action == "details":
        form = ContractForm(request.POST, instance=contract)
        if form.is_valid():
            form.save()
            # Owners picked on a site it no longer belongs to stop getting its dates.
            contract.parcel_deals.remove(*contract.parcel_deals.exclude(site_id=contract.site_id))
            dates.sync_site(contract, user)
            messages.success(request, "Saved.")
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
    return FileResponse(fh, as_attachment=not name.lower().endswith(INLINE_TYPES), filename=name)


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
    body = textmod.reader_html(doc.text, doc.page_starts, terms=terms.keys(), links=links, anchors=anchors,
                               badges=badges, highlight=highlight) if doc.text else ""
    outline = sorted(((num, s) for num, (s, _) in own.items()), key=lambda x: x[1])
    return render(request, "crm/contract_reader.html", {
        "contract": contract, "doc": doc, "docs": docs, "body": mark_safe(body), "glossary": terms,
        "outline": [(num, doc.text[s:s + 90].split("\n", 1)[0].strip()) for num, s in outline][:400],
        "base": base, "changed": sum(len(v) for v in badges.values()),
    })


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


def contract_compare(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    docs = [d for d in ai.chain(contract) if d.text]
    by_pk = {d.pk: d for d in docs}
    a = by_pk.get(int(request.GET.get("a") or 0))
    b = by_pk.get(int(request.GET.get("b") or 0))
    if not (a and b) and len(docs) >= 2:
        drafts = [d for d in docs if d.is_draft]
        a, b = (drafts[-2], drafts[-1]) if len(drafts) >= 2 else (docs[-2], docs[-1])
    body, stats = (compare.redline(a.text, b.text) if a and b and a != b else (None, {}))
    return render(request, "crm/contract_compare.html", {
        "contract": contract, "docs": docs, "a": a, "b": b, "body": mark_safe(body or ""), "stats": stats,
        "question": COMPARE_QUESTION, "ai_ready": ai_enabled(),
    })


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
