"""Sale comps: find them around a parcel, a site or in a county, sum them up ($/acre median and quartiles), and the
team-only value range a parcel or site gets from them.

Prices only ever come from recorded sales (county records or what the team enters from a deed, broker or flyer),
never from asking prices or assessed values. Texas publishes no sale prices, so Texas comps are the team's own.
"""
import math
import statistics
from datetime import date, timedelta
from urllib.parse import urlencode

from django.db.models import Q

from ..models import LandCounty, LandSite, Parcel, SaleComp

DEFAULT_MILES = 15
RANGE_YEARS = 5  # value range: sales in the last 5 years, widened to WIDER_YEARS when there are too few
WIDER_YEARS = 10
ACRE_BAND = 3  # value range: comps from a third to three times the land's acres
MIN_COMPS = 3
TEXAS_NOTE = ("Texas is a non-disclosure state: appraisal districts don't publish sale prices, so the only Texas "
              "comps are the ones the team enters (from a deed, a broker or a flyer).")


def miles_between(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def site_center(site):
    """(lat, lon, acres, county) for a site: acre-weighted centre of its parcels; county = the largest parcel's."""
    parcels = list(site.parcels.select_related("county"))
    if not parcels:
        return None
    acres = sum(p.acres for p in parcels) or 1
    big = max(parcels, key=lambda p: p.acres)
    return (sum(p.lat * p.acres for p in parcels) / acres, sum(p.lon * p.acres for p in parcels) / acres,
            sum(p.acres for p in parcels), big.county)


def _float(raw):
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def find(*, lat=None, lon=None, miles=None, county=None, min_acres=None, max_acres=None, since=None, until=None,
         arms="", land_only=False, outside=False, exclude_parcels=()):
    """Comps matching the filters, nearest first when there's a centre (newest first otherwise). Each comp gets
    .miles (None when it has no location). With a centre and a radius, comps without a location count when they
    are in `county`. arms: '' leaves out sales marked not arms-length, 'confirmed' keeps only arms-length ones,
    'all' keeps everything. outside: leave out comps marked internal_only (for briefs)."""
    qs = SaleComp.objects.select_related("county", "parcel", "site")
    if arms == "confirmed":
        qs = qs.filter(arms_length=True)
    elif arms != "all":
        qs = qs.exclude(arms_length=False)
    if land_only:
        qs = qs.filter(improved=False)
    if outside:
        qs = qs.filter(internal_only=False)
    if min_acres:
        qs = qs.filter(acres__gte=min_acres)
    if max_acres:
        qs = qs.filter(acres__lte=max_acres)
    if since:
        qs = qs.filter(sale_date__gte=since)
    if until:
        qs = qs.filter(sale_date__lte=until)
    if exclude_parcels:
        qs = qs.exclude(parcel_id__in=list(exclude_parcels))
    if lat is not None and lon is not None and miles:
        dlat, dlon = miles / 69.0, miles / (69.0 * max(math.cos(math.radians(lat)), 0.1))
        near = Q(lat__gte=lat - dlat, lat__lte=lat + dlat, lon__gte=lon - dlon, lon__lte=lon + dlon)
        qs = qs.filter(near | Q(lat__isnull=True, county=county) if county else near)
    elif county:
        qs = qs.filter(county=county)
    out = []
    for c in qs:
        c.miles = miles_between(lat, lon, c.lat, c.lon) if lat is not None and c.lat is not None else None
        if c.miles is not None and miles and c.miles > miles:
            continue
        out.append(c)
    if lat is not None:
        out.sort(key=lambda c: (c.miles is None, c.miles or 0, -c.sale_date.toordinal()))
    return out


def summarize(comps):
    """Count, median and quartile $/acre, and the date span."""
    ppa = sorted(c.price_per_acre for c in comps if c.price_per_acre)
    if not ppa:
        return {"n": 0}
    q1, med, q3 = statistics.quantiles(ppa, n=4, method="inclusive") if len(ppa) > 1 else (ppa[0],) * 3
    dates = [c.sale_date for c in comps]
    return {"n": len(ppa), "median": round(med), "q1": round(q1), "q3": round(q3), "low": round(ppa[0]),
            "high": round(ppa[-1]), "since": min(dates), "until": max(dates)}


def from_params(params):
    """The comps page's filters (a QueryDict) -> (find kwargs, centre description dict)."""
    center = None
    parcel = Parcel.objects.select_related("county").filter(pk=params.get("parcel") or 0).first() \
        if str(params.get("parcel") or "").isdigit() else None
    site = LandSite.objects.filter(pk=params.get("site")).first() if str(params.get("site") or "").isdigit() else None
    county = LandCounty.objects.filter(fips=params.get("county")).first() if params.get("county") else None
    if parcel:
        center = {"lat": parcel.lat, "lon": parcel.lon, "label": parcel.label, "county": parcel.county,
                  "parcel": parcel, "acres": parcel.acres}
    elif site:
        c = site_center(site)
        if c:
            center = {"lat": c[0], "lon": c[1], "label": site.name, "county": c[3], "site": site, "acres": c[2]}
    miles = _float(params.get("radius")) or (DEFAULT_MILES if center else None)
    kwargs = {
        "lat": center["lat"] if center else None, "lon": center["lon"] if center else None,
        "miles": miles if center else None, "county": center["county"] if center else county,
        "min_acres": _float(params.get("min_acres")), "max_acres": _float(params.get("max_acres")),
        "since": _date(params.get("since")), "until": _date(params.get("until")),
        "arms": params.get("arms") if params.get("arms") in ("confirmed", "all") else "",
        "land_only": bool(params.get("land")),
    }
    if center is None and county is None:
        kwargs["county"] = None
    return kwargs, center, county


def value_range(*, lat, lon, acres, county, target=None, today=None):
    """Team-only $/acre range for land of `acres` at (lat, lon): low / base / high are the lower quartile, median
    and upper quartile of nearby arms-length land comps (within DEFAULT_MILES or the same county, a third to three
    times the acres, the last RANGE_YEARS years, or WIDER_YEARS when that gives too few). Under MIN_COMPS comps it
    says so instead. target: {'parcel': pk} or {'site': pk}, for the link to the comps it came from."""
    today = today or date.today()
    exclude = [target["parcel"]] if target and "parcel" in target else []
    band = (round(acres / ACRE_BAND, 1), round(acres * ACRE_BAND, 1))
    for years in (RANGE_YEARS, WIDER_YEARS):
        since = today - timedelta(days=round(365.25 * years))
        comps = find(lat=lat, lon=lon, miles=DEFAULT_MILES, county=county, min_acres=band[0], max_acres=band[1],
                     since=since, land_only=True, exclude_parcels=exclude)
        if len(comps) >= MIN_COMPS:
            break
    s = summarize(comps)
    params = {**(target or {}), "radius": DEFAULT_MILES, "min_acres": band[0], "max_acres": band[1],
              "since": since.isoformat(), "land": 1}
    out = {"enough": s["n"] >= MIN_COMPS, "n": s["n"], "years": years, "band": band, "miles": DEFAULT_MILES,
           "query": urlencode(params), "min_comps": MIN_COMPS,
           "texas": bool(county and county.state == "TX")}
    if out["enough"]:
        out.update(low=s["q1"], base=s["median"], high=s["q3"], since=s["since"], until=s["until"],
                   total_low=round(s["q1"] * acres), total_base=round(s["median"] * acres),
                   total_high=round(s["q3"] * acres))
    return out


def parcel_value_range(parcel, today=None):
    return value_range(lat=parcel.lat, lon=parcel.lon, acres=parcel.acres, county=parcel.county,
                       target={"parcel": parcel.pk}, today=today)


def site_value_range(site, today=None):
    c = site_center(site)
    if not c:
        return None
    return value_range(lat=c[0], lon=c[1], acres=c[2], county=c[3], target={"site": site.pk}, today=today)


def nearby(*, lat, lon, county, miles=DEFAULT_MILES, years=WIDER_YEARS, limit=6, outside=False, exclude_parcels=(),
           today=None):
    """The 'Sale comps nearby' panel: up to `limit` comps within `miles` from the last `years` years, nearest first,
    plus a summary of all of them."""
    today = today or date.today()
    comps = find(lat=lat, lon=lon, miles=miles, county=county, since=today - timedelta(days=round(365.25 * years)),
                 outside=outside, exclude_parcels=exclude_parcels)
    return {"comps": comps[:limit], "total": len(comps), "summary": summarize(comps), "miles": miles, "years": years,
            "texas": bool(county and county.state == "TX"), "texas_note": TEXAS_NOTE}
