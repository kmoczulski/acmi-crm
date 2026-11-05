"""The Granola text export: one .txt file of many meetings, read into the rows crm.meetings.import_meetings takes.

The daily Claude task writes it to the Granola Drive folder (see docs/HANDOFF.md):

    === MEETING ===
    ID: <Granola meeting id>
    TITLE: ...
    DATE: 2026-11-04 10:00 AM CST
    URL: https://notes.granola.ai/d/<id>
    ATTENDEES: Kellen Moczulski, Simon Shewmaker (ACMI); Chase Romney, Michael Tompkins (Fortress)
    --- NOTES ---
    <Granola's summary, markdown>
    === END ===

The attendees carry names, not emails. `resolve_attendees` fills an email where one record knows the name (a team
member, or exactly one Person), and each outside "(Company)" label is kept in `groups` so the meeting can still be
linked to that company. A companion index file (titles and links only) has no meetings in it and reads as empty.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

START = "=== MEETING ==="
END = "=== END ==="
NOTES = "--- NOTES ---"
FIELDS = ("ID", "TITLE", "DATE", "URL", "ATTENDEES")
OFFSETS = {"UTC": 0, "GMT": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7}
HOME_ZONE = "America/Chicago"
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*([AP]M)?\s*([A-Za-z]{2,5})?$", re.I)
GROUP_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
HEADING_RE = re.compile(r"^[A-Z0-9][A-Z0-9 /&,'’()\-:.+]{2,}$")  # "NEXT STEPS", "ERIS ENERGETICS BACKGROUND"
STEP_HEADING_RE = re.compile(r"^(next steps|action items|follow[- ]ups?)$", re.I)
OWNED_RE = re.compile(r"^- (.+?)\s*\(([^()]{1,40})\):\s*(.+)$")  # "- Send comps (Kellen): Fortress asked."
DETAILED_RE = re.compile(r"^- (.+?):\s+(.+)$")  # "- Send comps: Fortress asked."


def markdown(notes):
    """The export's notes -> the markdown crm.meetings reads: ALL-CAPS lines become "## " headings, and each bullet
    under Next Steps / Action Items "- Title (Owner): detail" becomes "- **Title** (Owner)" with the detail under it."""
    out, steps = [], False
    for line in (notes or "").strip().splitlines():
        text = line.strip()
        if text and not line.startswith((" ", "\t", "-", "*", "#")) and HEADING_RE.match(text) and re.search("[A-Z]{2}", text):
            steps = bool(STEP_HEADING_RE.match(text))
            out.append(f"## {text}")
            continue
        if steps and line.startswith("- "):
            m = OWNED_RE.match(line.rstrip())
            if m:
                out += [f"- **{m.group(1)}** ({m.group(2)})", f"  {m.group(3)}"]
                continue
            m = DETAILED_RE.match(line.rstrip())
            if m:
                out += [f"- **{m.group(1)}**", f"  {m.group(2)}"]
                continue
        out.append(line.rstrip())
    return "\n".join(out)


def parse_date(text):
    """'2026-11-04 10:00 AM CST' -> an aware datetime (the abbreviation as written; none -> Central), or None."""
    m = DATE_RE.match((text or "").strip())
    if not m:
        return None
    day, clock, ampm, abbr = m.groups()
    fmt = "%Y-%m-%d %I:%M %p" if ampm else "%Y-%m-%d %H:%M"
    try:
        naive = datetime.strptime(f"{day} {clock}" + (f" {ampm.upper()}" if ampm else ""), fmt)
    except ValueError:
        return None
    hours = OFFSETS.get((abbr or "").upper())
    if hours is None:
        return naive.replace(tzinfo=ZoneInfo(HOME_ZONE))
    return naive.replace(tzinfo=timezone(timedelta(hours=hours)))


def parse_attendees(text):
    """'A, B (ACMI); C (Fortress)' -> [{"name": "A", "group": "ACMI"}, ...]. A name with no label has group ""."""
    out = []
    for chunk in (text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = GROUP_RE.match(chunk)
        names, group = (m.group(1), m.group(2).strip()) if m else (chunk, "")
        for name in names.split(","):
            name = name.strip()
            if name:
                out.append({"name": name, "group": group})
    return out


def parse(text):
    """Every meeting in the export as {"source": "granola", "id", "title", "start", "url", "summary", "attendees":
    [{"name", "group"}]}. A block with no ID or an unreadable date is left out (reported by `problems`)."""
    rows, _ = _read(text)
    return rows


def problems(text):
    """Why blocks were left out: [(title or id, reason)]."""
    return _read(text)[1]


def _read(text):
    rows, bad = [], []
    for block in _blocks(text or ""):
        head, _, notes = block.partition(NOTES)
        fields = {}
        for line in head.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip().upper() in FIELDS:
                fields[key.strip().upper()] = value.strip()
        label = fields.get("TITLE") or fields.get("ID") or "(no title)"
        start = parse_date(fields.get("DATE"))
        if not fields.get("ID"):
            bad.append((label, "no ID"))
        elif start is None:
            bad.append((label, f"unreadable date {fields.get('DATE', '')!r}"))
        else:
            rows.append({
                "source": "granola", "id": fields["ID"], "title": fields.get("TITLE", ""),
                "start": start.isoformat(), "url": fields.get("URL", ""), "summary": markdown(notes),
                "attendees": parse_attendees(fields.get("ATTENDEES")),
            })
    return rows, bad


def _blocks(text):
    block = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == START:
            block = []
        elif stripped == END:
            if block is not None:
                yield "\n".join(block)
            block = None
        elif block is not None:
            block.append(line)
    # A file cut off before its last END is dropped rather than half-read.


def load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return resolve_attendees(parse(fh.read()))


# ---------------------------------------------------------------------------
# Names -> emails
# ---------------------------------------------------------------------------


def _key(name):
    return re.sub(r"[^a-z]+", " ", (name or "").lower()).strip()


def _is_company(label):
    """An outside "(Company)" label worth matching. Not ACMI's own, and not the counts ("13 total") or lists of
    usernames ("Astrauss, Bhwilliams") Granola sometimes puts in the brackets."""
    low = (label or "").lower()
    return bool(low) and not low.startswith(("acmi", "the acmi")) and "," not in low and "total" not in low


def resolve_attendees(rows):
    """Give each attendee an email where exactly one record has that name, and set row["groups"] to the outside
    "(Company)" labels. Team members match by full name or first-initial + last name ("Mguerrero"), outside people
    by a unique Person with that full name. Anyone else stays name-only."""
    from .models import Person
    from .permissions import team_users

    team = {}
    for u in team_users():
        if not u.email:
            continue
        full = _key(f"{u.first_name} {u.last_name}")
        if full:
            team.setdefault(full, u.email.lower())
        if u.first_name and u.last_name:
            team.setdefault(_key(f"{u.first_name[0]}{u.last_name}"), u.email.lower())
    people = {}
    for full, email in Person.objects.exclude(email__isnull=True).values_list("full_name", "email"):
        people.setdefault(_key(full), set()).add(email)
    for r in rows:
        groups = []
        for a in r["attendees"]:
            k = _key(a["name"])
            email = team.get(k)
            if not email and len(people.get(k, ())) == 1:
                email = next(iter(people[k]))
            if email:
                a["email"] = email
            if _is_company(a["group"]) and a["group"] not in groups:
                groups.append(a["group"])
        r["groups"] = groups
        r["attendees"] = [{k: v for k, v in a.items() if k != "group"} for a in r["attendees"]]
    return rows
