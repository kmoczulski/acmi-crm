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
def settings_sections(context):
    """The Settings sections this person may open, as [(heading, [section, ...])], with the page being drawn marked
    active (crm/settings_nav.py); settings_base.html draws the tab strip from it."""
    from crm import settings_nav

    request = context["request"]
    match = getattr(request, "resolver_match", None)
    return settings_nav.groups(request.user, match.url_name if match else "")


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
def mail_when(value):
    """When an email came, date and time ("Oct 30, 9:12 a.m."), with the year once it's from another year."""
    if not value:
        return ""
    from django.utils.dateformat import format as date_format

    local = timezone.localtime(value)
    return date_format(local, "M j, g:i a" if local.year == timezone.localdate().year else "M j, Y, g:i a")


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
        if record.email and record.email_valid is False:
            return format_html('{} <span class="pill pill-bad">Bounced</span>', record.email)
        return record.email or ""
    if key == "blasts":
        if not record.email:
            return format_html('<span class="muted">No email</span>')
        reasons = []
        if record.is_excluded:
            reasons.append("Excluded")
        elif record.company_id and record.company.is_excluded:
            reasons.append("Company excluded")
        if record.email_valid is False:
            reasons.append("Email bad")
        if not reasons:
            return format_html('<span class="muted">In blasts</span>')
        return format_html_join(" ", '<span class="pill pill-bad">{}</span>', ((r,) for r in reasons))
    value = getattr(record, key, "")
    return "" if value is None else value


@register.simple_tag
def conversation(activity):
    """'Kellen → Ben Allen' for sent mail, 'Ben Allen → Kellen, Simon' for inbound; else who/what did it."""
    # Sorted by name: a many-to-many has no order of its own (Postgres returns it in any order).
    team = ", ".join(sorted((username(u) for u in activity.team_members.all()), key=str.lower)) \
        or username(activity.actor)
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


@register.filter
def dollars(value):
    """$1,234,567 (whole dollars, for budget tables where the columns must add up); blank for nothing."""
    if value in (None, ""):
        return ""
    v = round(float(value))
    return f"-${-v:,}" if v < 0 else f"${v:,}"


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


EXTENSION = re.compile(r"\s*(?:x|ext\.?|extension)\s*\d+\s*$", re.I)


@register.filter
def phone_numbers(phones):
    """A person's saved numbers as rows for a card or table: {text, label, href}. "301-555-0100 x204 (office)" shows as
    "301-555-0100 x204" with "office" beside it, and taps through to tel:3015550100 (a dialler can't take the label or
    the extension). A saved value with no digits in it has no link."""
    from crm.forms import PHONE_LABEL

    rows = []
    for saved in phones or []:
        saved = str(saved).strip()
        m = PHONE_LABEL.search(saved)
        text, label = (saved[:m.start()].strip(), m.group(1)) if m else (saved, "")
        if not text:
            continue
        number = EXTENSION.sub("", text)
        dial = ("+" if number.lstrip().startswith("+") else "") + re.sub(r"\D", "", number)
        rows.append({"text": text, "label": label, "href": f"tel:{dial}" if dial.strip("+") else ""})
    return rows


@register.filter
def prose(text):
    """Plain text with light markdown (blank-line paragraphs, # headings, - bullets, **bold**) as safe HTML: the
    company description and other written fields."""
    from crm.meetings import render_markdown

    return render_markdown(text or "")


@register.filter
def clean_html(html):
    """Mail HTML made safe to show: rebuilt with formatting tags and safe links only (drafts saved before the
    parser-based cleaner existed are cleaned again here)."""
    from django.utils.safestring import mark_safe

    from crm.news.drafts import sanitize_html

    return mark_safe(sanitize_html(html or ""))


@register.filter
def stale_checks(checks):
    """Names of live checks whose last refresh failed (their earlier result is still shown)."""
    return [name.replace("_", " ") for name, v in (checks or {}).items() if isinstance(v, dict) and v.get("refresh_error")]


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


@register.filter
def offer(option_list, current=None):
    """{% for v, l in statuses|offer:site.status %}: a dropdown's options without the ones hidden in Settings."""
    from crm.choices import visible

    return visible(option_list, current)


