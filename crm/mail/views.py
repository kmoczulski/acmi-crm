import hmac
from functools import wraps

import requests
from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from crm import gcal, prefs
from django.urls import reverse

from crm.models import Company, Firm, MailConnection, MailFilter, Mailbox, MailboxAccess, UnmatchedEmail
from crm.permissions import is_manager, is_partner, manager_required, team_users

from . import connect, shared, sync
from .imap import MailboxError


def mail_home(request):
    """The old Mail review address: it's Needs filing now."""
    return redirect(reverse("crm:needs_filing") + (f"?{request.GET.urlencode()}" if request.GET else ""))


def needs_filing(request):
    """One card per organization we don't know yet (crm/needs_filing.py), the "Same person?" suggestions, and the
    mailbox setup and filters further down."""
    from crm import needs_filing as nf
    from crm.directory import sort
    from crm.models import AddressSuggestion, Project

    mailbox = Mailbox.objects.filter(user=request.user).first()
    only = (request.GET.get("domain") or "").strip().lower()
    cards, page, total = nf.cards(request.GET.get("page") or 1, only=only)
    return render(request, "crm/needs_filing.html", {
        "cards": cards, "page": page, "total": total, "only": only,
        "same_person": AddressSuggestion.objects.filter(status=AddressSuggestion.Status.OPEN)
        .select_related("person__company")[:30],
        "projects": Project.objects.filter(active=True),
        "mailbox": mailbox,
        # Read through Mail in the hub (the Gmail API), a mailbox needs no app password.
        "has_password": bool(mailbox and (mailbox.backend == Mailbox.Backend.GMAIL_API or mailbox.get_password()
                                          or connect.is_on(request.user))),
        "env_var": f"ACMI_IMAP_PASSWORD_{request.user.username.upper()}",
        "mailboxes": Mailbox.objects.select_related("user"),
        "filters": MailFilter.objects.all(),
        "filter_kinds": MailFilter.Kind.choices,
        "firm_kinds": Firm.Kind.choices,
        "filings": sort.recent_filings(),
        "default_email": request.user.email,
    })


@require_POST
def needs_filing_act(request):
    """A card's buttons: accept (what the headline proposes, or what Change picked), later (Not now) or never."""
    from crm import needs_filing as nf
    from crm.models import Project

    domain = (request.POST.get("domain") or "").strip().lower()
    action = request.POST.get("action", "")
    back = reverse("crm:needs_filing") + (f"?page={request.POST['page']}" if request.POST.get("page", "").isdigit() else "")
    if not domain:
        return redirect(back)
    if action == "later":
        nf.not_now(domain, request.user)
        messages.success(request, f"{domain} is out of the way for {nf.SNOOZE_DAYS} days.")
    elif action == "never":
        nf.never(domain, request.user)
        messages.success(request, f"{domain} won't be offered again. Mail with people already in the CRM is still "
                                  "logged.")
    elif action == "accept":
        existing = None
        if (request.POST.get("existing") or "").strip():
            existing = Company.objects.filter(name__iexact=request.POST["existing"].strip()).first()
            if existing is None:
                messages.error(request, f"No company named “{request.POST['existing'].strip()}”.")
                return redirect(back)
        project = Project.objects.filter(pk=request.POST.get("project") or 0).first()
        kind = request.POST.get("kind", "")
        try:
            text = nf.accept(domain, request.user, as_=request.POST.get("as") or None,
                             kind=kind if kind in Firm.Kind.values else "", name=request.POST.get("name", ""),
                             project=project, existing=existing)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, text)
    return redirect(back)


@require_POST
def same_person(request, pk):
    """"Same person, add this address": yes adds it to the person (and the waiting email to their timeline)."""
    from crm.models import AddressSuggestion

    s = get_object_or_404(AddressSuggestion, pk=pk, status=AddressSuggestion.Status.OPEN)
    if request.POST.get("action") == "add":
        n = sync.add_address(s, request.user)
        messages.success(request, f"{s.address} is on {s.person} now: {n} email{'s' if n != 1 else ''} logged.")
    else:
        AddressSuggestion.objects.filter(address=s.address, person=s.person,
                                         status=AddressSuggestion.Status.OPEN).update(
            status=AddressSuggestion.Status.DISMISSED, decided_by=request.user)
        messages.success(request, f"Noted: {s.address} isn't {s.person}.")
    return redirect(reverse("crm:needs_filing") + "#same-person")


