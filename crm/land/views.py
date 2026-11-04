"""Land search pages: map, parcel card, live lookups, review (Watch / Pursue / Pass), search and export."""
import csv
import json

from django.contrib import messages
from django.db.models import F, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from shapely.geometry import shape

from ..entitlements import logic as entitlement_logic
from ..models import Campus, LandCounty, LandSite, Parcel, Project
from ..permissions import is_manager, manager_required, refuse, team_users
from . import diligence, layers, score, signals, zoning_ref
from .lookup import identify, run_checks
from .sources import LIMIT_LAYERS, STORED_LAYERS

MAX_SHAPES = 2500
# List order on the map: the buy-box score, or the separate off-market motivation / owner-group size.
SORTS = {"": ("-score", "-acres"), "motivation": ("-motivation", "-score", "-acres"),
         "portfolio": ("-portfolio_acres", "-score", "-acres")}
LAND_USE_GROUPS = {
    "industrial": ("F2",),
    "ag": ("D", "E"),
    "commercial": ("F1", "C"),
}


def _filtered(params):
    """Parcels matching the filter query string (shared by the map, the list and the export)."""
    qs = Parcel.objects.select_related("county")
    counties = [c for c in params.getlist("county") if c]
    if counties:
        qs = qs.filter(county__fips__in=counties)
    for name, lookup in (("min_acres", "acres__gte"), ("max_acres", "acres__lte"), ("min_score", "score__gte")):
        try:
            if params.get(name):
                qs = qs.filter(**{lookup: float(params[name])})
        except ValueError:
            pass
    uses = [u for u in params.getlist("use") if u in LAND_USE_GROUPS]
    if uses:
        cond = Q()
        for u in uses:
            for code in LAND_USE_GROUPS[u]:
                cond |= Q(state_land_use__startswith=code) | Q(state_land_use__contains="," + code)
        qs = qs.filter(cond)
    zclasses = [z for z in params.getlist("zoning") if z in score.ZONING_LABELS]
    if zclasses:
        qs = qs.filter(zoning_class__in=zclasses)
    if params.get("unincorporated"):
        qs = qs.filter(in_city="")
    if params.get("out_of_state"):  # the owner mails from another state than the land's
        qs = qs.exclude(mail_state="").exclude(mail_state=F("county__state"))
    sigs = [s for s in params.getlist("signal") if s in dict(signals.FILTERS)]
    if sigs:
        qs = qs.filter(signals.filter_q(sigs))
    if params.get("portfolio"):
        qs = qs.filter(portfolio_parcels__gte=2)
    if params.get("no_killers"):
        qs = qs.filter(killers=[])
    status = params.get("status", "open")
    if status == "open":
        qs = qs.exclude(status=Parcel.Status.PASS)
    elif status == "new":
        qs = qs.filter(status="")
    elif status in Parcel.Status.values:
        qs = qs.filter(status=status)
    try:
        if params.get("max_ppa"):
            ids = [p.pk for p in qs.only("pk", "market_value", "acres")
                   if p.value_per_acre is not None and p.value_per_acre <= float(params["max_ppa"])]
            qs = qs.filter(pk__in=ids)
    except ValueError:
        pass
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(owner_name__icontains=q) | Q(situs_address__icontains=q) | Q(prop_id=q)
                       | Q(geo_id=q) | Q(care_of__icontains=q) | Q(legal_desc__icontains=q))
    near = []
    for layer in ("transmission", "highway", "rail"):
        try:
            if params.get(f"{layer}_mi"):
                near.append((layer, float(params[f"{layer}_mi"])))
        except ValueError:
            pass
    return qs, near


def _list_use(p):
    if p.zoning:
        return f"{p.zoning.split(' / ')[0]} · {score.ZONING_LABELS.get(p.zoning_class, '')}".strip(" ·")
    return score.land_use_label(p.state_land_use)


def _near_ok(p, near):
    return all((p.near.get(layer) or {}).get("miles", 999) <= limit for layer, limit in near)


def land_search(request):
    from django.db.models import Count, Max, Min

    counties = LandCounty.objects.annotate(
        n=Count("parcels"), s=Min("parcels__min_lat"), w=Min("parcels__min_lon"),
        nn=Max("parcels__max_lat"), e=Max("parcels__max_lon")).filter(n__gt=0)
    focus = None
    if request.GET.get("parcel"):
        focus = Parcel.objects.filter(pk=request.GET["parcel"]).first()
    totals = {
        "parcels": Parcel.objects.count(),
        "pursue": Parcel.objects.filter(status=Parcel.Status.PURSUE).count(),
        "watch": Parcel.objects.filter(status=Parcel.Status.WATCH).count(),
    }
    return render(request, "crm/land_search.html", {
        "counties": counties, "focus": focus, "totals": totals, "layer_counts": layers.layer_counts(),
        "statuses": Parcel.Status.choices, "zoning_classes": score.ZONING_CLASSES, "signal_filters": signals.FILTERS,
        "incentive_programs": __import__("crm.land.incentives", fromlist=["public"]).public(),
        "fema_flood_layer": __import__("crm.land.sources", fromlist=["x"]).fema_flood_layer_id(wait=False),
        "zoning_counts": dict(Parcel.objects.values_list("zoning_class").annotate(n=Count("id"))),
        "county_bounds": {c.fips: [[c.s, c.w], [c.nn, c.e]] for c in counties},
    })


def parcels_json(request):
    """Parcels in the map view that match the filters. Polygons when zoomed in, points when zoomed out."""
    qs, near = _filtered(request.GET)
    try:
        west, south, east, north = (float(v) for v in request.GET["bbox"].split(","))
    except (KeyError, ValueError):
        raise Http404("bbox required")
    qs = qs.filter(max_lon__gte=west, min_lon__lte=east, max_lat__gte=south, min_lat__lte=north)
    zoom = int(request.GET["zoom"]) if request.GET.get("zoom", "").isdigit() else 10
    as_points = zoom < 12
    fields = ["pk", "county__name", "key", "prop_id", "owner_name", "acres", "score", "status", "killers", "lat", "lon",
              "situs_address", "near", "in_city", "state_land_use", "market_value", "zoning_class", "zoning",
              "signals", "motivation", "portfolio_parcels"]
    order = SORTS.get(request.GET.get("sort"), SORTS[""])
    rows = qs.order_by(*order).only(*[f for f in fields if "__" not in f], "county__name",
                                                  *([] if as_points else ["geometry"]))
    features, listed, total = [], [], 0
    for p in rows.iterator(chunk_size=2000):
        if near and not _near_ok(p, near):
            continue
        total += 1
        if len(features) >= MAX_SHAPES:
            continue
        props = {"id": p.pk, "score": p.score, "acres": round(p.acres), "status": p.status, "killer": bool(p.killers),
                 "owner": p.owner_name}
        geom = {"type": "Point", "coordinates": [round(p.lon, 6), round(p.lat, 6)]} if as_points else json.loads(p.geometry)
        features.append({"type": "Feature", "geometry": geom, "properties": props})
        if len(listed) < 150:
            listed.append({**props, "county": p.county.name, "address": p.situs_address,
                           "use": _list_use(p), "city": p.in_city,
                           "ppa": p.value_per_acre, "signals": [signals.LABELS.get(c, c) for c in p.signals.split()],
                           "group": p.portfolio_parcels or 0})
    return JsonResponse({"type": "FeatureCollection", "features": features, "total": total,
                         "truncated": total > len(features), "points": as_points, "list": listed})


