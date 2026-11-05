"""Settings: the landing page, Google connections, Appearance and Dropdown lists (rename, reorder, hide and, on open
lists, add the options of any dropdown). The other sections live with the feature they set up (notifications in
push_views, Mail in mail/views, Outreach in outreach/views, contract templates, data rooms) and draw themselves inside
the same shell, settings_base.html. crm/settings_nav.py lists them all."""
import re
from collections import Counter

from django.conf import settings
from django.contrib import messages
from django.db.models import Count
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

import requests

from . import choices, gcal, google_access, settings_nav
from .chat import connect as chat_connect
from .mail import connect as mail_connect
from .mail.views import team_only
from .models import ChoiceOption, PushSubscription
from .permissions import is_manager, manager_required


def _status(user):
    """A line on where each of the person's own sections stand. Cheap on purpose: nothing here calls Google."""
    devices = PushSubscription.objects.filter(user=user).count()
    mail_on = mail_connect.is_on(user)
    on = [name for name, flag in (("Calendar", gcal.connected(user)), ("Gmail", mail_on),
                                  ("Chat", chat_connect.is_on(user))) if flag]
    return {
        "notifications": f"Browser alerts on in {devices} browser{'s' if devices != 1 else ''}" if devices
        else "Browser alerts are off",
        "google": ("Connected: " + ", ".join(on)) if on else "Nothing connected yet",
        "mail": "" if mail_on else "Mail is off",
    }


@team_only
def settings_home(request):
    """The front door: every section this person may open, with a line on where their own stand."""
    status = _status(request.user)
    sections = [(heading, [{**s, "status": status.get(s["key"], "")} for s in rows])
                for heading, rows in settings_nav.groups(request.user)]
    return render(request, "crm/settings_home.html", {"sections": sections})


@team_only
def profile(request):
    """Who you are, how the hub looks for you, your dashboard and Sign out."""
    from .permissions import is_partner

    user = request.user
    role = "Partner" if is_partner(user) else ("Manager" if is_manager(user) else "Team member")
    return render(request, "crm/settings_profile.html", {"role": role})


@team_only
def google_connections(request):
    """Everything the hub may use in this person's Google account, one row each, with its own button. The buttons post
    to the views that already switch each piece; the hidden `next` (crm/settings_nav.py) brings the person back here."""
    return render(request, "crm/settings_google.html", {
        "rows": google_access.rows(request, request.path),
        "google_ready": settings.GOOGLE_SIGNIN_ENABLED,
        "connected": gcal.connected(request.user),
    })


@team_only
@require_POST
def google_drive_connect(request):
    if not google_access.drive_needed(request.user):
        messages.error(request, "Drive isn't needed for your account.")
        return redirect("crm:settings_google")
    if not settings.GOOGLE_SIGNIN_ENABLED or not gcal.connected(request.user):
        messages.error(request, "Connect your Calendar first.")
        return redirect("crm:settings_google")
    settings_nav.remember(request)
    request.session[google_access.DRIVE_SESSION_KEY] = True
    return redirect(google_access.drive_connect_url(request))


@team_only
def google_drive_done(request):
    """Where Google sends the person back after Connect Drive."""
    if not request.session.pop(google_access.DRIVE_SESSION_KEY, False):
        return settings_nav.back(request, "crm:settings_google")
    try:
        google_access.drive_finish(request.user)
    except (google_access.DriveConnectError, requests.RequestException) as exc:
        messages.error(request, str(exc) if isinstance(exc, google_access.DriveConnectError)
                       else "Couldn't reach Google. Try again.")
    else:
        messages.success(request, "Drive is connected. The hub can read your Granola folder.")
    return settings_nav.back(request, "crm:settings_google")


@team_only
@require_POST
def google_disconnect(request):
    """Take back the hub's whole Google permission (Google drops them together)."""
    google_access.disconnect_all(request.user)
    messages.success(request, "The hub's Google access is removed. Mail, Chat, scheduling and Calendar are off. "
                              "Connect any of them again below.")
    return redirect("crm:settings_google")


@manager_required
def dropdown_lists(request):
    return render(request, "crm/settings_dropdowns.html", {"lists": choices.lists()})


def _usage(key):
    """{value: how many records use it} across every field built from the list."""
    n = Counter()
    for f in choices._registry[key]["fields"]:
        for row in f.model.objects.values(f.name).annotate(c=Count("pk")):
            n[row[f.name]] += row["c"]
    return n


