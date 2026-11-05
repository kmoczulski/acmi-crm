"""The inbox (docs/EMAIL_CLIENT.md, step 3): folders, the thread list, the reading pane, search, attachments.

Read only for now: archive, labels and the rest come in step 4, compose and reply in step 5. Everything is reached
through the signed-in person's own mailbox and Google permission, so nobody (Managers and admins included) can open
anyone else's mail; a thread or message id that isn't theirs is simply not found at Gmail.
"""
import io
import logging
import re
import zipfile
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import requests
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.html import escape
from django.utils.http import content_disposition_header, url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from crm import pipeline, prefs
from crm.ai import enabled as ai_enabled
from crm.models import Company, MailFiling, MailLabel, MailSnooze, MailThread, Mailbox, Project, UnmatchedEmail

from . import actions as mail_actions, comments as mail_comments, connect, filing as mail_filing, index, shared
from . import render as mail_render, search as mail_search, summary as mail_summary, threads as mail_threads
from .gmail_api import NotFound, client_for as gmail_client_for, parallel_map, parse_api_message
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


MAILBOX_SESSION = "mail_mb"


def current_mailbox(request):
    """The mailbox the person is looking at: their own, or a shared one they were given (?mb=<id> switches, ?mb=own
    goes back; kept in the session). None when they have neither."""
    given = shared.shared_for(request.user)
    pick = request.GET.get("mb", "")
    if pick == "own":
        request.session.pop(MAILBOX_SESSION, None)
    elif pick.isdigit() and any(m.pk == int(pick) for m in given):
        request.session[MAILBOX_SESSION] = int(pick)
    chosen = next((m for m in given if m.pk == request.session.get(MAILBOX_SESSION)), None)
    if chosen is not None:
        return chosen
    request.session.pop(MAILBOX_SESSION, None)
    if connect.is_on(request.user):
        return own_mailbox(request.user)
    return given[0] if given else None


def mail_required(view):
    """Team only, and a mailbox the person may read (their own with Mail on, or a shared one they were given), handed
    to the view with its Gmail client."""
    @team_only
    def wrapped(request, *args, **kwargs):
        mailbox = current_mailbox(request)
        if mailbox is None:
            messages.info(request, "Turn on Mail to read your email in the hub.")
            return redirect("crm:mail_settings")
        try:
            client = gmail_client_for(mailbox)
        except MailboxError as exc:
            messages.error(request, str(exc))
            return redirect("crm:mail_settings")
        request.mail_role = shared.role(request.user, mailbox)
        return view(request, mailbox, client, *args, **kwargs)

    wrapped.__name__ = view.__name__
    return wrapped


READ_ONLY = "You can read this shared mailbox, but not send from it or change it. A Manager can give you more."
GMAIL_DOWN = "Couldn't reach Gmail. Try again in a moment."


def gmail_error(exc):
    """What to tell the person when a Gmail call failed: the mailbox error's own words, or a line for a network fault."""
    return str(exc) if isinstance(exc, MailboxError) else GMAIL_DOWN


def may_change(request, mailbox):
    """True if the person may send from and change this mailbox; otherwise says why."""
    if shared.can_change(request.user, mailbox):
        return True
    messages.error(request, READ_ONLY)
    return False


