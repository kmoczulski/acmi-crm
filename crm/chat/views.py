"""Chat pages (docs/CHAT.md): the full page, the dock's list and conversation boxes, and the small JSON calls behind them.

Every call goes through the signed-in person's own Chat permission, so a conversation that isn't theirs is simply not
found at Google; nothing here takes anyone else's id.
"""
import json
import logging
import mimetypes
import os
import re
from datetime import timedelta
from functools import wraps

import requests
from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_POST

from crm.mail.views import team_only
from crm.models import ChatConnection

from . import connect, service
from .api import BadRequest, ChatError, ChatNotFound, media_name, space_name
from .render import IMAGE_TYPES, decorate, parse_time, stamp

log = logging.getLogger(__name__)
PAGE = 40  # messages a conversation opens with, and each "Earlier messages"
AROUND = 15  # messages shown above one a search found
THREAD = re.compile(r"^spaces/[\w-]+/threads/[\w.-]+$")
OFF = "Chat is off. Turn it on on the Chat page."
NOT_THERE = "That conversation isn't in your Chat."
MAX_TEXT = 4000


def api(view):
    """A small JSON call: team only, Chat on, the person's own client. Chat trouble comes back as {"error": words}."""
    @team_only
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not connect.is_on(request.user):
            return JsonResponse({"error": OFF}, status=403)
        try:
            return view(request, service.client_for(request.user), *args, **kwargs)
        except ChatNotFound:
            return JsonResponse({"error": NOT_THERE}, status=404)
        except service.NeedsPermission as exc:
            return JsonResponse({"error": str(exc), "needs_permission": True}, status=403)
        except BadRequest as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except ChatError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        except requests.RequestException:
            log.warning("Chat call failed for %s", request.user, exc_info=True)
            return JsonResponse({"error": "Couldn't reach Google Chat. Try again."}, status=502)

    return wrapped


def _window_id(request):
    return re.sub(r"[^A-Za-z0-9_-]", "", request.GET.get("w", ""))[:24]


def _conversation(user, client, space, around=""):
    """Everything the conversation partial shows for one space; opening it marks it read, as in Chat itself. around: the
    createTime of a message a search found: the page opens at it instead of at the newest."""
    row = service.find(user, client, space)
    moment, hit, older_more = parse_time(around), "", False
    if moment:
        before, raw_before, directory = service.load(user, client, space, before=stamp(moment), limit=AROUND)
        after, raw_after, _ = service.load(user, client, space, after=stamp(moment - timedelta(microseconds=1)), limit=PAGE)
        views, raw = decorate(before + after), raw_before + raw_after
        hit = next((v["id"] for v in views if v["time"] == moment), "")
        older_more = len(raw_before) >= AROUND
    else:
        views, raw, directory = service.load(user, client, space, limit=PAGE)
        older_more = len(raw) >= PAGE
        if row["unread"]:
            service.mark_read(user, client, space)
    me = service.me_of(user)
    mine = service.my_id(directory, me)
    others = [p["name"] for uid, p in directory.items() if uid != mine and not (p["email"] and p["email"] == me)]
    people = "" if row["type"] == "DIRECT_MESSAGE" else \
        ", ".join(others[:6]) + (f" and {len(others) - 6} more" if len(others) > 6 else "")
    members = [{"id": uid, "name": p["name"], "email": p["email"]} for uid, p in directory.items()
               if uid != mine and not (p["email"] and p["email"] == me)]
    conn = connect.connection(user)
    return {"space": row, "messages": views, "latest": raw[-1].get("createTime", "") if raw else "",
            "earliest": raw[0].get("createTime", "") if raw else "", "more": older_more, "people": people, "me": me,
            "members": sorted(members, key=lambda p: p["name"].lower()), "hit": hit, "sig": "" if moment else service.signature(views),
            "can_create": bool(conn and conn.can_create), "everyone": row["type"] != "DIRECT_MESSAGE",
            "max_mb": settings.CHAT_MAX_UPLOAD_MB}


def _previous(params):
    """The message just above new ones on the page, so a refresh carries the grouping on."""
    uid = params.get("prev_uid", "")
    if not uid:
        return None
    return {"uid": uid, "time": parse_time(params.get("prev_time", "")), "reply": params.get("prev_reply") == "1"}


