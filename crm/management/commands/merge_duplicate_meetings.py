from django.core.management.base import BaseCommand

from crm import meetings


class Command(BaseCommand):
    help = ("Fold calendar meetings into the Granola note for the same meeting (same time within 15 minutes, similar "
            "title) and drop the doubled calendar timeline entries. Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the changes (back up the database first)")

    def handle(self, *args, **opts):
        pairs, dropped = meetings.merge_duplicates(apply=opts["apply"])
        verb = "Merged" if opts["apply"] else "Would merge"
        self.stdout.write(f"{verb} {len(pairs)} calendar meetings into Granola notes; "
                          f"{'dropped' if opts['apply'] else 'would drop'} {dropped} doubled timeline entries.")
        for note, cal in pairs:
            self.stdout.write(f"  meeting {cal.pk} -> {note.pk} ({note.start:%Y-%m-%d %H:%M})")