@mail_required
def inbox(request, mailbox, client):
    label = request.GET.get("label", "INBOX")
    query = request.GET.get("q", "").strip()[:500]
    tab = request.GET.get("tab", "all") if label == "INBOX" and not query else "all"
    token = request.GET.get("p", "")
    offset = int(request.GET.get("o", "0")) if request.GET.get("o", "0").isdigit() else 0
    open_id = request.GET.get("t", "")
    exact = bool(request.GET.get("exact"))
    within = request.GET.get("in", "")[:100] if query else ""  # "In this folder only" from the search options
    if within and within != "ALL":
        label = within
    gmail_query, widened = (query, []) if exact else mail_search.expand(query)
    thread_base = "?" + urlencode({k: v for k, v in [("label", label), ("q", query), ("exact", "1" if exact else ""),
                                                     ("in", within), ("tab", tab if tab != "all" else ""),
                                                     ("t", open_id)] if v})
    if open_id and request.headers.get("X-Mail-Pane"):
        return _pane(request, mailbox, client, open_id, label, query, tab, thread_base)
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
                mailbox, client, label=None if (query and not within) or label == "ALL" else label, query=gmail_query,
                page_token=token)
        chips = index.chips(mailbox, threads)
        index.retag(mailbox, threads, chips)
        for t in threads:
            t.chips = chips.get(t.gmail_id, [])
        reader = None
        if open_id:
            reader = reader_for(request, mailbox, client, open_id, threads)
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
        "label_tree": index.label_tree(user_labels),
        "label_name": "Search" if query else current, "query": query, "widened": widened, "exact": exact, "within": within, "tabs": TABS if label == "INBOX" and not query else [],
        "tab": tab, "threads": threads, "next_token": next_token, "next_offset": next_offset, "offset": offset,
        "page_token": token, "total": total, "reader": reader, "open_id": open_id, "error": error,
        "indexing": tab in TAB_FILTERS and not mailbox.inbox_indexed_at,
        "undo": request.session.pop("mail_undo", None),
        "mailboxes": ([own_mailbox(request.user)] if connect.is_on(request.user) else []) + shared.shared_for(request.user),
        "can_change": shared.can_change(request.user, mailbox),
        "snooze_choices": mail_actions.snooze_choices(),
        "snoozed": dict(MailSnooze.objects.filter(mailbox=mailbox, gmail_id__in=[t.gmail_id for t in threads])
                        .values_list("gmail_id", "until")),
        "here": request.get_full_path(),
        "thread_base": thread_base,
        "follow_up_count": _follow_up_count(request.user),
    })


def _follow_up_count(user):
    """Threads waiting on us for the Follow-ups link in the folder pane; never stops the inbox from drawing."""
    try:
        return len(pipeline.awaiting_reply(user, days=60))
    except Exception:
        log.warning("Could not count follow-ups for %s", user, exc_info=True)
        return 0


def _pane(request, mailbox, client, open_id, label, query, tab, thread_base):
    """Just the reading pane, for a conversation opened in place from the list: no list from Gmail, no page around
    it. A Gmail failure answers 502 and the page falls back to loading normally."""
    try:
        _, user_labels = index.labels(mailbox, client)
        reader = reader_for(request, mailbox, client, open_id)
    except NotFound:
        raise Http404("That conversation isn't in your mailbox.")
    except (MailboxError, requests.RequestException) as exc:
        log.warning("Reading pane failed for %s: %s", mailbox, exc)
        return HttpResponse(status=502)
    return render(request, "crm/partials/mail_reader.html", {
        "mailbox": mailbox, "user_labels": user_labels, "label": label, "query": query, "tab": tab,
        "reader": reader, "open_id": open_id, "can_change": shared.can_change(request.user, mailbox),
        "snooze_choices": mail_actions.snooze_choices(), "here": request.get_full_path(), "thread_base": thread_base,
    })


@mail_required
def warm(request, mailbox, client):
    """Read a conversation from Gmail into the cache while the mouse rests on it, so it opens at once when clicked.
    Only reads: nothing is marked read and nothing is logged."""
    gmail_id = request.GET.get("t", "")[:40]
    if gmail_id:
        try:
            mail_render.thread_texts(mailbox, client, gmail_id)
        except (NotFound, MailboxError, requests.RequestException):
            pass
    return HttpResponse(status=204)


