from datetime import date

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.forms import UserChoiceField, active_users
from crm.models import EmailTemplate, Mailbox, OutreachFile, OutreachSettings, Person, SendBatch, SendItem
from crm.cloudrun import JobError
from crm.permissions import manager_required

from . import queue as outreach_queue
from . import report as weekly
from .sender import SendError, compose, send_test, start_in_background, stop_batch

CONFIRM_WORD = "SEND"


class SettingsForm(forms.ModelForm):
    owner_filter = UserChoiceField(queryset=active_users(), required=False)

    class Meta:
        model = OutreachSettings
        fields = "__all__"
        widgets = {"report_recipients": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        campaigns = sorted({c for c in EmailTemplate.objects.values_list("campaign", flat=True) if c})
        self.fields["current_campaign"].widget = forms.Select(choices=[(c, c) for c in campaigns])


class TemplateForm(forms.ModelForm):
    class Meta:
        model = EmailTemplate
        fields = ["template_id", "name", "campaign", "priority", "sector_match", "subject", "body_html", "body_plain",
                  "active"]
        widgets = {"body_html": forms.Textarea(attrs={"rows": 10}), "body_plain": forms.Textarea(attrs={"rows": 8})}


def _mailbox(user):
    return Mailbox.objects.filter(user=user).first()


def outreach_home(request):
    settings = OutreachSettings.load()
    full_queue, skipped = outreach_queue.build(settings)
    sent_today = outreach_queue.sent_today(request.user)
    preview = []
    for entry in full_queue[:40]:
        subject, _, _ = compose(entry.template, entry.person, entry.greeting, settings)
        preview.append({"entry": entry, "subject": subject})
    return render(request, "crm/outreach_home.html", {
        "settings": settings,
        "queue_size": len(full_queue),
        "preview": preview,
        "skipped": [(outreach_queue.SKIP_LABELS.get(k, k), n) for k, n in skipped.most_common()],
        "templates": EmailTemplate.objects.filter(campaign=settings.current_campaign, active=True),
        "files": OutreachFile.objects.filter(active=True) if settings.include_attachments else [],
        "sent_today": sent_today,
        "remaining_today": max(0, settings.daily_cap - sent_today),
        "batches": SendBatch.objects.select_related("created_by")[:10],
        "mailbox": _mailbox(request.user),
        "confirm_word": CONFIRM_WORD,
        "running": SendBatch.objects.filter(status__in=[SendBatch.Status.QUEUED, SendBatch.Status.SENDING]).first(),
    })


@require_POST
def outreach_test(request):
    template = get_object_or_404(EmailTemplate, pk=request.POST.get("template"))
    mailbox = _mailbox(request.user)
    queue, _ = outreach_queue.build(limit=1)
    person = queue[0].person if queue else Person.objects.exclude(email=None).first()
    try:
        send_test(request.user, template, person, mailbox.email if mailbox else "")
        messages.success(request, f"Test email sent to {mailbox.email} (rendered for {person}).")
    except (SendError, OSError) as exc:
        messages.error(request, str(exc))
    return redirect("crm:outreach_home")


@require_POST
@manager_required
def outreach_start(request):
    settings = OutreachSettings.load()
    if request.POST.get("confirm", "").strip() != CONFIRM_WORD:
        messages.error(request, f"Type {CONFIRM_WORD} to confirm.")
        return redirect("crm:outreach_home")
    if _mailbox(request.user) is None:
        messages.error(request, "Connect your mailbox on the Mail page first.")
        return redirect("crm:outreach_home")
    # Check-and-create as one locked step, so two people pressing Send together can't start two batches to the same
    # people (the settings row is the lock on Postgres; SQLite's IMMEDIATE transactions already queue writers).
    with transaction.atomic():
        OutreachSettings.objects.select_for_update().filter(pk=settings.pk).first()
        if SendBatch.objects.filter(status__in=[SendBatch.Status.QUEUED, SendBatch.Status.SENDING]).exists():
            messages.error(request, "A send is already running. Wait for it to finish or stop it.")
            return redirect("crm:outreach_home")
        remaining = max(0, settings.daily_cap - outreach_queue.sent_today(request.user))
        limit = request.POST.get("limit", "")
        limit = min(int(limit), remaining) if limit.isdigit() else remaining
        entries, _ = outreach_queue.build(settings, limit=limit)
        if not entries:
            messages.error(request, "Nobody to send to (queue empty or today's cap reached).")
            return redirect("crm:outreach_home")
        batch = SendBatch.objects.create(created_by=request.user, campaign=settings.current_campaign)
        SendItem.objects.bulk_create([SendItem(batch=batch, person=e.person, template=e.template,
                                               email=e.person.email) for e in entries])
    try:
        start_in_background(batch)  # after the commit, so the sending process sees the batch
    except JobError as exc:  # production: the Cloud Run job didn't start; don't leave a batch blocking new sends
        SendBatch.objects.filter(pk=batch.pk, status=SendBatch.Status.QUEUED).update(
            status=SendBatch.Status.FAILED, error=str(exc), finished_at=timezone.now())
        messages.error(request, f"{exc}. Nothing was sent.")
        return redirect(batch)
    messages.success(request, f"Sending {len(entries)} emails in the background.")
    return redirect(batch)


def send_batch(request, pk):
    batch = get_object_or_404(SendBatch.objects.select_related("created_by"), pk=pk)
    items = batch.items.select_related("person__company", "template")
    return render(request, "crm/send_batch.html", {
        "batch": batch, "counts": batch.counts(), "items": items[:500],
        "active": batch.status in (SendBatch.Status.QUEUED, SendBatch.Status.SENDING),
    })


@require_POST
def send_batch_stop(request, pk):
    batch = get_object_or_404(SendBatch, pk=pk)
    if stop_batch(batch):
        messages.info(request, "This batch had stopped sending on its own; it is now closed.")
    else:
        messages.info(request, "Stopping after the current email.")
    return redirect("crm:send_batch", pk=pk)


@manager_required
def outreach_settings(request):
    form = SettingsForm(request.POST or None, instance=OutreachSettings.load())
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("crm:outreach_home")
    return render(request, "crm/form.html", {"form": form, "title": "Outreach settings",
                                             "cancel": "/outreach/"})


def template_list(request):
    settings = OutreachSettings.load()
    return render(request, "crm/outreach_templates.html", {
        "templates": EmailTemplate.objects.order_by("-campaign", "template_id"), "current": settings.current_campaign,
    })


@manager_required
def template_edit(request, pk=None):
    template = get_object_or_404(EmailTemplate, pk=pk) if pk else None
    form = TemplateForm(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid():
        template = form.save()
        messages.success(request, "Template saved.")
        return redirect("crm:template_edit", pk=template.pk)
    preview = None
    if template:
        queue, _ = outreach_queue.build(limit=1)
        person = queue[0].person if queue else Person.objects.exclude(email=None).first()
        if person:
            subject, html, _ = compose(template, person, outreach_queue.greeting_for(person))
            preview = {"person": person, "subject": subject, "html": html}
    return render(request, "crm/outreach_template_edit.html", {"form": form, "template": template, "preview": preview})


def weekly_report(request):
    day = None
    if request.GET.get("week"):
        try:
            day = date.fromisoformat(request.GET["week"])
        except ValueError:
            day = None
    data = weekly.build(day)
    settings = OutreachSettings.load()
    if request.method == "POST":
        recipients = [r.strip() for r in settings.report_recipients.split(",") if "@" in r]
        try:
            weekly.send(data, request.user, recipients)
            messages.success(request, f"Report emailed to {', '.join(recipients)}.")
        except (SendError, OSError) as exc:
            messages.error(request, str(exc))
        return redirect(f"{request.path}?week={data['start']:%Y-%m-%d}")
    prev_week = (data["start"] - timezone.timedelta(days=7)).date()
    next_week = (data["start"] + timezone.timedelta(days=7)).date()
    return render(request, "crm/report_weekly.html", {
        "r": data, "prev_week": prev_week, "next_week": next_week if next_week <= timezone.localdate() else None,
        "recipients": settings.report_recipients,
    })
