import re

from django import template
from django.contrib.humanize.templatetags.humanize import naturaltime
from django.utils import timezone
from django.utils.html import format_html, format_html_join

register = template.Library()

PRIORITY_CLASS = {"High": "pill-high", "Mid": "pill-mid", "Low": "pill-low"}
RESPONSE_CLASS = {
    "Responded": "pill-good", "Meeting Scheduled": "pill-good", "Referred": "pill-good",
    "Not Interested": "pill-bad", "Undeliverable": "pill-bad",
}


@register.simple_tag(takes_context=True)
def qs_set(context, **kwargs):
    """Current query string with some keys replaced."""
    params = context["request"].GET.copy()
    for key, value in kwargs.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    if "page" not in kwargs:
        params.pop("page", None)
    return "?" + params.urlencode()


@register.simple_tag(takes_context=True)
def sort_link(context, column):
    current = context["table"].sort
    if column.sort == current:
        new = "-" + column.sort
        arrow = " ▲"
    elif "-" + column.sort == current:
        new = column.sort
        arrow = " ▼"
    else:
        new = "-" + column.sort if column.key in ("last_interaction", "touch_count", "last_response", "people_count") else column.sort
        arrow = ""
    return format_html('<a href="{}">{}{}</a>', qs_set(context, sort=new), column.label, arrow)


@register.filter
def pill(value, kind="priority"):
    if not value:
        return ""
    cls = (PRIORITY_CLASS if kind == "priority" else RESPONSE_CLASS).get(value, "")
    return format_html('<span class="pill {}">{}</span>', cls, value)


@register.filter
def ago(value):
    if not value:
        return format_html('<span class="muted">—</span>')
    days = (timezone.now() - value).days
    return format_html('<span title="{}">{}</span>', timezone.localtime(value).strftime("%Y-%m-%d"), naturaltime(value) if days < 1 else f"{days}d ago")


@register.filter
def username(user):
    if not user:
        return ""
    return user.get_full_name() or user.username


@register.simple_tag(takes_context=True)
def cell(context, record, column):
    key = column.key
    if key == "name":
        url = record.get_absolute_url() + "?" + context["from_param"]
        name = str(record)
        flag = format_html(' <span class="pill pill-bad">Excluded</span>') if record.is_excluded else ""
        return format_html('<a class="rowlink" href="{}">{}</a>{}', url, name, flag)
    if key == "status":
        return format_html('<span class="stage stage-{}">{}</span>', record.status, record.get_status_display())
    if key == "priority":
        return pill(record.priority)
    if key == "owner":
        return username(record.owner)
    if key == "last_interaction":
        return ago(record.last_interaction_at)
    if key == "last_response":
        return pill(record.last_response, "response")
    if key == "last_reply":
        return ago(record.last_reply_at) if record.last_reply_at else ""
    if key == "deal":
        from crm.models import STAGE_ORDER

        deals = sorted(record.opportunities.all(), key=lambda o: -STAGE_ORDER[o.stage])
        if not deals:
            return ""
        return format_html('<a href="{}"><span class="stage">{}</span></a>', deals[0].get_absolute_url(), deals[0].get_stage_display())
    if key == "next_task":
        due = getattr(record, "next_task_due", None)
        if not due:
            return ""
        cls = "overdue" if due < timezone.localdate() else ""
        return format_html('<span class="{}">{}</span>', cls, due.strftime("%b %d"))
    if key in ("flags", "domains"):
        return format_html_join(" ", '<span class="tag">{}</span>', ((v,) for v in getattr(record, key) or []))
    if key == "company":
        if not record.company_id:
            return ""
        return format_html('<a href="{}">{}</a>', record.company.get_absolute_url(), record.company.name)
    if key == "projects":
        from crm.projects import company_projects

        return format_html_join("", '<span class="chip chip-project">{}</span>', ((p.name,) for p, _ in company_projects(record)))
    if key == "connection":
        if not record.strongest_connection_user_id:
            return ""
        return format_html('{} <span class="muted">{}</span>', username(record.strongest_connection_user),
                           record.connection_strength)
    if key == "fit_score":
        return f"{record.fit_score}/5" if record.fit_score else ""
    if key == "campus":
        return record.campus_label
    if key == "email":
        return record.email or ""
    value = getattr(record, key, "")
    return "" if value is None else value