def _page_context(request):
    conn = connect.connection(request.user)
    return {"on": connect.is_on(request.user), "unavailable": connect.unavailable_reason(request.user), "conn": conn,
            "can_create": bool(conn and conn.can_create), "people": service.teammates(request.user)}


def _tokens(raw):
    """Upload tokens the page holds for files on the message being sent (a JSON list), cleaned."""
    try:
        values = json.loads(raw or "[]")
    except ValueError:
        return []
    return [v for v in values if isinstance(v, str) and 0 < len(v) <= 4096][:service.MAX_FILES] if isinstance(values, list) else []


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@team_only
def home(request):
    """The Chat page: your conversations on the left, the open one on the right (?s=<space>, and &at=<time> to open at a
    message a search found). It is also what the split view's side shows, and a click on a conversation swaps the right
    side in place (X-Chat-Pane)."""
    ctx = {**_page_context(request), "rows": [], "convo": None, "current": ""}
    if ctx["on"]:
        try:
            client = service.client_for(request.user)
            ctx["rows"] = service.overview(request.user, client)["rows"]
            space = space_name(request.GET.get("s", ""))
            if space:
                ctx["convo"] = _conversation(request.user, client, space, request.GET.get("at", ""))
                ctx["current"] = ctx["convo"]["space"]["id"]
        except ChatNotFound:
            if request.headers.get("X-Chat-Pane"):
                raise Http404(NOT_THERE)
            ctx["error"] = NOT_THERE
        except ChatError as exc:
            ctx["error"] = str(exc)
        except requests.RequestException:
            log.warning("Chat page failed for %s", request.user, exc_info=True)
            ctx["error"] = "Couldn't reach Google Chat. Try again."
    if request.headers.get("X-Chat-Pane") and ctx["convo"]:
        return render(request, "crm/partials/chat_conversation.html", {"convo": ctx["convo"]})
    return render(request, "crm/chat.html", ctx)


@team_only
def window(request):
    """One conversation in a box of the dock (static/crm/dock.js)."""
    ctx = {**_page_context(request), "window": _window_id(request), "layout": "crm/window_base.html", "convo": None}
    space = space_name(request.GET.get("s", ""))
    if not ctx["on"]:
        ctx["error"] = OFF
    elif not space:
        ctx["error"] = "Pick a conversation from the Chat list."
    else:
        try:
            ctx["convo"] = _conversation(request.user, service.client_for(request.user), space, request.GET.get("at", ""))
        except ChatNotFound:
            ctx["error"] = NOT_THERE
        except ChatError as exc:
            ctx["error"] = str(exc)
        except requests.RequestException:
            ctx["error"] = "Couldn't reach Google Chat. Try again."
    return render(request, "crm/chat_window.html", ctx)


@team_only
def list_box(request):
    """The list of conversations in the dock's Chat box."""
    ctx = {**_page_context(request), "window": _window_id(request), "layout": "crm/window_base.html", "rows": []}
    if ctx["on"]:
        try:
            ctx["rows"] = service.overview(request.user, service.client_for(request.user))["rows"]
        except ChatError as exc:
            ctx["error"] = str(exc)
        except requests.RequestException:
            ctx["error"] = "Couldn't reach Google Chat. Try again."
    return render(request, "crm/chat_list.html", ctx)


# ---------------------------------------------------------------------------
# The small calls behind them
# ---------------------------------------------------------------------------


@api
def rows(request, client):
    """The list's rows again (it refreshes itself), and how many conversations are unread."""
    ov = service.overview(request.user, client)
    html = render_to_string("crm/partials/chat_rows.html", {"rows": ov["rows"], "current": request.GET.get("current", "")})
    return JsonResponse({"html": html, "unread": ov["unread"]})


@api
def messages_json(request, client):
    """New messages (?after=<createTime of the last one shown>) or earlier ones (?before=<the first shown>)."""
    space = space_name(request.GET.get("s", ""))
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    after, before = request.GET.get("after", ""), request.GET.get("before", "")
    limit = PAGE if before else 100
    views, raw, _ = service.load(request.user, client, space, after=after or None, before=before or None, limit=limit,
                                 previous=None if before else _previous(request.GET))
    return JsonResponse({
        "html": render_to_string("crm/partials/chat_messages.html", {"messages": views}),
        "latest": raw[-1].get("createTime", "") if raw else "", "earliest": raw[0].get("createTime", "") if raw else "",
        "count": len(raw), "more": len(raw) >= limit, "from_others": any(not v["mine"] for v in views),
    })


