"""Per-person layout choices, saved on the server so they follow you to any computer.

UserPrefs.data holds:
  "dashboard": [{"id": widget id, "w": columns of 12, "h": height in px or 0 for auto}, ...] in display order;
               widgets missing from the list are hidden.
  "company": the same for the company page's overview (one layout, every company).
  "cols:<page>": [column labels hidden on that page's tables]
  "density:<page>": "compact" or ""
The view bar's saved views stay in SavedView, keyed by page (VIEW_PAGES).
"""
import json

from django.http import HttpResponseBadRequest, JsonResponse
from django.views.decorators.http import require_POST

from .models import UserPrefs

# Pages with the shared view bar: key -> (label, url name). Companies and People keep their own table views.
VIEW_PAGES = {
    "pipeline": ("Tenant pipeline", "crm:opportunity_board"),
    "tasks": ("Tasks", "crm:task_list"),
    "meetings": ("Calendar", "crm:meeting_list"),
    "sites": ("Sites", "crm:land_sites_page"),
    "contracts": ("Contracts", "crm:contract_list"),
    "rent_roll": ("Rent roll", "crm:rent_roll"),
    "directory": ("Directory", "crm:directory"),
    "assignments": ("Client searches", "crm:assignments"),
    "entitlements": ("Entitlements", "crm:approval_list"),
    "capital": ("Capital", "crm:capital_index"),
    "projects": ("Projects", "crm:project_list"),
    "report": ("Tenant pipeline report", "crm:pipeline_report"),
}

# Dashboard widgets: id -> (title, default width of 12, default height px (0 = fit content), description).
WIDGETS = {
    "kpis": ("Pipeline numbers", 12, 0, "Open, weighted and signed pipeline, deals needing attention, and each campus."),
    "week": ("This week", 12, 0, "Your meetings Monday to Friday, or everyone's."),
    "critical": ("Critical dates", 12, 0, "A month of contract, closing and entitlement dates."),
    "my_day": ("My day", 6, 0, "Replies waiting on us, your tasks, and your deals without a next step."),
    "deadlines": ("Deadlines", 6, 0, "Contract dates in the next 14 days."),
    "replies": ("Recent positive responses", 6, 0, "People who answered outreach with interest."),
    "stages": ("Pipeline by stage", 6, 0, "Deals, value and weighted value per stage."),
    "attention": ("Deals that need attention", 6, 0, "No next step, overdue or gone quiet."),
    "suggestions": ("Possible deals found in email", 6, 0, "LOI, PDA, NDA or lease talk with no deal in the CRM yet."),
    "changes": ("This week in the pipeline", 6, 0, "Stage moves and new deals in the last 7 days."),
    "activity": ("Recent activity", 6, 0, "Emails, meetings and notes from the last week."),
    "my_sites": ("My sites", 6, 0, "Land sites you own or work on."),
    "funnel": ("Outreach funnel", 6, 0, "Companies by outreach stage."),
}
DEFAULT_DASHBOARD = [
    {"id": i, "w": WIDGETS[i][1], "h": WIDGETS[i][2]}
    for i in ("kpis", "week", "my_day", "attention", "stages", "deadlines", "critical", "replies", "suggestions",
              "changes", "activity", "funnel")
]
WIDTHS = (3, 4, 6, 8, 12)  # the quick width buttons; dragging the corner gives any width from 2 to 12 columns
# Widgets that draw their own title row (with week or month arrows); their frame title shows only while customizing.
HEADLESS = {"week", "critical"}

# The company page's overview, laid out the same way (Kellen, 2026-10-26: "the company card should be customizable
# like the dashboard"). One layout per person, for every company.
COMPANY_WIDGETS = {
    "facts": ("At a glance", 12, 0, "Stage, owner, last touch, next meeting, fit and campus interest."),
    "overview": ("Overview", 8, 0, "What the company is, in its own words: the description, with sector, HQ, website "
                                   "and LinkedIn, and its categories and flags."),
    "feed": ("Activity", 8, 0, "Comments, notes, meeting notes and email in one list, newest first."),
    "details": ("Details", 4, 0, "The record itself: sector, website, domains, flags. Edit it here."),
    "meetings": ("Meetings", 8, 0, "The next meeting and the last three, with their notes and action items."),
    "actions": ("Action items and tasks", 4, 0, "Open items from meeting notes, and the company's tasks."),
    "people": ("Who we know", 4, 0, "People at the company, most recently in touch first."),
    "deals": ("Open deals", 4, 0, "Tenant deals in progress, with their next step."),
    "summary": ("Claude's summary", 4, 0, "The written summary of where things stand."),
    "files": ("Files", 4, 0, "The Google Drive folder and documents."),
    "team": ("Team connections", 4, 0, "Who on our side knows them, and how well."),
    "lists": ("Boards and lists", 4, 0, "Board cards and lists this company is on."),
}
DEFAULT_COMPANY = [{"id": i, "w": COMPANY_WIDGETS[i][1], "h": 0} for i in COMPANY_WIDGETS]

