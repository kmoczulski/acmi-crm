"""Create prospective land sites (assemblages) from a JSON file, or top up existing ones.

    manage.py import_land_sites data/imports/meih_land_sites.json [--dry-run] [--overwrite]

File: {"campus": "MEIH", "county": "Charles", "sites": [{"name", "status", "project"?, "accounts": [...],
"notes", "milestones"?: {"loi_sent": "YYYY-MM-DD", ...}, "county"?}]}. Parcels missing from land search are
fetched from the county by account number, whatever their size. Sites are matched by name.

Existing sites keep what the team has edited (status, notes, campus/project, dates already set) and only gain
parcels and milestone dates they don't have yet; --overwrite replaces everything from the file.
"""
import json

from django.core.management.base import BaseCommand
from django.db import transaction

from crm.land.parcels import ensure_parcels, normalize_account
from crm.land.tracker import ensure_deals
from crm.models import Campus, LandSite, LandSiteEvent, Project


class Command(BaseCommand):
    help = "Create or top up land sites (assemblages) from a JSON file."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--overwrite", action="store_true", help="Replace status, notes and dates from the file")

    def handle(self, *args, **opts):
        data = json.load(open(opts["path"], encoding="utf-8"))
        campus = Campus.objects.filter(code=data.get("campus")).first() if data.get("campus") else None
        default_county = data.get("county")
        statuses = set(LandSite.Status.values)
        milestone_keys = {k for k, _, _ in LandSite.MILESTONES}
        for spec in data["sites"]:
            county = spec.get("county", default_county)
            accounts = spec.get("accounts") or []
            parcels = ensure_parcels(county, accounts, log=self.stdout.write) if accounts and county else {}
            keys = [normalize_account("24017", a) for a in accounts]
            found = [parcels[k] for k in keys if k in parcels]
            missing = [k for k in keys if k not in parcels]
            status = spec.get("status", "prospect")
            if status not in statuses:
                raise ValueError(f"{spec['name']}: unknown status {status!r}")
            dates = {k: v for k, v in (spec.get("milestones") or {}).items() if k in milestone_keys}
            project = Project.objects.filter(name=spec["project"]).first() if spec.get("project") else None
            self.stdout.write(f"{spec['name']}: {len(found)} parcels, {sum(p.acres for p in found):,.1f} ac, {status}"
                              + (f"  (missing {', '.join(missing)})" if missing else ""))
            if opts["dry_run"]:
                continue
            with transaction.atomic():
                site = LandSite.objects.filter(name=spec["name"]).first()
                created = site is None
                if created or opts["overwrite"]:
                    site = site or LandSite(name=spec["name"])
                    site.campus = campus or (project.campus if project else None)
                    site.project = project
                    site.status = status
                    site.notes = spec.get("notes", "")
                    site.milestones = {**(site.milestones or {}), **dates} if not opts["overwrite"] else dates
                    site.save()
                    site.parcels.set(found)
                    LandSiteEvent.objects.create(site=site, kind="edit",
                                                 text="Imported from the shared drive" if created else "Re-imported (overwrite)")
                    self._parcel_deals(site, spec.get("parcel_deals") or [], opts["overwrite"])
                else:
                    added = {k: v for k, v in dates.items() if not (site.milestones or {}).get(k)}
                    if added:
                        site.milestones = {**(site.milestones or {}), **added}
                        site.save(update_fields=["milestones", "updated_at"])
                        LandSiteEvent.objects.create(
                            site=site, kind="milestone",
                            text="Dates from the shared drive files: " + ", ".join(
                                f"{dict((k, l) for k, l, _ in LandSite.MILESTONES)[k]} {v}" for k, v in added.items()))
                    new_parcels = [p for p in found if not site.parcels.filter(pk=p.pk).exists()]
                    if new_parcels:
                        site.parcels.add(*new_parcels)
                self._parcel_deals(site, spec.get("parcel_deals") or [], opts["overwrite"])

    def _parcel_deals(self, site, specs, overwrite):
        """Per-parcel deals: [{accounts, status, milestones, notes}]. Only fills parcels nobody has edited yet
        (still "Under review" with no dates or notes), unless --overwrite."""
        if not specs:
            return
        ensure_deals(site)
        deals = {d.parcel.key: d for d in site.parcel_deals.select_related("parcel")}
        changed = 0
        for spec in specs:
            for acct in spec.get("accounts") or []:
                d = deals.get(normalize_account("24017", acct))
                if d is None:
                    continue
                untouched = d.status == "review" and not d.milestones and not d.notes
                if not (untouched or overwrite):
                    continue
                d.status = spec.get("status", d.status)
                d.milestones = spec.get("milestones") or {}
                d.notes = spec.get("notes", "")
                d.save()
                changed += 1
        if changed:
            LandSiteEvent.objects.create(site=site, kind="parcel",
                                         text=f"Per-parcel deal status for {changed} parcels from the shared drive files")
