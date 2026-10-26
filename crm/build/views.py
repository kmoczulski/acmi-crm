"""A project's build page (/projects/<id>/build/): schedule, team, GC selection, budget, change orders and draws, each
edited in place, and Claude leveling the GC proposals."""
from django import forms
from django.contrib import messages
from django.db.models import Max
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe

from ..ai import enabled as ai_enabled
from ..forms import CommaNumbersMixin
from ..contracts import ai as contract_ai
from ..contracts import text as textmod
from ..models import Bid, BudgetLine, BuildMilestone, ChangeOrder, Draw, Project, ProjectAnswer, TeamMember
from ..permissions import is_manager, refuse
from . import ai, logic

INLINE_TYPES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_UPLOAD_MB = 60


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class MilestoneForm(forms.ModelForm):
    class Meta:
        model = BuildMilestone
        fields = ["phase", "name", "baseline", "forecast", "actual", "note"]
        widgets = {"baseline": _date(), "forecast": _date(), "actual": _date()}


class TeamForm(CommaNumbersMixin, forms.ModelForm):
    class Meta:
        model = TeamMember
        fields = ["role", "firm", "contact", "fee", "invoiced", "status", "notes"]


class BidForm(CommaNumbersMixin, forms.ModelForm):
    class Meta:
        model = Bid
        fields = ["firm", "contact", "status", "amount", "fee_pct", "general_conditions", "duration_months",
                  "received_on", "interview_on", "strengths", "concerns", "file"]
        widgets = {"received_on": _date(), "interview_on": _date(),
                   "strengths": forms.Textarea(attrs={"rows": 2}), "concerns": forms.Textarea(attrs={"rows": 2})}

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f and getattr(f, "size", 0) > MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(f"Files up to {MAX_UPLOAD_MB} MB.")
        return f


class LineForm(CommaNumbersMixin, forms.ModelForm):
    class Meta:
        model = BudgetLine
        fields = ["group", "code", "name", "original", "committed", "paid", "forecast"]


class ChangeOrderForm(CommaNumbersMixin, forms.ModelForm):
    class Meta:
        model = ChangeOrder
        fields = ["number", "title", "reason", "amount", "days", "status", "line", "submitted_on", "decided_on",
                  "notes"]
        widgets = {"submitted_on": _date(), "decided_on": _date()}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["line"].queryset = BudgetLine.objects.filter(project=project) if project else BudgetLine.objects.none()


class DrawForm(CommaNumbersMixin, forms.ModelForm):
    class Meta:
        model = Draw
        fields = ["number", "period_end", "requested", "funded", "retainage", "status", "submitted_on", "funded_on",
                  "notes"]
        widgets = {"period_end": _date(), "submitted_on": _date(), "funded_on": _date()}


FORMS = {  # what each add / edit action works on: (model, form, list anchor)
    "milestone": (BuildMilestone, MilestoneForm, "schedule"),
    "team": (TeamMember, TeamForm, "team"),
    "bid": (Bid, BidForm, "bids"),
    "line": (BudgetLine, LineForm, "budget"),
    "co": (ChangeOrder, ChangeOrderForm, "changes"),
    "draw": (Draw, DrawForm, "draws"),
}


def _cite_href(bids):
    def href(c):
        bid = bids.get(c.get("doc"))
        return ((bid.href if bid else "") or "#") + (f"#page={c['page']}" if c.get("page") else "")
    return href


def project_build(request, pk):
    project = get_object_or_404(Project.objects.select_related("campus", "parent"), pk=pk)
    if request.method == "POST":
        return _post(request, project)
    today = timezone.localdate()
    s = logic.status(project, today)
    bids = {b.pk: b for b in s["bids"]}
    answers = list(project.answers.select_related("asked_by")[:10])
    for a in answers:
        body, a.sources = contract_ai.render(a, _cite_href(bids))
        a.html = mark_safe(body)
    return render(request, "crm/project_build.html", {
        "project": project, "s": s, "answers": answers, "today": today, "ai_ready": ai_enabled(),
        "can_manage": is_manager(request.user), "readable": ai.proposals(project),
        "phases": BuildMilestone.Phase.choices, "roles": TeamMember.Role.choices,
        "team_statuses": TeamMember.Status.choices, "bid_statuses": Bid.Status.choices,
        "groups": BudgetLine.Group.choices, "co_reasons": ChangeOrder.Reason.choices,
        "co_statuses": ChangeOrder.Status.choices, "draw_statuses": Draw.Status.choices,
        "next_draw": (project.draws.aggregate(m=Max("number"))["m"] or 0) + 1,
    })


def _read(bid, upload):
    data = upload.read()
    upload.seek(0)
    bid.text, bid.page_starts, bid.text_status = textmod.extract(data, upload.name)
    bid.pages = len(bid.page_starts)