# Every page with a widget grid: its widgets, default layout, headless widgets and where the widget templates live.
LAYOUTS = {
    "dashboard": {"name": "dashboard", "widgets": WIDGETS, "default": DEFAULT_DASHBOARD, "headless": HEADLESS,
                  "dir": "crm/widgets/"},
    "company": {"name": "page", "widgets": COMPANY_WIDGETS, "default": DEFAULT_COMPANY, "headless": {"facts"},
                "dir": "crm/widgets/company/"},
}


def get_prefs(user):
    if not getattr(user, "is_authenticated", False):
        return {}
    row = UserPrefs.objects.filter(user=user).only("data").first()
    return row.data if row else {}


def set_pref(user, key, value):
    row, _ = UserPrefs.objects.get_or_create(user=user)
    if value is None:
        row.data.pop(key, None)
    else:
        row.data[key] = value
    row.save(update_fields=["data", "updated_at"])


def _int(value, low, high, default):
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def clean_layout(value, page="dashboard"):
    """Keep known widgets once each: width 2-12 columns, x so it fits, y in grid rows (dashboard.js packs them),
    height in px (0 = fit the content). Old layouts without x / y are placed in order by dashboard.js."""
    widgets = LAYOUTS[page]["widgets"]
    out, seen = [], set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or item.get("id") not in widgets or item["id"] in seen:
            continue
        seen.add(item["id"])
        w = _int(item.get("w"), 2, 12, widgets[item["id"]][1])
        clean = {"id": item["id"], "w": w, "h": _int(item.get("h") or 0, 0, 2000, 0)}
        if item.get("x") is not None and item.get("y") is not None:
            clean["x"] = _int(item.get("x"), 0, 12 - w, 0)
            clean["y"] = _int(item.get("y"), 0, 5000, 0)
        out.append(clean)
    return out


def clean_dashboard(value):
    return clean_layout(value, "dashboard")


def layout_for(user, page):
    """The person's layout for a page: their saved one, or the default."""
    spec = LAYOUTS[page]
    saved = get_prefs(user).get(page)
    layout = clean_layout(saved, page) if saved is not None else [dict(w) for w in spec["default"]]
    for item in layout:
        item["title"] = spec["widgets"][item["id"]][0]
    return layout


def dashboard_layout(user):
    return layout_for(user, "dashboard")


def layout_context(user, page, customize=False):
    """What crm/partials/widget_grid.html needs to draw a page's grid: the layout, the gallery of widgets that can be
    added, and the frame's labels."""
    spec = LAYOUTS[page]
    layout = layout_for(user, page)
    shown = {w["id"] for w in layout}
    return {
        "page": page, "name": spec["name"], "layout": layout, "shown": shown, "customize": customize,
        "headless": spec["headless"], "widths": WIDTHS, "dir": spec["dir"],
        "gallery": [{"id": k, "title": v[0], "description": v[3], "shown": k in shown} for k, v in spec["widgets"].items()],
    }


@require_POST
def prefs_save(request):
    """POST key + JSON value. Keys: a layout page (dashboard, company), cols:<page>, density:<page>."""
    key = request.POST.get("key", "")
    try:
        value = json.loads(request.POST.get("value", "null"))
    except ValueError:
        return HttpResponseBadRequest("Bad value")
    if key in LAYOUTS:
        value = None if value is None else clean_layout(value, key)
    elif key.startswith("cols:") and len(key) < 60:
        value = [str(v)[:60] for v in value][:40] if isinstance(value, list) else None
    elif key.startswith("density:") and len(key) < 60:
        value = "compact" if value == "compact" else None
    else:
        return HttpResponseBadRequest("Unknown setting")
    set_pref(request.user, key, value)
    return JsonResponse({"ok": True})
