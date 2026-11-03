"""Entitlement pages: every approval across the campuses with what's next, one approval's steps, conditions and
papers, and the permit matrix a project or site starts from."""
from datetime import date
from types import SimpleNamespace

from django.contrib import messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from ..ai import enabled as ai_enabled
from ..contracts import ai as contract_ai
from ..contracts import text as textmod
from ..models import (
    Approval, ApprovalCondition, ApprovalDocument, ApprovalStep, Campus, Contract, ContractDate, LandSite, Project,
)
from ..permissions import is_manager, refuse
from . import ai, logic
from .forms import ApprovalForm, DocumentForm

INLINE_TYPES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp")
CLOSED = ("withdrawn", "expired", "denied")


def _for_label(a):
    return str(a.project or a.site or a.campus or "")


def approval_list(request):
    show = request.GET.get("show", "active")
    campus = request.GET.get("campus", "")
    kind = request.GET.get("kind", "")
    qs = logic.with_related(Approval.objects.all())
    if show == "active":
        qs = qs.exclude(status__in=CLOSED)
    if campus:
        qs = qs.filter(campus__code__iexact=campus)
    if kind in Approval.Kind.values:
        qs = qs.filter(kind=kind)
    today = timezone.localdate()
    approvals = logic.annotate(list(qs), today)
    approvals.sort(key=lambda a: (a.campus.code if a.campus else "~", _for_label(a).lower(),
                                  a.next.date if a.next else (a.needed_by or date.max), a.title.lower()))
    groups = []
    for a in approvals:
        key = (a.campus.code if a.campus else "No campus", _for_label(a))
        if not groups or groups[-1]["key"] != key:
            groups.append({"key": key, "campus": key[0], "label": key[1], "rows": []})
        groups[-1]["rows"].append(a)
    coming = sorted(logic.due_rows(30, today), key=lambda r: r.date)
    for r in coming:
        r.left = (r.date - today).days
        r.late = -r.left
    return render(request, "crm/entitlement_list.html", {
        "groups": groups, "count": len(approvals), "show": show, "campus": campus, "kind": kind,
        "kinds": Approval.Kind.choices, "campuses": Campus.objects.all(), "matrix": logic.MATRIX,
        "projects": Project.objects.filter(active=True).select_related("campus"),
        "sites": LandSite.objects.exclude(status=LandSite.Status.DROPPED).order_by("name"),
        "coming": {"overdue": [r for r in coming if r.left < 0], "soon": [r for r in coming if r.left >= 0],
                   "days": 30},
    })


def approval_create(request):
    initial = {"owner": request.user}
    project = Project.objects.filter(pk=request.GET.get("project") or 0).first()
    site = LandSite.objects.filter(pk=request.GET.get("site") or 0).first()
    contract = Contract.objects.filter(pk=request.GET.get("contract") or 0).first()
    if project:
        initial.update(project=project, campus=project.campus)
    if site:
        initial.update(site=site, campus=site.campus)
    if contract:
        initial.update(contract=contract, site=contract.site, campus=contract.campus)
    form = ApprovalForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        approval = form.save(commit=False)
        approval.created_by = request.user
        approval.save()
        messages.success(request, "Added. Log its pre-application meeting, submittal and hearings as they're set.")
        return redirect(approval)
    return render(request, "crm/form.html", {"form": form, "title": "New approval",
                                             "cancel": reverse("crm:approval_list")})


@require_POST
def approval_matrix(request):
    """Start a project's or site's approvals from the usual permit matrix (the ticked kinds)."""
    project = Project.objects.filter(pk=request.POST.get("project") or 0).first()
    site = LandSite.objects.filter(pk=request.POST.get("site") or 0).first()
    back = request.POST.get("back") or reverse("crm:approval_list")
    if not back.startswith("/") or back.startswith("//"):
        back = reverse("crm:approval_list")
    if not (project or site):
        messages.error(request, "Pick the project or land site first.")
        return redirect(back)
    made = logic.start(request.POST.getlist("kinds"), project=project, site=site, user=request.user)
    messages.success(request, f"Added {len(made)} approval{'s' if len(made) != 1 else ''} for {project or site}."
                     if made else "Those approvals are already there.")
    return redirect(back)


def _cite_href(doc):
    def href(c):
        return (doc.href or "#") + (f"#page={c['page']}" if c.get("page") else "")
    return href


