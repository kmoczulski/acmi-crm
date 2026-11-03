"""Which land fits a tenant's program: each candidate site checked against the deal's acres and MW and the tenant's
explosives (QD) cases, rail and gas needs, then ranked.

Distances use the bands of the buy-box score (crm/land/score.py). The QD check places the tenant's explosives
buildings on the site's outline with the QD planner (crm/land/qd.py, the same engine as the site planner) and asks
whether every inhabited building distance (IBD) arc stays on the property; arcs that run off it need buyouts or
easements.
"""
import logging

from ..land import qd, trim
from ..land.score import substation_name
from ..land.tracker import site_deals
from ..models import LandSite

log = logging.getLogger(__name__)

WEIGHTS = {"acres": 30, "qd": 30, "power": 20, "rail": 10, "gas": 5, "ready": 5}
READY = {"acquired": 1.0, "contract": 0.8, "loi": 0.5, "outreach": 0.3, "review": 0.2, "prospect": 0.1}
BIG_LOAD_MW = 10  # at or above this, the load wants a transmission line close by, not just a substation


def buildings(needs):
    """The QD planner's building dicts, with their distances, for the needs' explosives cases (bad rows skipped)."""
    out = []
    for c in (needs.qd_cases if needs else None) or []:
        try:
            b = {"name": c.get("name") or "Explosives building", "hd": c.get("hd") or "HD 1.1",
                 "newqd": float(c.get("newqd") or 0), "pes": "Aboveground structure",
                 "kind": c.get("kind") or "operating", "sqft": float(c.get("sqft") or 0)}
            if b["newqd"] <= 0:
                continue
            b["dist"] = qd.building_distances(b)
        except (TypeError, ValueError):
            continue
        out.append(b)
    return out


def _nearest(parcels, layer):
    """(miles, feature) for the stored layer's nearest feature over the site's parcels; (None, {}) if unknown."""
    best = (None, {})
    for p in parcels:
        n = (p.near or {}).get(layer)
        if n and n.get("miles") is not None and (best[0] is None or n["miles"] < best[0]):
            best = (n["miles"], n)
    return best


def _band(miles, bands):
    """The score fraction for a distance: the first band it falls in, else 0."""
    if miles is None:
        return 0
    return next((f for limit, f in bands if miles <= limit), 0)


def _acres(deals):
    """(gross, net usable): net from the parcels' live checks, scaled to the trimmed portion; gross where unknown."""
    gross = net = 0.0
    known = True
    for d in deals:
        gross += d.acres or 0
        nu = ((d.parcel.checks or {}).get("net_usable") or {}).get("acres")
        if nu is not None and d.parcel.acres:
            net += (d.acres or 0) * min(1.0, float(nu) / float(d.parcel.acres))
        else:
            net += d.acres or 0
            known = False
    return round(gross, 1), round(net, 1), known


def qd_check(site, blds):
    """{"ok", "frac", "detail", "summary"}: lay the explosives buildings out on the site; do the IBD arcs stay on it?"""
    try:
        geom = trim.site_geometry(site)
    except Exception:  # noqa: BLE001 - a bad outline shouldn't break the ranking
        log.warning("No outline for site %s", site.pk, exc_info=True)
        geom = None
    if geom is None or geom.is_empty:
        return {"ok": None, "frac": 0, "detail": "no outline to test", "summary": None}
    c = geom.centroid
    to_m, _, _ = qd.projector(c.y, c.x)
    parcel_m = to_m(geom)
    results, summary = qd.evaluate(parcel_m, blds, qd.fit(parcel_m, blds))
    widest = max(b["dist"]["ibd"] for b in blds)
    if summary["all_clear"]:
        detail = (f"arcs close on the site (widest IBD {widest:,.0f} ft); {summary['encumbered_acres']:,.0f} ac "
                  f"inside arcs, {summary['unencumbered_acres']:,.0f} ac free")
        return {"ok": True, "frac": 1.0, "detail": detail, "summary": summary}
    off = summary.get("off_site_acres") or 0
    conflicts = sum(len(r["conflicts"]) for r in results) // 2
    detail = (f"IBD arcs run {off:,.0f} ac off the site (widest {widest:,.0f} ft)" if off >= 1 else
              "buildings too close together to meet their separations")
    if conflicts and off >= 1:
        detail += "; buildings also too close together"
    frac = max(0.0, 1 - off / summary["arc_acres"]) if summary["arc_acres"] else 0
    return {"ok": False, "frac": round(frac * 0.8, 2), "detail": detail, "summary": summary}


