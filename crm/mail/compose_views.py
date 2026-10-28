"""The compose page (docs/EMAIL_CLIENT.md, step 5): new message, reply, reply all, forward; drafts; signature."""
import logging

import requests
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from crm import prefs
from crm.models import EmailTemplate, Person

from . import compose, index, render as mail_render
from .gmail_api import NotFound
from .imap import MailboxError
from . import shared
from .inbox_views import GMAIL_DOWN, READ_ONLY, mail_required, may_change
from .views import team_only

log = logging.getLogger(__name__)


def _original(mailbox, client, message_id):
    if not message_id:
        return None
    try:
        return mail_render.get_message(mailbox, client, message_id)
    except NotFound:
        raise Http404("That email isn't in your mailbox.")
    except requests.RequestException:
        raise MailboxError(GMAIL_DOWN)


def _fields_from_post(request):
    """What the person typed, checked: recipients parsed, ComposeError for a bad address."""
    post = request.POST
    return {
        "to": compose.parse_recipients(post.get("to", ""), "To"),
        "cc": compose.parse_recipients(post.get("cc", ""), "Cc"),
        "bcc": compose.parse_recipients(post.get("bcc", ""), "Bcc"),
        "subject": post.get("subject", "").replace("\r", " ").replace("\n", " "),
        "body": post.get("body", ""),
        "body_html": post.get("body_html", ""),
        "thread_id": post.get("thread_id", ""),
        "in_reply_to": post.get("in_reply_to", ""),
        "references": post.get("references", ""),
    }


def _first_name(addresses):
    from email.utils import getaddresses

    for _, addr in getaddresses([addresses or ""]):
        person = Person.objects.filter(email__iexact=addr).only("first_name").first()
        if person and person.first_name:
            return person.first_name
    return ""


@mail_required
def compose_page(request, mailbox, client):
    """GET: the form, filled in for a reply / reply all / forward (?reply=<message id>&mode=…) or for an address
    (?to=…, from a person or company page). POST: send."""
    try:
        original = _original(mailbox, client, request.POST.get("reply_to_id") or request.GET.get("reply", ""))
    except MailboxError as exc:
        messages.error(request, str(exc))
        return redirect("crm:inbox")
    mode = request.POST.get("mode") or request.GET.get("mode", "reply" if original else "new")
    if mode not in ("new", "reply", "all", "forward"):
        mode = "new"
    if request.method == "POST":
        if not shared.can_change(request.user, mailbox):
            return _form_again(request, mailbox, client, original, mode, READ_ONLY)
        return _send(request, mailbox, client, original, mode)
    if not may_change(request, mailbox):
        return redirect("crm:inbox")
    fields = {"to": request.GET.get("to", ""), "cc": "", "subject": request.GET.get("subject", ""), "thread_id": "",
              "in_reply_to": "", "references": "", "reply_to_id": "", "mode": mode, "forward_files": []}
    if original:
        fields.update(compose.reply_fields(mailbox, original, mode))
    return render(request, "crm/compose.html", {
        "f": fields, "body": "", "original": mail_render.summary(original) if original else None,
        "signature_html": compose.signature_parts(request.user, mailbox, client)[1], "mailbox": mailbox,
        "templates": list(EmailTemplate.objects.filter(active=True).order_by("template_id")
                          .values("template_id", "name", "subject", "body_plain")),
        "first_name": _first_name(fields["to"]), "sending_on": compose.settings.MAIL_SEND_ENABLED,
        "back": request.GET.get("back", ""),
    })


def _send(request, mailbox, client, original, mode):
    back = request.POST.get("back") or reverse("crm:inbox")
    try:
        f = _fields_from_post(request)
        files = [(u.name, u.read(), u.content_type) for u in request.FILES.getlist("files")]
        quote = ("", "")
        if original:
            quote = compose.quoted(mailbox, original, "forward" if mode == "forward" else "reply")
            if mode == "forward":
                files = compose.forwarded_files(mailbox, client, original, request.POST.getlist("forward_part")) + files
        raw, _ = compose.build(mailbox, request.user, f["to"], f["cc"], f["bcc"], f["subject"], f["body"], quote=quote,
                               in_reply_to=f["in_reply_to"], references=f["references"], files=files,
                               sig=compose.signature_parts(request.user, mailbox, client), body_html=f["body_html"])
        result = compose.send(mailbox, client, raw, thread_id=f["thread_id"] if mode in ("reply", "all") else "",
                              draft_id=request.POST.get("draft_id", ""), user=request.user)
        shared.log(mailbox, request.user, "send", result.get("threadId", ""), f["subject"])
    except compose.ComposeError as exc:
        return _form_again(request, mailbox, client, original, mode, str(exc))
    except (MailboxError, requests.RequestException) as exc:
        text = str(exc) if isinstance(exc, MailboxError) else "Couldn't reach Gmail, so nothing was sent. Try again."
        return _form_again(request, mailbox, client, original, mode, text)
    try:
        index.refresh(mailbox, client, [result.get("threadId", "")])
    except Exception:  # the email went; the list catches up on its own
        log.warning("Index refresh after sending failed for %s", mailbox, exc_info=True)
    messages.success(request, "Sent.")
    if mode in ("reply", "all") and back.startswith("/") and not back.startswith("//") and "t=" in back:
        url = back  # back to the thread you were reading, now with your reply in it
    else:
        url = f"{reverse('crm:inbox')}?label=SENT&t={result.get('threadId', '')}"
    if request.POST.get("inline"):  # the reading pane's own reply box: it reloads the thread itself
        return JsonResponse({"ok": True, "url": url})
    return redirect(url)


