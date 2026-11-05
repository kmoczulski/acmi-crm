"""Meetings from Granola notes and Google Calendar: the Meetings calendar and company timelines.

Input is a JSON list (written by the scheduled Claude task, or by hand), one object per meeting:

    {"source": "granola" | "calendar", "id": "...", "title": "...", "start": "2026-10-14T08:00:00-05:00",
     "end": "...", "url": "...", "summary": "<Granola markdown>", "attendees": [{"email": "...", "name": "..."}],
     "transcript": "<text>" or [{"speaker": "...", "text": "...", "start": "..."}]}

A transcript is saved for good (crm/transcripts.py) and written to a .txt file once the import commits.

Every meeting with at least one other person (or real notes) becomes a Meeting on the calendar. Re-importing the
same Granola note (by its id) updates the row in place, so the scheduled import can read overlapping days.
A calendar event within 15 minutes of a Granola note with a similar title is the same meeting, whichever arrives
first (the calendar sync usually does): the Granola note takes over the calendar row and its timeline entries.
An emailed invite (source "invite", crm/invites.py) at the same time with a similar title or a shared outside
attendee is taken over the same way by the calendar event or the Granola note.
Each meeting is linked to the directory firms and companies its attendees belong to, and notes the outside
attendees no record knows (crm/needs_filing.py).
Meetings with outside companies also become one timeline Activity per company (matched by
attendee email, then email domain): past ones move a company to "Meeting held", future ones set
"Next meeting". Re-importing updates in place and keeps tasks already made from action items.

Action items become tasks (make_task): the owner from the name in brackets, a "by Friday" in the item read as the
due date, and the same item (same title, same owner) from another meeting within a week joins the existing task.
"""
import html
import json
import re
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.safestring import mark_safe

from . import interactions, transcripts
from .merge import core_name
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
            m = re.match(r"\*\*(.+?)\*\*\s*(?:[(\[]([^)\]]+)[)\]])?\s*(.*)$", body)
            if m:
                title, owner, rest = m.group(1), m.group(2) or "", m.group(3)
            else:
                title, owner, rest = body, "", ""
                o = re.search(r"[(\[]([A-Z][^)\]]{1,40})[)\]]\s*$", title)
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
    from .permissions import team_users

    for u in team_users():
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
        if str(path).lower().endswith(".txt"):  # the Granola text export (crm/granola_text.py)
            from . import granola_text

            rows += granola_text.load(path)
            continue
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
    from . import needs_filing

    known = needs_filing.Known()
    team = _team_lookup()
    domains = {}
    for pk, ds in Company.objects.values_list("pk", "domains"):
        for d in ds or []:
            domains.setdefault(d.lower(), pk)
    by_label = {}  # company labels in text exports ("Fortress"); a name two companies share links to neither
    for pk, name in Company.objects.values_list("pk", "name"):
        k = core_name(name)
        by_label[k] = pk if k not in by_label else None
    emails = {a["email"].lower() for m in meetings for a in m.get("attendees", []) if a.get("email")}
    people = {p.email: p for p in Person.objects.filter(email__in=emails).select_related("company")}
    now = timezone.now()
    stats = {"meetings": 0, "calendar": 0, "skipped": 0, "created": 0, "updated": 0, "internal_only": 0,
             "merged": 0, "transcripts": 0, "unmatched": set(), "new": 0, "need_filing": 0}
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
            for label in m.get("groups", []):  # "(Fortress)" in a text export: no email to match a company by
                company_id = by_label.get(core_name(label))
                if company_id:
                    by_company.setdefault(company_id, None)
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
            stats["new"] += meeting.pk is None
            meeting.attendees = _merge_attendees(
                [{"name": a.get("name", ""), "email": a["email"]} for a in attendees], meeting.attendees,
                keep=meeting.source != m.get("source", "calendar") or getattr(meeting, "_took_invite", False))
            meeting.save()
            meeting.team.add(*internal)
            # Declined in Google: off their calendar (the team is only ever added to, so an earlier sync's row stays).
            meeting.team.remove(*[team[e] for e in m.get("declined", []) if e in team and team[e] not in internal])
            meeting.companies.add(*by_company.keys())
            needs_filing.link(meeting, known)
            stats["need_filing"] += bool(meeting.unfiled)
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