def near_rows(parcel):
    """Distances to the nearest power, road, rail, pipeline and military features, as {label, miles, name} rows."""
    rows = []
    for layer, spec in STORED_LAYERS.items():
        n = (parcel.near or {}).get(layer)
        if layer in LIMIT_LAYERS or not n:
            continue
        if layer == "highway":
            name = score.road_name(n)
        elif layer == "substation":
            name = score.substation_name(n).replace("an unnamed", "Unnamed")
            kv = n.get("MAX_VOLTAG")
            name += f" · {kv:g} kV" if isinstance(kv, (int, float)) and kv > 0 else ""
        elif layer == "transmission":
            name = " · ".join(str(x) for x in [n.get("name"), f"{n['VOLT_CLASS']} kV" if n.get("VOLT_CLASS") else ""] if x)
        else:
            name = n.get("name") or ""
        name = " · ".join(x for x in name.split(" · ") if x.strip().upper() not in ("NOT AVAILABLE", "UNKNOWN", "NA"))
        if name.isupper() and len(name) > 4:  # keep railroad marks like UP, BNSF, CSXT
            name = name.title()
        rows.append({"layer": layer, "label": spec["label"], "miles": n["miles"], "name": name})
    return rows


def _owned(parcel):
    """ACMI's closed purchases of this parcel: [(site, closing date, buying entity)]."""
    from datetime import date

    out = []
    for deal in parcel.site_deals.filter(status=LandSite.Status.ACQUIRED).select_related("site"):
        entity = (deal.site.contracts.filter(status="closed").exclude(our_entity="")
                  .values_list("our_entity", flat=True).first())
        raw = (deal.milestones or {}).get("closed")
        out.append((deal.site, date.fromisoformat(raw) if raw else None, entity or "ACMI"))
    return out


def _sales_context(parcel):
    """Last transfer, off-market signals, owner group, nearby comps and the team-only value range for the card."""
    from . import comps, portfolio, signals

    if not parcel:
        return {}
    return {
        "signal_chips": signals.chips(parcel), "group": portfolio.summary(parcel),
        "sale_comps": comps.nearby(lat=parcel.lat, lon=parcel.lon, county=parcel.county, limit=5,
                                   exclude_parcels=[parcel.pk]),
        "value_range": comps.parcel_value_range(parcel),
        "own_comp": parcel.sale_comps.filter(source="county").first(),
    }


def _card(request, parcel, live=None):
    checks = (parcel.checks or {}) if parcel else {}
    return render(request, "crm/partials/land_card.html", {
        **_sales_context(parcel),
        "owned": _owned(parcel) if parcel else [],
        "p": parcel, "live": live, "checks": checks, "near_rows": near_rows(parcel) if parcel else [],
        "zoning_ref": zoning_ref.district_for(parcel) if parcel else None,
        "land_use": score.land_use_label(parcel.state_land_use) if parcel else "",
        "unconfirmed": score.ALWAYS_UNCONFIRMED, "statuses": Parcel.Status.choices,
        "projects": Project.objects.filter(active=True).select_related("campus"),
        "ccn_rows": [(k, label, checks[k]) for k, label in (("water", "Water"), ("sewer", "Sewer")) if checks.get(k)],
        "category_rows": [parcel.overlays[k] for k in ("water_category", "sewer_category")
                          if parcel and (parcel.overlays or {}).get(k)],
    })


def parcel_card(request, pk):
    parcel = get_object_or_404(Parcel.objects.select_related("county", "company", "status_by"), pk=pk)
    return _card(request, parcel)


@require_POST
def parcel_check(request, pk):
    parcel = get_object_or_404(Parcel.objects.select_related("county", "company", "status_by"), pk=pk)
    run_checks(parcel)
    return _card(request, parcel)


def parcel_geojson(request, pk):
    parcel = get_object_or_404(Parcel, pk=pk)
    return JsonResponse({"type": "Feature", "geometry": json.loads(parcel.geometry),
                         "properties": {"id": parcel.pk, "score": parcel.score, "status": parcel.status}})


@require_POST
def parcel_status(request, pk):
    parcel = get_object_or_404(Parcel.objects.select_related("county", "company"), pk=pk)
    status = request.POST.get("status", "")
    if status not in Parcel.Status.values:
        raise Http404
    parcel.status, parcel.status_by, parcel.status_at = status, request.user, timezone.now()
    reason = (request.POST.get("reason") or "").strip()
    if reason:
        stamp = timezone.localtime().strftime("%b %d")
        parcel.notes = f"{stamp} {parcel.get_status_display()}: {reason}\n{parcel.notes}".strip()
    parcel.save()
    return _card(request, parcel)


@require_POST
def parcel_to_project(request, pk):
    """Put a parcel on a project's land sites: its own site if it is already in one, else a new one-parcel site."""
    from .tracker import ensure_deals

    parcel = get_object_or_404(Parcel.objects.select_related("county", "company", "status_by"), pk=pk)
    project = get_object_or_404(Project, pk=request.POST.get("project") or 0)
    site = parcel.sites.first()
    if site:
        site.project, site.campus = project, site.campus or project.campus
        site.save()
        _log(site, request.user, "edit", f"Moved to project {project.name} from the parcel card")
    else:
        label = parcel.situs_address or parcel.owner_name.title() or f"Parcel {parcel.prop_id or parcel.key}"
        site = LandSite.objects.create(
            name=f"{label} ({parcel.acres:,.0f} ac, {parcel.county.name} Co.)"[:200], project=project,
            campus=project.campus, created_by=request.user,
            milestones={"identified": timezone.localdate().isoformat()})
        site.parcels.add(parcel)
        ensure_deals(site)
        _log(site, request.user, "edit", f"Site created from the parcel card for project {project.name}")
    return _card(request, parcel)


@require_POST
def project_site_set(request, pk):
    """From a project page: add an existing land site to it (action=add, site=<id>) or take one off (action=remove)."""
    project = get_object_or_404(Project, pk=pk)
    site = get_object_or_404(LandSite, pk=request.POST.get("site") or 0)
    if request.POST.get("action") == "remove":
        if site.project_id == project.pk:
            site.project = None
            site.save()
            _log(site, request.user, "edit", f"Taken off project {project.name}")
    else:
        site.project, site.campus = project, site.campus or project.campus
        site.save()
        _log(site, request.user, "edit", f"Added to project {project.name}")
    return redirect(project)


@require_POST
def parcel_notes(request, pk):
    parcel = get_object_or_404(Parcel.objects.select_related("county", "company", "status_by"), pk=pk)
    parcel.notes = request.POST.get("notes", "").strip()
    parcel.save(update_fields=["notes", "updated_at"])
    return _card(request, parcel)


def _stored_at(hit, lat, lon):
    """The stored parcel behind a live identify hit, or None: same key, else same property id, else the stored parcel
    whose outline holds the point and covers most of the live one. The outline match finds Texas ids merged into one
    shared shape (the state service may answer with a sibling id) and parcels whose id changed."""
    county = LandCounty.objects.filter(fips=hit["county"]["fips"]).first()
    if county is None:
        return None
    qs = Parcel.objects.filter(county=county).select_related("county", "company", "status_by")
    for field, value in (("key", hit.get("key")), ("prop_id", hit["fields"].get("prop_id"))):
        if value:
            found = qs.filter(**{field: value}).first()
            if found:
                return found
    if not hit.get("geometry"):
        return None
    from shapely import make_valid
    from shapely.geometry import Point

    live, point = make_valid(shape(hit["geometry"])), Point(lon, lat)
    best, best_share = None, 0.5
    for p in qs.filter(min_lon__lte=lon, max_lon__gte=lon, min_lat__lte=lat, max_lat__gte=lat):
        g = make_valid(shape(json.loads(p.geometry)))
        smaller = min(g.area, live.area)
        if not g.buffer(1e-6).contains(point) or not smaller:
            continue
        share = g.intersection(live).area / smaller
        if share > best_share:
            best, best_share = p, share
    return best


