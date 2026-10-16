"""The weekly campus update, drafted by Claude from the Summary tab (crm/campus.py), then edited and sent by the team.

Claude only rewrites what the tab already holds (deal stages, the week's email and meetings, land milestones, the
team's updates, the computed waiting / aging flags) into the usual bullet email, one line per item. Every item stays
in, in the tab's order, and nothing may be added that isn't in the facts. Needs ANTHROPIC_API_KEY (like the AI
summaries in crm/ai.py); the draft is saved as a CampusReport that people edit before a manager sends it.
"""
import json
import logging
from datetime import timedelta

from django.utils import timezone

from . import campus as campus_mod
from .ai import enabled
from .models import CampusReport, Meeting

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
NOTES_CHARS = 600  # Granola notes per meeting sent to the model

SYSTEM = """You draft the weekly campus update email for ACMI Properties, which develops purpose-built \
manufacturing campuses for defense, aerospace and energetics companies and finances and builds the facilities \
tenants occupy. The team sends this update to leadership every Monday.

You get one campus: its tenants (prospective tenant companies and their deal stage), its land (sites being \
assembled, owner by owner) and its projects (buildings and programs), each with the team's updates, facts from \
email and meetings, and flags the CRM computed from dates. Write one line per item in the team's usual style, \
"<Name> - <what happened or where it stands>", for example: "ERG - sent them an LOI capturing their updated \
requirements on Sept 24; haven't heard back."

- Use only the facts given. Never invent dates, numbers, meetings, documents or intentions.
- Lead with what changed this week, then who the ball is with and how long it has been when the facts say so \
("still waiting on redlines, promised five weeks ago").
- Keep every item, in the order given. An item with nothing new gets "no updates" (add the reason if a fact gives one).
- For a land site with owner lines, put each owner in sub_items as "<Owner> - <status or what happened>".
- Plain and brief, first person plural ("we"), dates like "Sept 24", no hype, no greeting or sign-off."""

SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {"type": "string", "description": "An optional one-line opener for the email, or empty."},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Tenants, Land, Projects or Other."},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "line": {"type": "string", "description": "The item's one-line status."},
                                "sub_items": {"type": "array", "items": {"type": "string"},
                                              "description": "Owner-by-owner lines for a land site, else empty."},
                            },
                            "required": ["name", "line", "sub_items"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["intro", "sections"],
    "additionalProperties": False,
}


class DraftUnavailable(Exception):
    pass


def week_of(day=None):
    day = day or timezone.localdate()
    return day - timedelta(days=day.weekday())


def facts(data, week_start):
    """The Summary tab as plain text for the model: every item with its facts, this week's activity and meeting
    notes, and last week's sent update for continuity."""
    c = data["campus"]
    lines = [f"CAMPUS: {c.code} ({c.name}). Update for the week of {week_start:%b %d, %Y}; "
             f"today is {data['today']:%a %b %d}."]
    since = week_start - timedelta(days=7)
    for title, items in (("TENANTS", data["tenants"]), ("LAND", data["land"]), ("PROJECTS", data["projects"])):
        lines.append(f"\n{title}")
        if not items:
            lines.append("(none)")
        for i in items:
            lines.append(f"- {i['name']}" + (f" [{i['status']}]" if i["status"] else ""))
            for u in i["obj"].campus_updates.filter(happened_on__gte=since).order_by("happened_on")[:8]:
                who = u.author.get_full_name() if u.author else "team"
                extra = f" ({u.get_waiting_on_display()})" if u.waiting_on else ""
                lines.append(f"  update {u.happened_on:%b %d} by {who}: {u.text}{extra}")
            lines += [f"  fact: {f}" for f in i["facts"]]
            if i["waiting"]:
                lines.append(f"  waiting: {i['waiting']}")
            lines += [f"  flag: {f}" for f in i["flags"]]
            lines += [f"  owner: {o['owner']} - {o['status']}" + (f", last milestone {o['last']}" if o["last"] else "")
                      for o in i.get("owners") or []]
    other = [u for u in data["other"] if u.happened_on >= since]
    if other:
        lines.append("\nOTHER")
        lines += [f"- {u.happened_on:%b %d}: {u.text}" for u in other]
    if data["feed"]:
        lines.append("\nTHIS WEEK'S ACTIVITY (newest first)")
        for d in data["feed"]:
            lines += [f"- {d['day']:%b %d} [{r['kind']}] {r['what']}" for r in d["rows"]]
    company_ids = [i["obj"].company_id for i in data["tenants"]]
    notes = Meeting.objects.filter(companies__in=company_ids, start__date__gte=since,
                                   start__lte=timezone.now()).exclude(summary="").distinct().order_by("start")
    if notes:
        lines.append("\nMEETING NOTES THIS WEEK")
        for m in notes[:12]:
            lines.append(f"- {timezone.localtime(m.start):%b %d} {m.title}: " + " ".join(m.summary.split())[:NOTES_CHARS])
    last = CampusReport.objects.filter(campus=c, status=CampusReport.Status.SENT, week_of__lt=week_start).first()
    if last:
        lines += ["\nLAST WEEK'S UPDATE (as sent)", last.body]
    return "\n".join(lines)