def _merge_attendees(new, old, keep=False):
    """The incoming attendee list; with keep (another source's row), the old list plus anyone new. Guests an emailed
    invite added ("via": "invite") stay when the row's own source re-syncs without them."""
    if not keep:
        if not new:
            return old
        seen = {(a.get("email") or "").lower() for a in new}
        return list(new) + [a for a in old or []
                            if a.get("via") == "invite" and (a.get("email") or "").lower() not in seen]
    out, seen = list(old or []), {(a.get("email") or "").lower() for a in old or []}
    for a in new:
        if a["email"].lower() not in seen:
            out.append(a)
            seen.add(a["email"].lower())
    return out


def _outside(attendees):
    return {(a.get("email") or "").lower() for a in attendees or []
            if "@" in (a.get("email") or "") and (a.get("email") or "").lower().rsplit("@", 1)[-1] not in INTERNAL_DOMAINS}


def same_invite(meeting, title, attendees):
    """An emailed invite is the same meeting as a row at the same time with a similar title or a shared outside
    attendee (invites from the other side often carry their own title)."""
    return _same_meeting(meeting.title, title) or bool(_outside(meeting.attendees) & _outside(attendees))


def _invite_for(m, start):
    return next((x for x in _near(start, "invite") if same_invite(x, m.get("title"), m.get("attendees"))), None)


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
                for task in cal.tasks.all():
                    task.meetings.add(note)
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
    if meeting is None and source in ("calendar", "granola"):
        # An emailed invite got there first: the calendar event or the note takes over its row.
        meeting = _invite_for(m, start)
        if meeting is not None:
            meeting.external_id, meeting.source = ext, source
            meeting._took_invite = True
            # The invite's guests are marked, so later syncs of this calendar event or note keep them.
            meeting.attendees = [{**a, "via": "invite"} for a in meeting.attendees or []]
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


# ---------------------------------------------------------------------------
# Action items → tasks
# ---------------------------------------------------------------------------

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
SAME_TASK_DAYS = 7


def due_from_text(text, base):
    """A due date from the words in an action item, counted from the meeting's day: "by Friday", "by tomorrow",
    "end of week", "next week", "end of month", "by Oct 30", "by 10/30", "today" / "EOD". None when it says none."""
    t = " ".join((text or "").lower().split())
    if re.search(r"\b(today|eod|end of (the )?day)\b", t):
        return base
    if re.search(r"\btomorrow\b", t):
        return base + timedelta(days=1)
    if re.search(r"\b(end of (the )?week|eow|this week)\b", t):
        return base + timedelta(days=(4 - base.weekday()) % 7)
    if re.search(r"\bnext week\b", t):
        return base + timedelta(days=7 - base.weekday())
    if re.search(r"\b(end of (the )?month|eom)\b", t):
        import calendar

        return base.replace(day=calendar.monthrange(base.year, base.month)[1])
    m = re.search(r"\b(?:by|before|on|due)\s+(?:(?:this|next)\s+)?(" + "|".join(WEEKDAYS)
                  + r"|mon|tue|tues|wed|thu|thur|thurs|fri)\b", t)
    if m:
        day = next(i for i, w in enumerate(WEEKDAYS) if w.startswith(m.group(1)[:3]))
        ahead = (day - base.weekday()) % 7
        if "next " + m.group(1) in t:
            ahead += 7
        return base + timedelta(days=ahead)
    m = re.search(r"\b(" + "|".join(MONTHS) + r")(?:uary|ruary|ch|il|e|y|ust|t|tember|ober|ember)?\.?\s+(\d{1,2})"
                  r"(?:st|nd|rd|th)?\b", t)
    if m:
        return _dated(base, MONTHS.index(m.group(1)) + 1, int(m.group(2)))
    m = re.search(r"\b(?:by|before|on|due)\s+(\d{1,2})/(\d{1,2})\b", t)
    if m:
        return _dated(base, int(m.group(1)), int(m.group(2)))
    return None