def identify_view(request):
    """Click on the map: our stored parcel if we have it, otherwise the live county/state record."""
    try:
        lat, lon = float(request.GET["lat"]), float(request.GET["lon"])
    except (KeyError, ValueError):
        raise Http404
    hit = identify(lat, lon)
    if not hit:
        return HttpResponse('<div class="land-card"><p class="muted">No parcel record here.</p></div>')
    stored = _stored_at(hit, lat, lon)
    if stored:
        return _card(request, stored)
    return _card(request, None, live={**hit, "lat": lat, "lon": lon,
                                      "land_use": score.land_use_label(hit["fields"].get("state_land_use")),
                                      "geometry_json": json.dumps(hit["geometry"]) if hit["geometry"] else ""})


def _shape_key(geom):
    """Key for land the source gives no id: from its outline, so clicking the same tract twice finds one parcel."""
    import hashlib

    sig = ",".join(f"{v:.5f}" for v in geom.bounds) + f",{geom.area:.9f}"
    return "shp-" + hashlib.sha1(sig.encode()).hexdigest()[:12]


def _store_parcel_at(lat, lon, hit=None):
    """The parcel at a point stored in land search (fetched live and saved if we don't have it), or None."""
    from .parcels import assign_fields, owner_key

    hit = hit or identify(lat, lon)
    if not hit or not hit["geometry"]:
        return None
    c = hit["county"]
    county, _ = LandCounty.objects.get_or_create(fips=c["fips"], defaults={
        "name": c["name"], "state": c["state"], "source": c.get("source", ""),
        "cad_search_url": c.get("cad_search_url", "")})
    geom = shape(hit["geometry"])
    fields = hit["fields"]
    stored = _stored_at(hit, lat, lon)
    if stored is not None:
        return stored  # already in land search (maybe under a merged or older id): keep its data as loaded
    key = hit.get("key") or fields.get("prop_id") or fields.get("geo_id") or _shape_key(geom)
    parcel = Parcel(county=county, key=key)
    assign_fields(parcel, fields)
    parcel.geometry = json.dumps(hit["geometry"])
    pt = geom.representative_point()
    parcel.lat, parcel.lon = pt.y, pt.x
    parcel.min_lon, parcel.min_lat, parcel.max_lon, parcel.max_lat = geom.bounds
    same = Parcel.objects.filter(county=county, owner_name=parcel.owner_name).exclude(pk=parcel.pk)         if owner_key(parcel.owner_name) else Parcel.objects.none()
    parcel.owner_acres = round(parcel.acres + sum(same.values_list("acres", flat=True)), 1)
    parcel.owner_parcels = same.count() + 1
    parcel.near, parcel.in_city = layers.measure(geom)
    score.apply(parcel)
    parcel.save()
    return parcel


@require_POST
def parcel_save(request):
    """Save the parcel at a point into land search (for parcels under the size cut-off, or outside loaded counties)."""
    try:
        lat, lon = float(request.POST["lat"]), float(request.POST["lon"])
    except (KeyError, ValueError):
        raise Http404
    parcel = _store_parcel_at(lat, lon)
    if parcel is None:
        raise Http404
    run_checks(parcel)
    return _card(request, parcel)


@require_POST
def assemble_point(request):
    """Assemble mode: the stored parcel at a clicked point (stored on the spot if it wasn't), as {id, acres, owner}."""
    try:
        lat, lon = float(request.POST["lat"]), float(request.POST["lon"])
    except (KeyError, ValueError):
        raise Http404
    hit = identify(lat, lon)
    parcel = (_stored_at(hit, lat, lon) or _store_parcel_at(lat, lon, hit)) if hit else None
    if parcel is None:
        raise Http404
    return JsonResponse({"id": parcel.pk, "acres": round(parcel.acres, 1), "owner": parcel.owner_name})


def search_json(request):
    """Owner / address / property id search across every loaded county (search box)."""
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})
    qs = Parcel.objects.select_related("county").filter(
        Q(owner_name__icontains=q) | Q(situs_address__icontains=q) | Q(prop_id=q) | Q(geo_id=q) | Q(care_of__icontains=q)
    ).order_by("-acres")[:25]
    return JsonResponse({"results": [{
        "id": p.pk, "owner": p.owner_name, "acres": round(p.acres), "county": p.county.name, "score": p.score,
        "address": p.situs_address, "lat": p.lat, "lon": p.lon, "status": p.status,
    } for p in qs]})


def export_csv(request):
    qs, near = _filtered(request.GET)
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="land-search-{timezone.localdate():%Y-%m-%d}.csv"'
    w = csv.writer(resp)
    w.writerow(["Score", "Deal-killers", "Status", "County", "Property ID", "Acres", "Owner", "Care of",
                "Mailing address", "Mail city", "Mail state", "Mail ZIP", "Property address", "Land use", "Zoning",
                "Zoning category", "Market value",
                "Value / acre", "City limits", "Transmission (mi)", "Substation (mi)", "Highway (mi)", "Rail (mi)",
                "Military base (mi)", "Last transfer", "Last price", "How conveyed", "Off-market signals",
                "Owner group parcels", "Owner group acres", "Latitude", "Longitude", "Link"])
    base = request.build_absolute_uri("/")[:-1]
    for p in qs.order_by("-score", "-acres")[:20000]:
        if near and not _near_ok(p, near):
            continue
        m = lambda layer: (p.near.get(layer) or {}).get("miles", "")  # noqa: E731
        w.writerow([p.score, "; ".join(p.killers), p.get_status_display(), p.county.name, p.prop_id, round(p.acres, 1),
                    p.owner_name, p.care_of, p.mail_address, p.mail_city, p.mail_state, p.mail_zip, p.situs_address,
                    score.land_use_label(p.state_land_use) or p.state_land_use, p.zoning,
                    score.ZONING_LABELS.get(p.zoning_class, ""), p.market_value or "",
                    p.value_per_acre or "", p.in_city or "Unincorporated", m("transmission"), m("substation"),
                    m("highway"), m("rail"), m("military"),
                    p.last_sale_date.isoformat() if p.last_sale_date else "", p.last_sale_price or "", p.last_sale_type,
                    "; ".join(signals.LABELS.get(c, c) for c in p.signals.split()), p.portfolio_parcels or "",
                    p.portfolio_acres or "", round(p.lat, 6), round(p.lon, 6), base + p.get_absolute_url()])
    return resp


def _latlon(value):
    lat, lon = (float(v) for v in value.split(","))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError
    return lat, lon


def drive_route(request):
    """Drive time and distance between two points, both directions. ?a=lat,lon&b=lat,lon"""
    from .drive import route

    try:
        a, b = _latlon(request.GET["a"]), _latlon(request.GET["b"])
    except (KeyError, ValueError):
        return JsonResponse({"error": "Two points needed"}, status=400)
    try:
        return JsonResponse(route(a, b))
    except Exception as exc:
        return JsonResponse({"error": str(exc)[:200]}, status=502)