def _select(project, bid, user, today):
    """Pick the GC: this bid Selected, the others still in play Not selected, the GC on the team at the bid's price,
    and the "GC selected" milestone dated today."""
    bid.status, bid.updated_by = Bid.Status.SELECTED, user
    bid.save()
    project.bids.exclude(pk=bid.pk).filter(status__in=logic.OPEN_BIDS + (Bid.Status.INVITED,)).update(
        status=Bid.Status.NOT_SELECTED)
    member = project.team.filter(role=TeamMember.Role.GC, firm__iexact=bid.firm).first() or TeamMember(
        project=project, role=TeamMember.Role.GC, firm=bid.firm)
    member.status, member.contact = TeamMember.Status.ENGAGED, member.contact or bid.contact
    if member.fee is None:
        member.fee = bid.amount
    member.save()
    project.milestones.filter(name=logic.GC_SELECTED, actual__isnull=True).update(actual=today)


def _post(request, project):
    action = request.POST.get("action", "")
    user, today, anchor = request.user, timezone.localdate(), ""
    kind, _, verb = action.partition("_")
    if kind in FORMS and verb in ("add", "edit", "delete"):
        model, form_class, anchor = FORMS[kind]
        obj = (get_object_or_404(model, pk=request.POST.get("id"), project=project) if verb != "add"
               else model(project=project))
        if verb == "delete":
            if not is_manager(user):
                return refuse(request)
            if kind == "bid" and obj.file:
                obj.file.delete(save=False)
            obj.delete()
            return redirect(reverse("crm:project_build", args=[project.pk]) + f"#{anchor}")
        extra = {"project": project} if kind == "co" else {}
        form = form_class(request.POST, request.FILES, instance=obj, **extra)
        if form.is_valid():
            obj = form.save(commit=False)
            if kind == "bid":
                if "file" in request.FILES:
                    _read(obj, request.FILES["file"])
                    if obj.status == Bid.Status.INVITED:
                        obj.status = Bid.Status.RECEIVED
                    obj.received_on = obj.received_on or today
                obj.updated_by = user
            if verb == "add" and hasattr(obj, "position"):
                obj.position = (model.objects.filter(project=project).aggregate(m=Max("position"))["m"] or 0) + 1
            if kind == "co" and obj.status != ChangeOrder.Status.PENDING and not obj.decided_on:
                obj.decided_on = today
            if kind == "draw" and obj.status == Draw.Status.FUNDED:
                obj.funded_on = obj.funded_on or today
                obj.funded = obj.requested if obj.funded is None else obj.funded
            obj.save()
        else:
            messages.error(request, "Not saved: " + " ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items()))
    elif action == "schedule_start":
        anchor = "schedule"
        n = logic.start_schedule(project)
        messages.success(request, f"Added {n} milestones. Enter the baseline dates from the approved schedule.")
    elif action == "budget_start":
        anchor = "budget"
        n = logic.start_budget(project)
        messages.success(request, f"Added {n} budget lines.")
    elif action == "bid_select":
        anchor = "bids"
        bid = get_object_or_404(Bid, pk=request.POST.get("id"), project=project)
        _select(project, bid, user, today)
        messages.success(request, f"{bid.firm} selected; added to the team as the GC.")
    elif action == "co_decide":
        anchor = "changes"
        co = get_object_or_404(ChangeOrder, pk=request.POST.get("id"), project=project)
        value = request.POST.get("status", "")
        if value in ChangeOrder.Status.values:
            co.status, co.decided_on = value, (today if value != ChangeOrder.Status.PENDING else None)
            co.save(update_fields=["status", "decided_on"])
    elif action in ("level", "ask", "bid_terms"):
        anchor = "bids"
        try:
            if action == "level":
                answer = ai.level(project, user)
                anchor = f"answer-{answer.pk}"
            elif action == "ask":
                question = (request.POST.get("question") or "").strip()[:2000]
                if question:
                    answer = ai.ask(project, question, user)
                    anchor = f"answer-{answer.pk}"
            else:
                bid = get_object_or_404(Bid, pk=request.POST.get("id"), project=project)
                filled = ai.read_terms(bid, user)
                messages.success(request, f"Filled in {', '.join(filled)} for {bid.firm}." if filled
                                 else "Nothing new to fill in: those fields are already set or not in the proposal.")
        except ai.Unavailable as exc:
            messages.error(request, str(exc))
    elif action == "answer_delete":
        anchor = "bids"
        answer = get_object_or_404(ProjectAnswer, pk=request.POST.get("id"), project=project)
        if not (is_manager(user) or answer.asked_by_id == user.pk):
            return refuse(request)
        answer.delete()
    return redirect(reverse("crm:project_build", args=[project.pk]) + (f"#{anchor}" if anchor else ""))


def bid_file(request, pk):
    """A GC proposal, for signed-in users. PDFs open in the browser (so #page=N works)."""
    bid = get_object_or_404(Bid, pk=pk)
    if not bid.file:
        raise Http404
    try:
        fh = bid.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("That file is no longer on the server.")
    name = bid.file.name.rsplit("/", 1)[-1]
    return FileResponse(fh, as_attachment=not name.lower().endswith(INLINE_TYPES), filename=name)