def reader_for(request, mailbox, client, gmail_id, threads=()):
    """The reading pane: the messages in the person's order (newest first unless they chose oldest), the attachments
    panel, the outline, the people for its filter, and the other parts of a conversation Gmail split."""
    order = request.GET.get("order")
    if order in ("newest", "oldest"):
        prefs.set_pref(request.user, "mail_order", order)
    else:
        order = prefs.get_prefs(request.user).get("mail_order", "newest")
    subject, msgs = mail_render.thread_view(mailbox, client, gmail_id)  # newest first
    for m in msgs:
        m["thread"] = gmail_id
        m["files"] = [a for a in m["attachments"] if not mail_threads.is_noise(a)]  # logos left out here too
    row = next((t for t in threads if t.gmail_id == gmail_id), None) \
        or MailThread.objects.filter(mailbox=mailbox, gmail_id=gmail_id).first()
    groups, hidden = mail_threads.group(mail_threads.files_of(msgs))
    unread = [m for m in msgs if m["unread"]]
    texts, message_ids = mail_render.thread_texts(mailbox, client, gmail_id)  # Find in this thread; team notes
    labels = set()  # the thread's labels for the toolbar
    for raw in mail_render.get_thread(mailbox, client, gmail_id).get("messages", []):
        labels |= set(raw.get("labelIds") or [])
    if unread and shared.can_change(request.user, mailbox):
        mark_read(mailbox, client, gmail_id, row)
    shared.log(mailbox, request.user, "open", gmail_id, subject)
    snooze = MailSnooze.objects.filter(mailbox=mailbox, gmail_id=gmail_id).first()
    own = mailbox.email.lower()
    sender_domains = {m["from_addr"].rsplit("@", 1)[-1] for m in msgs if m["from_addr"] and m["from_addr"] != own}
    return {
        "filings": mail_filing.filings_for(mailbox, gmail_id),
        "queued_domains": sorted(set(UnmatchedEmail.objects.filter(domain__in=sender_domains)
                                     .values_list("domain", flat=True))),
        "comments": mail_comments.for_thread(request.user, None, ids=message_ids),
        "labels": labels, "in_inbox": "INBOX" in labels, "starred": "STARRED" in labels,
        "in_trash": "TRASH" in labels, "snoozed_until": snooze.until if snooze else None,
        "id": gmail_id, "subject": subject, "order": order,
        "messages": msgs if order == "newest" else list(reversed(msgs)),
        "latest": next((m["id"] for m in msgs if not m["draft"]), msgs[0]["id"] if msgs else ""),
        "first_unread": unread[-1]["id"] if unread else "",
        "chips": (row.chips if hasattr(row, "chips") else index.chips(mailbox, [row]).get(gmail_id, [])) if row else [],
        "file_groups": groups, "files_hidden": hidden,
        "file_types": sorted({g["type"] for g in groups}, key=list(mail_threads.TYPE_NAMES).index),
        "outline": mail_threads.outline(msgs), "people": mail_threads.people(msgs),
        "other_parts": mail_threads.other_parts(mailbox, client, gmail_id, subject, msgs),
        "summarize": ai_enabled(), "texts": texts,
    }


def mark_read(mailbox, client, gmail_id, row=None):
    """Opening a thread marks it read, as in Gmail. A failure here never stops the thread from showing."""
    try:
        client.modify_thread(gmail_id, remove=["UNREAD"])
    except (MailboxError, requests.RequestException):
        log.warning("Couldn't mark %s read for %s", gmail_id, mailbox, exc_info=True)
        return
    mail_render.forget(mailbox, gmail_id)
    MailThread.objects.filter(mailbox=mailbox, gmail_id=gmail_id).update(unread=False)
    if row is not None:
        row.unread = False


def _safe_next(request, fallback):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()) \
            and nxt.startswith("/"):
        return nxt
    return fallback


@mail_required
@require_POST
def label_edit(request, mailbox, client):
    """Rename or delete one of the person's own labels (and the ones nested under it), in Gmail and here."""
    back = _safe_next(request, reverse("crm:inbox"))
    if not may_change(request, mailbox):
        return redirect(back)
    label_id, action = request.POST.get("label", ""), request.POST.get("action", "")
    try:
        if action == "rename":
            old = MailLabel.objects.filter(mailbox=mailbox, gmail_id=label_id).values_list("name", flat=True).first()
            name = mail_actions.rename_label(mailbox, client, label_id, request.POST.get("name", ""))
            messages.success(request, f"Renamed {old} to {name}.")
            shared.log(mailbox, request.user, "label", "", f"renamed {old} to {name}")
        elif action == "delete":
            old = MailLabel.objects.filter(mailbox=mailbox, gmail_id=label_id).values_list("name", flat=True).first()
            n = mail_actions.delete_label(mailbox, client, label_id)
            more = f" and {n - 1} label{'s' if n > 2 else ''} under it" if n > 1 else ""
            messages.success(request, f"Deleted the label {old}{more}. The emails are still in your mailbox.")
            shared.log(mailbox, request.user, "label", "", f"deleted {old}")
            if f"label={label_id}" in back:
                back = reverse("crm:inbox")
        else:
            messages.error(request, "Pick Rename or Delete.")
    except mail_actions.ActionError as exc:
        messages.error(request, str(exc))
    except (MailboxError, requests.RequestException):
        log.warning("Label change failed for %s", mailbox, exc_info=True)
        messages.error(request, "Couldn't reach Gmail, so the label wasn't changed. Try again.")
    return redirect(back)


