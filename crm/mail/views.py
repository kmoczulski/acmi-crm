import hmac
from functools import wraps

import requests
from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from crm import gcal, prefs
from crm.models import Company, Firm, MailConnection, MailFilter, Mailbox, UnmatchedEmail
from crm.permissions import is_partner

from . import connect, sync
from .imap import MailboxError


def mail_home(request):
    mailbox = Mailbox.objects.filter(user=request.user).first()
    queue = sync.review_queue()
    from crm.directory import sort

    sorts = sort.suggestions()
    for g in queue:
        g["sort"] = sorts.get(g["domain"])
        if g["sort"] and g["sort"].verdict == "company" and g["sort"].name:
            g["suggested_name"] = g["sort"].name  # Claude's reading of the name, from the signatures
    return render(request, "crm/mail.html", {
        "mailbox": mailbox,
        # Read through Mail in the hub (the Gmail API), a mailbox needs no app password.
        "has_password": bool(mailbox and (mailbox.backend == Mailbox.Backend.GMAIL_API or mailbox.get_password()
                                          or connect.is_on(request.user))),
        "env_var": f"ACMI_IMAP_PASSWORD_{request.user.username.upper()}",
        "mailboxes": Mailbox.objects.select_related("user"),
        "queue": queue,
        "queue_total": sum(g["count"] for g in queue),
        "filters": MailFilter.objects.all(),
        "filter_kinds": MailFilter.Kind.choices,
        "firm_kinds": Firm.Kind.choices,
        "filings": sort.recent_filings(),
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
    from crm.directory import sort  # Claude sorts the new domains, as in scripts/sync_mail.cmd

    try:
        out["sorting"] = sort.auto_sort()
    except Exception as exc:  # noqa: BLE001 - sorting must not fail the mail sync
        out["sorting_error"] = str(exc)
    from crm import notify  # due reminders reach browsers with the hub closed, as in scripts/sync_mail.cmd

    try:
        notify.sweep_everyone()
    except Exception as exc:  # noqa: BLE001 - reminders must not fail the mail sync
        out["reminders_error"] = str(exc)
    return JsonResponse(out)


# ---------------------------------------------------------------------------
# Mail in the hub: each person's own switch (crm/mail/connect.py, docs/EMAIL_CLIENT.md step 1)
# ---------------------------------------------------------------------------


def signature_settings(user):
    """What the signature panel on Mail in the hub shows: the mode, the custom text and Gmail's own signature."""
    from . import compose
    from .gmail_api import GmailClient

    out = {"signature_mode": compose.signature_mode(user),
           "signature": prefs.get_prefs(user).get("mail_signature", ""), "gmail_signature": ""}
    mailbox = Mailbox.objects.filter(user=user).first()
    if mailbox is not None and connect.is_on(user):
        try:
            out["gmail_signature"] = compose.gmail_signature(mailbox, GmailClient(user))
        except MailboxError:
            pass
    return out


def team_only(view):
    """Mail pages answer "not found" to a capital partner even if a partner ever got past PartnerOnlyMiddleware."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if is_partner(request.user):
            raise Http404()
        return view(request, *args, **kwargs)

    return wrapped


@team_only
def mail_settings(request):
    conn = connect.connection(request.user)
    return render(request, "crm/mail_settings.html", {
        "conn": conn,
        "on": connect.is_on(request.user),
        "unavailable": connect.unavailable_reason(request.user),
        "calendar_connected": gcal.connected(request.user),
        **signature_settings(request.user),
    })


@team_only
@require_POST
def mail_turn_on(request):
    reason = connect.unavailable_reason(request.user)
    if reason:
        messages.error(request, reason)
        return redirect("crm:mail_settings")
    request.session[connect.SESSION_KEY] = True
    return redirect(connect.connect_url(request))


@team_only
def mail_connected(request):
    """Where Google sends the person back after Turn on Mail."""
    if not request.session.pop(connect.SESSION_KEY, False):
        return redirect("crm:mail_settings")
    try:
        connect.finish(request.user)
    except (connect.MailConnectError, requests.RequestException) as exc:
        text = str(exc) if isinstance(exc, connect.MailConnectError) else "Couldn't reach Google. Try again."
        MailConnection.objects.update_or_create(user=request.user, defaults={"last_error": text})
        messages.error(request, text)
        return redirect("crm:mail_settings")
    messages.success(request, "Mail is on. Gmail is connected to the hub.")
    return redirect("crm:mail_settings")


@team_only
@require_POST
def mail_turn_off(request):
    removed = connect.turn_off(request.user)
    if removed:
        messages.success(request, "Mail is off. The hub's Gmail permission was removed at Google. Google keeps one "
                                  "permission for the hub, so your calendar was disconnected too: use Connect Google "
                                  "Calendar below to bring it back.")
    else:
        messages.success(request, "Mail is off.")
    return redirect("crm:mail_settings")