@api
@require_POST
def refresh(request, client):
    """Re-read the newest messages for what others changed since (a reaction, an edit, a deletion): {"changed": false}
    when the page already shows it, else the messages to draw in place of the page's own from "first" on."""
    space = space_name(request.POST.get("s", ""))
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    out = service.refresh_tail(request.user, client, space, request.POST.get("sig", ""))
    if not out["changed"] or not out["views"]:
        return JsonResponse({"changed": False, "sig": out["sig"]})
    raw = out["raw"]
    return JsonResponse({"changed": True, "sig": out["sig"], "first": out["views"][0]["id"],
                         "html": render_to_string("crm/partials/chat_messages.html", {"messages": out["views"]}),
                         "latest": raw[-1].get("createTime", "")})


@api
@require_POST
def send(request, client):
    space = space_name(request.POST.get("s", ""))
    text = request.POST.get("text", "").strip()
    files = _tokens(request.POST.get("files", ""))
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    if not text and not files:
        return JsonResponse({"error": "Write something first."}, status=400)
    if len(text) > MAX_TEXT:
        return JsonResponse({"error": "That's too long for one message. Split it up."}, status=400)
    thread = request.POST.get("thread", "")
    view = service.post(request.user, client, space, text, thread if THREAD.match(thread) else "",
                        quote=request.POST.get("quote", ""), attachments=files)
    decorate([view], _previous(request.POST))
    return JsonResponse({"ok": True, "html": render_to_string("crm/partials/chat_messages.html", {"messages": [view]}),
                         "latest": view["iso"]})


@api
@require_POST
def react(request, client):
    """Put an emoji on a message, or take the person's own away. Answers the message's emoji row to draw."""
    space = space_name(request.POST.get("s", ""))
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    out = service.toggle_reaction(request.user, client, space, request.POST.get("m", ""), request.POST.get("emoji", ""))
    return JsonResponse({"on": out["on"], "html": render_to_string("crm/partials/chat_reactions.html", {"reactions": out["reactions"]})})


@api
def reactions(request, client):
    """Who gave which emoji on a message (?s=&m=)."""
    space = space_name(request.GET.get("s", ""))
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    return JsonResponse({"emojis": service.reactors(request.user, client, space, request.GET.get("m", ""))})


@api
@require_POST
def edit(request, client):
    """Change one of the person's own messages."""
    space = space_name(request.POST.get("s", ""))
    text = request.POST.get("text", "").strip()
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    if not text:
        return JsonResponse({"error": "A message can't be empty. Delete it instead."}, status=400)
    if len(text) > MAX_TEXT:
        return JsonResponse({"error": "That's too long for one message."}, status=400)
    view = service.edit(request.user, client, space, request.POST.get("m", ""), text)
    return JsonResponse({"ok": True, "html": str(view["html"]), "raw": view["raw"], "edited": view["edited"]})


@api
@require_POST
def delete(request, client):
    """Take back one of the person's own messages (force: with the replies in its thread)."""
    space = space_name(request.POST.get("s", ""))
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    service.remove(request.user, client, space, request.POST.get("m", ""), force=request.POST.get("force") == "1")
    return JsonResponse({"ok": True})


def _file_name(value):
    """A file name that is only a name: no folders, nothing odd, never empty."""
    name = os.path.basename(str(value or "").replace("\\", "/"))
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*').strip(" .")[:150]
    return name or "file"


@api
@require_POST
def upload(request, client):
    """One file the person attached to a message they're writing: put in Chat now, so sending only names it."""
    space = space_name(request.POST.get("s", ""))
    file = request.FILES.get("file")
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    if file is None:
        return JsonResponse({"error": "Pick a file."}, status=400)
    limit = settings.CHAT_MAX_UPLOAD_MB * 1024 * 1024
    if file.size > limit:
        return JsonResponse({"error": f"That file is bigger than {settings.CHAT_MAX_UPLOAD_MB} MB, which is as much as the "
                                      "hub can pass on. Share it from Drive in Google Chat instead."}, status=400)
    if file.size == 0:
        return JsonResponse({"error": "That file is empty."}, status=400)
    name = _file_name(file.name)
    kind = (file.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0].strip()
    done = service.upload(request.user, client, space, name, kind, file.read())
    return JsonResponse({**done, "size": file.size, "type": kind})