def _form_again(request, mailbox, client, original, mode, error):
    """The form as the person left it, with what went wrong. Uploaded files have to be picked again."""
    post = request.POST
    if post.get("inline"):  # the reading pane's reply box keeps what was typed and shows the error itself
        return JsonResponse({"error": error}, status=400)
    fields = {k: post.get(k, "") for k in ("to", "cc", "subject", "thread_id", "in_reply_to", "references",
                                           "reply_to_id")}
    fields.update({"mode": mode, "bcc": post.get("bcc", ""),
                   "forward_files": mail_render.attachments(original) if original and mode == "forward" else []})
    return render(request, "crm/compose.html", {
        "f": fields, "body": post.get("body", ""), "body_html": compose.formatted(post.get("body_html", "")),
        "error": error, "draft_id": post.get("draft_id", ""),
        "kept_parts": post.getlist("forward_part"),
        "original": mail_render.summary(original) if original else None,
        "signature_html": compose.signature_parts(request.user, mailbox, client)[1], "mailbox": mailbox,
        "templates": [],
        "first_name": "", "sending_on": compose.settings.MAIL_SEND_ENABLED, "back": post.get("back", ""),
    }, status=400)


@mail_required
def reply_fields(request, mailbox, client):
    """For the reading pane's inline reply box: who it goes to, the subject and threading for a reply, reply all or
    forward of one message, the original's files on a forward, and the signature to show."""
    try:
        original = _original(mailbox, client, request.GET.get("reply", ""))
    except MailboxError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    if original is None:
        raise Http404()
    if not shared.can_change(request.user, mailbox):
        return JsonResponse({"error": READ_ONLY}, status=403)
    mode = request.GET.get("mode", "reply")
    if mode not in ("reply", "all", "forward"):
        mode = "reply"
    fields = compose.reply_fields(mailbox, original, mode)
    return JsonResponse({
        **{k: fields.get(k, "") for k in ("to", "cc", "subject", "thread_id", "in_reply_to", "references",
                                         "reply_to_id", "mode")},
        "forward_files": [{"part": a["part"], "name": a["name"], "size": a["size"]}
                          for a in fields.get("forward_files", [])],
        "signature_html": compose.signature_parts(request.user, mailbox, client)[1],
        "sending_on": compose.settings.MAIL_SEND_ENABLED,
    })


@team_only
def calendar_week(request):
    """Your calendar for a week (?start=YYYY-MM-DD, the first day shown) for the Times panel beside an email."""
    from datetime import date, datetime, time

    from django.utils import timezone

    from crm import gcal

    try:
        day = date.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        day = timezone.localdate()
    start = timezone.make_aware(datetime.combine(day, time()))
    try:
        events = gcal.week(request.user, start)
    except gcal.CalendarError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except requests.RequestException:
        log.warning("Calendar week failed for %s", request.user, exc_info=True)
        return JsonResponse({"error": "Couldn't reach Google Calendar. Try again."}, status=502)
    return JsonResponse({"start": day.isoformat(), "events": events})


@mail_required
@require_POST
def save_draft(request, mailbox, client):
    """Autosave: what's typed so far, as a Gmail draft. Returns {draft: id}."""
    if not shared.can_change(request.user, mailbox):
        return JsonResponse({"error": READ_ONLY}, status=403)
    try:
        f = _fields_from_post(request)
    except compose.ComposeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    try:
        original = _original(mailbox, client, request.POST.get("reply_to_id", ""))
    except MailboxError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    mode = request.POST.get("mode", "new")
    if original:
        f["quote"] = compose.quoted(mailbox, original, "forward" if mode == "forward" else "reply")
    if mode not in ("reply", "all"):
        f["thread_id"] = ""
    try:
        draft = compose.save_draft(mailbox, client, request.user, f, draft_id=request.POST.get("draft_id", ""))
    except (MailboxError, requests.RequestException, compose.ComposeError) as exc:
        return JsonResponse({"error": str(exc) or "Couldn't save the draft."}, status=503)
    return JsonResponse({"draft": draft})


@mail_required
@require_POST
def discard_draft(request, mailbox, client):
    if not may_change(request, mailbox):
        return redirect("crm:inbox")
    draft_id = request.POST.get("draft_id", "")
    if draft_id:
        try:
            client.delete_draft(draft_id)
        except (MailboxError, requests.RequestException, NotFound):
            pass
    messages.success(request, "Draft discarded.")
    return redirect(request.POST.get("back") if (request.POST.get("back") or "").startswith("/")
                    and not request.POST.get("back", "").startswith("//") else reverse("crm:inbox"))


@team_only
@require_POST
def save_signature(request):
    """Which signature goes under your emails: the one in Gmail (default), one written here, or none."""
    mode = request.POST.get("mode", "custom")
    if mode not in ("gmail", "custom", "none"):
        mode = "gmail"
    prefs.set_pref(request.user, "mail_signature_mode", mode)
    if mode == "custom":
        prefs.set_pref(request.user, "mail_signature", request.POST.get("signature", "")[:2000])
    messages.success(request, "Signature saved.")
    return redirect("crm:mail_settings")
