"""Per-parcel deal tracking inside a site, and the site's global tracker (rolled up by acres and stage).

An assemblage closes owner by owner, so each parcel in a site has its own SiteParcelDeal (status, milestone
dates, notes). The global tracker shows how many acres sit at each stage, the furthest and the slowest parcel,
and every parcel's milestones side by side, grouped by owner.
"""
from collections import OrderedDict

from ..models import LandSite, SiteParcelDeal

STAGE_ORDER = LandSite.STATUS_ORDER  # furthest first; dropped last
STAGE_LABEL = dict(LandSite.Status.choices)


def _new_deal(site, parcel, single_owner):
    """A parcel's starting deal: single-owner sites copy the site's status and dates (one negotiation); otherwise
    it starts at "Under review"."""
    return SiteParcelDeal(site=site, parcel=parcel,
                          status=site.status if single_owner else LandSite.Status.REVIEW,
                          milestones=dict(site.milestones or {}) if single_owner else {})


def _single_owner(parcels):
    return len({(p.owner_name or "").strip().upper() for p in parcels}) == 1


def ensure_deals(site):
    """Write side: give every parcel in the site a deal row and drop the rows of parcels no longer in it. Call it
    after changing a site's parcels; pages read site_deals() and never write."""
    parcels = list(site.parcels.all())
    have = set(site.parcel_deals.values_list("parcel_id", flat=True))
    single_owner = _single_owner(parcels)
    new = [_new_deal(site, p, single_owner) for p in parcels if p.pk not in have]
    if new:
        SiteParcelDeal.objects.bulk_create(new)
    site.parcel_deals.exclude(parcel__in=parcels).delete()


def apply_milestones(target, dates, user=None, why="", log=True):
    """Set milestone dates on a site, or on one parcel's deal, from elsewhere (a contract: crm/contracts/dates.py).
    Logs each change on the site (unless `log` is off: a whole-site contract logs once, on the site) and moves the
    status forward to match, never back; a dropped site stays dropped. Returns how many dates changed."""
    from ..models import LandSiteEvent

    site = target if isinstance(target, LandSite) else target.site
    labels = {key: label for key, label, _ in LandSite.MILESTONES}
    whose = "" if isinstance(target, LandSite) else f" ({(target.parcel.owner_name or 'parcel').strip().title()})"
    note = f" ({why})" if why else ""
    milestones, changed = dict(target.milestones or {}), 0
    for key, when in dates.items():
        if key in labels and milestones.get(key) != when.isoformat():
            milestones[key] = when.isoformat()
            changed += 1
            if log:
                LandSiteEvent.objects.create(site=site, user=user, kind="milestone",
                                             text=f"{labels[key]}{whose}: {when:%b} {when.day}, {when.year}{note}"[:500])
    if not changed:
        return 0
    target.milestones = milestones
    fields = ["milestones", "updated_at"]
    implied = target.status_for_milestones()
    if implied and target.status != LandSite.Status.DROPPED and STAGE_ORDER.index(implied) < STAGE_ORDER.index(target.status):
        old = STAGE_LABEL.get(target.status, target.status)
        target.status = implied
        fields.append("status")
        if log:
            LandSiteEvent.objects.create(site=site, user=user, kind="status",
                                         text=f"Status{whose} {old} → {STAGE_LABEL[implied]}{note}"[:500])
    target.save(update_fields=fields)
    return changed


def missing_deals(site):
    """True when some parcel in the site has no deal row yet (sites saved before deal rows were made on write)."""
    return site.parcels.exclude(pk__in=site.parcel_deals.values("parcel_id")).exists()


def site_deals(site):
    """Read side: the deal of every parcel now in the site. Stored rows, plus an unsaved starting deal for a parcel
    that has no row yet (what ensure_deals would create), so reading a site never writes to the database."""
    parcels = list(site.parcels.select_related("county"))
    stored = {d.parcel_id: d for d in site.parcel_deals.select_related("parcel", "parcel__county")}
    single_owner = _single_owner(parcels)
    return [stored.get(p.pk) or _new_deal(site, p, single_owner) for p in parcels]


