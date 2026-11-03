from django.core.management.base import BaseCommand

from crm import pipeline_import

DEFAULT_TRACKER = (
    r"G:\Shared drives\1 ACMI Properties\1 Projects\Fortress Pipeline Meetings\2026.09.25"
    r"\ACMI_Properties_Pipeline_Tracker.xlsx"
)
DEFAULT_TRELLO = "data/imports/trello_properties_prospecting.json"


class Command(BaseCommand):
    help = "Create opportunities from the Fortress pipeline tracker and the Trello prospecting board."

    def add_arguments(self, parser):
        parser.add_argument("--tracker", default=DEFAULT_TRACKER)
        parser.add_argument("--trello", default=DEFAULT_TRELLO, help="JSON export of the Trello board")
        parser.add_argument("--dry-run", action="store_true", help="Show what would happen, change nothing")

    def handle(self, *args, **opts):
        report = pipeline_import.run(opts["tracker"], opts["trello"], self.stdout, dry_run=opts["dry_run"])
        self.stdout.write(f"Opportunities created: {report['created']}, updated: {report['updated']}")
        self.stdout.write(f"Company owners filled from deal owners: {report['owners_filled']}")
        if report["companies_created"]:
            self.stdout.write(f"New companies ({len(report['companies_created'])}): " + ", ".join(report["companies_created"]))
        for line in report["differences"]:
            self.stdout.write(f"  stage differs: {line}")
        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing was saved."))
