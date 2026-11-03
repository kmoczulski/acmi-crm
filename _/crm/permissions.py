"""Who may do the actions that reach outside the CRM or can't be undone.

Everyone signed in works records and land. Members of the "Managers" group (and superusers) can also send outreach
batches, change the outreach settings and templates, delete or merge records, delete land sites and switch client
share links on. Migration 0032 put every account that existed then in Managers, so nobody lost anything; people who
sign in with Google for the first time start without it until an admin adds them (Admin › Groups).
"""
from functools import wraps

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

MANAGERS = "Managers"
# Capital partners' accounts (crm/dataroom/): they sign in to their data room and nowhere else
# (crm.middleware.PartnerOnlyMiddleware), and are never part of the team (team_users).
PARTNERS = "Partners"
REFUSED = "Only managers can do that. Ask an admin to add you to the Managers group."


def is_partner(user):
    if not (user and user.is_authenticated):
        return False
    cached = getattr(user, "_acmi_is_partner", None)
    if cached is None:
        cached = user._acmi_is_partner = user.groups.filter(name=PARTNERS).exists()
    return cached


def team_users():
    """Everyone on the ACMI team: active accounts, never a capital partner's."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(is_active=True).exclude(groups__name=PARTNERS)


def is_manager(user):
    if not (user and user.is_authenticated):
        return False
    if user.is_superuser:
        return True
    cached = getattr(user, "_acmi_is_manager", None)
    if cached is None:
        cached = user._acmi_is_manager = user.groups.filter(name=MANAGERS).exists()
    return cached


def refuse(request):
    """403 for scripts, otherwise back to the page the request came from with a message."""
    if request.path.endswith(".json") or request.content_type == "application/json" \
            or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"error": REFUSED}, status=403)
    messages.error(request, REFUSED)
    back = request.META.get("HTTP_REFERER", "")
    ok = back and url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()},
                                                  require_https=request.is_secure())
    return redirect(back if ok else "/")


def manager_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_manager(request.user):
            return refuse(request)
        return view(request, *args, **kwargs)

    return wrapped
