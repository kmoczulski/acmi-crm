"""Per-parcel deal tracking inside a site, and the site's global tracker (rolled up by acres and stage).

An assemblage closes owner by owner, so each parcel in a site has its own SiteParcelDeal (status, milestone
dates, notes). The global tracker shows how many acres sit at each stage, the furthest and the slowest parcel,
and every parcel's milestones side by side, grouped by owner.
"""
from collections import OrderedDict

from ..models import LandSite, SiteParcelDeal

STAGE_ORDER = LandSite.STATUS_ORDER  # furthest first; dropped last
STAGE_LABEL = dict(LandSite.Status.choices)


def ensure_deals(site):
    """Make sure every parcel in the site has a deal row. Single-owner sites copy the site's status and dates
    (one negotiation); other new rows start at "Under review"."""
    parcels = list(site.parcels.all())
    have = set(site.parcel_deals.values_list("parcel_id", flat=True))
    owners = {(p.owner_name or "").strip().upper() for p in parcels}
    single_owner = len(owners) == 1
    new = [SiteParcelDeal(site=site, parcel=p,
                          status=site.status if single_owner else LandSite.Status.REVIEW,
                          milestones=dict(site.milestones or {}) if single_owner else {})
           for p in parcels if p.pk not in have]
    if new:
        SiteParcelDeal.objects.bulk_create(new)
    # Parcels removed from the site lose their deal row.
    site.parcel_deals.exclude(parcel__in=parcels).delete()


def summary(site):
    """Global tracker for a site."""
    ensure_deals(site)
    every = list(site.parcel_deals.select_related("parcel", "parcel__county"))
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