def _slug(label, taken, max_length):
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:max_length] or "option"
    value, i = base, 2
    while value in taken:
        tail = f"_{i}"
        value, i = base[:max_length - len(tail)] + tail, i + 1
    return value


@manager_required
def dropdown_edit(request, key):
    meta = choices.info(key)
    if meta is None:
        raise Http404("No such list")
    usage = _usage(key)
    if request.method == "POST":
        if request.POST.get("action") == "reset":
            removed = [o for o in ChoiceOption.objects.filter(list_key=key, added=True) if usage.get(o.value)]
            ChoiceOption.objects.filter(list_key=key).exclude(pk__in=[o.pk for o in removed]).delete()
            for o in removed:  # options the team added that records still use stay, at the end
                o.position = None
                o.save()
            messages.success(request, f"{meta['title']}: back to the built-in options."
                             + (" Added options still in use were kept." if removed else ""))
            return redirect("crm:dropdown_edit", key=key)
        current = choices.options(key)
        order = [v for v in request.POST.getlist("order") if v in {c[0] for c in current}]
        order += [c[0] for c in current if c[0] not in order]
        existing = {o.value: o for o in ChoiceOption.objects.filter(list_key=key)}
        builtin = [v for v in order if v in meta["defaults"]]
        reordered = builtin != list(meta["defaults"])[:len(builtin)] or any(
            v not in meta["defaults"] for v in order[:len(builtin)])
        errors = []
        for pos, value in enumerate(order):
            o = existing.get(value) or ChoiceOption(list_key=key, value=value)
            added = value not in meta["defaults"]
            if added and request.POST.get(f"remove_{value}"):
                if usage.get(value):
                    errors.append(f"“{o.label or value}” is used on {usage[value]} record{'s' if usage[value] != 1 else ''}: hide it instead.")
                elif o.pk:
                    o.delete()
                continue
            label = (request.POST.get(f"label_{value}") or "").strip()[:200]
            o.label = "" if not added and label == meta["defaults"].get(value) else (label or o.label)
            o.hidden = bool(request.POST.get(f"hidden_{value}"))
            o.position, o.added, o.updated_by = pos, added, request.user
            if not added and not o.label and not o.hidden and pos == list(meta["defaults"]).index(value) and not reordered:
                if o.pk:  # back to exactly the built-in option: nothing to store
                    o.delete()
                continue
            o.save()
        new = (request.POST.get("new_label") or "").strip()[:200]
        if new:
            like = request.POST.get("new_like", "")
            if not meta["open"] and like not in meta["defaults"]:
                errors.append(f"Choose which built-in option “{new}” works like.")
            elif new.lower() in {l.lower() for _, l, _, _ in choices.options(key)}:
                errors.append(f"“{new}” is already on the list.")
            else:
                taken = {v for v, _, _, _ in choices.options(key)}
                pos = len(order)
                if like in order:  # sits just after the option it works like; the ones after it move down one
                    pos = order.index(like) + 1
                    for i, value in enumerate(order[pos:], start=pos + 1):
                        o, _ = ChoiceOption.objects.get_or_create(list_key=key, value=value,
                                                                  defaults={"added": value not in meta["defaults"]})
                        o.position = i
                        o.save(update_fields=["position"])
                ChoiceOption.objects.create(list_key=key, value=_slug(new, taken, min(meta["max_length"], 40)), label=new,
                                            position=pos, added=True, behaves_as="" if meta["open"] else like,
                                            updated_by=request.user)
        for e in errors:
            messages.error(request, e)
        if not errors:
            messages.success(request, f"{meta['title']} saved.")
        return redirect("crm:dropdown_edit", key=key)
    likes = {v: o.behaves_as for v, o in choices._edits().get(key, {}).items() if o.behaves_as}
    rows = [{"value": v, "label": l, "hidden": h, "added": a, "default": meta["defaults"].get(v, ""),
             "used": usage.get(v, 0), "like": dict(choices._registry[key]["defaults"]).get(likes.get(v, ""), "")}
            for v, l, h, a in choices.options(key)]
    return render(request, "crm/settings_dropdown.html", {"meta": meta, "rows": rows, "builtins": choices._registry[key]["defaults"],
                                                          "edited": ChoiceOption.objects.filter(list_key=key).exists()})
