"""Load Granola notes (or calendar events) from JSON files into the Meetings calendar. Safe to run again on files
that overlap: each Granola note is matched by its id and updated in place. Afterwards the Managers get one bell
note with the counts and how many meetings need filing.

On Cloud Run the daily Granola task uploads its JSON to a Drive folder instead (settings.GRANOLA_DRIVE_FOLDER), and
sync_all calls import_from_drive() to load each new file once."""
import tempfile
from pathlib import Path, PurePath

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.urls import reverse

from crm import drive, meetings, notify
from crm.directory.sort import managers
from crm.models import ImportBatch, Notification

# A Drive file's import is recorded as an ImportBatch whose `sheet` is "granola:<file id>" (the spreadsheet import
# page leaves these out).
DRIVE_MARK = "granola:"
FORMATS = (".json", ".txt")  # the daily task's JSON, or the text export (crm/granola_text.py)


def file_key(f):
    """What records a Drive file as imported. A text export is rewritten under the same file ID, so its modified time is
    part of the key and an updated file is read again (the importer updates meetings in place)."""
    if f.name.lower().endswith(".txt") and f.modified:
        return f"{f.id}@{f.modified:%Y%m%d%H%M%S}"
    return f.id


class Command(BaseCommand):
    help = "Load Granola notes / Google Calendar events (JSON, see crm/meetings.py) into the Meetings calendar."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+", help="JSON files of meetings (Granola notes first is best)")
        parser.add_argument("--quiet", action="store_true", help="No bell note")

    def handle(self, *args, **opts):
        stats = meetings.import_meetings(meetings.load(*opts["paths"]))
        self.stdout.write(
            f"{stats['calendar']} meetings on the calendar ({stats['merged']} calendar events merged into Granola notes, "
            f"{stats['skipped']} solo/empty skipped); {stats['meetings']} with outside companies "
            f"({stats['created']} timeline entries new, {stats['updated']} updated); "
            f"{stats['transcripts']} transcripts saved"
        )
        if stats["unmatched"]:
            self.stdout.write("  domains with no company: " + ", ".join(stats["unmatched"][:40]))
        self.stdout.write(f"  {stats['need_filing']} meeting{'s' if stats['need_filing'] != 1 else ''} need filing")
        if not opts["quiet"]:
            note(stats)


def note(stats):
    """One bell note per Manager for the run."""
    from django.utils import timezone

    n, new, filing = stats["calendar"], stats["new"], stats["need_filing"]
    text = (f"Granola import: {n} meeting{'s' if n != 1 else ''} ({new} new, {n - new} updated), "
            f"{stats['transcripts']} transcript{'s' if stats['transcripts'] != 1 else ''}; "
            f"{filing} meeting{'s' if filing != 1 else ''} need filing.")
    url = reverse("crm:needs_filing") if filing else reverse("crm:meeting_list")
    for m in managers():
        notify.send(m, Notification.Kind.MEETING, text, url, None, key=f"granola-import:{timezone.now().isoformat()}")


def import_from_drive():
    """Import the Granola JSON files in the Drive folder that haven't been imported yet, all in one run (so calendar
    events merge into Granola notes across files), then record each file so it isn't read again. A file that won't
    download or parse is skipped and tried again next run. Returns {"files": [names imported], "failed": [(name,
    error)], "stats": the import's counts or None}. Nothing to do without the setting."""
    out = {"files": [], "failed": [], "stats": None}
    folder = drive.folder_id_from(getattr(settings, "GRANOLA_DRIVE_FOLDER", ""))
    if not folder:
        return out
    done = {s[len(DRIVE_MARK):] for s in
            ImportBatch.objects.filter(sheet__startswith=DRIVE_MARK).values_list("sheet", flat=True)}
    # With ACMI_GRANOLA_DRIVE_USER the folder is read with that person's Google sign-in, not the service account.
    who = {"as_user": settings.GRANOLA_DRIVE_USER} if getattr(settings, "GRANOLA_DRIVE_USER", "") else {}
    new = [f for f in drive.list_files(folder, refresh=True, **who)
           if f.name.lower().endswith(FORMATS) and f.mime not in drive.EXPORTS and file_key(f) not in done]
    if not new:
        return out
    c = drive.client(**who)
    with tempfile.TemporaryDirectory() as tmp:
        good = []
        for i, f in enumerate(new):
            path = Path(tmp) / f"{i}-{PurePath(f.name).name}"
            try:
                response, _ = c.download(f.id, f.mime)
                with open(path, "wb") as fh:
                    for chunk in response.iter_content(64 * 1024):
                        fh.write(chunk)
                meetings.load(path)  # read it now, so one bad file doesn't stop the others
            except Exception as exc:  # noqa: BLE001 - reported, and tried again next run
                out["failed"].append((f.name, f"{exc.__class__.__name__}: {exc}"))
                continue
            good.append((f, path))
        if not good:
            return out
        try:
            stats = _import(good)
        except Exception:  # noqa: BLE001 - one file the import can't take; try each alone so the rest still go in
            stats, imported = None, []
            for f, path in good:
                try:
                    one = _import([(f, path)])
                except Exception as exc:  # noqa: BLE001 - reported, and tried again next run
                    out["failed"].append((f.name, f"{exc.__class__.__name__}: {exc}"))
                    continue
                imported.append((f, path))
                stats = one if stats is None else {
                    k: (stats[k] + v if isinstance(v, int) else stats[k]) for k, v in one.items()}
            good = imported
    if not good:
        return out
    out["files"], out["stats"] = [f.name for f, _ in good], stats
    note(stats)
    return out


def _import(files):
    """Import these (DriveFile, path) pairs in one transaction and record each file, or roll the lot back."""
    with transaction.atomic():
        stats = meetings.import_meetings(meetings.load(*[p for _, p in files]))
        counts = {k: v for k, v in stats.items() if k != "unmatched"}
        for f, _ in files:
            ImportBatch.objects.create(filename=f.name[:255], sheet=f"{DRIVE_MARK}{file_key(f)}"[:120], counts=counts)
    return stats
