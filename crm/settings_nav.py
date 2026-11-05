"""The sections of Settings, in one list.

The tab strip on every Settings page (crm/templates/crm/settings_base.html), the Settings landing page, the top bar's lit
tab and the Ctrl K palette all read this list, so they agree on what Settings holds and who may open each part.

It also holds the way back from a Google button. Mail, Chat and Scheduling each send the person to Google and back, and
each used to land on its own page. Pressed on a Settings page, they come back to that page: the form carries a hidden
`next`, remembered across Google's round trip in the session.
"""
from dataclasses import dataclass

from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .permissions import is_manager

RETURN_KEY = "settings_return"
PREFIX = "/settings/"


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    url_name: str  # in the crm namespace
    blurb: str  # the line under its name on the Settings landing page
    managers: bool = False  # only Managers see it
    query: str = ""  # added to its link
    also: tuple = ()  # url names of its other pages, so its tab stays lit on them


SECTIONS = (
    Section("profile", "Profile", "settings_profile", "Who you are in the hub, light or dark, your dashboard, sign out."),
    Section("google", "Google access", "settings_google",
            "What the hub may use in your Google account: sign-in, Calendar, scheduling, Gmail, Chat, Drive."),
    Section("mail", "Mail", "mail_settings", "Your signature, CRM labels in Gmail and shared mailboxes."),
    Section("notifications", "Notifications", "notification_settings",
            "Which alerts reach you, in the hub and as browser notifications."),
    Section("dropdowns", "Dropdown lists", "dropdown_lists", "Rename, reorder and hide the options in every dropdown.",
            managers=True, also=("dropdown_edit",)),
    Section("outreach", "Outreach", "outreach_settings", "The campaign, the daily limit and who gets the weekly report.",
            managers=True),
    Section("contract_templates", "Contract templates", "contract_templates",
            "How Claude abstracts each kind of contract.", managers=True,
            also=("contract_template_new", "contract_template_edit")),
    Section("data_rooms", "Data rooms", "data_rooms", "Who sees which deals and files.", managers=True, query="?all=1"),
)

GROUPS = (("Your settings", False), ("For managers", True))


def groups(user, current=""):
    """[(heading, [section, ...])] for the sections this person may open, theirs first. Each section is a dict with
    key, label, url, blurb and active (`current` is the url name of the page being drawn)."""
    manager = is_manager(user)
    out = []
    for heading, managers in GROUPS:
        if managers and not manager:
            continue
        rows = [{"key": s.key, "label": s.label, "url": reverse(f"crm:{s.url_name}") + s.query, "blurb": s.blurb,
                 "active": current == s.url_name or current in s.also}
                for s in SECTIONS if s.managers == managers]
        out.append((heading, rows))
    return out


def clean_next(request, value):
    """A Settings page path from a form's hidden `next`, or "". Never another site and never another part of the hub."""
    value = (value or "").strip()
    if value.startswith(PREFIX) and url_has_allowed_host_and_scheme(
            value, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return value
    return ""


def remember(request):
    """Call just before sending the person to Google: a Settings page that started it gets them back afterwards."""
    target = clean_next(request, request.POST.get("next"))
    if target:
        request.session[RETURN_KEY] = target
    else:
        request.session.pop(RETURN_KEY, None)


def back(request, default):
    """Where to go once a Google round trip or an on / off switch is done: the Settings page whose button was pressed
    (its hidden `next`, or the one remembered across Google), else `default`, a url name."""
    remembered = request.session.pop(RETURN_KEY, "")
    return redirect(clean_next(request, request.POST.get("next")) or clean_next(request, remembered) or default)
