"""Tidy stored parcels the way loads now do (dry run unless --commit):

    python manage.py clean_parcels                 # every county: what would be removed
    python manage.py clean_parcels Pinal_AZ --commit

Removes slivers under 0.01 ac and keeps each identical outline once (records without a parcel number, condo-style
splits). Parcels with team work on them (status, notes, landowner, sites, QD or site plans) are never removed. Owner
totals on the remaining parcels refresh on the county's next load_parcels.
"""
from django.core.management.base import BaseCommand, CommandError

from crm.land.parcels import clean_stored, loaded_county


class Command(BaseCommand):
    help = "Remove sliver parcels and duplicate outlines from land search (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("county", nargs="?", help="One county, e.g. Pinal_AZ or Hays_TX (default: all)")
        parser.add_argument("--commit", action="store_true", help="Apply the changes")

    def handle(self, *args, **opts):
        county = None
        if opts["county"]:
            county = loaded_county(opts["county"].replace("_", " "))
            if county is None:
                raise CommandError(f"{opts['county']!r}: not a loaded county")
        report, removed = clean_stored(county, commit=opts["commit"])
        verb = "removed" if opts["commit"] else "to remove"
        for label, counts in sorted(report.items()):
            self.stdout.write(f"{label}: " + ", ".join(f"{n:,} {what}" + ("" if "kept" in what else f" {verb}")
                                                       for what, n in sorted(counts.items())))
        if not report:
            self.stdout.write("Nothing to tidy.")
        verb = "Removed" if opts["commit"] else "Would remove"
        self.stdout.write(f"{verb} {removed:,} parcels." + ("" if opts["commit"] else " Dry run: re-run with --commit."))
