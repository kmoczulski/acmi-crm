"""Chat pages (docs/CHAT.md): the full page, the dock's list and conversation boxes, and the small JSON calls behind them.

Every call goes through the signed-in person's own Chat permission, so a conversation that isn't theirs is simply not
found at Google; nothing here takes anyone else's id.
"""
import logging
import re
from functools import wraps

import requests
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from crm.mail.views import team_only
from crm.models import ChatConnection, Person

from . import connect, service
from .api import ChatError, ChatNotFound, space_name
from .render import decorate, parse_time

log = logging.getLogger(__name__)
PAGE = 40  # messages a conversation opens with, and each "Earlier messages"
THREAD = re.compile(r"^spaces/[\w-]+/threads/[\w.-]+$")
OFF = "Chat is off. Turn it on on the Chat page."
NOT_THERE = "That conversation isn't in your Chat."


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
        except ChatError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        except requests.RequestException:
            log.warning("Chat call failed for %s", request.user, exc_info=True)
            return JsonResponse({"error": "Couldn't reach Google Chat. Try again."}, status=502)

    return wrapped


def _window_id(request):
    return re.sub(r"[^A-Za-z0-9_-]", "", request.GET.get("w", ""))[:24]


def _records(directory, me):
    """People in a conversation who are in the CRM, with their companies: small links in its header."""
    emails = [p["email"] for p in directory.values() if p["email"] and p["email"] != me][:25]
    if not emails:
        return []
    out, seen = [], set()
    for person in Person.objects.filter(email__in=emails).select_related("company")[:8]:
        out.append({"label": str(person), "url": person.get_absolute_url()})
        if person.company_id and person.company_id not in seen:
            seen.add(person.company_id)
            out.append({"label": person.company.name, "url": person.company.get_absolute_url()})
    return out


def _conversation(user, client, space):
    """Everything the conversation partial shows for one space; opening it marks it read, as in Chat itself."""
    row = service.find(user, client, space)
    views, raw, directory = service.load(user, client, space, limit=PAGE)
    if row["unread"]:
        service.mark_read(user, client, space)
    me = service.me_of(user)
    others = [p["name"] for p in directory.values() if not (p["email"] and p["email"] == me)]
    people = "" if row["type"] == "DIRECT_MESSAGE" else \
        ", ".join(others[:6]) + (f" and {len(others) - 6} more" if len(others) > 6 else "")
    return {"space": row, "messages": views, "latest": raw[-1].get("createTime", "") if raw else "",
            "earliest": raw[0].get("createTime", "") if raw else "", "more": len(raw) >= PAGE, "people": people,
            "records": _records(directory, me), "me": me}


def _previous(params):
    """The message just above new ones on the page, so a refresh carries the grouping on."""
    uid = params.get("prev_uid", "")
    if not uid:
        return None
    return {"uid": uid, "time": parse_time(params.get("prev_time", "")), "reply": params.get("prev_reply") == "1"}


def _page_context(request):
    return {"on": connect.is_on(request.user), "unavailable": connect.unavailable_reason(request.user),
            "conn": connect.connection(request.user)}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@team_only
def home(request):
    """The Chat page: your conversations on the left, the open one on the right (?s=<space>). It is also what the split
    view's side shows, and a click on a conversation swaps the right side in place (X-Chat-Pane)."""
    ctx = {**_page_context(request), "rows": [], "convo": None, "current": ""}
    if ctx["on"]:
        try:
            client = service.client_for(request.user)
            ctx["rows"] = service.overview(request.user, client)["rows"]
            space = space_name(request.GET.get("s", ""))
            if space:
                ctx["convo"] = _conversation(request.user, client, space)
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
            ctx["convo"] = _conversation(request.user, service.client_for(request.user), space)
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
def send(request, client):
    space = space_name(request.POST.get("s", ""))
    text = request.POST.get("text", "").strip()
    if not space:
        return JsonResponse({"error": NOT_THERE}, status=404)
    if not text:
        return JsonResponse({"error": "Write something first."}, status=400)
    if len(text) > 4000:
        return JsonResponse({"error": "That's too long for one message. Split it up."}, status=400)
    thread = request.POST.get("thread", "")
    view = service.post(request.user, client, space, text, thread if THREAD.match(thread) else "")
    decorate([view], _previous(request.POST))
    return JsonResponse({"ok": True, "html": render_to_string("crm/partials/chat_messages.html", {"messages": [view]}),
                         "latest": view["iso"]})


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
