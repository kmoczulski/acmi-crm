"""The inbox (docs/EMAIL_CLIENT.md, step 3): folders, the thread list, the reading pane, search, attachments.

Read only for now: archive, labels and the rest come in step 4, compose and reply in step 5. Everything is reached
through the signed-in person's own mailbox and Google permission, so nobody (Managers and admins included) can open
anyone else's mail; a thread or message id that isn't theirs is simply not found at Gmail.
"""
import logging

import requests
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import content_disposition_header
from django.views.decorators.clickjacking import xframe_options_sameorigin

from crm.models import MailThread, Mailbox

from . import connect, index, render as mail_render
from .gmail_api import GmailClient, NotFound
from .imap import MailboxError
from .views import team_only

log = logging.getLogger(__name__)

TABS = [("all", "All"), ("tenants", "Tenants"), ("projects", "Projects"), ("firms", "Capital & directory"),
        ("other", "Everything else")]
TAB_FILTERS = {"tenants": {"is_tenant": True}, "projects": {"is_project": True}, "firms": {"is_firm": True},
               "other": {"is_tenant": False, "is_project": False, "is_firm": False}}
PAGE = 50


def own_mailbox(user):
    """The person's mailbox for the inbox. Someone with Mail on but no timeline mailbox gets one that only feeds the
    inbox (enabled=False keeps the timeline sync off, as before)."""
    mailbox = Mailbox.objects.filter(user=user).first()
    if mailbox is None:
        mailbox = Mailbox.objects.create(user=user, email=user.email.lower(), enabled=False,
                                         backend=Mailbox.Backend.GMAIL_API, backfill_done=True)
    return mailbox


def mail_required(view):
    """Team only, Mail on, and the person's own mailbox and Gmail client handed to the view."""
    @team_only
    def wrapped(request, *args, **kwargs):
        if not connect.is_on(request.user):
            messages.info(request, "Turn on Mail to read your email in the hub.")
            return redirect("crm:mail_settings")
        mailbox = own_mailbox(request.user)
        try:
            client = GmailClient(request.user)
        except MailboxError as exc:
            messages.error(request, str(exc))
            return redirect("crm:mail_settings")
        return view(request, mailbox, client, *args, **kwargs)

    wrapped.__name__ = view.__name__
    return wrapped


@mail_required
def inbox(request, mailbox, client):
    label = request.GET.get("label", "INBOX")
    query = request.GET.get("q", "").strip()[:500]
    tab = request.GET.get("tab", "all") if label == "INBOX" and not query else "all"
    token = request.GET.get("p", "")
    offset = int(request.GET.get("o", "0")) if request.GET.get("o", "0").isdigit() else 0
    open_id = request.GET.get("t", "")
    error, threads, next_token, total, next_offset = "", [], "", 0, None
    try:
        system, user_labels = index.labels(mailbox, client)
        if tab in TAB_FILTERS:
            qs = MailThread.objects.filter(mailbox=mailbox, in_inbox=True, **TAB_FILTERS[tab]) \
                .order_by("-last_message_at")
            threads = list(qs[offset:offset + PAGE + 1])
            if len(threads) > PAGE:
                threads, next_offset = threads[:PAGE], offset + PAGE
        else:
            threads, next_token, total = index.page(
                mailbox, client, label=None if query or label == "ALL" else label, query=query, page_token=token)
        index.retag(mailbox, threads)
        chips = index.chips(mailbox, threads)
        for t in threads:
            t.chips = chips.get(t.gmail_id, [])
        reader = None
        if open_id:
            subject, msgs = mail_render.thread_view(mailbox, client, open_id)
            row = next((t for t in threads if t.gmail_id == open_id), None) \
                or MailThread.objects.filter(mailbox=mailbox, gmail_id=open_id).first()
            reader = {"id": open_id, "subject": subject, "messages": msgs,
                      "chips": index.chips(mailbox, [row]).get(open_id, []) if row else []}
    except NotFound:
        raise Http404("That conversation isn't in your mailbox.")
    except (MailboxError, requests.RequestException) as exc:
        log.warning("Inbox failed for %s: %s", mailbox, exc)
        error = str(exc) if isinstance(exc, MailboxError) else "Couldn't reach Gmail. Try again in a moment."
        system, user_labels, reader = list(mailbox.labels.filter(system=True)), \
            list(mailbox.labels.filter(system=False)), None
    current = {lb.gmail_id: lb.name for lb in [*system, *user_labels]}.get(label, "All mail" if label == "ALL" else label)
    return render(request, "crm/inbox.html", {
        "mailbox": mailbox, "system_labels": system, "user_labels": user_labels, "label": label,
        "label_name": "Search" if query else current, "query": query, "tabs": TABS if label == "INBOX" and not query else [],
        "tab": tab, "threads": threads, "next_token": next_token, "next_offset": next_offset, "offset": offset,
        "page_token": token, "total": total, "reader": reader, "open_id": open_id, "error": error,
        "indexing": tab in TAB_FILTERS and not mailbox.inbox_indexed_at,
    })


@xframe_options_sameorigin
@mail_required
def message_body(request, mailbox, client, message_id):
    """One message's body for the reading pane's frame: cleaned HTML under a policy with no scripts, and remote
    images only after Show images."""
    show = request.GET.get("images") == "1"
    try:
        msg = mail_render.get_message(mailbox, client, message_id)
    except NotFound:
        raise Http404()
    body, blocked = mail_render.body_html(mailbox, msg, show_images=show)
    response = HttpResponse(mail_render.frame_document(body))
    response["Content-Security-Policy"] = mail_render.frame_policy(show)
    response["X-Blocked-Images"] = str(blocked)
    response["Cache-Control"] = "private, no-store"
    return response


@mail_required
def message_part(request, mailbox, client, message_id, part_id):
    """An attachment, streamed from Gmail and never stored. Always a download, except an inline picture (png, jpeg,
    gif, webp) that a message shows in its body."""
    try:
        msg = mail_render.get_message(mailbox, client, message_id)
        part = mail_render.find_part(msg, part_id)
        if part is None or not (part.get("body") or {}).get("attachmentId"):
            raise Http404()
        data = client.attachment(message_id, part["body"]["attachmentId"])
    except NotFound:
        raise Http404()
    mime = part.get("mimeType", "")
    inline = request.GET.get("inline") == "1" and mime in mail_render.INLINE_IMAGE_TYPES
    response = HttpResponse(data, content_type=mime if inline else "application/octet-stream")
    name = part.get("filename") or "attachment"
    response["Content-Disposition"] = content_disposition_header(not inline, name)
    response["Cache-Control"] = "private, no-store"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
