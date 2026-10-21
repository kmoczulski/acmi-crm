"""The company page's meeting cards, action items and activity filters (view: crm.views.company_detail).

Meetings lead the page: each card shows the Granola summary in place, with its "Next Steps" listed separately as
action items (owner, done state, Make task). The open action items across every meeting sit with the company's tasks.
"""
from django.core.paginator import Paginator
from django.db.models import Count
from django.utils import timezone

from . import meetings
from .models import Activity, Task

MEETINGS_PAGE = 10

# Activity tab filter: ?kind=<key> -> Activity kinds. "All" is no filter.
ACTIVITY_FILTERS = {
    "meetings": ("Meetings", [Activity.Kind.MEETING, Activity.Kind.GRANOLA_NOTE, Activity.Kind.CALL]),
    "email": ("Email", [Activity.Kind.EMAIL_SENT, Activity.Kind.EMAIL_RECEIVED, Activity.Kind.REPLY,
                        Activity.Kind.BOUNCE, Activity.Kind.AUTO_REPLY]),
    "notes": ("Notes and tasks", [Activity.Kind.NOTE, Activity.Kind.TASK]),
    "other": ("Changes and news", [Activity.Kind.STAGE_CHANGE, Activity.Kind.FIELD_CHANGE, Activity.Kind.NEWS_TRIGGER,
                                   Activity.Kind.RECORD_CREATED]),
}


def filter_activity(activities, key):
    """(filtered queryset, active key or "") for the Activity tab's chips."""
    if key in ACTIVITY_FILTERS:
        return activities.filter(kind__in=ACTIVITY_FILTERS[key][1]), key
    return activities, ""


def activity_chips(activities):
    """[(key, label, count)] for the Activity tab's chips, "" for All. Background events are left out, as the
    timeline leaves them out by default."""
    by_kind = dict(activities.filter(is_background=False).order_by().values_list("kind").annotate(n=Count("id")))
    chips = [("", "All", sum(by_kind.values()))]
    for key, (label, members) in ACTIVITY_FILTERS.items():
        chips.append((key, label, sum(by_kind.get(k, 0) for k in members)))
    return chips


def _cards(meeting_list, tasks):
    now = timezone.now()
    cards = []
    for m in meeting_list:
        items = [_item(m, n, i, tasks) for n, i in enumerate(m.action_items or [])]
        cards.append({
            "m": m,
            "upcoming": m.start > now,
            "summary_html": meetings.render_markdown(meetings.summary_without_actions(m.summary)),
            "items": items,
            "open_items": sum(1 for i in items if not i["done"]),
            "outside": [a.get("name") or a.get("email") for a in m.outside_attendees],
        })
    return cards


def _item(m, index, item, tasks):
    task = tasks.get(item.get("task_id"))
    return {**item, "index": index, "meeting": m, "task": task, "done": bool(task and task.done)}


def _tasks_for(meeting_list):
    ids = [i["task_id"] for m in meeting_list for i in m.action_items or [] if i.get("task_id")]
    return Task.objects.select_related("assignee").in_bulk(ids)


def meeting_context(company, request, tab):
    """Everything the overview, Meetings and Action items tabs need about the company's meetings."""
    all_meetings = list(company.meetings.order_by("-start"))
    tasks = _tasks_for(all_meetings)
    now = timezone.now()
    past = [m for m in all_meetings if m.start <= now]
    upcoming = sorted((m for m in all_meetings if m.start > now), key=lambda m: m.start)

    open_items, done_items = [], []
    for m in past + upcoming:
        for n, i in enumerate(m.action_items or []):
            item = _item(m, n, i, tasks)
            (done_items if item["done"] else open_items).append(item)

    ctx = {
        "meeting_count": len(all_meetings),
        "next_meeting": upcoming[0] if upcoming else None,
        "latest_meetings": _cards(([upcoming[0]] if upcoming else []) + past[:3], tasks),
        "open_items": open_items,
        "done_items": done_items,
        "meeting_task_ids": set(tasks),
    }
    if tab == "meetings":
        page = Paginator(upcoming[::-1] + past, MEETINGS_PAGE).get_page(request.GET.get("mpage"))
        ctx["meeting_page"] = page
        ctx["meeting_cards"] = _cards(page.object_list, tasks)
    return ctx
