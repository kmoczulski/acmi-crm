from django.core.management.base import BaseCommand

from crm import meetings


class Command(BaseCommand):
    help = "Load Granola notes / Google Calendar events (JSON, see crm/meetings.py) into the Meetings calendar."

    def add_arguments(self, parser):
        parser.add_argument("paths", nargs="+", help="JSON files of meetings (Granola notes first is best)")

    def handle(self, *args, **opts):
        stats = meetings.import_meetings(meetings.load(*opts["paths"]))
        self.stdout.write(
            f"{stats['calendar']} meetings on the calendar ({stats['merged']} calendar events merged into Granola notes, "
            f"{stats['skipped']} solo/empty skipped); {stats['meetings']} with outside companies "
            f"({stats['created']} timeline entries new, {stats['updated']} updated)"
        )
        if stats["unmatched"]:
            self.stdout.write("  domains with no company: " + ", ".join(stats["unmatched"][:40]))
