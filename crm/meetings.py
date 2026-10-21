"""Meetings from Granola notes and Google Calendar: the Meetings calendar and company timelines.

Input is a JSON list (written by the scheduled Claude task, or by hand), one object per meeting:

    {"source": "granola" | "calendar", "id": "...", "title": "...", "start": "2026-10-14T08:00:00-05:00",
     "end": "...", "url": "...", "summary": "<Granola markdown>", "attendees": [{"email": "...", "name": "..."}],
     "transcript": "<text>" or [{"speaker": "...", "text": "...", "start": "..."}]}

A transcript is saved for good (crm/transcripts.py) and written to a .txt file once the import commits.

Every meeting with at least one other person (or real notes) becomes a Meeting on the calendar.
A calendar event within 15 minutes of a Granola note with a similar title is the same meeting, whichever arrives
first (the calendar sync usually does): the Granola note takes over the calendar row and its timeline entries.
Meetings with outside companies also become one timeline Activity per company (matched by
attendee email, then email domain): past ones move a company to "Meeting held", future ones set
"Next meeting". Re-importing updates in place and keeps tasks already made from action items.
"""
import html
import json
import re
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.safestring import mark_safe

from . import interactions, transcripts
from .models import Activity, Company, CompanyStatus, Meeting, MeetingTranscript, Person, suppress_history

INTERNAL_DOMAINS = {"acmigroup.com"}
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com", "me.com"}
SOURCES = {"granola": Activity.Source.GRANOLA, "calendar": Activity.Source.CALENDAR}
EMPTY_SUMMARIES = {"", "no summary", "no transcript content was recorded for this meeting."}
ACTION_HEADINGS = re.compile(r"^#{1,4}\s*(next steps|action items|follow[- ]ups?)\s*$", re.I)


# ---------------------------------------------------------------------------
# Granola markdown
# ---------------------------------------------------------------------------


def clean_summary(text):
    text = (text or "").strip()
    return "" if text.lower() in EMPTY_SUMMARIES else text


def action_items(summary):
    """Bullets under "Next Steps" / "Action Items": [{title, owner, detail}].

    Granola writes them as "- **Do the thing** (Kellen)" followed by an indented detail line.
    """
    items, in_section = [], False
    for line in (summary or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_section = bool(ACTION_HEADINGS.match(stripped))
            continue
        if not in_section or not stripped:
            continue
        if line.startswith("- ") or line.startswith("* "):
            body = stripped[2:].strip()
            m = re.match(r"\*\*(.+?)\*\*\s*(?:\(([^)]+)\))?\s*(.*)$", body)
            if m:
                title, owner, rest = m.group(1), m.group(2) or "", m.group(3)
            else:
                title, owner, rest = body, "", ""
                o = re.search(r"\(([A-Z][^)]{1,40})\)\s*$", title)
                if o:
                    owner, title = o.group(1), title[: o.start()].strip()
            items.append({"title": _plain(title), "owner": _plain(owner), "detail": _plain(rest)})
        elif items and line.startswith((" ", "\t")):
            items[-1]["detail"] = (items[-1]["detail"] + " " + _plain(stripped)).strip()
    return items


def summary_without_actions(summary):
    """The summary minus its "Next Steps" / "Action Items" section, for pages that list those items on their own."""
    kept, in_section = [], False
    for line in (summary or "").splitlines():
        if line.strip().startswith("#"):
            in_section = bool(ACTION_HEADINGS.match(line.strip()))
        if not in_section:
            kept.append(line)
    return "\n".join(kept)


def _plain(text):
    return html.unescape(re.sub(r"\*\*|\\", "", text or "")).strip()


def render_markdown(text):
    """Granola's markdown subset -> safe HTML: headings, nested bullets, bold, paragraphs."""
    out, depth_stack = [], []

    def close_lists(to_depth):
        while depth_stack and depth_stack[-1] >= to_depth:
            depth_stack.pop()
            out.append("</ul>")

    def inline(s):
        s = html.escape(html.unescape(s.replace("\\~", "~").replace("\\&", "&").replace("\\", "")))
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", raw.strip())
        bullet = re.match(r"^(\s*)[-*]\s+(.*)$", raw)
        if heading:
            close_lists(0)
            out.append(f"<h4>{inline(heading.group(2))}</h4>")
        elif bullet:
            depth = len(bullet.group(1).replace("\t", "  ")) // 2
            if not depth_stack or depth > depth_stack[-1]:
                out.append("<ul>")
                depth_stack.append(depth)
            else:
                close_lists(depth + 1)
            out.append(f"<li>{inline(bullet.group(2))}</li>")
        else:
            if depth_stack and raw.startswith((" ", "\t")):
                out.append(f'<div class="li-detail">{inline(raw.strip())}</div>')
            else:
                close_lists(0)
                out.append(f"<p>{inline(raw.strip())}</p>")
    close_lists(0)
    return mark_safe("".join(out))


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _team_lookup():
    """acmigroup addresses -> users, by stored email or first-initial+last-name (sshewmaker@)."""
    lookup = {}
    for u in get_user_model().objects.filter(is_active=True):
        if u.email:
            lookup[u.email.lower()] = u
        if u.first_name and u.last_name:
            lookup[f"{u.first_name[0]}{u.last_name}@acmigroup.com".lower()] = u
    return lookup


def _company_for(email, people, domains):
    person = people.get(email)
    if person and person.company_id:
        return person.company_id, person
    domain = email.rsplit("@", 1)[-1]
    if domain in FREE_MAIL:
        return None, person
    return domains.get(domain), person


def _norm(title):
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(title or "").lower()).strip()