def approval_detail(request, pk):
    approval = get_object_or_404(logic.with_related(Approval.objects.select_related("contract")), pk=pk)
    if request.method == "POST":
        return _post(request, approval)
    today = timezone.localdate()
    logic.annotate([approval], today)
    steps = list(approval.steps.all())
    for s in steps:
        s.missed = not s.done and s.date < today
        s.left = (s.date - today).days
    documents = list(approval.documents.select_related("added_by"))
    by_pk = {d.pk: d for d in documents}
    for d in documents:
        if d.summary:
            body, d.sources = contract_ai.render(SimpleNamespace(blocks=d.summary), _cite_href(d))
            d.summary_html = mark_safe(body)
    conditions = list(approval.conditions.all())
    for c in conditions:
        doc = by_pk.get(c.source_id)
        if doc and doc.href:
            c.source_href = doc.href + (f"#page={doc.page_at(c.source_start)}" if c.source_start is not None else "")
    psa_dates = (list(approval.contract.dates.filter(kind=ContractDate.Kind.APPROVAL, confirmed=True))
                 if approval.contract_id else [])
    return render(request, "crm/approval_detail.html", {
        "a": approval, "steps": steps, "documents": documents, "today": today,
        "kept": [c for c in conditions if c.confirmed], "proposed": [c for c in conditions if not c.confirmed],
        "psa_dates": psa_dates, "form": ApprovalForm(instance=approval), "doc_form": DocumentForm(),
        "statuses": Approval.Status.choices, "step_kinds": ApprovalStep.Kind.choices,
        "condition_statuses": ApprovalCondition.Status.choices, "ai_ready": ai_enabled(),
        "can_manage": is_manager(request.user),
    })