@register.inclusion_tag("crm/partials/view_bar.html", takes_context=True)
def view_bar(context, page):
    """The bar above a list or board: saved views (yours and shared ones), set one as the page's default,
    show or hide table columns and switch to compact rows. Column and density choices are per person (crm/prefs.py)."""
    from django.db.models import Q

    from crm.models import SavedView

    request = context["request"]
    params = request.GET.copy()
    for key in ("page", "_v"):
        params.pop(key, None)
    query = params.urlencode()
    views = list(SavedView.objects.filter(object_type=page).filter(Q(owner=request.user) | Q(shared=True)).order_by("name"))
    mine_default = next((v for v in views if v.is_default and v.owner_id == request.user.pk), None)
    user_prefs = context.get("PREFS") or {}
    return {
        "page": page,
        "views": views,
        "current": next((v for v in views if v.query == query), None),
        "query": query,
        "path": request.path,
        "default_query": mine_default.query if mine_default else "",
        "hidden_cols": user_prefs.get(f"cols:{page}", []),
        "compact": user_prefs.get(f"density:{page}") == "compact",
        "user": request.user,
        "csrf_token": context.get("csrf_token"),
    }


@register.filter
def due_class(day, done=False):
    """CSS class for a due date: due-late (red) when past, due-today (amber) when today."""
    if not day or done:
        return ""
    today = timezone.localdate()
    return "due-late" if day < today else "due-today" if day == today else ""


REPORT_NUMERIC = {"acres", "size", "cost", "psf", "rent", "roc", "win", "weighted"}


@register.filter
def report_align(key):
    """Table cell class for a tenant pipeline report column (crm/reports.py)."""
    return "num" if key in REPORT_NUMERIC else "wrap" if key == "notes" else ""


@register.filter
def roc_pct(value):
    """A return on cost (0.085) as "8.5%"."""
    return f"{value:.1%}" if value else ""


@register.simple_tag
def report_cell(o, key):
    """One deal's cell in the tenant pipeline report, formatted for the page."""
    from crm.reports import cell_value

    if key == "stage":
        return format_html('<span class="stage stage-{}">{}</span>', o.stage, o.get_stage_display())
    if key == "tenant":
        # The name in this column is the company. Open that page, not the deal (which sits a click further in).
        name = format_html('<a href="{}"><b>{}</b></a>', o.company.get_absolute_url(), o.company.name)
        if o.name and o.name != o.company.name:
            return format_html('{}<div class="muted lc-small"><a href="{}">{}</a></div>',
                               name, o.get_absolute_url(), o.name)
        return name
    if key == "lease_doc":
        if o.lease_doc_url:
            return format_html('<a href="{}" target="_blank" rel="noopener" title="{}">{}</a>', o.lease_doc_url,
                               o.lease_doc_name or o.lease_doc_label, o.lease_doc_label)
        return o.lease_doc_label
    value = cell_value(o, key)
    if value in (None, ""):
        return "TBD" if key == "cost" else ""
    if key == "roc":
        return f"{value:.1%}"
    if key in ("cost", "psf", "rent", "weighted"):
        return f"${value:,.0f}"
    if key == "win":
        return f"{value:.0%}"
    if key == "size":
        return f"{value:,}"
    if key == "acres":
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if key in ("target_close", "est_delivery"):
        return f"{value:%b} {value.year}"
    return value


@register.simple_tag
def greeting():
    """"Good morning" before noon, "Good afternoon" until 5 PM, "Good evening" after, in the team's time zone. The
    dashboard then redoes it in the browser from the viewer's own clock (home.html), so Eastern sees Eastern."""
    hour = timezone.localtime().hour
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"


@register.filter
def file_type_name(kind):
    """"word" -> "Word", for the reading pane's attachment filters (crm/mail/threads.py)."""
    from crm.mail.threads import TYPE_NAMES

    return TYPE_NAMES.get(kind, kind)


_SEARCH_OP = re.compile(r'(?:^|\s)-?\w+:(?:"[^"]*"|\([^)]*\)|\S+)')


@register.filter
def mark_terms(text, query):
    """Escape text and wrap the plain words of a mail search in <mark>, as Gmail does. Operators (from:, has:...),
    negated words and Gmail's {} / OR syntax are not highlighted."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe

    text = text or ""
    q = _SEARCH_OP.sub(" ", query or "")
    found = [quoted or bare for quoted, bare in re.findall(r'"([^"]+)"|(\S+)', q)]
    words = sorted({w.strip("(){}") for w in found if not w.startswith("-") and w.upper() != "OR"} - {""},
                   key=len, reverse=True)[:8]
    if not words:
        return text
    pattern = re.compile("(" + "|".join(re.escape(w) for w in words) + ")", re.I)
    return mark_safe("".join(f"<mark>{escape(part)}</mark>" if i % 2 else escape(part)
                             for i, part in enumerate(pattern.split(text))))