@require_POST
def mailbox_setup(request):
    email = request.POST.get("email", "").strip().lower()
    if "@" not in email:
        messages.error(request, "Enter your Gmail address.")
        return redirect("crm:needs_filing")
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
    return redirect("crm:needs_filing")


@require_POST
def mailbox_sync_now(request):
    mailbox = get_object_or_404(Mailbox, user=request.user)
    try:
        stats = sync.sync_mailbox(mailbox)
    except sync.SyncBusy as exc:
        messages.error(request, str(exc))
        return redirect("crm:needs_filing")
    mailbox.refresh_from_db()
    if mailbox.last_error:
        messages.error(request, mailbox.last_error)
    else:
        summary = ", ".join(f"{v} {k}" for k, v in sorted(stats.items()) if v)
        messages.success(request, f"Synced: {summary or 'nothing new'}.")
    return redirect("crm:needs_filing")


@require_POST
def review_approve(request):
    domain = request.POST.get("domain", "")
    existing = request.POST.get("existing", "").strip()
    company = None
    if existing:
        company = Company.objects.filter(name__iexact=existing).first()
        if company is None:
            messages.error(request, f"No company named “{existing}”. Leave it blank to create a new one.")
            return redirect("crm:needs_filing")
    try:
        company, stats = sync.approve_domain(domain, company=company, company_name=request.POST.get("name", ""),
                                             user=request.user)
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("crm:needs_filing")
    messages.success(request, f"{domain} → {company.name}: {stats['activities created']} emails logged.")
    return redirect("crm:needs_filing")


@require_POST
def review_dismiss(request):
    domain = request.POST.get("domain", "")
    kind = request.POST.get("kind", MailFilter.Kind.IGNORE)
    if kind not in MailFilter.Kind.values:
        kind = MailFilter.Kind.IGNORE
    sync.dismiss_domain(domain, kind=kind, user=request.user)
    messages.success(request, f"{domain} will be {'protected' if kind == MailFilter.Kind.PROTECT else 'ignored'}.")
    return redirect("crm:needs_filing")


@require_POST
def review_bulk(request):
    domains = [d for d in request.POST.getlist("domains") if d]
    action = request.POST.get("action")
    if not domains:
        messages.error(request, "Tick at least one domain.")
        return redirect("crm:needs_filing")
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
    return redirect("crm:needs_filing")


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
    return redirect("crm:needs_filing")


@require_POST
def filter_delete(request, pk):
    MailFilter.objects.filter(pk=pk).delete()
    return redirect("crm:needs_filing")


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


@csrf_exempt
@require_POST
def gmail_push(request):
    """Pub/Sub's push for a Gmail watch (crm/mail/push.py): read what changed in that one mailbox now. Only a request
    signed by the push subscription's service account counts; anything it sends is only a nudge, since the sync reads
    from the mailbox's own history marker. Always answers 204 once verified, so Pub/Sub doesn't redeliver."""
    from . import push

    if not push.verified(request):
        return HttpResponseForbidden("not from the push subscription")
    address = push.notified_address(request.body)
    if address:
        push.sync_for(address)
    return HttpResponse(status=204)


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
@require_POST
def crm_labels_switch(request):
    """Mail in the hub › CRM labels in Gmail: on (the default) or off for this person."""
    on = request.POST.get("on") == "1"
    prefs.set_pref(request.user, "mail_crm_labels", on)
    messages.success(request, "CRM labels in Gmail are on." if on else "CRM labels in Gmail are off. The tags in the hub stay.")
    return redirect("crm:mail_settings")


