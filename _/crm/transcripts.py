"""Meeting transcripts: kept in the CRM and written out as one .txt file per meeting.

Granola's free plan only shows the last 30 days, so a transcript is saved the first time it is seen and never thrown
away: a later import with no transcript (the meeting aged out) leaves the saved one alone. The .txt files go to
settings.TRANSCRIPT_DIR, one folder per campus ("General" when the meeting isn't tied to one):

    <TRANSCRIPT_DIR>/MEIH/2026-10-14 Thales intro.txt

Writing is idempotent: a file is only rewritten when its text changed.
"""
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .models import MeetingTranscript, Opportunity

UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def normalize(transcript):
    """Transcript text from what Granola gives: a string, or a list of {speaker, text, start} pieces."""
    if not transcript:
        return ""
    if isinstance(transcript, str):
        return transcript.strip()
    lines = []
    for piece in transcript:
        if isinstance(piece, str):
            lines.append(piece.strip())
            continue
        text = (piece.get("text") or "").strip()
        if not text:
            continue
        speaker = (piece.get("speaker") or piece.get("source") or "").strip()
        stamp = (piece.get("start") or piece.get("start_timestamp") or piece.get("timestamp") or "")
        stamp = str(stamp)[11:19] if len(str(stamp)) >= 19 else str(stamp)
        lines.append(" ".join(x for x in (f"[{stamp}]" if stamp else "", f"{speaker}:" if speaker else "", text) if x))
    return "\n".join(lines).strip()


def save(meeting, transcript, source="granola"):
    """Store a transcript for a meeting (new, or changed and not shorter). Returns the MeetingTranscript or None."""
    text = normalize(transcript)
    if not text:
        return None
    current = MeetingTranscript.objects.filter(meeting=meeting).first()
    if current and (current.text == text or len(text) < len(current.text) * 0.8):
        return current  # same, or a truncated re-fetch: keep what we have
    obj = current or MeetingTranscript(meeting=meeting)
    obj.text, obj.source, obj.words = text, source, len(text.split())
    obj.save()
    return obj


def campus_code(meeting):
    """The campus a meeting belongs to: the most common campus among its companies' opportunities, or ''."""
    codes = Counter(Opportunity.objects.filter(company__in=meeting.companies.all(), campus__isnull=False)
                    .values_list("campus__code", flat=True))
    return codes.most_common(1)[0][0] if codes else ""


def file_name(meeting):
    title = UNSAFE.sub(" ", meeting.title or "Meeting").strip(" .")[:80] or "Meeting"
    return f"{timezone.localtime(meeting.start):%Y-%m-%d} {title}.txt"


def as_text(t):
    m = t.meeting
    when = timezone.localtime(m.start).strftime("%A, %B %d, %Y %I:%M %p")
    if m.end:
        when += timezone.localtime(m.end).strftime(" - %I:%M %p")
    people = "; ".join(f"{a.get('name') or ''} <{a['email']}>".strip() for a in m.attendees if a.get("email"))
    parts = [m.title, when, f"Attendees: {people or 'not recorded'}",
             f"Companies: {', '.join(c.name for c in m.companies.all()) or 'none matched'}"]
    if m.url:
        parts.append(f"Granola: {m.url}")
    if m.summary:
        parts += ["", "== Notes ==", m.summary.strip()]
    parts += ["", "== Transcript ==", t.text, ""]
    return "\n".join(parts)


def write(t, root=None):
    """Write (or refresh) one transcript's .txt file. Returns the path, or None when nothing changed."""
    root = Path(root or settings.TRANSCRIPT_DIR)
    path = root / (campus_code(t.meeting) or "General") / file_name(t.meeting)
    body = as_text(t)
    if path.exists() and path.read_text(encoding="utf-8") == body:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # write then rename, so a file is never half-written
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    os.replace(tmp, path)
    if t.file_path and t.file_path != str(path) and Path(t.file_path).exists():
        Path(t.file_path).unlink()  # the meeting moved campus or was renamed: no stale duplicate
    MeetingTranscript.objects.filter(pk=t.pk).update(file_path=str(path))
    return path


def export_all(root=None):
    """Write every transcript whose file is missing or out of date. Returns the paths written."""
    written = []
    for t in MeetingTranscript.objects.select_related("meeting").prefetch_related("meeting__companies"):
        path = write(t, root)
        if path:
            written.append(path)
    return written
