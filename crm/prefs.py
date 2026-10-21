"""Per-person layout choices, saved on the server so they follow you to any computer.

UserPrefs.data holds:
  "dashboard": [{"id": widget id, "w": columns of 12, "h": height in px or 0 for auto}, ...] in display order;
               widgets missing from the list are hidden.
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
WIDTHS = (3, 4, 6, 8, 12)
# Widgets that draw their own title row (with week or month arrows); their frame title shows only while customizing.
HEADLESS = {"week", "critical"}


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


def clean_dashboard(value):
    """Keep known widgets once each, with a width from WIDTHS and a sane height."""
    out, seen = [], set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or item.get("id") not in WIDGETS or item["id"] in seen:
            continue
        seen.add(item["id"])
        w = item.get("w") if item.get("w") in WIDTHS else WIDGETS[item["id"]][1]
        try:
            h = max(0, min(int(item.get("h") or 0), 2000))
        except (TypeError, ValueError):
            h = 0
        out.append({"id": item["id"], "w": w, "h": h})
    return out


def dashboard_layout(user):
    saved = get_prefs(user).get("dashboard")
    layout = clean_dashboard(saved) if saved is not None else DEFAULT_DASHBOARD
    for item in layout:
        item["title"] = WIDGETS[item["id"]][0]
    return layout


@require_POST
def prefs_save(request):
    """POST key + JSON value. Keys: dashboard, cols:<page>, density:<page>."""
    key = request.POST.get("key", "")
    try:
        value = json.loads(request.POST.get("value", "null"))
    except ValueError:
        return HttpResponseBadRequest("Bad value")
    if key == "dashboard":
        value = None if value is None else clean_dashboard(value)
    elif key.startswith("cols:") and len(key) < 60:
        value = [str(v)[:60] for v in value][:40] if isinstance(value, list) else None
    elif key.startswith("density:") and len(key) < 60:
        value = "compact" if value == "compact" else None
    else:
        return HttpResponseBadRequest("Unknown setting")
    set_pref(request.user, key, value)
    return JsonResponse({"ok": True})