def _same_meeting(a, b):
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or a in b or b in a)


def load(*paths):
    """Read one or more JSON files; Granola notes come first so calendar events merge into them."""
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            rows += json.load(fh)
    by_id = {}
    for r in rows:  # the same Granola note in two files: keep the one with a summary
        key = (r.get("source"), r["id"])
        if key not in by_id or (r.get("summary") and not by_id[key].get("summary")):
            by_id[key] = r
    return sorted(by_id.values(), key=lambda r: r.get("source") != "granola")


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


@transaction.atomic
def import_meetings(meetings):
    team = _team_lookup()
    domains = {}
    for pk, ds in Company.objects.values_list("pk", "domains"):
        for d in ds or []:
            domains.setdefault(d.lower(), pk)
    emails = {a["email"].lower() for m in meetings for a in m.get("attendees", []) if a.get("email")}
    people = {p.email: p for p in Person.objects.filter(email__in=emails).select_related("company")}
    now = timezone.now()
    stats = {"meetings": 0, "calendar": 0, "skipped": 0, "created": 0, "updated": 0, "internal_only": 0,
             "merged": 0, "transcripts": 0, "unmatched": set()}
    touched, saved = set(), []
    token = suppress_history.set(True)
    try:
        for m in meetings:
            start = _parse(m["start"])
            attendees = [a for a in m.get("attendees", []) if a.get("email") and "@" in a["email"]]
            internal, by_company = [], {}
            for a in attendees:
                email = a["email"].lower()
                if email.rsplit("@", 1)[-1] in INTERNAL_DOMAINS:
                    if email in team and team[email] not in internal:
                        internal.append(team[email])
                    continue
                company_id, person = _company_for(email, people, domains)
                if company_id:
                    by_company.setdefault(company_id, person)
                elif email.rsplit("@", 1)[-1] not in FREE_MAIL:
                    stats["unmatched"].add(email.rsplit("@", 1)[-1])
            summary = clean_summary(m.get("summary"))
            has_transcript = bool(transcripts.normalize(m.get("transcript")))
            others = [a for a in attendees if a["email"].lower() not in team or len(internal) > 1]
            if len(attendees) < 2 and not summary and not has_transcript:
                stats["skipped"] += 1  # solo calendar blocks ("Gym") and empty notes
                continue

            meeting = _meeting_for(m, start, summary)
            if meeting is None:
                stats["skipped"] += 1
                continue
            if meeting.source == "granola" and m.get("source") == "calendar":
                stats["merged"] += 1
            meeting.attendees = [{"name": a.get("name", ""), "email": a["email"]} for a in attendees] or meeting.attendees
            meeting.save()
            meeting.team.add(*internal)
            meeting.companies.add(*by_company.keys())
            stats["calendar"] += 1
            if has_transcript:
                saved_t = transcripts.save(meeting, m.get("transcript"), m.get("source") or "granola")
                if saved_t:
                    saved.append(saved_t.pk)
                    stats["transcripts"] += 1

            if not by_company:
                stats["internal_only"] += 1
                continue
            if meeting.source == "granola" and m.get("source") == "calendar":
                _drop_calendar_activities(m["id"])  # the Granola note's own entry covers it
                continue
            stats["meetings"] += 1
            source = SOURCES.get(m.get("source"), Activity.Source.CALENDAR)
            for company_id, person in by_company.items():
                act, created = Activity.objects.update_or_create(
                    source=source, source_id=f"{m['id']}:{company_id}"[:255],
                    defaults={
                        "kind": Activity.Kind.MEETING, "occurred_at": start, "company_id": company_id,
                        "person": person, "summary": (html.unescape(m.get("title") or "Meeting"))[:500],
                        "body": summary, "url": (m.get("url") or "")[:1000],
                    },
                )
                if internal:
                    act.team_members.add(*internal)
                stats["created" if created else "updated"] += 1
                touched.add(company_id)
                if start <= now:
                    Company.objects.filter(pk=company_id, status__in=[
                        CompanyStatus.PROSPECT, CompanyStatus.CONTACTED, CompanyStatus.ENGAGED,
                    ]).update(status=CompanyStatus.MEETING)
    finally:
        suppress_history.reset(token)
    interactions.recompute(company_ids=touched, person_ids=[])
    if saved:  # the .txt copies, once the rows are committed
        transaction.on_commit(lambda: transcripts.export_all())
    stats["unmatched"] = sorted(stats["unmatched"])
    return stats


