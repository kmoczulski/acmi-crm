from django.core.management.base import BaseCommand

from crm.land.parcels import rescore


class Command(BaseCommand):
    help = "Re-score every parcel (after a buy-box change). --remeasure also recomputes distances to stored layers."

    def add_arguments(self, parser):
        parser.add_argument("--remeasure", action="store_true")

    def handle(self, *args, **opts):
        n = rescore(remeasure=opts["remeasure"], log=self.stdout.write)
        self.stdout.write(f"Scored {n:,} parcels")
