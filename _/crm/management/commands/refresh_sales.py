from django.core.management.base import BaseCommand, CommandError

from crm.land import portfolio, sales, signals
from crm.land.parcels import loaded_county
from crm.models import LandCounty


class Command(BaseCommand):
    help = ("Fill last-transfer data (and Charles's mailing addresses and values) on stored parcels from each county's "
            "sale source, without reloading geometry; then re-work the off-market signals, county sale comps and owner "
            "groups. Dry run unless --commit. Example: refresh_sales Charles Hays --commit, or refresh_sales --all")

    def add_arguments(self, parser):
        parser.add_argument("counties", nargs="*", help="County names ('Charles', 'Hays TX', 'Pinal AZ') or FIPS")
        parser.add_argument("--all", action="store_true", help="Every stored county")
        parser.add_argument("--commit", action="store_true", help="Write the changes (default: only report them)")

    def handle(self, *args, **opts):
        if opts["all"]:
            picked = list(LandCounty.objects.filter(parcels__isnull=False).distinct().order_by("state", "name"))
        else:
            picked = []
            for name in opts["counties"]:
                county = loaded_county(name.replace("_", " "))
                if county is None:
                    raise CommandError(f"No stored county {name!r}")
                picked.append(county)
        if not picked:
            raise CommandError("Name the counties, or use --all")
        for county in picked:
            if not sales.source_for(county):
                note = " (Texas publishes no sale prices)" if county.state == "TX" else ""
                self.stdout.write(f"{county.name} {county.state}: no published sale or deed data{note}")
                if opts["commit"]:
                    self.stdout.write(f"  signals updated on {signals.apply_county(county):,} parcels")
                continue
            stats = sales.refresh_county(county, commit=opts["commit"], log=self.stdout.write, rebuild_groups=False)
            fields = ", ".join(f"{f} {n:,}" for f, n in sorted(stats["fields"].items())) or "nothing"
            self.stdout.write(f"  {stats['records']:,} source records; {stats['matched']:,} of {stats['parcels']:,} "
                              f"stored parcels matched; {stats['changed']:,} would change ({fields}); then "
                              f"{stats['with_date']:,} would have a transfer date, {stats['with_price']:,} a price"
                              if not opts["commit"] else
                              f"  {stats['changed']:,} parcels changed ({fields}); {stats['with_date']:,} have a "
                              f"transfer date, {stats['with_price']:,} a price; {stats.get('comps', 0):,} county comps")
        if opts["commit"]:
            portfolio.rebuild(log=self.stdout.write)
        else:
            self.stdout.write("Dry run: nothing written. Add --commit to save.")
