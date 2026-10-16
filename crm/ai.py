"""AI relationship summaries for company and person pages, written by Claude.

The record's recent timeline (emails, replies, notes, stage changes, news),
team connections, documents and open tasks are sent to the Anthropic API and
come back as a short structured summary, stored in AISummary until refreshed.
Needs ANTHROPIC_API_KEY in the environment (.env).
"""
import json
import logging
import os

from django.utils import timezone

from .models import Activity, AISummary, Attachment

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
TIMELINE_LIMIT = 80  # most recent foreground events sent to the model
BODY_CHARS = 700  # per email; quoted threads are mostly repeats

SYSTEM = """You write relationship summaries for ACMI Properties' CRM. ACMI develops \
purpose-built manufacturing campuses for defense, aerospace and energetics companies \
(NSIH in Indiana near NSWC Crane; MEIH in Maryland near NSWC Indian Head) and finances \
and builds facilities that tenants occupy when complete.

You are given one company or person's CRM record and timeline. Write for an ACMI \
teammate who is about to contact them and needs the state of play in thirty seconds.

- Ground every statement in the record. Never invent meetings, numbers, documents or intentions.
- Name the people involved on both sides and which teammate owns each conversation.
- Mass outreach emails with no reply are background: mention them only as a count.
- Dates as "Oct 13". Plain, direct sentences; no hype, no filler.
- If the record is thin, say so briefly rather than padding."""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence: where the relationship stands right now."},
        "history": {"type": "string", "description": "2-4 sentences: how it got here, from first contact to now."},
        "recent": {
            "type": "array", "items": {"type": "string"},
            "description": "Up to 5 bullet points on the most recent meaningful activity, newest first.",
        },
        "documents": {"type": "string", "description": "What documents ACMI has sent or received, or empty."},
        "next_step": {"type": "string", "description": "The obvious next action for ACMI, or empty if unclear."},
    },
    "required": ["headline", "history", "recent", "documents", "next_step"],
    "additionalProperties": False,
}


class SummaryUnavailable(Exception):
    pass


def enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _when(dt):
    return timezone.localtime(dt).strftime("%b %d, %Y")


def _record_text(company=None, person=None):
    from .interactions import team_connections

    if company is not None:
        r = company
        lines = [
            f"COMPANY: {r.name}",
            f"Stage: {r.get_status_display()}",
            f"Priority: {r.priority or 'not set'}",
            f"Sector: {r.sector or 'unknown'}",
            f"Fit score: {r.fit_score or 'not scored'}/5",
            f"Campus of interest: {r.campus_label or 'not known'}",
            f"Owner: {r.owner.get_full_name() if r.owner else 'unassigned'}",
            f"Outreach emails sent: {r.touch_count}; last response: {r.last_response or 'none'}",
        ]
        people = r.people.all()[:15]
        if people:
            lines.append("People: " + "; ".join(f"{p} ({p.title or 'no title'})" for p in people))
        if r.notes:
            lines.append(f"Notes: {r.notes}")
        activities = Activity.objects.filter(company=r)
        attachments = Attachment.objects.filter(activity__company=r)
        team = team_connections(company=r)
    else:
        r = person
        lines = [
            f"PERSON: {r}",
            f"Title: {r.title or 'unknown'}",
            f"Company: {r.company.name if r.company else 'none'}",
            f"Outreach emails sent: {r.touch_count}; last response: {r.last_response or 'none'}",
        ]
        if r.notes:
            lines.append(f"Notes: {r.notes}")
        activities = Activity.objects.filter(person=r)
        attachments = Attachment.objects.filter(activity__person=r)
        team = team_connections(person=r)

    if team:
        lines.append("ACMI team in touch: " + "; ".join(
            f"{t['user'].get_full_name() or t['user'].username} ({t['sent']} sent, {t['inbound']} received, "
            f"last {_when(t['last'])}, {t['strength']})" for t in team if t["user"]
        ))

    docs = attachments.select_related("activity", "activity__person").order_by("-activity__occurred_at")[:30]
    if docs:
        lines.append("\nDOCUMENTS:")
        for a in docs:
            direction = "sent" if a.activity.kind == Activity.Kind.EMAIL_SENT else "received"
            lines.append(f"- {_when(a.activity.occurred_at)} {direction} [{a.category}] {a.filename}")

    # Unanswered campaign sends are noise; count them instead of listing each one.
    foreground = activities.filter(is_background=False).select_related("person", "actor").prefetch_related(
        "team_members"
    )
    campaign_sends = foreground.filter(kind=Activity.Kind.EMAIL_SENT, source=Activity.Source.IMPORT).count()
    if campaign_sends:
        lines.append(f"\n({campaign_sends} workbook campaign emails sent, not listed individually.)")
    events = foreground.exclude(kind=Activity.Kind.EMAIL_SENT, source=Activity.Source.IMPORT).order_by(
        "-occurred_at"
    )[:TIMELINE_LIMIT]
    lines.append("\nTIMELINE (newest first):")
    for a in events:
        team_names = ", ".join(u.get_full_name() or u.username for u in a.team_members.all())
        who = []
        if a.person:
            who.append(str(a.person))
        if team_names:
            who.append(f"ACMI: {team_names}")
        line = f"- {_when(a.occurred_at)} {a.get_kind_display()}"
        if who:
            line += f" ({'; '.join(who)})"
        line += f": {a.summary}"
        if a.outcome:
            line += f" [{a.outcome}]"
        lines.append(line)
        if a.body and a.kind != Activity.Kind.EMAIL_SENT:
            lines.append("  " + " ".join(a.body.split())[:BODY_CHARS])
    return "\n".join(lines), activities.order_by("-id").values_list("id", flat=True).first()


def generate(company=None, person=None):
    """Write (or rewrite) the stored summary for a company or person. Returns the AISummary."""
    if not enabled():
        raise SummaryUnavailable("Add ANTHROPIC_API_KEY to the .env file to turn on AI summaries.")
    import anthropic

    record_text, last_activity_id = _record_text(company=company, person=person)
    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": record_text}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            # If a safety classifier declines, the API retries on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise SummaryUnavailable("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.") from exc
    except anthropic.RateLimitError as exc:
        raise SummaryUnavailable("Anthropic is rate-limiting requests. Try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        log.warning("Summary failed: %s", exc)
        raise SummaryUnavailable(f"Anthropic API error ({exc.status_code}). Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise SummaryUnavailable("Could not reach Anthropic. Check the internet connection.") from exc

    if response.stop_reason == "refusal":
        raise SummaryUnavailable("Claude declined to summarize this record.")
    if response.stop_reason == "max_tokens":
        raise SummaryUnavailable("The summary was cut off. Try again.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)

    summary, _ = AISummary.objects.update_or_create(
        company=company, person=person,
        defaults={
            "headline": data["headline"], "history": data["history"], "recent": data["recent"][:5],
            "documents": data["documents"], "next_step": data["next_step"],
            "generated_at": timezone.now(), "last_activity_id": last_activity_id or 0, "model": response.model,
        },
    )
    return summary


def stale_count(summary, company=None, person=None):
    """How many foreground events arrived since the summary was written."""
    if summary is None:
        return 0
    qs = Activity.objects.filter(is_background=False, id__gt=summary.last_activity_id)
    return qs.filter(company=company).count() if company is not None else qs.filter(person=person).count()
