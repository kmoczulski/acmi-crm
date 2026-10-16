from django.core.management.base import BaseCommand

from crm.land.layers import load_layers
from crm.land.sources import STORED_LAYERS


class Command(BaseCommand):
    help = "Copy bases, power lines, substations, gas pipelines, rail, highways and city limits for the Texas Triangle."

    def add_arguments(self, parser):
        parser.add_argument("layers", nargs="*", help="Default: all")

    def handle(self, *args, **opts):
        load_layers(opts["layers"] or None, log=self.stdout.write)
