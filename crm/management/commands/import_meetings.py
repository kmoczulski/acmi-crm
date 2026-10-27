"""Load Granola notes (or calendar events) from JSON files into the Meetings calendar. Safe to run again on files
that overlap: each Granola note is matched by its id and updated in place. Afterwards the Managers get one bell
note with the counts and how many meetings need filing."""
from django.core.management.base import BaseCommand
from django.urls import reverse

from crm import meetings, notify
from crm.directory.sort import managers
from crm.models import Notification


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
