"""Capital pages: every lender and investor across the projects (/capital/), one project's stack (/projects/<id>/capital/),
one source with its term sheets, touches and log (/capital/<id>/), and term sheets side by side (/capital/compare/)."""
from datetime import date

from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..ai import enabled as ai_enabled
from ..contracts import text as textmod
from ..models import CapitalLog, CapitalProvider, CapitalSource, Campus, Contract, Project, TermSheet
from ..permissions import is_manager, refuse
from . import ai, logic
from .forms import ProviderForm, SourceForm, TermSheetForm

INLINE_TYPES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp")
WAITING = {"us": "Waiting on us", "them": "Waiting on them", "third": "Waiting on a third party"}


def _date_or_none(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def capital_index(request):
    """The portfolio: who we're talking to for debt and equity across the projects, their stage, the latest terms,
    what's waiting on whom, and offers about to expire."""
    show = request.GET.get("show", "active")
    kind = request.GET.get("kind", "")
    campus = request.GET.get("campus", "")
    today = timezone.localdate()
    qs = logic.with_related(CapitalSource.objects.all())
    if show == "active":
        qs = qs.filter(status__in=CapitalSource.ACTIVE)
    elif show == "closed":
        qs = qs.filter(status=CapitalSource.Status.CLOSED)
    if kind == "debt":
        qs = qs.filter(kind__in=CapitalSource.DEBT)
    elif kind == "equity":
        qs = qs.filter(kind__in=CapitalSource.EQUITY)
    if campus:
        qs = qs.filter(project__campus__code__iexact=campus)
    sources = logic.annotate(list(qs), today)
    sources.sort(key=lambda s: (s.project.campus.code if s.project.campus_id else "~", s.project.name.lower(),
                                logic.sort_key(s)))
    groups = []
    for s in sources:
        if not groups or groups[-1]["project"] != s.project:
            groups.append({"project": s.project, "rows": []})
        groups[-1]["rows"].append(s)
    for g in groups:
        g["stack"] = logic.stack(g["project"], [s for s in g["rows"]])
    stages = [(v, l, sum(1 for s in sources if s.status == v)) for v, l in CapitalSource.Status.choices]
    deadlines = sorted(logic.due_rows(60, today), key=lambda r: r.date)
    for r in deadlines:
        r.left = (r.date - today).days
        r.late = -r.left
    return render(request, "crm/capital_index.html", {
        "groups": groups, "count": len(sources), "show": show, "kind": kind, "campus": campus,
        "campuses": Campus.objects.all(), "stages": [x for x in stages if x[2]],
        "waiting": {k: [s for s in sources if s.waiting_on == k] for k in ("us", "them", "third")},
        "waiting_labels": WAITING,
        "deadlines": {"overdue": [r for r in deadlines if r.left < 0], "soon": [r for r in deadlines if r.left >= 0],
                      "days": 60},
        "quiet": [s for s in sources if s.quiet],
        "projects": Project.objects.filter(active=True).select_related("campus"),
        "kinds": CapitalSource.Kind.choices,
    })


def project_capital(request, pk):
    """One project's stack: every source in stack order, recent touches across them, and the latest terms side by side."""
    project = get_object_or_404(Project.objects.select_related("campus", "parent"), pk=pk)
    today = timezone.localdate()
    sources = logic.annotate(list(logic.with_related(project.capital_sources.all())), today)
    st = logic.stack(project, sources)
    feed = []
    for s in sources:
        for t in logic.touches(s, today, days=60, limit=10):
            t.source = s
            feed.append(t)
    feed.sort(key=lambda t: t.date, reverse=True)
    latest = [s.ts for s in sorted(sources, key=logic.sort_key) if s.ts and s.is_active]
    return render(request, "crm/capital_project.html", {
        "project": project, "stack": st, "sources": sorted(sources, key=logic.sort_key), "feed": feed[:30],
        "latest": latest, "compare": logic.compare(latest) if len(latest) > 1 else [],
        "form": SourceForm(initial={"project": project, "owner": request.user}),
        "today": today,
    })


def source_create(request):
    project = Project.objects.filter(pk=request.GET.get("project") or request.POST.get("project") or 0).first()
    initial = {"owner": request.user, "project": project}
    if request.GET.get("kind") in CapitalSource.Kind.values:
        initial["kind"] = request.GET["kind"]
    form = SourceForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        source = form.save(commit=False, user=request.user)
        source.created_by, source.status_since = request.user, timezone.localdate()
        source.save()
        CapitalLog.objects.create(source=source, kind=CapitalLog.Kind.STATUS, user=request.user,
                                  text=f"Added as {source.get_status_display().lower()}")
        messages.success(request, f"Added {source.provider}. Log calls, upload term sheets and set the next step here.")
        return redirect(source)
    back = reverse("crm:project_capital", args=[project.pk]) if project else reverse("crm:capital_index")
    return render(request, "crm/form.html", {"form": form, "title": "New capital source", "cancel": back,
                                             "provider_names": True})


def source_detail(request, pk):
    source = get_object_or_404(logic.with_related(CapitalSource.objects.all()), pk=pk)
    if request.method == "POST":
        return _post(request, source)
    today = timezone.localdate()
    logic.annotate([source], today)
    sheets = list(source.term_sheets.all())
    for t in sheets:
        t.form = TermSheetForm(instance=t, prefix=f"ts{t.pk}")
        t.quoted = [(logic.term_label(f), v) for f, v in (t.sources or {}).items() if f in logic.TERM_KINDS]
        t.terms = [(f, logic.term_label(f), logic.show(t, f), (t.sources or {}).get(f)) for f, _, _ in logic.TERMS
                   if logic.show(t, f)]
    return render(request, "crm/capital_source.html", {
        "s": source, "sheets": sheets, "touches": logic.touches(source, today),
        "upcoming": logic.upcoming_meetings(source.provider), "compare": logic.compare(sheets) if len(sheets) > 1 else [],
        "form": SourceForm(instance=source), "ts_form": TermSheetForm(initial={"dated": today}, prefix="new"),
        "statuses": CapitalSource.Status.choices, "log_kinds": [
            (k, l) for k, l in CapitalLog.Kind.choices if k in ("note", "call", "meeting")],
        "contracts": Contract.objects.filter(kind__in=[Contract.Kind.LOAN, Contract.Kind.JV]).order_by("title"),
        "ai_ready": ai_enabled(), "can_manage": is_manager(request.user), "today": today,
        "head_fields": ["kind", "status", "dated", "file", "title"],
    })


def _read_file(ts, upload):
    data = upload.read()
    upload.seek(0)
    ts.text, ts.page_starts, ts.text_status = textmod.extract(data, upload.name)
    ts.pages = len(ts.page_starts)


def _post(request, source):
    action = request.POST.get("action", "")
    user, today, anchor = request.user, timezone.localdate(), ""
    if action == "details":
        old_status = source.status
        form = SourceForm(request.POST, instance=source)
        if form.is_valid():
            new_status = form.cleaned_data["status"]
            source = form.save(commit=False, user=user)
            source.status = old_status  # set_status dates and logs the change
            source.save()
            logic.set_status(source, new_status, user, today)
            messages.success(request, "Saved.")
        else:
            messages.error(request, "Not saved: " + " ".join(" ".join(v) for v in form.errors.values()))
    elif action == "status":
        if logic.set_status(source, request.POST.get("status", ""), user, today,
                            note=(request.POST.get("note") or "").strip()[:200]):
            if source.status in (CapitalSource.Status.PASSED, CapitalSource.Status.DECLINED, CapitalSource.Status.CLOSED):
                source.waiting_on = ""
                source.save(update_fields=["waiting_on", "updated_at"])
    elif action == "waiting":
        value = request.POST.get("waiting_on", "")
        if value in ("", "us", "them", "third"):
            source.waiting_on = value
            source.waiting_since = today if value else None
            source.next_step = (request.POST.get("next_step") or source.next_step).strip()[:300]
            if "next_step_on" in request.POST:
                source.next_step_on = _date_or_none(request.POST.get("next_step_on"))
            source.save()
    elif action == "contact":
        source.contact = (request.POST.get("contact") or "").strip()[:200]
        source.save(update_fields=["contact", "updated_at"])
    elif action == "log":
        anchor = "touches"
        text = (request.POST.get("text") or "").strip()
        kind = request.POST.get("kind", "note")
        if text and kind in ("note", "call", "meeting"):
            CapitalLog.objects.create(source=source, kind=kind, text=text, user=user,
                                      happened_on=_date_or_none(request.POST.get("happened_on")) or today)
    elif action == "log_delete":
        anchor = "touches"
        entry = get_object_or_404(CapitalLog, pk=request.POST.get("entry"), source=source)
        if not (is_manager(user) or entry.user_id == user.pk):
            return refuse(request)
        entry.delete()
    elif action == "link_contract":
        contract = Contract.objects.filter(pk=request.POST.get("contract") or 0).first()
        source.contract = contract
        source.save(update_fields=["contract", "updated_at"])
    elif action == "start_contract":
        if source.contract_id:
            return redirect(source.contract)
        kind = Contract.Kind.JV if source.kind in CapitalSource.EQUITY else Contract.Kind.LOAN
        contract = Contract.objects.create(
            title=f"{source.project.name} {source.label or source.get_kind_display()} ({source.provider})"[:200],
            kind=kind, status=Contract.Status.DRAFTING, counterparty=source.provider.name,
            campus=source.project.campus, price=source.amount_committed or source.amount_sought, owner=source.owner,
            created_by=user)
        source.contract = contract
        source.save(update_fields=["contract", "updated_at"])
        messages.success(request, "Started the agreement. Upload drafts there; once signed its dates reach the calendar.")
        return redirect(contract)
    elif action == "ts_add":
        anchor = "term-sheets"
        form = TermSheetForm(request.POST, request.FILES, prefix="new")
        if form.is_valid():
            ts = form.save(commit=False)
            ts.source, ts.added_by, ts.version = source, user, logic.next_version(source)
            upload = request.FILES.get("new-file")
            if upload:
                _read_file(ts, upload)
                if not ts.title:
                    ts.title = upload.name.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", " ")[:300]
            ts.save()
            CapitalLog.objects.create(source=source, kind=CapitalLog.Kind.TERM_SHEET, user=user,
                                      happened_on=ts.dated or today,
                                      text=f"{ts.get_kind_display()} v{ts.version} received" + (
                                          f": {logic.money(ts.amount)}" if ts.amount else ""))
            logic.advance(source, CapitalSource.Status.TERM_SHEET, user, today)
            note = {"scan": " It's a scan: Claude can read it, but quotes can't be matched to text.",
                    "unsupported": " That file type can't be read.", "error": " The file couldn't be read."}
            text = f"Added {ts.get_kind_display().lower()} v{ts.version}." + note.get(ts.text_status, "")
            if upload and request.POST.get("read") == "1" and ai_enabled():
                try:
                    filled = ai.read_terms(ts, user)
                    text += (f" Claude filled in {len(filled)} term{'s' if len(filled) != 1 else ''}; check each "
                             "against its quote." if filled else " Claude found no terms to fill in.")
                except ai.Unavailable as exc:
                    text += f" Claude couldn't read it: {exc}"
            messages.success(request, text)
            anchor = f"ts-{ts.pk}"
        else:
            messages.error(request, "Not added: " + " ".join(" ".join(v) for v in form.errors.values()))
    elif action in ("ts_edit", "ts_read", "ts_delete", "ts_status"):
        ts = get_object_or_404(TermSheet, pk=request.POST.get("ts"), source=source)
        anchor = f"ts-{ts.pk}"
        if action == "ts_edit":
            form = TermSheetForm(request.POST, request.FILES, instance=ts, prefix=f"ts{ts.pk}")
            if form.is_valid():
                ts = form.save(commit=False)
                upload = request.FILES.get(f"ts{ts.pk}-file")
                if upload:
                    _read_file(ts, upload)
                changed = [f for f in form.changed_data if f in (ts.sources or {})]
                for f in changed:  # a term typed over Claude's no longer comes from the quote
                    ts.sources.pop(f, None)
                ts.save()
                messages.success(request, "Saved.")
            else:
                messages.error(request, "Not saved: " + " ".join(" ".join(v) for v in form.errors.values()))
        elif action == "ts_status":
            value = request.POST.get("status", "")
            if value in TermSheet.Status.values and value != ts.status:
                ts.status = value
                ts.save(update_fields=["status"])
                CapitalLog.objects.create(source=source, kind=CapitalLog.Kind.TERM_SHEET, user=user,
                                          text=f"{ts.get_kind_display()} v{ts.version}: {ts.get_status_display().lower()}")
        elif action == "ts_read":
            try:
                filled = ai.read_terms(ts, user)
                messages.success(request, f"Claude filled in {len(filled)} term{'s' if len(filled) != 1 else ''}"
                                 + (f": {', '.join(filled)}. Check each against its quote." if filled
                                    else ". Terms already typed in are left alone."))
            except ai.Unavailable as exc:
                messages.error(request, str(exc))
        else:
            if not is_manager(user):
                return refuse(request)
            if ts.file:
                ts.file.delete(save=False)
            ts.delete()
            anchor = "term-sheets"
    elif action == "delete":
        if not is_manager(user):
            return refuse(request)
        for ts in source.term_sheets.all():
            if ts.file:
                ts.file.delete(save=False)
        project = source.project
        source.delete()
        messages.success(request, f"Deleted {source}.")
        return redirect("crm:project_capital", project.pk)
    return redirect(source.get_absolute_url() + (f"#{anchor}" if anchor else ""))


def compare(request):
    """Term sheets side by side (?ts=1&ts=2...), printable with the logo. Differences are marked."""
    ids = [i for i in request.GET.getlist("ts") if i.isdigit()]
    sheets = list(TermSheet.objects.filter(pk__in=ids).select_related("source", "source__project", "source__provider"))
    sheets.sort(key=lambda t: (logic.sort_key(t.source), t.source.provider.name.lower(), t.version))
    if not sheets:
        raise Http404("Pick the term sheets to compare.")
    projects = {t.source.project for t in sheets}
    return render(request, "crm/capital_compare.html", {
        "sheets": sheets, "rows": logic.compare(sheets), "project": projects.pop() if len(projects) == 1 else None,
        "today": timezone.localdate(),
    })


def term_sheet_file(request, pk):
    ts = get_object_or_404(TermSheet, pk=pk)
    if not ts.file:
        raise Http404
    try:
        fh = ts.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("That file is no longer on the server.")
    name = ts.file.name.rsplit("/", 1)[-1]
    return FileResponse(fh, as_attachment=not name.lower().endswith(INLINE_TYPES), filename=name)


@require_POST
def quick_add(request, pk):
    """The project page's "Add a source" row: provider, kind and amount, then on to the source's page."""
    project = get_object_or_404(Project, pk=pk)
    data = request.POST.copy()
    data.setdefault("status", CapitalSource.Status.IDENTIFIED)
    data["project"] = project.pk
    data.setdefault("owner", request.user.pk)
    form = SourceForm(data)
    if form.is_valid():
        source = form.save(commit=False, user=request.user)
        source.created_by, source.status_since = request.user, timezone.localdate()
        source.save()
        CapitalLog.objects.create(source=source, kind=CapitalLog.Kind.STATUS, user=request.user,
                                  text=f"Added as {source.get_status_display().lower()}")
        messages.success(request, f"Added {source.provider}.")
        return redirect(source)
    messages.error(request, "Not added: " + " ".join(" ".join(v) for v in form.errors.values()))
    return redirect("crm:project_capital", project.pk)


def provider_list(request):
    """Lenders, brokers, investors and agencies: their own list, apart from Companies, never in outreach."""
    providers = CapitalProvider.objects.prefetch_related("sources__project")
    form = ProviderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        provider = form.save(commit=False)
        provider.created_by = request.user
        provider.save()
        moved = logic.file_queued(provider)
        messages.success(request, f"Added {provider.name}." + (
            f" Filed {moved} queued email{'s' if moved != 1 else ''} on it." if moved else ""))
        return redirect(provider)
    return render(request, "crm/capital_providers.html", {"providers": providers, "form": form})


def provider_detail(request, pk):
    provider = get_object_or_404(CapitalProvider, pk=pk)
    form = ProviderForm(instance=provider)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "details":
            form = ProviderForm(request.POST, instance=provider)
            if form.is_valid():
                provider = form.save()
                moved = logic.file_queued(provider)
                messages.success(request, "Saved." + (
                    f" Filed {moved} email{'s' if moved != 1 else ''} from the review queue on it." if moved else ""))
                return redirect(provider)
            messages.error(request, "Not saved: " + " ".join(" ".join(v) for v in form.errors.values()))
        elif action == "contact_add":
            if (request.POST.get("name") or "").strip():
                provider.contacts = (provider.contacts or []) + [{
                    k: (request.POST.get(k) or "").strip()[:200] for k in ("name", "title", "email", "phone")}]
                provider.save()
            return redirect(provider.get_absolute_url() + "#contacts")
        elif action == "contact_delete":
            try:
                i = int(request.POST.get("i", ""))
            except ValueError:
                i = -1
            provider.contacts = [c for n, c in enumerate(provider.contacts or []) if n != i]
            provider.save()
            return redirect(provider.get_absolute_url() + "#contacts")
        elif action == "delete":
            if not is_manager(request.user):
                return refuse(request)
            if provider.sources.exists():
                messages.error(request, "Remove its capital sources first.")
                return redirect(provider)
            provider.delete()
            messages.success(request, f"Deleted {provider.name}.")
            return redirect("crm:capital_providers")
        else:
            return redirect(provider)
    today = timezone.localdate()
    sources = logic.annotate(list(logic.with_related(provider.sources.all())), today, touch=False)
    return render(request, "crm/capital_provider.html", {
        "p": provider, "form": form, "sources": sources,
        "touches": logic.touches(None, today, provider=provider, limit=60),
        "upcoming": logic.upcoming_meetings(provider),
    })


def provider_names(request):
    return JsonResponse(list(CapitalProvider.objects.values_list("name", flat=True)), safe=False)
