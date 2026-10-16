from django.core.management.base import BaseCommand, CommandError

from crm.land.parcels import load_county
from crm.land.sources import txgio_parcel_catalog


class Command(BaseCommand):
    help = "Load Texas county parcels (TxGIO StratMap) of 20+ acres. Example: load_parcels Hays Caldwell"

    def add_arguments(self, parser):
        parser.add_argument("counties", nargs="+", help="County names or FIPS codes")
        parser.add_argument("--min-acres", type=float, default=20)
        parser.add_argument("--keep-zip", action="store_true", help="Keep the downloaded zip in data/land/raw")
        parser.add_argument("--force-prune", action="store_true",
                            help="Remove parcels missing from the source even when far fewer came back than are stored "
                                 "(normally that is treated as a partial download and nothing is removed)")

    def handle(self, *args, **opts):
        catalog = txgio_parcel_catalog()
        for name in opts["counties"]:
            try:
                load_county(name.replace("_", " "), min_acres=opts["min_acres"], keep_zip=opts["keep_zip"],
                            catalog=catalog, log=self.stdout.write, force_prune=opts["force_prune"])
            except ValueError as exc:
                raise CommandError(str(exc))