def drive_isochrone(request):
    """Drive-time bands around a point. ?at=lat,lon&minutes=10,20,30&direction=from|to"""
    from .drive import DEFAULT_MINUTES, isochrones

    try:
        at = _latlon(request.GET["at"])
        minutes = [int(m) for m in request.GET.get("minutes", "").split(",") if m.strip()] or DEFAULT_MINUTES
    except (KeyError, ValueError):
        return JsonResponse({"error": "A point is needed"}, status=400)
    try:
        return JsonResponse(isochrones(at, minutes, reverse=request.GET.get("direction") == "to"))
    except Exception as exc:
        return JsonResponse({"error": str(exc)[:200]}, status=502)


@require_POST
def qd_layout(request):
    """QD site screen. JSON in: {"parcel": id, "buildings": [...], "action": "calc" | "fit" | "save"}.

    Returns each building's distances and position, whether its IBD arc stays on the parcel, separation
    conflicts, and how much of the parcel the arcs encumber.
    """
    from shapely.geometry import Point

    from . import qd

    try:
        data = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Bad request"}, status=400)
    buildings = [b for b in (data.get("buildings") or []) if isinstance(b, dict)][:40]
    for b in buildings:
        try:
            b["dist"] = qd.building_distances(b)
        except (ValueError, TypeError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
    site = LandSite.objects.filter(pk=data.get("site")).first() if data.get("site") else None
    ids = [int(i) for i in (data.get("parcels") or []) if str(i).isdigit()]
    if site and not ids:
        ids = list(site.parcels.values_list("pk", flat=True))
    group = list(Parcel.objects.filter(pk__in=ids)) if ids else []
    parcel = None if group else (Parcel.objects.filter(pk=data.get("parcel")).first() if data.get("parcel") else None)
    if site and group:
        from . import trim

        geom = trim.site_geometry(site, ids)
    else:
        geom = _union_geometry(group) if group else (shape(json.loads(parcel.geometry)) if parcel else None)
    centre = geom.representative_point() if geom else None
    if centre is None:
        located = [b for b in buildings if b.get("lat") is not None]
        if not located:
            return JsonResponse({"buildings": [{**b, "result": None} for b in buildings], "summary": None})
        centre = Point(located[0]["lon"], located[0]["lat"])
    to_m, fwd, inv = qd.projector(centre.y, centre.x)
    parcel_m = to_m(geom) if geom else None
    if parcel_m is not None and parcel_m.geom_type == "MultiPolygon":
        # Assemblages that don't touch: lay out on the largest piece, but judge arcs against all of it.
        whole = parcel_m
        parcel_m = max(parcel_m.geoms, key=lambda g: g.area)
    else:
        whole = parcel_m
    if data.get("action") == "fit" and parcel_m is not None and buildings:
        positions = qd.fit(parcel_m, buildings)
    else:
        default = fwd(centre.x, centre.y)
        positions = [fwd(b["lon"], b["lat"]) if b.get("lat") is not None else default for b in buildings]
    results, summary = qd.evaluate(whole, buildings, positions, with_max=True)
    from shapely.geometry import mapping as to_geojson
    from shapely.ops import transform as reproject

    to_wgs = lambda g: to_geojson(reproject(inv, g.simplify(0.5)))  # noqa: E731
    out = []
    for b, (x, y), r in zip(buildings, positions, results):
        lon, lat = inv(x, y)
        shapes = {k: to_wgs(g) for k, g in qd.arcs_m(b, x, y).items()}
        fp = qd.footprint(b, x, y)
        if fp.geom_type != "Point":
            shapes["footprint"] = to_wgs(fp)
        out.append({**b, "lat": round(lat, 7), "lon": round(lon, 7), "result": r, "shapes": shapes})
    target = site or parcel
    if target and data.get("action") in ("fit", "save"):
        keep = ("name", "kind", "sqft", "length", "rotation", "hd", "newqd", "pes", "barricaded", "mce", "k", "lat", "lon")
        # Saving no buildings is Clear: drop the plan rather than keep an empty one.
        target.qd_design = {"buildings": [{k: b.get(k) for k in keep} for b in out],
                            "saved_at": timezone.now().isoformat(), "saved_by": request.user.username} if out else {}
        target.save(update_fields=["qd_design", "updated_at"])
    boundary = to_geojson(geom) if geom is not None else None
    return JsonResponse({"buildings": out, "summary": summary, "boundary": boundary})


def qd_design(request, pk):
    parcel = get_object_or_404(Parcel, pk=pk)
    return JsonResponse(parcel.qd_design or {})



def _union_geometry(parcels):
    from shapely import make_valid
    from shapely.ops import unary_union

    geoms = []
    for p in parcels:
        g = shape(json.loads(p.geometry))
        geoms.append(g if g.is_valid else make_valid(g))
    # A hair of buffer closes slivers between neighbouring parcels so an assemblage reads as one boundary.
    return unary_union([g.buffer(0.00001) for g in geoms]).buffer(-0.00001)


def _site_json(site):
    from . import trim

    acres = trim.site_acres(site)  # also makes sure every parcel has its deal row
    excluded = set(site.parcel_deals.filter(included=False).values_list("parcel_id", flat=True))
    parcels = [p for p in site.parcels.select_related("county") if p.pk not in excluded]
    owners = sorted({p.owner_name for p in parcels if p.owner_name})
    return {
        "id": site.pk, "name": site.name, "status": site.status, "status_label": site.get_status_display(),
        "campus": site.campus_id, "project": site.project_id, "home": site.home_label, "notes": site.notes,
        "parcels": [p.pk for p in parcels], "excluded": sorted(excluded), "acres": acres,
        "owners": owners, "has_qd": bool((site.qd_design or {}).get("buildings")),
        "url": site.map_url, "page_url": site.get_absolute_url(), "deal_clock": _deal_clock(site),
    }


def _deal_clock(site):
    """'142 days since identified' / '98 days identified to close' for a site, '' with no milestone dates."""
    ts = site.timeline_summary()
    if not ts:
        return ""
    n = ts["days"]
    unit = "day" if n == 1 else "days"
    return f"{n} {unit} {'identified to close' if ts['closed'] else 'since ' + ts['start_label']}"


def sites_json(request):
    """Saved land sites, plus the campus and project choices for the save dialog."""
    sites = LandSite.by_status(LandSite.objects.select_related("campus", "project").prefetch_related("parcels"))
    return JsonResponse({
        "sites": [_site_json(s) for s in sites],
        "campuses": [{"id": c.pk, "label": f"{c.code} · {c.name}"} for c in Campus.objects.all()],
        "projects": [{"id": p.pk, "label": p.name, "campus": p.campus_id} for p in Project.objects.filter(active=True)],
        "statuses": LandSite.Status.choices,
    })


def site_json(request, pk):
    site = get_object_or_404(LandSite, pk=pk)
    data = _site_json(site)
    from shapely.geometry import mapping

    from . import trim

    geom = trim.site_geometry(site)
    data["geometry"] = mapping(geom) if geom is not None else None
    data["qd_design"] = site.qd_design or {}
    return JsonResponse(data)


@require_POST
def site_save(request):
    """Create or update a site from the map. JSON: {id?, name, campus?, project?, status?, notes?, parcels: [ids]}."""
    try:
        data = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Bad request"}, status=400)
    site = LandSite.objects.filter(pk=data.get("id")).first() if data.get("id") else LandSite(created_by=request.user)
    is_new, old_status = site.pk is None, site.status
    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Give the site a name."}, status=400)
    site.name = name[:200]
    site.campus = Campus.objects.filter(pk=data.get("campus")).first() if data.get("campus") else None
    site.project = Project.objects.filter(pk=data.get("project")).first() if data.get("project") else None
    if site.project and not site.campus:
        site.campus = site.project.campus
    if data.get("status") in LandSite.Status.values:
        site.status = data["status"]
    if "notes" in data:
        site.notes = data.get("notes") or ""
    site.save()
    if "parcels" in data:
        from .tracker import ensure_deals

        keep = set(site.parcel_deals.filter(included=False).values_list("parcel_id", flat=True)) if site.pk else set()
        site.parcels.set(Parcel.objects.filter(pk__in=[int(i) for i in data["parcels"] if str(i).isdigit()] + list(keep)))
        ensure_deals(site)
    if is_new:
        _log(site, request.user, "edit", f"Site created on the map with {site.parcels.count()} parcels")
        if not (site.milestones or {}).get("identified"):
            site.milestones = {**(site.milestones or {}), "identified": timezone.localdate().isoformat()}
            site.save(update_fields=["milestones"])
    elif old_status != site.status:
        _log(site, request.user, "status", f"Status → {site.get_status_display()} (from the map)")
    return JsonResponse(_site_json(site))


@require_POST
@manager_required
def site_delete(request, pk):
    get_object_or_404(LandSite, pk=pk).delete()
    return JsonResponse({"ok": True})


def assemblage_json(request):
    """Totals for a set of parcels being assembled on the map. ?ids=1,2,3"""
    ids = [int(i) for i in request.GET.get("ids", "").split(",") if i.strip().isdigit()]
    parcels = list(Parcel.objects.filter(pk__in=ids).select_related("county"))
    owners = {}
    for p in parcels:
        o = owners.setdefault(p.owner_name or "Unknown owner", {"owner": p.owner_name or "Unknown owner", "acres": 0, "n": 0})
        o["acres"] += p.acres
        o["n"] += 1
    value = sum(p.market_value or 0 for p in parcels)
    geom = _union_geometry(parcels) if parcels else None
    return JsonResponse({
        "ids": [p.pk for p in parcels], "acres": round(sum(p.acres for p in parcels), 1),
        "owners": sorted(({**o, "acres": round(o["acres"], 1)} for o in owners.values()), key=lambda o: -o["acres"]),
        "market_value": value or None, "contiguous": bool(geom is not None and geom.geom_type == "Polygon"),
        "counties": sorted({p.county.name for p in parcels}),
    })


# ---------------------------------------------------------------------------
# Saved map views: the map position, base map, layers, filters, tools shown and panels open.
# Stored as SavedView(object_type="land") with the state as JSON in `query`.
# ---------------------------------------------------------------------------


def _view_json(v, user):
    try:
        state = json.loads(v.query or "{}")
    except ValueError:
        state = {}
    return {"id": v.pk, "name": v.name, "shared": v.shared, "mine": v.owner_id == user.pk,
            "owner": v.owner.get_full_name() or v.owner.username, "is_default": v.is_default and v.owner_id == user.pk,
            "state": state}


def views_json(request):
    from ..models import ObjectType, SavedView

    views = SavedView.objects.filter(object_type=ObjectType.LAND).filter(Q(owner=request.user) | Q(shared=True))
    return JsonResponse({"views": [_view_json(v, request.user) for v in views.select_related("owner")]})


@require_POST
def view_save(request):
    """JSON: {id?, name, state, shared, is_default}. Only the owner can change a view."""
    from ..models import ObjectType, SavedView

    try:
        data = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Bad request"}, status=400)
    name = (data.get("name") or "").strip()[:120]
    if data.get("id"):
        view = get_object_or_404(SavedView, pk=data["id"], owner=request.user, object_type=ObjectType.LAND)
    elif name:
        view = SavedView.objects.filter(owner=request.user, object_type=ObjectType.LAND, name=name).first() \
            or SavedView(owner=request.user, object_type=ObjectType.LAND)
    else:
        return JsonResponse({"error": "Give the view a name."}, status=400)
    if name:
        view.name = name
    if "state" in data:
        view.query = json.dumps(data["state"])[:20000]
    if "shared" in data:
        view.shared = bool(data["shared"])
    if "is_default" in data:
        view.is_default = bool(data["is_default"])
        if view.is_default:
            SavedView.objects.filter(owner=request.user, object_type=ObjectType.LAND).exclude(pk=view.pk) \
                .update(is_default=False)
    view.save()
    return JsonResponse(_view_json(view, request.user))


@require_POST
def view_delete(request, pk):
    from ..models import ObjectType, SavedView

    get_object_or_404(SavedView, pk=pk, owner=request.user, object_type=ObjectType.LAND).delete()
    return JsonResponse({"ok": True})


def sites_geojson(request):
    """Every saved site as one outline (its parcels merged), for the "Saved sites" map layer."""
    from shapely.geometry import mapping

    feats = []
    from . import trim

    for site in LandSite.objects.select_related("campus", "project"):
        pieces = trim.parts(site)
        if not pieces:
            continue
        geom = trim.union([g for _, g in pieces]).simplify(0.00002, preserve_topology=True)
        feats.append({"type": "Feature", "geometry": mapping(geom), "properties": {
            "id": site.pk, "name": site.name, "status": site.status, "status_label": site.get_status_display(),
            "home": site.home_label, "acres": round(sum(d.acres for d, _ in pieces), 1), "parcels": len(pieces)}})
    return JsonResponse({"type": "FeatureCollection", "features": feats})


def sites_page(request):
    """Prospective sites by campus and project, each group sorted by deal stage, with totals."""
    from collections import Counter

    status = request.GET.get("status", "active")
    view = "board" if request.GET.get("view") == "board" else "campus"
    mine = request.GET.get("mine") == "1"
    every = LandSite.objects.select_related("campus", "project", "owner").prefetch_related("parcels__county", "events", "team")
    if mine:
        every = every.filter(Q(owner=request.user) | Q(team=request.user) | Q(created_by=request.user, owner__isnull=True)).distinct()
    # Dropped sites always go to the Archive at the bottom, not the campus / project groups.
    sites = every.exclude(status=LandSite.Status.DROPPED)
    stage_n = Counter(sites.values_list("status", flat=True))
    stage_chips = [(v, l, stage_n[v]) for v, l in LandSite.Status.choices if v != LandSite.Status.DROPPED]
    if view == "campus" and status in LandSite.Status.values and status != LandSite.Status.DROPPED:
        sites = sites.filter(status=status)
    archive = []
    for s in sorted(every.filter(status=LandSite.Status.DROPPED), key=lambda x: x.updated_at, reverse=True):
        dropped = next((e.when for e in s.events.all() if e.kind == "status" and "Dropped" in e.text), None)
        from . import trim

        archive.append({"site": s, "when": dropped or s.updated_at, "acres": trim.site_acres(s),
                        "parcels": s.parcel_deals.filter(included=True).count(),
                        "reason": (s.notes or "").strip().splitlines()[0][:220] if s.notes else ""})
    from . import tracker

    rows = []
    for s in LandSite.by_status(sites):
        deals = [d for d in tracker.site_deals(s) if d.included]
        parcels = [d.parcel for d in deals]
        owners = Counter()
        for p in parcels:
            owners[p.owner_name or "Unknown owner"] += p.acres
        zoning = Counter()
        for p in parcels:
            label = (p.zoning.split(" / ")[0] if p.zoning else "") or score.land_use_label(p.state_land_use) or "—"
            zoning[label] += p.acres
        from .site_card import killer_lines

        killers = killer_lines(parcels)
        checked = [p for p in parcels if (p.checks or {}).get("net_usable")]
        net = sum(p.checks["net_usable"]["acres"] for p in checked) if checked and len(checked) == len(parcels) else None
        values = [p.market_value for p in parcels if p.market_value]
        rows.append({
            "site": s, "parcels": len(parcels), "acres": round(sum(d.acres for d in deals), 1), "net": net,
            "clock": (lambda ts: f"{ts['days']}d" if ts else "")(s.timeline_summary()),
            "owners": owners.most_common(), "zoning": [z for z, _ in zoning.most_common(3)],
            "counties": sorted({p.county.name for p in parcels}),
            "score": max((p.score for p in parcels), default=None), "killers": killers,
            "value": sum(values) if values else None, "has_qd": bool((s.qd_design or {}).get("buildings")),
            "note": (s.notes or "").strip().splitlines()[0][:220] if s.notes else "",
            "progress": tracker.progress(s) if len({(p.owner_name or "").upper() for p in parcels}) > 1 else [],
            "open_tasks": s.tasks.filter(done=False).count(), "comments": s.comments.count(),
            "tenants": s.tenant_deals.count(), "team": list(s.team.all()),
        })
    groups = {}
    for r in rows:
        s = r["site"]
        key = (s.campus.code if s.campus else "~", s.project.name if s.project else "")
        g = groups.setdefault(key, {"campus": s.campus, "project": s.project, "rows": [], "by_status": Counter()})
        g["rows"].append(r)
        g["by_status"][s.get_status_display()] += 1
    ordered = [groups[k] for k in sorted(groups)]
    for g in ordered:
        live = [r for r in g["rows"] if r["site"].status != LandSite.Status.DROPPED]
        g["acres"] = round(sum(r["acres"] for r in live), 1)
        g["count"] = len(g["rows"])
        g["by_status"] = [(label, g["by_status"][label]) for _, label in LandSite.Status.choices if g["by_status"][label]]
    board = [{"value": v, "label": l, "rows": [r for r in rows if r["site"].status == v]}
             for v, l in LandSite.Status.choices if v != LandSite.Status.DROPPED]
    for col in board:
        col["acres"] = round(sum(r["acres"] for r in col["rows"]), 1)
    return render(request, "crm/land_sites.html", {
        "groups": ordered, "status": status, "archive": archive, "view": view, "mine": mine, "board": board,
        "stage_chips": stage_chips, "stage_values": [c[0] for c in stage_chips], "active_count": sum(stage_n.values()),
        "total": len(rows), "total_acres": round(sum(r["acres"] for r in rows), 1),
    })


def _log(site, user, kind, text):
    from ..models import LandSiteEvent

    LandSiteEvent.objects.create(site=site, user=user, kind=kind, text=text[:500])


def _set_status(site, status, user, why=""):
    if status in LandSite.Status.values and status != site.status:
        old = site.get_status_display()
        site.status = status
        site.save(update_fields=["status", "updated_at"])
        _log(site, user, "status", f"Status {old} → {site.get_status_display()}" + (f" ({why})" if why else ""))


def site_detail(request, pk):
    """One prospective site: details, status, deal milestones, parcels and change log."""
    from datetime import date

    from django.contrib import messages
    from django.shortcuts import redirect

    site = get_object_or_404(LandSite.objects.select_related("campus", "project"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "details":
            changes = []
            name = (request.POST.get("name") or "").strip()[:200]
            if name and name != site.name:
                changes.append(f"renamed from “{site.name}”")
                site.name = name
            campus = Campus.objects.filter(pk=request.POST.get("campus") or 0).first()
            project = Project.objects.filter(pk=request.POST.get("project") or 0).first()
            if project and not campus:
                campus = project.campus
            if (campus, project) != (site.campus, site.project):
                changes.append(f"moved to {' / '.join(str(x) for x in [campus, project] if x) or 'no campus'}")
                site.campus, site.project = campus, project
            notes = request.POST.get("notes", "")
            if notes.strip() != (site.notes or "").strip():
                changes.append("notes edited")
                site.notes = notes
            site.save()
            if changes:
                _log(site, request.user, "edit", "; ".join(changes).capitalize())
            _set_status(site, request.POST.get("status"), request.user)
            messages.success(request, "Saved.")
        elif action == "milestones":
            new = {}
            for key, label, _ in LandSite.MILESTONES:
                raw = (request.POST.get(key) or "").strip()
                if raw:
                    try:
                        new[key] = date.fromisoformat(raw).isoformat()
                    except ValueError:
                        messages.error(request, f"{label}: use a date.")
                        return redirect("crm:land_site_detail", pk=site.pk)
            old = site.milestones or {}
            labels = {k: label for k, label, _ in LandSite.MILESTONES}
            for key in labels:
                if new.get(key) != old.get(key):
                    if new.get(key):
                        d = date.fromisoformat(new[key])
                        _log(site, request.user, "milestone", f"{labels[key]}: {d:%b} {d.day}, {d.year}")
                    else:
                        _log(site, request.user, "milestone", f"{labels[key]} date cleared")
            site.milestones = new
            site.save(update_fields=["milestones", "updated_at"])
            implied = site.status_for_milestones()
            order = LandSite.STATUS_ORDER
            if implied and site.status != "dropped" and order.index(implied) < order.index(site.status):
                _set_status(site, implied, request.user, "from milestones")
            messages.success(request, "Timeline saved.")
        elif action == "parcel_deal":
            from ..models import SiteParcelDeal

            deal = get_object_or_404(SiteParcelDeal, site=site, pk=request.POST.get("deal"))
            new_dates = {}
            for key, label, _ in LandSite.MILESTONES:
                raw = (request.POST.get(key) or "").strip()
                if raw:
                    try:
                        new_dates[key] = date.fromisoformat(raw).isoformat()
                    except ValueError:
                        messages.error(request, f"{label}: use a date.")
                        return redirect("crm:land_site_detail", pk=site.pk)
            status = request.POST.get("status") or deal.status
            notes = request.POST.get("notes", deal.notes)
            targets = [deal]
            owner = (deal.parcel.owner_name or "").strip().upper()
            if request.POST.get("apply_owner") and owner:
                targets = [d for d in site.parcel_deals.select_related("parcel")
                           if (d.parcel.owner_name or "").strip().upper() == owner]
            labels = {k: label for k, label, _ in LandSite.MILESTONES}
            changes = []
            for d in targets:
                if d.milestones != new_dates:
                    for key in labels:
                        if new_dates.get(key) != (d.milestones or {}).get(key) and d is deal:
                            changes.append(f"{labels[key]} {date.fromisoformat(new_dates[key]):%b} "
                                           f"{date.fromisoformat(new_dates[key]).day}" if new_dates.get(key)
                                           else f"{labels[key]} cleared")
                d.milestones = new_dates
                implied = d.status_for_milestones()
                order = LandSite.STATUS_ORDER
                d.status = status
                if implied and d.status != "dropped" and order.index(implied) < order.index(d.status):
                    d.status = implied  # dates move a parcel forward, never back
                d.notes = notes
                d.save()
            who = (deal.parcel.owner_name or "Unknown owner").title()
            scope = f"{who} ({len(targets)} parcels)" if len(targets) > 1 else f"{who} · {deal.parcel.prop_id or deal.parcel.key}"
            _log(site, request.user, "parcel",
                 f"{scope}: {deal.get_status_display()}" + (f"; {', '.join(changes)}" if changes else ""))
            messages.success(request, f"Updated {scope}.")
        elif action == "note":  # older form name: now a comment
            text = (request.POST.get("text") or "").strip()
            if text:
                from .site_card import add_comment

                add_comment(site, request.user, text)
        elif action == "people":
            from .site_card import save_people

            save_people(site, request.POST, request.user)
            messages.success(request, "Saved.")
        elif action in ("tenant_link", "tenant_unlink"):
            from .site_card import link_tenant

            link_tenant(site, request.POST.get("opportunity"), request.user, unlink=action == "tenant_unlink")
        elif action == "include_parcel":
            from ..models import SiteParcelDeal

            deal = get_object_or_404(SiteParcelDeal.objects.select_related("parcel"), site=site, pk=request.POST.get("deal"))
            deal.included = request.POST.get("included") == "1"
            deal.save(update_fields=["included", "updated_at"])
            p = deal.parcel
            _log(site, request.user, "edit", f"{'Included' if deal.included else 'Excluded'} parcel "
                 f"{p.prop_id or p.key} ({(p.owner_name or 'Unknown owner').title()}, {deal.acres:,.1f} ac)")
        elif action == "remove_parcel":
            p = site.parcels.filter(pk=request.POST.get("parcel")).first()
            if p:
                from .tracker import ensure_deals

                site.parcels.remove(p)
                ensure_deals(site)  # its deal row goes with it
                _log(site, request.user, "edit", f"Removed parcel {p.prop_id or p.key} ({p.owner_name}, {p.acres:,.0f} ac)")
        elif action == "delete":
            if not is_manager(request.user):
                return refuse(request)
            name = site.name
            site.delete()
            messages.success(request, f"Deleted site “{name}”. Its parcels stay in land search.")
            return redirect("crm:land_sites_page")
        return redirect("crm:land_site_detail", pk=site.pk)

    from . import incentives, tracker
    from .sources import fema_flood_layer_id

    parcels = list(site.parcels.select_related("county").order_by("-acres"))
    if tracker.missing_deals(site):  # the grid edits deal rows by id, so a site saved before they existed gets them
        tracker.ensure_deals(site)
    tr = tracker.summary(site)
    deal_by_parcel = {d.parcel_id: d for d in tr["deals"]}
    for p in parcels:
        p.deal = deal_by_parcel.get(p.pk)
    map_features = []
    for p in parcels:
        d = p.deal
        map_features.append({"type": "Feature", "geometry": json.loads(d.geometry_json() if d else p.geometry), "properties": {
            "id": p.pk, "deal": d.pk if d else None, "owner": (p.owner_name or "Unknown owner").title(),
            "acres": round(d.acres if d else p.acres, 1), "parcel_acres": round(p.acres, 1),
            "prop_id": p.prop_id or p.key, "status": d.status if d else site.status,
            "status_label": d.get_status_display() if d else site.get_status_display(),
            "included": d.included if d else True, "trimmed": bool(d and d.trimmed),
            "whole": json.loads(p.geometry) if d and d.trimmed else None, "cuts": (d.portion_cuts or []) if d else [],
            "zoning": p.zoning or "", "url": p.get_absolute_url()}})
    return render(request, "crm/land_site_detail.html", {
        "tracker": tr, "milestone_defs": LandSite.MILESTONES,
        "map_data": {"type": "FeatureCollection", "features": map_features},
        "incentive_programs": incentives.public(), "fema_flood_layer": fema_flood_layer_id(wait=False),
        "site": site, "parcels": parcels, "acres": tr["total_acres"],
        "owners": len({d.parcel.owner_name for d in tr["included"]}), "milestones": site.milestone_rows(),
        "events": site.events.exclude(kind="note").select_related("user")[:100], "statuses": LandSite.Status.choices,
        "comments": site.comments.select_related("author", "task", "opportunity")[:100], "comment_target": ("site", site.pk),
        "users": team_users().order_by("first_name", "username"),
        "team_ids": set(site.team.values_list("pk", flat=True)),
        **_tenant_context(site),
        "campuses": Campus.objects.all(), "projects": Project.objects.filter(active=True).select_related("campus"),
        "today": date.today(), "contracts": _site_contracts(site),
        "dd": diligence.summary(site) if site.diligence.exists() else None,
        "letters_logged": site.letters.count(), "last_letter": site.letters.first(),
        "approvals": entitlement_logic.annotate(list(entitlement_logic.with_related(site.approvals.all()))),
        "matrix": entitlement_logic.MATRIX, "new_approval_url": reverse("crm:approval_create") + f"?site={site.pk}",
        **_site_sales(site, parcels, same_zoning=request.GET.get("zoning") == "same"),
    })


def _site_sales(site, parcels, same_zoning=False):
    """Sale comps around the site and its team-only value range."""
    from . import comps

    center = comps.site_center(site) if parcels else None
    if not center:
        return {}
    zonings = sorted({p.zoning for p in parcels if p.zoning}) if same_zoning else None
    return {"site_comps": comps.nearby(lat=center[0], lon=center[1], county=center[3], limit=6,
                                       exclude_parcels=[p.pk for p in parcels], zonings=zonings),
            "site_range": comps.site_value_range(site),
            "site_zonings": sorted({p.zoning for p in parcels if p.zoning}),
            "same_zoning": same_zoning}


def _tenant_context(site):
    from .site_card import campus_tenants, link_choices, tenants

    linked = tenants(site)
    return {"tenants": linked, "tenant_choices": link_choices(site, linked), "unlinked_campus": campus_tenants(site, linked)}


def _site_contracts(site):
    """The site's contracts with their next open date, for the Contracts panel."""
    from ..contracts import dates as contract_dates

    from django.db.models import Count

    contracts = list(site.contracts.select_related("owner").annotate(n_rounds=Count("term_rounds")))
    nxt = contract_dates.next_dates(contracts)
    for c in contracts:
        c.next = nxt.get(c.pk)
    return contracts



@require_POST
def site_trim(request, pk):
    """Trim one parcel of a site to the portion the site needs.
    JSON: {parcel, action: "preview" | "save" | "reset", lines: [[[lon, lat], ...], ...], keep: [piece index]}."""
    from ..models import SiteParcelDeal
    from . import trim

    site = get_object_or_404(LandSite, pk=pk)
    try:
        data = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Bad request"}, status=400)
    from .tracker import ensure_deals

    ensure_deals(site)
    deal = SiteParcelDeal.objects.select_related("parcel", "site").filter(site=site, parcel_id=data.get("parcel")).first()
    if not deal:
        return JsonResponse({"error": "That parcel isn't on this site."}, status=404)
    lines = [l for l in (data.get("lines") or []) if isinstance(l, list) and len(l) >= 2][:20]
    action = data.get("action")
    p = deal.parcel
    label = f"{p.prop_id or p.key} ({(p.owner_name or 'Unknown owner').title()})"
    if action == "reset":
        trim.reset(deal)
        _log(site, request.user, "edit", f"Parcel {label} back to the whole parcel ({p.acres:,.1f} ac)")
        return JsonResponse({"ok": True, "acres": deal.acres})
    if not lines:
        return JsonResponse({"error": "Draw a cut line across the parcel first."}, status=400)
    if action == "save":
        keep = [int(i) for i in (data.get("keep") or []) if str(i).lstrip("-").isdigit()]
        trim.apply(deal, lines, keep)
        if deal.trimmed:
            _log(site, request.user, "edit", f"Trimmed parcel {label} to {deal.acres:,.1f} of {p.acres:,.1f} ac")
        else:
            _log(site, request.user, "edit", f"Parcel {label} kept whole ({p.acres:,.1f} ac)")
        return JsonResponse({"ok": True, "acres": deal.acres, "trimmed": deal.trimmed})
    out = trim.preview(deal, lines)
    if len(out["pieces"]) < 2:
        out["warning"] = "That line doesn't cut the parcel in two. Start and end it outside the parcel's edge."
    return JsonResponse(out)


def _plan_target(data):
    """(geometry WGS84, parcels, record to save on) for a site plan / QD request."""
    site = LandSite.objects.filter(pk=data.get("site")).first() if data.get("site") else None
    ids = [int(i) for i in (data.get("parcels") or []) if str(i).isdigit()]
    if site and not ids:
        ids = list(site.parcels.values_list("pk", flat=True))
    group = list(Parcel.objects.filter(pk__in=ids).select_related("county")) if ids else []
    if not group and data.get("parcel"):
        group = list(Parcel.objects.filter(pk=data["parcel"]).select_related("county"))
    if not group:
        return None, [], None
    if site:
        from . import trim

        geom = trim.site_geometry(site, [p.pk for p in group])
        if geom is None:
            return None, [], None
        group = [d.parcel for d, _ in trim.parts(site, [p.pk for p in group])]
    else:
        geom = _union_geometry(group) if len(group) > 1 else shape(json.loads(group[0].geometry))
    target = site if site else (group[0] if len(group) == 1 else None)
    return geom, group, target


def _dominant_zone(parcels):
    """(fips, zone code) with the most acres across the parcels."""
    from collections import Counter

    acres = Counter()
    for p in parcels:
        parts = ((p.overlays or {}).get("zoning") or {}).get("parts") or {}
        if parts:
            for code, pct in parts.items():
                acres[(p.county.fips, code)] += p.acres * pct / 100
        elif p.zoning:
            acres[(p.county.fips, p.zoning.split(" / ")[0])] += p.acres
        else:
            acres[(p.county.fips, "")] += p.acres
    (fips, code), _ = acres.most_common(1)[0]
    return fips, code


@require_POST
def site_plan(request):
    """Quick site plan. JSON in: {parcel|parcels|site, zone?, overrides?, program, parking_mode, exclude, action}.
    action "save" also stores the inputs on the site (or the single parcel)."""
    from shapely.geometry import mapping
    from shapely.ops import transform as reproject

    from . import qd, siteplan
    from .lookup import constraint_geometry

    try:
        data = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "Bad request"}, status=400)
    geom, parcels, target = _plan_target(data)
    if geom is None:
        return JsonResponse({"error": "Open a parcel or an assemblage first."}, status=400)
    fips, current = _dominant_zone(parcels)
    zone = data.get("zone") if data.get("zone") is not None else current
    std = siteplan.standards_for(fips, zone)
    for key, value in (data.get("overrides") or {}).items():
        if key in ("front", "side", "rear", "height", "stories", "far", "isr", "coverage") and value not in ("", None):
            try:
                std[key] = float(value)
            except (TypeError, ValueError):
                pass
        elif key in ("far", "isr", "coverage") and value == "":
            std[key] = None
    program = [p for p in (data.get("program") or []) if isinstance(p, dict) and p.get("type") in siteplan.BUILDING_TYPES][:12]
    if not program:
        program = [{"type": "flex", "count": 4}, {"type": "warehouse"}]
    c = geom.representative_point()
    to_m, fwd, inv = qd.projector(c.y, c.x)
    site_m = to_m(geom if geom.is_valid else geom.buffer(0))
    exclude_m = None
    excluded_acres = 0
    exclusion_note = ""
    if data.get("exclude"):
        import hashlib

        from django.core.cache import cache
        from shapely import wkb

        # The planner re-runs on every edit: ask FEMA and NWI once per outline per day, and don't wait minutes on
        # them (the default is 90 s x 3 tries per service).
        key = "siteplan-cons:" + hashlib.sha1(geom.wkb).hexdigest()
        try:
            hit = cache.get(key)
            down = cache.get(key + ":down")
            if hit is None and down:
                raise RuntimeError(down)  # failed a few minutes ago: don't wait on it again for every edit
            if hit is None:
                try:
                    found = constraint_geometry(geom, timeout=20, tries=1)
                except Exception as exc:
                    cache.set(key + ":down", str(exc)[:60], 300)
                    raise
                hit = found.wkb if found is not None and not found.is_empty else b""
                cache.set(key, hit, 24 * 3600)
            cons = wkb.loads(hit) if hit else None
            if cons is not None and not cons.is_empty:
                exclude_m = to_m(cons)
                excluded_acres = round(exclude_m.area * siteplan.SQFT_PER_M2 / siteplan.SQFT_PER_ACRE, 1)
        except Exception as exc:  # lay the site out anyway and say the exclusion was skipped
            exclusion_note = f"Flood/wetland maps didn't respond ({str(exc)[:60]}); laid out without excluding them."
    setback = data.get("setback")
    try:
        setback = float(setback) if setback not in (None, "") else None
    except (TypeError, ValueError):
        setback = None
    shapes, summary = siteplan.layout(site_m, std, program, setback_ft=setback, exclude_m=exclude_m,
                                      parking_mode=data.get("parking_mode", "market"))
    to_wgs = lambda g: mapping(reproject(inv, g))  # noqa: E731
    features = [{"type": "Feature", "geometry": to_wgs(shapes["envelope"]), "properties": {"kind": "envelope"}}] \
        if not shapes["envelope"].is_empty else []
    if exclude_m is not None:
        features.append({"type": "Feature", "geometry": to_wgs(exclude_m), "properties": {"kind": "excluded"}})
    for g in shapes["parking"]:
        features.append({"type": "Feature", "geometry": to_wgs(g), "properties": {"kind": "parking"}})
    for b in shapes["buildings"]:
        features.append({"type": "Feature", "geometry": to_wgs(b["geom"]), "properties": {
            "kind": "building", "type": b["type"], "label": b["label"], "color": b["color"],
            "gfa": round(b["gfa"]), "stalls": b["stalls"], "stories": b["stories"]}})
    summary["excluded_acres"] = excluded_acres
    summary["note"] = exclusion_note
    if data.get("action") == "save" and target is not None:
        keep = {k: data.get(k) for k in ("zone", "overrides", "program", "parking_mode", "exclude", "setback")}
        target.site_plan = {**keep, "saved_at": timezone.now().isoformat(), "saved_by": request.user.username,
                            "summary": {k: summary[k] for k in ("gfa_sf", "buildings", "stalls", "site_acres")}}
        target.save(update_fields=["site_plan", "updated_at"])
    return JsonResponse({
        "standards": std, "current_zone": current, "fips": fips, "zones": siteplan.zone_choices(fips),
        "types": siteplan.BUILDING_TYPES, "code_parking": siteplan.CODE_PARKING,
        "features": {"type": "FeatureCollection", "features": features}, "summary": summary,
        "saved": (target.site_plan if target is not None else {}) or {},
        "saved_to": "site" if isinstance(target, LandSite) else ("parcel" if target is not None else None),
    })