@mail_required
@require_POST
def act(request, mailbox, client):
    """Archive, read / unread, star, label, move, Trash or snooze the picked threads, then back to where you were,
    with Undo. The undo is kept in the session for the next page only."""
    action = request.POST.get("action", "")
    ids = request.POST.getlist("t")
    nxt = _safe_next(request, reverse("crm:inbox"))
    if not may_change(request, mailbox):
        return redirect(nxt)
    until = mail_actions.snooze_until(request.POST.get("when", ""), request.POST.get("custom", "")) \
        if action == "snooze" else None
    undoing = request.POST.get("undo") == "1"
    try:
        text, undo = mail_actions.apply(mailbox, client, action, ids, label=request.POST.get("label", ""), until=until)
    except mail_actions.ActionError as exc:
        messages.error(request, str(exc))
        return redirect(nxt)
    except (MailboxError, requests.RequestException) as exc:
        messages.error(request, str(exc) if isinstance(exc, MailboxError) else "Couldn't reach Gmail. Try again.")
        return redirect(nxt)
    shared.log(mailbox, request.user, "change", ids[0] if len(ids) == 1 else "", f"{action} ({len(ids)})")
    request.session.pop("mail_undo", None)
    wants_json = "application/json" in request.headers.get("Accept", "")
    if undoing:
        if wants_json:
            return JsonResponse({"ok": True, "text": "Undone."})
        messages.success(request, "Undone.")
    elif undo:
        request.session["mail_undo"] = {**undo, "text": text}
    else:
        if wants_json:
            return JsonResponse({"ok": True, "text": text})
        messages.success(request, text)
    if wants_json:
        return JsonResponse({"ok": True, "text": text, "undo": undo or None})
    if action in mail_actions.LEAVES_THE_LIST and not undoing:
        parts = urlsplit(nxt)
        query = [(k, v) for k, v in parse_qsl(parts.query) if not (k == "t" and v in ids)]
        nxt = urlunsplit(parts._replace(query=urlencode(query)))
    return redirect(nxt)


@mail_required
@require_POST
def thread_summary(request, mailbox, client, gmail_id):
    """Summarize, on click only: Claude reads this thread's new words and says where it stands."""
    try:
        data = mail_summary.summarize(mailbox, mail_render.get_thread(mailbox, client, gmail_id))
    except NotFound:
        raise Http404()
    except mail_summary.Unavailable as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except (MailboxError, requests.RequestException) as exc:
        return JsonResponse({"error": gmail_error(exc)}, status=503)
    ids = data["message_ids"]

    def link(n):
        return ids[n - 1] if isinstance(n, int) and 0 < n <= len(ids) else ""

    return JsonResponse({
        "where_it_stands": data["where_it_stands"], "next_step": data["next_step"],
        "open_items": [{**o, "id": link(o["message"])} for o in data["open_items"]],
        "documents": [{**d, "id": link(d["message"])} for d in data["documents"]],
    })


ZIP_LIMIT = 100 * 1024 * 1024