def _date_or_none(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _stamp(approval, today):
    """Dates follow the status: Submitted sets the submittal date, Approved or Denied the decision date."""
    if approval.status == Approval.Status.SUBMITTED and not approval.submitted_on:
        approval.submitted_on = today
    if approval.status in (Approval.Status.APPROVED, Approval.Status.DENIED) and not approval.approved_on:
        approval.approved_on = today


def _read(doc, upload):
    data = upload.read()
    upload.seek(0)
    doc.text, doc.page_starts, doc.text_status = textmod.extract(data, upload.name)
    doc.pages = len(doc.page_starts)


def _post(request, approval):
    action = request.POST.get("action", "")
    user, today, anchor = request.user, timezone.localdate(), ""
    if action == "details":
        form = ApprovalForm(request.POST, instance=approval)
        if form.is_valid():
            _stamp(form.save(commit=False), today)
            approval.save()
            messages.success(request, "Saved.")
        else:
            messages.error(request, "Not saved: " + " ".join(" ".join(v) for v in form.errors.values()))
    elif action == "status":
        value = request.POST.get("status", "")
        if value in Approval.Status.values:
            approval.status = value
            _stamp(approval, today)
            approval.save()
    elif action == "step_add":
        anchor = "steps"
        when = _date_or_none(request.POST.get("date"))
        kind = request.POST.get("kind", "")
        if when is None or kind not in ApprovalStep.Kind.values:
            messages.error(request, "A step needs a type and a date.")
        else:
            # Events that are logged once they happen (a submittal, comments, a decision) are done on the day;
            # a hearing or meeting today is still ahead until someone marks it.
            happened = kind in (ApprovalStep.Kind.SUBMITTED, ApprovalStep.Kind.COMMENTS,
                                ApprovalStep.Kind.RESUBMITTED, ApprovalStep.Kind.DECISION)
            done = request.POST.get("done") == "1" or when < today or (happened and when == today)
            ApprovalStep.objects.create(approval=approval, kind=kind, date=when, done=done, created_by=user,
                                        body=(request.POST.get("body") or "").strip()[:200],
                                        note=(request.POST.get("note") or "").strip())
            changed = False
            if kind == ApprovalStep.Kind.PREAPP:
                changed = logic.advance(approval, "preapp")
            elif kind == ApprovalStep.Kind.SUBMITTED and done:
                changed = logic.advance(approval, "submitted")
                if not approval.submitted_on:
                    approval.submitted_on, changed = when, True
            elif kind in (ApprovalStep.Kind.COMMENTS, ApprovalStep.Kind.RESUBMITTED):
                changed = logic.advance(approval, "review")
            elif kind == ApprovalStep.Kind.HEARING and not done:
                changed = logic.advance(approval, "hearing")
            elif kind == ApprovalStep.Kind.APPEAL and not approval.appeal_ends_on:
                approval.appeal_ends_on, changed = when, True
            if changed:
                approval.save()
    elif action in ("step_done", "step_delete"):
        anchor = "steps"
        step = get_object_or_404(ApprovalStep, pk=request.POST.get("step"), approval=approval)
        if action == "step_done":
            step.done = not step.done
            step.save(update_fields=["done"])
        elif is_manager(user) or step.created_by_id == user.pk:
            step.delete()
        else:
            return refuse(request)
    elif action == "condition_add":
        anchor = "conditions"
        text = (request.POST.get("text") or "").strip()
        if text:
            ApprovalCondition.objects.create(approval=approval, text=text, created_by=user,
                                             timing=(request.POST.get("timing") or "").strip()[:200],
                                             due_on=_date_or_none(request.POST.get("due_on")))
    elif action in ("condition_status", "condition_delete"):
        anchor = "conditions"
        c = get_object_or_404(ApprovalCondition, pk=request.POST.get("condition"), approval=approval)
        if action == "condition_status":
            value = request.POST.get("status", "")
            if value in ApprovalCondition.Status.values:
                c.status = value
                c.save(update_fields=["status"])
        elif is_manager(user) or c.created_by_id == user.pk or not c.confirmed:
            c.delete()
        else:
            return refuse(request)
    elif action == "condition_keep":
        anchor = "conditions"
        ids = [i for i in request.POST.getlist("condition") if i.isdigit()]
        kept = approval.conditions.filter(confirmed=False, pk__in=ids).update(confirmed=True)
        messages.success(request, f"Kept {kept} condition{'s' if kept != 1 else ''}.")
    elif action == "condition_discard":
        anchor = "conditions"
        approval.conditions.filter(confirmed=False).delete()
    elif action == "doc_upload":
        anchor = "documents"
        form = DocumentForm(request.POST, request.FILES)
        if form.is_valid():
            doc = form.save(commit=False)
            doc.approval, doc.added_by = approval, user
            if "file" in request.FILES:
                _read(doc, request.FILES["file"])
            doc.save()
            note = {"scan": " It's a scan: Claude can read it, but quotes can't be matched to text.",
                    "unsupported": " That file type can't be read.", "error": " The file couldn't be read."}
            messages.success(request, f"Added {doc.title}." + note.get(doc.text_status, ""))
        else:
            messages.error(request, "Not added: " + " ".join(" ".join(v) for v in form.errors.values()))
    elif action in ("doc_summarize", "doc_conditions", "doc_delete"):
        anchor = "documents"
        doc = get_object_or_404(ApprovalDocument, pk=request.POST.get("doc"), approval=approval)
        if action == "doc_delete":
            if not is_manager(user):
                return refuse(request)
            if doc.file:
                doc.file.delete(save=False)
            doc.delete()
        else:
            try:
                if action == "doc_summarize":
                    ai.summarize(doc, user)
                else:
                    anchor = "conditions"
                    made, filled = ai.find_conditions(doc, user)
                    messages.success(request, (f"Claude found {len(made)} condition{'s' if len(made) != 1 else ''}. "
                                               "Check each against its quote, then keep the right ones."
                                               if made else "Claude found no new conditions.")
                                     + (f" Filled in the {' and '.join(filled)}." if filled else ""))
            except ai.Unavailable as exc:
                messages.error(request, str(exc))
    elif action == "delete":
        if not is_manager(user):
            return refuse(request)
        for doc in approval.documents.all():
            if doc.file:
                doc.file.delete(save=False)
        approval.delete()
        messages.success(request, f"Deleted {approval.title}.")
        return redirect("crm:approval_list")
    return redirect(approval.get_absolute_url() + (f"#{anchor}" if anchor else ""))


def approval_file(request, doc_pk):
    """An approval document's file, for signed-in users. PDFs open in the browser (so #page=N works)."""
    doc = get_object_or_404(ApprovalDocument, pk=doc_pk)
    if not doc.file:
        raise Http404
    try:
        fh = doc.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("That file is no longer on the server.")
    name = doc.file.name.rsplit("/", 1)[-1]
    return FileResponse(fh, as_attachment=not name.lower().endswith(INLINE_TYPES), filename=name)