@team_only
def mail_settings(request):
    conn = connect.connection(request.user)
    return render(request, "crm/mail_settings.html", {
        "conn": conn,
        "on": connect.is_on(request.user),
        "unavailable": connect.unavailable_reason(request.user),
        "calendar_connected": gcal.connected(request.user),
        **signature_settings(request.user),
        "crm_labels_on": prefs.get_prefs(request.user).get("mail_crm_labels", True),
        "is_manager": is_manager(request.user),
        "shared_mailboxes": [
            {"mailbox": m, "grants": list(m.grants.select_related("user").order_by("user__first_name")),
             "connected": hasattr(m, "shared_token")}
            for m in Mailbox.objects.filter(shared=True).select_related("shared_token__connected_by").order_by("name")
        ] if is_manager(request.user) else [],
        "my_shared": shared.shared_for(request.user),
        "team": team_users().order_by("first_name", "username"),
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


# ---------------------------------------------------------------------------
# Shared mailboxes (crm/mail/shared.py, docs/EMAIL_CLIENT.md step 7): Managers connect them and give access
# ---------------------------------------------------------------------------


@team_only
@manager_required
@require_POST
def shared_connect(request):
    try:
        return redirect(shared.start(request, request.POST.get("email", "")))
    except shared.SharedError as exc:
        messages.error(request, str(exc))
        return redirect(reverse("crm:mail_settings") + "#shared")


@team_only
@manager_required
def shared_done(request):
    """Where Google sends the Manager back after signing in as the shared address."""
    try:
        mailbox = shared.finish(request)
    except (shared.SharedError, requests.RequestException) as exc:
        messages.error(request, str(exc) if isinstance(exc, shared.SharedError) else "Couldn't reach Google. Try again.")
    else:
        messages.success(request, f"{mailbox.email} is connected. Give teammates access below; its mail reaches "
                                  "timelines from the next sync.")
    return redirect(reverse("crm:mail_settings") + "#shared")


def _shared_mailbox(pk):
    return get_object_or_404(Mailbox, pk=pk, shared=True)


@team_only
@manager_required
@require_POST
def shared_grant(request, pk):
    from django.contrib.auth import get_user_model

    mailbox = _shared_mailbox(pk)
    person = get_user_model().objects.filter(pk=request.POST.get("user") or 0).first()
    if person is None or not team_users().filter(pk=person.pk).exists():
        messages.error(request, "Pick someone on the team.")
    else:
        MailboxAccess.objects.update_or_create(mailbox=mailbox, user=person, defaults={
            "can_send": request.POST.get("can_send") == "1", "granted_by": request.user})
        messages.success(request, f"{person.get_full_name() or person.username} can now use {mailbox.label}.")
    return redirect(reverse("crm:mail_settings") + "#shared")


@team_only
@manager_required
@require_POST
def shared_ungrant(request, pk, user_id):
    MailboxAccess.objects.filter(mailbox=_shared_mailbox(pk), user_id=user_id).delete()
    messages.success(request, "Access removed.")
    return redirect(reverse("crm:mail_settings") + "#shared")


@team_only
@manager_required
@require_POST
def shared_update(request, pk):
    """Rename a shared mailbox, switch its timeline logging, or disconnect it."""
    mailbox = _shared_mailbox(pk)
    if request.POST.get("action") == "disconnect":
        shared.disconnect(mailbox)
        messages.success(request, f"{mailbox.email} is disconnected from the hub. Its timeline entries stay.")
    else:
        mailbox.name = request.POST.get("name", mailbox.name).strip()[:120] or mailbox.name
        mailbox.enabled = request.POST.get("timeline") == "1"
        mailbox.save(update_fields=["name", "enabled"])
        messages.success(request, "Saved.")
    return redirect(reverse("crm:mail_settings") + "#shared")


@team_only
@manager_required
def shared_log(request, pk):
    mailbox = _shared_mailbox(pk)
    return render(request, "crm/mail_shared_log.html", {
        "mailbox": mailbox, "entries": mailbox.access_log.select_related("user")[:300]})
