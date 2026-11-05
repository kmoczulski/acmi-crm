from django.core.management.base import BaseCommand

from crm.land.layers import load_layers
from crm.land.sources import STORED_LAYERS


class Command(BaseCommand):
    help = "Copy bases, power lines, substations, gas pipelines, rail, highways and city limits for each region."

    def add_arguments(self, parser):
        parser.add_argument("layers", nargs="*", help="Default: all")
        parser.add_argument("--region", action="append", dest="regions",
                            help="Only refresh this region (texas_triangle, southern_md, north_al, fayetteville_nc, central_az, crane_in, south_fl, emery_ut, oahu_hi); repeatable")

    def handle(self, *args, **opts):
        load_layers(opts["layers"] or None, regions=opts["regions"], log=self.stdout.write)