def summary(site):
    """Global tracker for a site."""
    every = site_deals(site)
    # Excluded parcels stay listed (greyed) but count for nothing; trimmed parcels count their portion.
    deals = [d for d in every if d.included]
    total = sum(d.acres for d in deals) or 0
    by_stage = OrderedDict()
    for stage in STAGE_ORDER:
        hits = [d for d in deals if d.status == stage]
        if hits:
            acres = sum(d.acres for d in hits)
            by_stage[stage] = {"stage": stage, "label": STAGE_LABEL[stage], "parcels": len(hits),
                               "acres": round(acres, 1), "pct": round(100 * acres / total, 1) if total else 0,
                               "owners": sorted({(d.parcel.owner_name or "Unknown").title() for d in hits})}
    live = [d for d in deals if d.status != "dropped"]
    furthest = min(live, key=lambda d: STAGE_ORDER.index(d.status), default=None)
    slowest = max(live, key=lambda d: STAGE_ORDER.index(d.status), default=None)
    # Owner groups: parcels of one owner negotiate together, so the grid shows them together.
    owners = OrderedDict()
    for d in sorted(every, key=lambda d: (not d.included, STAGE_ORDER.index(d.status),
                                          (d.parcel.owner_name or "").upper(), -d.parcel.acres)):
        key = (d.parcel.owner_name or "Unknown owner").strip().upper()
        g = owners.setdefault(key, {"owner": (d.parcel.owner_name or "Unknown owner").title(), "deals": [], "acres": 0})
        g["deals"].append({"deal": d, "milestones": d.milestone_rows()})
        if d.included:
            g["acres"] = round(g["acres"] + d.acres, 1)
    # Earliest / latest date per milestone across parcels.
    span = []
    for key, label, _ in LandSite.MILESTONES:
        dates = sorted(d.milestones[key] for d in deals if (d.milestones or {}).get(key))
        span.append({"key": key, "label": label, "first": dates[0] if dates else None,
                     "last": dates[-1] if dates else None, "count": len(dates)})
    secured = sum(d.acres for d in deals if d.status in ("contract", "acquired"))
    return {
        "deals": every, "included": deals, "excluded": [d for d in every if not d.included],
        "trimmed": [d for d in deals if d.trimmed], "owners": list(owners.values()), "by_stage": list(by_stage.values()),
        "total_acres": round(total, 1), "parcels": len(deals), "all_parcels": len(every),
        "owner_count": len({(d.parcel.owner_name or "Unknown owner").strip().upper() for d in deals}),
        "secured_acres": round(secured, 1), "secured_pct": round(100 * secured / total, 1) if total else 0,
        "furthest": furthest, "slowest": slowest, "span": span,
        "is_assemblage": len(owners) > 1 or len(every) > 1,
        "suggested_status": _suggested(live),
    }


def _suggested(live):
    """The site is only as far along as its slowest live parcel (all parcels are needed for the assemblage)."""
    if not live:
        return None
    return max(live, key=lambda d: STAGE_ORDER.index(d.status)).status


def progress(site):
    """Compact acres-by-stage bar for list pages: [(stage, label, pct, acres, parcels)]."""
    deals = list(site.parcel_deals.select_related("parcel").filter(included=True))
    if not deals:
        return []
    total = sum(d.acres for d in deals) or 1
    out = []
    for stage in STAGE_ORDER:
        hits = [d for d in deals if d.status == stage]
        if hits:
            acres = sum(d.acres for d in hits)
            out.append({"stage": stage, "label": STAGE_LABEL[stage], "pct": round(100 * acres / total, 1),
                        "acres": round(acres, 1), "parcels": len(hits)})
    return out
