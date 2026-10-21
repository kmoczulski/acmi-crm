import hmac

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from crm.models import Company, Firm, MailFilter, Mailbox, UnmatchedEmail

from . import sync


def mail_home(request):
    mailbox = Mailbox.objects.filter(user=request.user).first()
    queue = sync.review_queue()
    return render(request, "crm/mail.html", {
        "mailbox": mailbox,
        "has_password": bool(mailbox and mailbox.get_password()),
        "env_var": f"ACMI_IMAP_PASSWORD_{request.user.username.upper()}",
        "mailboxes": Mailbox.objects.select_related("user"),
        "queue": queue,
        "queue_total": sum(g["count"] for g in queue),
        "filters": MailFilter.objects.all(),
        "filter_kinds": MailFilter.Kind.choices,
        "firm_kinds": Firm.Kind.choices,
        "default_email": request.user.email,
    })


@require_POST
def mailbox_setup(request):
    email = request.POST.get("email", "").strip().lower()
    if "@" not in email:
        messages.error(request, "Enter your Gmail address.")
        return redirect("crm:mail")
    days = request.POST.get("backfill_days", "365")
    mailbox, created = Mailbox.objects.get_or_create(user=request.user, defaults={"email": email})
    if not created and mailbox.email != email:
        # A different account: start that mailbox from scratch.
        mailbox.email = email
        mailbox.uid_validity = ""
        mailbox.last_uid = 0
        mailbox.backfill_done = False
    mailbox.backfill_days = int(days) if days.isdigit() else 365
    mailbox.enabled = True
    mailbox.save()
    messages.success(request, "Mailbox saved.")
    return redirect("crm:mail")


@require_POST
def mailbox_sync_now(request):
    mailbox = get_object_or_404(Mailbox, user=request.user)
    try:
        stats = sync.sync_mailbox(mailbox)
    except sync.SyncBusy as exc:
        messages.error(request, str(exc))
        return redirect("crm:mail")
    mailbox.refresh_from_db()
    if mailbox.last_error:
        messages.error(request, mailbox.last_error)
    else:
        summary = ", ".join(f"{v} {k}" for k, v in sorted(stats.items()) if v)
        messages.success(request, f"Synced: {summary or 'nothing new'}.")
    return redirect("crm:mail")


@require_POST
def review_approve(request):
    domain = request.POST.get("domain", "")
    existing = request.POST.get("existing", "").strip()
    company = None
    if existing:
        company = Company.objects.filter(name__iexact=existing).first()
        if company is None:
            messages.error(request, f"No company named “{existing}”. Leave it blank to create a new one.")
            return redirect("crm:mail")
    try:
        company, stats = sync.approve_domain(domain, company=company, company_name=request.POST.get("name", ""),
                                             user=request.user)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("crm:mail")
    messages.success(request, f"{domain} → {company.name}: {stats['activities created']} emails logged.")
    return redirect("crm:mail")


@require_POST
def review_dismiss(request):
    domain = request.POST.get("domain", "")
    kind = request.POST.get("kind", MailFilter.Kind.IGNORE)
    if kind not in MailFilter.Kind.values:
        kind = MailFilter.Kind.IGNORE
    sync.dismiss_domain(domain, kind=kind, user=request.user)
    messages.success(request, f"{domain} will be {'protected' if kind == MailFilter.Kind.PROTECT else 'ignored'}.")
    return redirect("crm:mail")


@require_POST
def review_bulk(request):
    domains = [d for d in request.POST.getlist("domains") if d]
    action = request.POST.get("action")
    if not domains:
        messages.error(request, "Tick at least one domain.")
        return redirect("crm:mail")
    if action in (MailFilter.Kind.IGNORE, MailFilter.Kind.PROTECT):
        for domain in domains:
            sync.dismiss_domain(domain, kind=action, user=request.user)
        verb = "Protected" if action == MailFilter.Kind.PROTECT else "Ignored"
        messages.success(request, f"{verb} {len(domains)} domain{'s' if len(domains) != 1 else ''}.")
    elif action == "create":
        logged = added = 0
        for domain in domains:
            try:
                _, stats = sync.approve_domain(domain, user=request.user)
            except ValueError:  # a directory firm's domain
                continue
            logged += stats["activities created"]
            added += 1
        messages.success(request, f"Added {added} companies; {logged} emails logged. Rename them from their pages.")
    return redirect("crm:mail")


@require_POST
def filter_add(request):
    pattern = request.POST.get("pattern", "").strip().lower()
    kind = request.POST.get("kind")
    if pattern and kind in MailFilter.Kind.values:
        MailFilter.objects.update_or_create(
            pattern=pattern.lstrip("@"),
            defaults={"kind": kind, "note": request.POST.get("note", ""), "created_by": request.user},
        )
        if kind == MailFilter.Kind.PROTECT:
            # Protected means never stored: drop anything already queued for it.
            UnmatchedEmail.objects.filter(address=pattern).delete()
            UnmatchedEmail.objects.filter(domain=pattern.lstrip("@")).delete()
    return redirect("crm:mail")


@require_POST
def filter_delete(request, pk):
    MailFilter.objects.filter(pk=pk).delete()
    return redirect("crm:mail")


def company_names(request):
    return JsonResponse(list(Company.objects.order_by("name").values_list("name", flat=True)), safe=False)


@csrf_exempt
@require_POST
def cron_sync(request):
    """Called by a scheduler (Cloud Scheduler) with header X-Cron-Secret."""
    secret = settings.CRON_SECRET
    given = request.headers.get("X-Cron-Secret", "")
    if not secret or not hmac.compare_digest(secret, given):
        return HttpResponseForbidden("bad secret")
    results = sync.sync_all()
    out = {k: dict(v) for k, v in results.items()}
    from crm import gcal  # calendar rides along, as in scripts/sync_mail.cmd

    try:
        gcal.sync_all()
    except Exception as exc:  # a calendar problem must not fail the mail sync
        out["calendar_error"] = str(exc)
    return JsonResponse(out)