def render(draft):
    """The email text from Claude's sections: the team's bullet format."""
    out = [draft["intro"].strip(), ""] if draft.get("intro", "").strip() else []
    for section in draft["sections"]:
        if not section["items"]:
            continue
        out.append(section["title"])
        for item in section["items"]:
            out.append(f"  - {item['name']} - {item['line']}".rstrip(" -"))
            out += [f"      - {s}" for s in item["sub_items"]]
    return "\n".join(out).strip() + "\n"


def draft(campus, user=None, today=None):
    """Write (or rewrite) this week's draft. Returns the CampusReport. A sent week is never overwritten."""
    if not enabled():
        raise DraftUnavailable("Add ANTHROPIC_API_KEY to the .env file to turn on Claude drafts.")
    today = today or timezone.localdate()
    start = week_of(today)
    report = CampusReport.objects.filter(campus=campus, week_of=start).first()
    if report and report.status == CampusReport.Status.SENT:
        raise DraftUnavailable("This week's update has already been sent.")
    import anthropic

    data = campus_mod.summary(campus, today=today)
    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM,
            messages=[{"role": "user", "content": facts(data, start)}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            # If a safety classifier declines, the API retries on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise DraftUnavailable("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.") from exc
    except anthropic.RateLimitError as exc:
        raise DraftUnavailable("Anthropic is rate-limiting requests. Try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        log.warning("Campus draft failed: %s", exc)
        raise DraftUnavailable(f"Anthropic API error ({exc.status_code}). Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise DraftUnavailable("Could not reach Anthropic. Check the internet connection.") from exc
    if response.stop_reason == "refusal":
        raise DraftUnavailable("Claude declined to draft this update.")
    if response.stop_reason == "max_tokens":
        raise DraftUnavailable("The draft was cut off. Try again.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    body = render(json.loads(text))
    report = report or CampusReport(campus=campus, week_of=start)
    report.body, report.model, report.generated_at = body, response.model, timezone.now()
    report.created_by = report.created_by or user
    if not report.recipients:
        report.recipients = default_recipients(campus)
    report.save()
    return report


def default_recipients(campus):
    """Who last week's update went to, else the outreach report list."""
    from .models import OutreachSettings

    last = CampusReport.objects.filter(campus=campus, status=CampusReport.Status.SENT).exclude(recipients="").first()
    return last.recipients if last else OutreachSettings.load().report_recipients


def send(report, user):
    """Email the update from the user's Gmail (HTML with the logo, plain-text alternative) and mark it sent."""
    from django.utils.html import escape

    from .models import Mailbox, OutreachSettings
    from .outreach.sender import SendError, Smtp, build_message

    recipients = [r.strip() for r in report.recipients.replace(";", ",").split(",") if "@" in r]
    if not recipients:
        raise SendError("Add at least one email address to send to.")
    mailbox = Mailbox.objects.filter(user=user).first()
    if mailbox is None:
        raise SendError("Connect your mailbox on the Mail page first.")
    settings = OutreachSettings.load()
    logo = (f'<img src="{escape(settings.signature_logo_url)}" alt="ACMI Properties" style="max-width:140px;'
            'margin-bottom:12px"><br>') if settings.signature_logo_url else ""
    html_body = (f'<html><body style="font-family:Arial,sans-serif">{logo}<pre style="font-family:Arial,sans-serif;'
                 f'font-size:14px;white-space:pre-wrap">{escape(report.body)}</pre></body></html>')
    subject = f"{report.campus.code} update, week of {report.week_of:%b %d}"
    msg = build_message(mailbox, ", ".join(recipients), subject, html_body, report.body, [], settings)
    with Smtp(mailbox) as smtp:
        smtp.send(msg)
    report.status, report.sent_by, report.sent_at = CampusReport.Status.SENT, user, timezone.now()
    report.recipients = ", ".join(recipients)
    report.save(update_fields=["status", "sent_by", "sent_at", "recipients", "updated_at"])
    return report