@mail_required
def thread_files_zip(request, mailbox, client, gmail_id):
    """Download all: the newest version of each document in the thread, in one zip, built in memory and never kept."""
    try:
        msgs = mail_threads.summaries_for(mailbox, client, gmail_id)
        groups, _ = mail_threads.group(mail_threads.files_of(msgs))
        buf, total, names = io.BytesIO(), 0, set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for g in groups:
                f = g["latest"]
                total += f["size"]
                if total > ZIP_LIMIT:
                    break
                part = mail_render.find_part(mail_render.get_message(mailbox, client, f["message"]), f["part"])
                if not part:
                    continue
                name, n = f["name"], 2
                while name.lower() in names:
                    stem, dot, ext = f["name"].rpartition(".")
                    name, n = (f"{stem} ({n}).{ext}" if dot else f"{f['name']} ({n})"), n + 1
                names.add(name.lower())
                zf.writestr(name, client.attachment(f["message"], part["body"]["attachmentId"]))
        subject = mail_threads.core_subject(mail_render.thread_view(mailbox, client, gmail_id)[0])[:80] or "Attachments"
    except NotFound:
        raise Http404()
    except (MailboxError, requests.RequestException) as exc:
        messages.error(request, gmail_error(exc))
        return redirect(reverse("crm:inbox") + "?" + urlencode({"t": gmail_id}))
    response = HttpResponse(buf.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = content_disposition_header(True, f"{subject} - files.zip")
    response["Cache-Control"] = "private, no-store"
    return response


FILE_QUERIES = {"pdf": "filename:pdf", "word": "filename:(doc OR docx)",
                "excel": "filename:(xls OR xlsx OR xlsm OR csv)", "image": "filename:(png OR jpg OR jpeg OR heic)",
                "slides": "filename:(ppt OR pptx)", "drawing": "filename:(dwg OR dxf)"}
FILES_PAGE = 25


@mail_required
def files(request, mailbox, client):
    """Every attachment in the mailbox, newest first, searchable and narrowed to a company or project, versions of
    the same document grouped (Outlook's Files view)."""
    words = request.GET.get("q", "").strip()[:200]
    kind = request.GET.get("type", "")
    company = Company.objects.filter(pk=request.GET.get("company") or 0).first()
    project = Project.objects.filter(pk=request.GET.get("project") or 0).first()
    terms = ["has:attachment", FILE_QUERIES.get(kind, ""), words]
    if company and company.domains:
        ds = " OR ".join(company.domains[:10])
        terms.append(f"(from:({ds}) OR to:({ds}))")
    if project:
        keys = [k for k in [project.name, *(project.keywords or [])] if k][:10]
        terms.append("(" + " OR ".join('"' + k.replace('"', "") + '"' for k in keys) + ")")
    query = " ".join(t for t in terms if t)
    error, groups, hidden, next_token = "", [], 0, ""
    try:
        ids, next_token = client.message_page(query, request.GET.get("p", ""), FILES_PAGE)
        if ids:
            client.prime()
        raws = parallel_map(lambda i: mail_render.get_message(mailbox, client, i), ids, 8)
        msgs = []
        for raw in raws:
            m = mail_render.summary(raw)
            m["thread"] = raw.get("threadId", "")
            msgs.append(m)
        groups, hidden = mail_threads.group(mail_threads.files_of(msgs))
        if kind:
            groups = [g for g in groups if g["type"] == kind]
    except (MailboxError, requests.RequestException) as exc:
        error = str(exc) if isinstance(exc, MailboxError) else "Couldn't reach Gmail. Try again in a moment."
    return render(request, "crm/inbox_files.html", {
        "mailbox": mailbox, "groups": groups, "hidden": hidden, "next_token": next_token, "error": error,
        "q": words, "type": kind, "types": [(k, v) for k, v in mail_threads.TYPE_NAMES.items() if k != "other"],
        "company": company, "project": project,
        "projects": Project.objects.filter(active=True).order_by("name"),
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
    except (MailboxError, requests.RequestException) as exc:  # the frame shows the words, not an error page
        response = HttpResponse(mail_render.frame_document(f"<p>{escape(gmail_error(exc))}</p>"), status=502)
        response["Content-Security-Policy"] = mail_render.frame_policy(False)
        response["Cache-Control"] = "private, no-store"
        return response
    body, blocked = mail_render.body_html(mailbox, msg, show_images=show)
    response = HttpResponse(mail_render.frame_document(body))
    response["Content-Security-Policy"] = mail_render.frame_policy(show)
    response["X-Blocked-Images"] = str(blocked)
    response["Cache-Control"] = "private, no-store"
    return response


@mail_required
@require_POST
def save_to_drive(request, mailbox, client, message_id, part_id):
    """Save one attachment into the filed company's mapped Drive folder, on this person's own permission."""
    from . import drive_save

    nxt = _safe_next(request, reverse("crm:inbox"))
    if not drive_save.has_scope(request.user):
        request.session["drive_save"] = {"message": message_id, "part": part_id, "next": nxt}
        return redirect(drive_save.connect_url(request, reverse("crm:inbox_save_drive_back")))
    filing = MailFiling.objects.filter(mailbox=mailbox, gmail_id=request.POST.get("t", "")).select_related("company").first()
    company = filing.company if filing else None
    folder = (company.drive_folder if company else "") or ""
    if not folder:
        messages.error(request, "File this thread to a company that has a Drive folder, then save the attachment.")
        return redirect(nxt)
    try:
        msg = mail_render.get_message(mailbox, client, message_id)
        part = mail_render.find_part(msg, part_id)
        if part is None or not (part.get("body") or {}).get("attachmentId"):
            raise Http404()
        data = client.attachment(message_id, part["body"]["attachmentId"])
        saved = drive_save.upload(request.user, name=part.get("filename") or "attachment",
                                  mime=part.get("mimeType", ""), data=data, folder_id=folder)
    except Http404:
        raise
    except drive_save.DriveSaveError as exc:
        messages.error(request, str(exc))
    except (MailboxError, requests.RequestException):
        messages.error(request, "Couldn't reach Gmail or Drive. Try again.")
    else:
        messages.success(request, f"Saved {part.get('filename') or 'the file'} to {company}'s Drive folder.")
        if saved.get("link"):
            messages.info(request, saved["link"])
    return redirect(nxt)


def save_to_drive_back(request):
    """Google sent the person back after they allowed Drive. The save they asked for is one click away."""
    pending = request.session.pop("drive_save", None) or {}
    messages.success(request, "Drive access allowed. Press Save to Drive on the attachment again.")
    return redirect(pending.get("next") or "crm:inbox")


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
    except (MailboxError, requests.RequestException) as exc:
        return HttpResponse(gmail_error(exc), status=502, content_type="text/plain; charset=utf-8")
    mime = part.get("mimeType", "")
    inline = request.GET.get("inline") == "1" and mime in mail_render.INLINE_IMAGE_TYPES
    if not inline:
        shared.log(mailbox, request.user, "download", msg.get("threadId", ""), part.get("filename", ""))
    response = HttpResponse(data, content_type=mime if inline else "application/octet-stream")
    name = part.get("filename") or "attachment"
    response["Content-Disposition"] = content_disposition_header(not inline, name)
    response["Cache-Control"] = "private, no-store"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response

# ---------------------------------------------------------------------------
# Step 6: filing a thread to a record, team notes, and opening an email from a timeline
# ---------------------------------------------------------------------------


@team_only
def suggest(request):
    """Address book for To / Cc / Bcc and the search box: people (name, email) and the companies, firms and people
    a search for these words also covers."""
    words = request.GET.get("q", "")[:100]
    records = [{"kind": k, "label": label} for k, label, _ in mail_search.matches(words)] \
        if request.GET.get("records") else []
    return JsonResponse({"people": mail_search.recipients(request.user, words), "records": records})


@mail_required
def file_options(request, mailbox, client):
    """What File to… offers for the typed words."""
    return JsonResponse({"options": [{"kind": k, "pk": pk, "name": name, "detail": detail}
                                     for k, pk, name, detail in mail_filing.options(request.GET.get("q", ""))]})


@mail_required
@require_POST
def file_to(request, mailbox, client):
    nxt = _safe_next(request, reverse("crm:inbox"))
    if not may_change(request, mailbox):  # filing changes the thread's labels in Gmail
        return redirect(nxt)
    gmail_id = request.POST.get("t", "")
    try:
        target = mail_filing.target_for(request.POST.get("kind", ""), request.POST.get("pk", ""))
        mail_filing.file_thread(mailbox, client, gmail_id, target, user=request.user,
                                attach_domain=request.POST.get("attach_domain") == "1")
    except mail_filing.FilingError as exc:
        messages.error(request, str(exc))
    except NotFound:
        raise Http404()
    except (MailboxError, requests.RequestException) as exc:
        messages.error(request, str(exc) if isinstance(exc, MailboxError) else "Couldn't reach Gmail. Try again.")
    else:
        shared.log(mailbox, request.user, "file", gmail_id, str(target))
        messages.success(request, f"Filed to {target}. It's on its timeline now.")
    return redirect(nxt)


@mail_required
@require_POST
def unfile(request, mailbox, client, pk):
    from django.shortcuts import get_object_or_404

    nxt = _safe_next(request, reverse("crm:inbox"))
    if not may_change(request, mailbox):
        return redirect(nxt)
    filing = get_object_or_404(MailFiling, pk=pk, mailbox=mailbox)
    target = filing.target
    mail_filing.unfile(filing, client)
    messages.success(request, f"Taken off {target}.")
    return redirect(nxt)


@mail_required
@require_POST
def comment_add(request, mailbox, client):
    nxt = _safe_next(request, reverse("crm:inbox"))
    gmail_id = request.POST.get("t", "")
    try:
        thread = mail_render.get_thread(mailbox, client, gmail_id)
        subject = mail_render.thread_view(mailbox, client, gmail_id)[0]
    except NotFound:
        raise Http404()
    except (MailboxError, requests.RequestException) as exc:  # the note needs the thread, so it waits for Gmail
        messages.error(request, gmail_error(exc))
        return redirect(nxt)
    if mail_comments.add(request.user, thread, subject, request.POST.get("body", "")):
        messages.success(request, "Note added.")
    return redirect(nxt)


@mail_required
def open_message(request, mailbox, client):
    """From a timeline: find that email (by its Message-ID) in your own mailbox and open its thread."""
    message_id = request.GET.get("m", "").strip()[:500]
    found = []
    if message_id:
        try:
            found, _ = client.message_page(f"rfc822msgid:{message_id.strip('<>')}", size=1)
        except (MailboxError, requests.RequestException):
            found = []
    if not found:
        messages.info(request, "That email isn't in your mailbox.")
        return redirect("crm:inbox")
    thread = mail_render.get_message(mailbox, client, found[0]).get("threadId", "")
    return redirect(f"{reverse('crm:inbox')}?label=ALL&t={thread}")


@mail_required
def thread_window(request, mailbox, client):
    """One conversation in the mail dock's little box (static/crm/dock.js), opened from a new-email alert: the messages
    and Reply / Reply all / Forward, which open their own boxes beside it. Reading it marks it read, as in the inbox.
    ?t=<thread> from an alert; ?m=<Message-ID> works as it does for /inbox/open/. Gmail trouble is shown in the box."""
    gmail_id = request.GET.get("t", "")[:40]
    notice, reader = "", None
    try:
        if not gmail_id:
            message_id = request.GET.get("m", "").strip()[:500]
            found = client.message_page(f"rfc822msgid:{message_id.strip('<>')}", size=1)[0] if message_id else []
            gmail_id = mail_render.get_message(mailbox, client, found[0]).get("threadId", "") if found else ""
        if gmail_id:
            reader = reader_for(request, mailbox, client, gmail_id)
    except NotFound:
        pass
    except (MailboxError, requests.RequestException) as exc:
        log.warning("Mail box for %s failed: %s", mailbox, exc)
        notice = gmail_error(exc)
    if reader is None and not notice:
        notice = "That email isn't in your mailbox."
    win = re.sub(r"[^A-Za-z0-9_-]", "", request.GET.get("w", ""))[:24]
    return render(request, "crm/mail_window.html", {
        "mailbox": mailbox, "reader": reader, "notice": notice, "window": win,
        "can_change": shared.can_change(request.user, mailbox),
        "layout": "crm/window_base.html",
    })