@team_only
def attachment(request):
    """A file uploaded to Chat, fetched as the signed-in person and passed on: shown in the page if it is a plain picture,
    otherwise only ever downloaded. ?r=<Chat's name for the file>&n=<its file name>&d=1 to force a download."""
    resource = media_name(request.GET.get("r", ""))
    if not resource or not connect.is_on(request.user):
        raise Http404()
    try:
        upstream = service.client_for(request.user).download(resource)
    except ChatNotFound:
        raise Http404() from None
    except (ChatError, requests.RequestException):
        log.warning("Chat file failed for %s", request.user, exc_info=True)
        return HttpResponse("Couldn't get that file from Google Chat.", status=502, content_type="text/plain")
    kind = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    inline = kind in IMAGE_TYPES and not request.GET.get("d")

    def chunks():
        try:
            yield from upstream.iter_content(65536)
        finally:
            upstream.close()

    response = StreamingHttpResponse(chunks(), content_type=kind if inline else "application/octet-stream")
    response["Content-Disposition"] = content_disposition_header(not inline, _file_name(request.GET.get("n")))
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; sandbox"
    response["Cache-Control"] = "private, max-age=3600"
    if upstream.headers.get("Content-Length") and not upstream.headers.get("Content-Encoding"):  # compressed: the length isn't ours
        response["Content-Length"] = upstream.headers["Content-Length"]
    return response


@api
@require_POST
def new(request, client):
    """Start (or open) a conversation: one person is a direct message, several a group chat, a name makes it a space."""
    conn = connect.connection(request.user)
    row = service.start_chat(request.user, client, request.POST.get("people", ""), request.POST.get("name", ""),
                             allowed=bool(conn and conn.can_create))
    return JsonResponse({"ok": True, "id": row["id"], "title": row["title"]})


@api
def search(request, client):
    """Messages that match some words, in every conversation (?q=, &page=<token> for more)."""
    out = service.search(request.user, client, request.GET.get("q", ""), request.GET.get("page") or None)
    return JsonResponse({"html": render_to_string("crm/partials/chat_search.html", {"results": out["results"]}),
                         "next": out["next"], "count": len(out["results"])})


@api
@require_POST
def read(request, client):
    space = space_name(request.POST.get("s", ""))
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    service.find(request.user, client, space)  # only a conversation that is really in this person's Chat
    return JsonResponse({"ok": service.mark_read(request.user, client, space)})


@api
@require_POST
def mute(request, client):
    space = space_name(request.POST.get("s", ""))
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    service.find(request.user, client, space)  # only a conversation that is really in this person's Chat
    return JsonResponse({"muted": service.toggle_mute(request.user, space.split("/", 1)[1])})


@api
def poll(request, client):
    """For the dock, every 20 seconds or so: how many conversations are unread and which. Also where new messages become
    alerts while the hub is open (the scheduled sweep does it when it isn't)."""
    ov = service.overview(request.user, client)
    made = service.alert(request.user, client)
    return JsonResponse({"unread": ov["unread"], "ids": [r["id"] for r in ov["rows"] if r["unread"] and not r["muted"]],
                         "alerts": made})


# ---------------------------------------------------------------------------
# Turning Chat on and off
# ---------------------------------------------------------------------------


@team_only
@require_POST
def turn_on(request):
    reason = connect.unavailable_reason(request.user)
    if reason:
        messages.error(request, reason)
        return redirect("crm:chat")
    request.session[connect.SESSION_KEY] = True
    return redirect(connect.connect_url(request))


@team_only
def connected(request):
    """Where Google sends the person back after Turn on Chat."""
    if not request.session.pop(connect.SESSION_KEY, False):
        return redirect("crm:chat")
    try:
        connect.finish(request.user)
    except (connect.ChatConnectError, requests.RequestException) as exc:
        text = str(exc) if isinstance(exc, connect.ChatConnectError) else "Couldn't reach Google. Try again."
        ChatConnection.objects.update_or_create(user=request.user, defaults={"last_error": text})
        messages.error(request, text)
        return redirect("crm:chat")
    messages.success(request, "Chat is on. Your conversations are here, and new messages alert you.")
    return redirect("crm:chat")


@team_only
@require_POST
def turn_off(request):
    connect.turn_off(request.user)
    messages.success(request, "Chat is off. The hub no longer reads your Google Chat. Google still lists the hub's Chat "
                              "permission; you can remove it at myaccount.google.com/permissions.")
    return redirect("crm:chat")
