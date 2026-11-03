from django.core.management.base import BaseCommand, CommandError

from crm.land import counties
from crm.land.owners import join_owners
from crm.models import LandCounty


class Command(BaseCommand):
    help = ("Add owner / mailing / value data from a file (assessor export, Regrid CSV...) to a county's loaded parcels, "
            "matched by parcel number. Dry run unless --commit. Example: load_owners Hoke_NC hoke.csv --commit")

    def add_arguments(self, parser):
        parser.add_argument("county", help="County, e.g. Hoke_NC, Greene_IN, Pinal_AZ")
        parser.add_argument("file", help=".csv, .xlsx, HTML-table .xls, .json or .geojson")
        parser.add_argument("--map", action="append", default=[], metavar="FIELD=COLUMN",
                            help="Name a column when it isn't recognised (key, owner_name, mail_address, mail_city, "
                                 "mail_state, mail_zip, land_value, market_value, acres...); repeatable")
        parser.add_argument("--commit", action="store_true", help="Write the changes (default: report only)")

    def handle(self, *args, **opts):
        cfg = counties.find(opts["county"].replace("_", " "))
        county = cfg and LandCounty.objects.filter(fips=cfg["fips"]).first()
        if not county:
            raise CommandError(f"{opts['county']!r}: no loaded county of that name (run load_parcels first)")
        overrides = {}
        for item in opts["map"]:
            field, _, column = item.partition("=")
            if not column:
                raise CommandError(f"--map expects FIELD=COLUMN, got {item!r}")
            overrides[field.strip()] = column.strip()
        try:
            stats = join_owners(county, opts["file"], overrides=overrides, commit=opts["commit"], log=self.stdout.write)
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"{county.name}: {stats['rows']:,} rows, {stats['matched']:,} matched to loaded parcels "
                          f"({stats['parcels']:,} stored), {stats['unmatched_rows']:,} rows with no stored parcel "
                          f"(expected for parcels under the size cutoff)")
        self.stdout.write(f"{stats['parcels_changed']:,} parcels would change; owners on file before: {stats['owners_before']:,}")
        for s in stats["samples"]:
            self.stdout.write(f"  e.g. {s}")
        if opts["commit"]:
            self.stdout.write(f"Saved. Owners on file now: {stats['owners_after']:,}")
        else:
            self.stdout.write("Dry run: add --commit to save.")
