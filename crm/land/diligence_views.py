"""The due-diligence page for a site ACMI is buying: the checklist with status, consultants, cost and dates, the
feasibility deadline, the go / no-go decision, and Claude's cited summaries of the reports."""
from types import SimpleNamespace

from django import forms
from django.contrib import messages
from django.db.models import Max
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe

from ..ai import enabled as ai_enabled
from ..contracts import ai
from ..contracts import text as textmod
from ..forms import CommaNumbersMixin, UserChoiceField, active_users
from ..models import Contract, DiligenceItem, LandSite, LandSiteEvent
from ..permissions import is_manager, refuse
from . import diligence

INLINE_TYPES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_UPLOAD_MB = 60


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class DiligenceForm(CommaNumbersMixin, forms.ModelForm):
    owner = UserChoiceField(queryset=active_users(), required=False, label="Chased by")

    class Meta:
        model = DiligenceItem
        fields = ["block", "name", "how", "status", "consultant", "owner", "budget", "cost", "ordered_on", "due_on",
                  "received_on", "findings", "file", "url"]
        widgets = {"ordered_on": _date(), "due_on": _date(), "received_on": _date(),
                   "findings": forms.Textarea(attrs={"rows": 3})}
        labels = {"block": "Group", "budget": "Budget ($)", "cost": "Invoiced ($)"}

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f and getattr(f, "size", 0) > MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(f"Files up to {MAX_UPLOAD_MB} MB.")
        return f


def _read(item, upload):
    """Pull the text out of an uploaded report; a new report needs a new summary."""
    data = upload.read()
    upload.seek(0)
    item.text, item.page_starts, item.text_status = textmod.extract(data, upload.name)
    item.pages = len(item.page_starts)
    item.summary, item.summary_at, item.summary_model = [], None, ""


def _stamp(item, today):
    """Dates follow the status: Ordered sets the order date, Received or reviewed the received date."""
    if item.status == DiligenceItem.Status.ORDERED and not item.ordered_on:
        item.ordered_on = today
    if item.status in ("received", "clear", "issue") and not item.received_on:
        item.received_on = today


def _log(site, user, text):
    LandSiteEvent.objects.create(site=site, user=user, kind="diligence", text=text[:500])


def _cite_href(item):
    def href(c):
        return (item.href or "#") + (f"#page={c['page']}" if c.get("page") else "")
    return href


def site_diligence(request, pk):
    site = get_object_or_404(LandSite.objects.select_related("campus"), pk=pk)
    contracts = list(site.contracts.filter(kind__in=[Contract.Kind.PSA, Contract.Kind.OPTION]))
    contract = next((c for c in contracts if str(c.pk) == request.GET.get("contract")), None)
    if request.method == "POST":
        return _post(request, site, contract)
    s = diligence.summary(site, contract=contract)
    for i in s["items"]:
        if i.summary:
            body, i.sources = ai.render(SimpleNamespace(blocks=i.summary), _cite_href(i))
            i.summary_html = mark_safe(body)
    return render(request, "crm/land_diligence.html", {
        "site": site, "s": s, "contracts": contracts, "statuses": DiligenceItem.Status.choices,
        "users": active_users(), "form": DiligenceForm(), "decisions": Contract.Decision.choices,
        "ai_ready": ai_enabled(), "can_manage": is_manager(request.user), "standard": len(diligence.STANDARD),
        "full": len(diligence.CHECKLIST),
    })


def _post(request, site, contract):
    action = request.POST.get("action", "")
    user, today, anchor = request.user, timezone.localdate(), ""
    if action == "start":
        n = diligence.start(site, request.POST.get("which", "standard"), user, contract)
        messages.success(request, f"Added {n} item{'s' if n != 1 else ''}. Set who's on each, the consultant, cost and "
                                  "due dates." if n else "Those items are already on the site.")
    elif action in ("add", "edit"):
        if action == "edit":
            item = get_object_or_404(DiligenceItem, pk=request.POST.get("item"), site=site)
        else:
            item = DiligenceItem(site=site, contract=contract or diligence.governing_contract(site),
                                 position=(site.diligence.aggregate(m=Max("position"))["m"] or 0) + 1)
        before = item.status if item.pk else None
        form = DiligenceForm(request.POST, request.FILES, instance=item)
        if form.is_valid():
            item = form.save(commit=False)
            if "file" in request.FILES:
                _read(item, request.FILES["file"])
            _stamp(item, today)
            item.updated_by = user
            item.save()
            if before is not None and before != item.status:
                _log(site, user, f"Due diligence: {item.name} → {item.get_status_display()}")
            anchor = f"dd-{item.pk}"
            messages.success(request, f"Saved {item.name}.")
        else:
            messages.error(request, "Not saved: " + " ".join(" ".join(v) for v in form.errors.values()))
    elif action == "status":
        item = get_object_or_404(DiligenceItem, pk=request.POST.get("item"), site=site)
        new = request.POST.get("status", "")
        if new in DiligenceItem.Status.values and new != item.status:
            item.status, item.updated_by = new, user
            _stamp(item, today)
            item.save()
            _log(site, user, f"Due diligence: {item.name} → {item.get_status_display()}")
        anchor = f"dd-{item.pk}"
    elif action == "delete":
        if not is_manager(user):
            return refuse(request)
        item = get_object_or_404(DiligenceItem, pk=request.POST.get("item"), site=site)
        if item.file:
            item.file.delete(save=False)
        item.delete()
        messages.success(request, f"Removed {item.name}.")
    elif action == "summarize":
        item = get_object_or_404(DiligenceItem, pk=request.POST.get("item"), site=site)
        anchor = f"dd-{item.pk}"
        try:
            diligence.summarize(item, user)
        except ai.Unavailable as exc:
            messages.error(request, str(exc))
    elif action == "decision":
        c = contract or diligence.governing_contract(site)
        value = request.POST.get("decision", "")
        anchor = "decision"
        if c is None:
            messages.error(request, "Add the purchase agreement first: the decision is kept on it.")
        elif value in Contract.Decision.values:
            c.decision, c.decision_note = value, (request.POST.get("decision_note") or "").strip()
            c.decision_on, c.decision_by = (today, user) if value else (None, None)
            c.save(update_fields=["decision", "decision_note", "decision_on", "decision_by", "updated_at"])
            _log(site, user, f"Feasibility decision on {c.title}: {c.get_decision_display()}"
                 + (f" ({c.decision_note})" if c.decision_note else ""))
            messages.success(request, "Decision saved." + (" Send the termination notice before the feasibility "
                                                           "period ends." if value == "terminate" else ""))
    url = reverse("crm:land_site_diligence", args=[site.pk]) + (f"?contract={contract.pk}" if contract else "")
    return redirect(url + (f"#{anchor}" if anchor else ""))


def diligence_file(request, pk):
    """A diligence report, for signed-in users. PDFs open in the browser (so #page=N works); others download."""
    item = get_object_or_404(DiligenceItem, pk=pk)
    if not item.file:
        raise Http404
    try:
        fh = item.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404("That file is no longer on the server.")
    name = item.file.name.rsplit("/", 1)[-1]
    return FileResponse(fh, as_attachment=not name.lower().endswith(INLINE_TYPES), filename=name)