def _dated(base, month, day):
    from datetime import date

    try:
        d = date(base.year, month, day)
    except ValueError:
        return None
    return d if d >= base - timedelta(days=31) else d.replace(year=base.year + 1)


def owner_for(name, users):
    """The teammate an action item's bracketed name means: first name, username, full name or last name."""
    words = [w.strip(",.").lower() for w in (name or "").split() if w.strip(",.")]
    if not words:
        return None
    full = " ".join(words)
    for test in (lambda u: (u.get_full_name() or "").lower() == full,
                 lambda u: (u.first_name or "").lower() == words[0] or u.username.lower() == words[0],
                 lambda u: len(words) == 1 and (u.last_name or "").lower() == words[0]):
        hit = [u for u in users if test(u)]
        if len(hit) == 1:
            return hit[0]
    return None


def same_task(title, assignee, meeting):
    """An open-or-done task with the same title and owner made within a week, or from a meeting within a week, and
    about the same record: the meeting's company, (no company) one of its directory firms, or (neither) another
    team-only meeting. "Send NDA [Simon]" on two different companies' calls stays two tasks."""
    from django.db.models import Q

    from .models import Task

    week = timedelta(days=SAME_TASK_DAYS)
    qs = (Task.objects.filter(title__iexact=title.strip(), assignee=assignee)
          .filter(Q(created_at__gte=timezone.now() - week)
                  | Q(meetings__start__gte=meeting.start - week, meetings__start__lte=meeting.start + week)))
    companies = list(meeting.companies.values_list("pk", flat=True))
    if companies:
        qs = qs.filter(company__in=companies)
    else:
        firms = list(meeting.firms.values_list("pk", flat=True))
        qs = qs.filter(company__isnull=True)
        # A firm call joins tasks from that firm's calls; a team-only meeting, tasks from other team-only meetings.
        qs = (qs.filter(meetings__firms__in=firms) if firms
              else qs.filter(meetings__isnull=False, meetings__companies__isnull=True, meetings__firms__isnull=True))
    return qs.distinct().order_by("-created_at").first()


def make_task(meeting, index, user, assignee=None, due=None, due_time=None):
    """Turn one action item into a task, or link it to the same task from a meeting a few days apart. Returns
    (task, created) or (None, False) when the item already has one."""
    from .models import Task
    from .forms import active_users

    item = meeting.action_items[index]
    if item.get("task_id") and Task.objects.filter(pk=item["task_id"]).exists():
        return None, False
    assignee = assignee or owner_for(item.get("owner"), list(active_users())) or user
    if due is None:
        due = due_from_text(f"{item.get('title', '')} {item.get('detail', '')}",
                            timezone.localtime(meeting.start).date())
    task = same_task(item["title"][:300], assignee, meeting)
    created = task is None
    if created:
        task = Task.objects.create(
            title=item["title"][:300], due_date=due, due_time=due_time if due else None, assignee=assignee,
            notes=(item.get("detail", "")
                   + f"\n\nFrom meeting: {meeting.title} ({meeting.start:%b %d}) {meeting.url}").strip(),
            company=meeting.companies.first(), created_by=user)
    task.meetings.add(meeting)
    items = list(meeting.action_items)
    items[index] = {**item, "task_id": task.pk}
    meeting.action_items = items
    meeting.save(update_fields=["action_items"])
    return task, created


def make_all_tasks(meeting, user):
    """"Make tasks for all N items": every item without a task. Returns (tasks made, items joined to one)."""
    made, joined = [], []
    for index in range(len(meeting.action_items or [])):
        task, created = make_task(meeting, index, user)
        if task is not None:
            (made if created else joined).append(task)
    return made, joined
