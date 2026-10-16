from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from crm.land import counties, zoning_ref
from crm.models import LandCounty, Parcel


def pct(n, total):
    return f"{100 * n / total:.0f}%" if total else "n/a"


class Command(BaseCommand):
    help = ("Coverage report for a loaded county: how many parcels have owners, mailing addresses, values, zoning, and "
            "which zoning codes have no definition in the zoning reference. Run it after load_parcels. "
            "Example: check_county Hoke_NC")

    def add_arguments(self, parser):
        parser.add_argument("county", help="County, e.g. Hoke_NC, Pinal_AZ")
        parser.add_argument("--samples", type=int, default=3, help="Largest parcels to print (default 3)")

    def handle(self, *args, **opts):
        cfg = counties.find(opts["county"].replace("_", " "))
        county = cfg and LandCounty.objects.filter(fips=cfg["fips"]).first()
        if not county:
            raise CommandError(f"{opts['county']!r}: not a configured, loaded county (add it to counties.py, run load_parcels)")
        out = self.stdout.write
        qs = Parcel.objects.filter(county=county)
        total = qs.count()
        header = (f"{county.name} County, {county.state} (FIPS {county.fips}): {total:,} parcels stored, "
                  f"source: {county.source or 'not recorded'}")
        out(header + (f", loaded {county.loaded_at:%Y-%m-%d}" if county.loaded_at else ""))
        if not total:
            return
        acres = qs.values_list("acres", flat=True)
        out(f"Acres: {sum(acres):,.0f} total, largest {max(acres):,.0f}, smallest {min(acres):,.2f}; "
            f"{qs.filter(acres__lt=county.min_acres).count():,} under the {county.min_acres:g} ac cutoff (loaded via full windows)")
        rows = [
            ("Owner name", qs.exclude(owner_name="")), ("Mailing address", qs.exclude(mail_address="")),
            ("Mailing state", qs.exclude(mail_state="")), ("Site address", qs.exclude(situs_address="")),
            ("Assessed value", qs.exclude(market_value=None) | qs.exclude(land_value=None)),
            ("Land use code", qs.exclude(state_land_use="")), ("Zoning", qs.exclude(zoning="")),
            ("City limits", qs.exclude(in_city="")), ("Live checks run", qs.exclude(checked_at=None)),
        ]
        for label, sub in rows:
            n = sub.count()
            flag = "   <-- check" if label in ("Owner name", "Mailing address") and total and n / total < 0.9 else ""
            out(f"  {label:<16}{n:>8,}  {pct(n, total):>5}{flag}")
        if county.region == "":
            out("  WARNING: no region set for this county (add it to STATE_COUNTY_REGIONS in sources.py)")
        keys = qs.values_list("key", flat=True)
        weak = [k for k in keys if not k or k.upper().startswith(("NAP", "PT-", "OID"))]
        if weak:
            out(f"  {len(weak):,} parcels keyed by object id / point (no parcel number): {', '.join(weak[:3])} ...")

        # zoning codes and whether the reference defines them
        codes = Counter()
        authority = {}
        unbucketed = Counter()
        for p in qs.exclude(zoning="").only("zoning", "overlays", "zoning_class", "county").iterator():
            code = p.zoning.split(" / ")[0].strip()
            codes[code] += 1
            authority[code] = ((p.overlays or {}).get("zoning") or {}).get("authority", "")
            if not p.zoning_class:
                unbucketed[code] += 1
        if codes:
            missing = []
            for code, n in codes.most_common():
                p = qs.filter(zoning=code).first() or qs.filter(zoning__startswith=code + " / ").first()
                key = zoning_ref.jurisdiction_key(p)
                data, district = zoning_ref.find(key, code) if key else (None, None)
                if not district:
                    missing.append(f"{code} ({n:,}{', ' + authority[code] if authority[code] else ''})")
            out(f"Zoning codes: {len(codes)} distinct. No definition in zoning_ref for {len(missing)}: "
                + (", ".join(missing[:25]) + (" ..." if len(missing) > 25 else "") if missing else "none"))
            if unbucketed:
                out("  Codes with no scoring bucket (score.ZONING_CODES): "
                    + ", ".join(f"{c} ({n:,})" for c, n in unbucketed.most_common(15)))
        else:
            out("Zoning: none stored (no zoning layer for this county; see zoning_ref.NO_LAYER).")
        for p in qs.order_by("-acres")[:opts["samples"]]:
            out(f"  e.g. {p.key}: {p.acres:,.0f} ac, owner {p.owner_name or '(none)'}, mails to "
                f"{', '.join(x for x in (p.mail_city, p.mail_state) if x) or '(none)'}, zoning {p.zoning or '(none)'}, score {p.score}")