def _near(start, source):
    return Meeting.objects.filter(source=source, start__gte=start - timedelta(minutes=15),
                                  start__lte=start + timedelta(minutes=15))


def _drop_calendar_activities(event_id):
    Activity.objects.filter(source=Activity.Source.CALENDAR, source_id__startswith=f"{event_id}:").delete()


def merge_duplicates(apply=False):
    """Fold calendar meetings into the Granola note for the same meeting (imported before the fix above), and drop
    calendar timeline entries for meetings already merged. Returns (pairs merged, timeline entries dropped)."""
    pairs = []
    for cal in Meeting.objects.filter(source="calendar"):
        note = next((g for g in _near(cal.start, "granola") if not g.calendar_id and _same_meeting(g.title, cal.title)), None)
        if note is not None and note.pk not in {n.pk for n, _ in pairs}:
            pairs.append((note, cal))
    event_ids = [c.external_id.split(":", 1)[1] for _, c in pairs]
    event_ids += list(Meeting.objects.filter(source="granola").exclude(calendar_id="").values_list("calendar_id", flat=True))
    stale = Activity.objects.none()
    for eid in event_ids:
        stale = stale | Activity.objects.filter(source=Activity.Source.CALENDAR, source_id__startswith=f"{eid}:")
    dropped = stale.count()
    if apply:
        with transaction.atomic():
            for note, cal in pairs:
                note.calendar_id = cal.external_id.split(":", 1)[1]
                note.end = note.end or cal.end
                note.save(update_fields=["calendar_id", "end"])
                note.companies.add(*cal.companies.all())
                note.team.add(*cal.team.all())
                cal.tasks.update(meeting=note)
                if not MeetingTranscript.objects.filter(meeting=note).exists():
                    MeetingTranscript.objects.filter(meeting=cal).update(meeting=note)
                cal.delete()
            stale.delete()
    return pairs, dropped


def _meeting_for(m, start, summary):
    """The Meeting row for this input: by id, or a Granola note at the same time for a calendar event."""
    source = m.get("source", "calendar")
    ext = f"{source}:{m['id']}"
    meeting = Meeting.objects.filter(external_id=ext).first() or Meeting.objects.filter(calendar_id=m["id"]).first()
    if meeting is not None and meeting.source == "granola" and source == "calendar":
        meeting.end = meeting.end or _parse(m.get("end"))  # merged already: keep Granola's title and link
        return meeting
    if meeting is None and source == "calendar":
        meeting = next((x for x in _near(start, "granola") if _same_meeting(x.title, m.get("title"))), None)
        if meeting is not None:
            meeting.calendar_id = m["id"]
            meeting.end = meeting.end or _parse(m.get("end"))
            return meeting
    if meeting is None and source == "granola":
        # The calendar sync got there first: the note takes over that row (its tasks, companies and team stay on it).
        cal = next((x for x in _near(start, "calendar") if _same_meeting(x.title, m.get("title"))), None)
        if cal is not None:
            meeting = cal
            meeting.calendar_id = cal.external_id.split(":", 1)[1]
            meeting.external_id, meeting.source = ext, "granola"
            _drop_calendar_activities(meeting.calendar_id)
    if meeting is None:
        meeting = Meeting(external_id=ext, source=source)
    meeting.title = html.unescape(m.get("title") or "Meeting")[:500]
    meeting.start = start
    meeting.end = _parse(m.get("end")) or meeting.end
    meeting.url = (m.get("url") or meeting.url or "")[:1000]
    if source == "granola":
        meeting.summary = summary
        kept = {i["title"]: i.get("task_id") for i in meeting.action_items or [] if i.get("task_id")}
        meeting.action_items = [{**i, "task_id": kept.get(i["title"])} for i in action_items(summary)]
    return meeting