def evaluate(site, opp, needs, blds=None):
    """One site against the program: [{"key", "label", "ok", "detail"}] checks, a 0-100 score, and the counts."""
    deals = [d for d in site_deals(site) if d.included]
    parcels = [d.parcel for d in deals]
    gross, net, net_known = _acres(deals)
    checks, earned, weight = [], 0.0, 0

    def check(key, label, ok, detail, frac):
        nonlocal earned, weight
        checks.append({"key": key, "label": label, "ok": ok, "detail": detail})
        earned += WEIGHTS[key] * frac
        weight += WEIGHTS[key]

    if opp.acreage_need:
        need = float(opp.acreage_need)
        what = "net usable" if net_known else "acres (net usable not checked yet)"
        check("acres", "Acres", net >= need, f"{net:,.0f} {what} of {need:,.0f} needed", min(1.0, net / need))
    blds = buildings(needs) if blds is None else blds
    if blds:
        q = qd_check(site, blds)
        check("qd", "Explosives arcs", q["ok"], q["detail"], q["frac"])
    if opp.power_need_mw:
        mw = float(opp.power_need_mw)
        t_mi, t = _nearest(parcels, "transmission")
        s_mi, s = _nearest(parcels, "substation")
        bits = []
        if t_mi is not None:
            kv = t.get("VOLT_CLASS") or t.get("VOLTAGE")
            bits.append(f"{t_mi:.1f} mi to a {kv} kV line" if kv else f"{t_mi:.1f} mi to a transmission line")
        if s_mi is not None:
            volt = s.get("MAX_VOLTAG")
            bits.append(f"{s_mi:.1f} mi to {substation_name(s)} substation"
                        + (f" ({volt:g} kV)" if isinstance(volt, (int, float)) and volt > 0 else ""))
        if mw >= BIG_LOAD_MW:
            frac = _band(t_mi, [(1, 1.0), (3, 0.7), (6, 0.3)])
            ok = None if t_mi is None else t_mi <= 3
        else:
            frac = max(_band(s_mi, [(2, 1.0), (5, 0.6)]), _band(t_mi, [(1, 1.0), (3, 0.7), (6, 0.3)]))
            ok = None if s_mi is None and t_mi is None else frac >= 0.6
        check("power", f"Power ({mw:g} MW)", ok, "; ".join(bits) or "power lines not checked yet", frac)
    if needs and needs.needs_rail:
        r_mi, r = _nearest(parcels, "rail")
        frac = _band(r_mi, [(1, 1.0), (3, 0.5), (6, 0.2)])
        ok = None if r_mi is None else (True if r_mi <= 1 else (None if r_mi <= 3 else False))
        detail = (f"{r_mi:.1f} mi to {r.get('name') or 'a rail line'}" + (" (a spur to build)" if r_mi > 1 else "")
                  if r_mi is not None else "rail not checked yet")
        check("rail", "Rail", ok, detail, frac)
    if needs and needs.needs_gas:
        g_mi, g = _nearest(parcels, "gas_pipeline")
        frac = _band(g_mi, [(1, 1.0), (3, 0.5)])
        ok = None if g_mi is None else g_mi <= 3
        check("gas", "Natural gas", ok, f"{g_mi:.1f} mi to a {g.get('name') or 'gas'} pipeline" if g_mi is not None
              else "pipelines not checked yet", frac)
    ready = READY.get(site.status, 0)
    # Not controlling a site yet makes it slower, not unfit: under contract or owned is a pass, anything else neutral.
    check("ready", "Control", True if ready >= 0.8 else None, site.get_status_display(), ready)
    score = round(100 * earned / weight) if weight else 0
    return {"site": site, "checks": checks, "score": score, "gross": gross, "net": net,
            "fails": sum(c["ok"] is False for c in checks), "campus_match": bool(opp.campus_id and
                                                                                  site.campus_id == opp.campus_id)}


def rank(opp, needs=None, everywhere=False):
    """Every site with land that isn't dropped, best fit first: fewest failed checks, then score; the deal's campus
    first. With a campus on the deal, only that campus's sites unless `everywhere` (or the campus has none)."""
    sites = LandSite.objects.exclude(status=LandSite.Status.DROPPED).select_related("campus")
    if opp.campus_id and not everywhere and sites.filter(campus=opp.campus_id).exists():
        sites = sites.filter(campus=opp.campus_id)
    blds = buildings(needs)
    rows = [evaluate(s, opp, needs, blds) for s in sites]
    rows = [r for r in rows if r["gross"] > 0]  # a site with no parcels yet has nothing to test
    return sorted(rows, key=lambda r: (r["fails"], not r["campus_match"], -r["score"], r["site"].name.lower()))