@register.simple_tag
def conversation(activity):
    """'Kellen → Ben Allen' for sent mail, 'Ben Allen → Kellen, Simon' for inbound; else who/what did it."""
    team = ", ".join(username(u) for u in activity.team_members.all()) or username(activity.actor)
    them = ""
    if activity.person_id:
        them = format_html('<a href="{}">{}</a>', activity.person.get_absolute_url(), activity.person)
    if activity.kind == "email_sent":
        return format_html("{} → {}", team or "ACMI", them or "—")
    if activity.kind in ("email_received", "reply", "auto_reply", "bounce"):
        return format_html("{} → {}", them or "—", team or "ACMI")
    if activity.actor_id:
        return username(activity.actor)
    return "workbook" if activity.source == "import" else ("system" if activity.source == "system" else them)


@register.simple_tag
def context_chip(activity):
    """'MEIH · 4390 Indian Head Hwy' for project mail, 'Prospecting' for outreach, nothing otherwise."""
    if activity.context == "project":
        names = sorted((p for p in activity.projects.all()), key=lambda p: (p.parent_id is not None, p.name))
        if not names:
            return ""
        return format_html_join(
            "", '<a class="chip chip-project" href="{}">{}</a>', ((p.get_absolute_url(), p.name) for p in names)
        )
    if activity.context == "prospecting":
        return format_html('<span class="chip chip-prospecting">Prospecting</span>')
    return ""


KIND_ICONS = {
    "email_sent": "✉", "email_received": "📥", "reply": "↩", "bounce": "⚠", "auto_reply": "⏱",
    "meeting": "📅", "granola_note": "📝", "call": "☎", "note": "🗒", "task": "✓",
    "stage_change": "➜", "field_change": "✎", "news_trigger": "📰", "record_created": "＋",
}


@register.filter
def kind_icon(kind):
    return KIND_ICONS.get(kind, "•")


@register.filter
def money(value):
    """$1.9B / $725M / $825K / $950; blank for nothing."""
    if value in (None, ""):
        return ""
    v = float(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= size:
            n = v / size
            return f"${n:.1f}{suffix}".replace(".0" + suffix, suffix)
    return f"${v:,.0f}"


EMAIL_KINDS = {"email_sent", "email_received", "reply", "auto_reply", "bounce"}


@register.filter
def body_text(activity, limit=600):
    """What a timeline row shows: for email, only the new message (no quoted thread, no signature block)."""
    body = activity.body or ""
    if activity.kind in EMAIL_KINDS:
        from crm.mail.parse import new_text
        from crm.views import SIGNOFF

        kept = []
        for line in new_text(body).replace("\ufffd", "'").splitlines():
            if kept and SIGNOFF.match(line.strip()):
                kept.append(line)  # keep "Thanks," but drop the signature block under it
                break
            kept.append(line)
        body = "\n".join(kept).strip()
    return body[:limit].rsplit(" ", 1)[0] + "…" if len(body) > limit else body


@register.filter
def mentions(text):
    """Escape a comment and highlight @names; keeps line breaks."""
    from django.utils.html import escape, linebreaks
    from django.utils.safestring import mark_safe

    html = re.sub(r"(@[A-Za-z][\w.-]*)", r'<span class="mention">\1</span>', escape(text or ""))
    return mark_safe(linebreaks(html, autoescape=False))


@register.filter
def get_item(mapping, key):
    """Dict lookup with a variable key: {{ counts|get_item:value }}."""
    return (mapping or {}).get(key)


@register.filter
def split_commas(value):
    """'a,b,c' -> ['a', 'b', 'c'] (for looping over a fixed list of field names in a template)."""
    return [v.strip() for v in str(value).split(",") if v.strip()]


@register.filter
def as_json(value):
    """JSON for a data-* attribute (autoescaping turns the quotes into &quot;, which the browser undoes)."""
    import json

    return json.dumps(value or {})
