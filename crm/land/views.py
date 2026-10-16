"""Land search pages: map, parcel card, live lookups, review (Watch / Pursue / Pass), search and export."""
import csv
import json
from datetime import timedelta

from django.contrib import messages
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from shapely.geometry import shape

from ..models import Activity, Campus, Company, LandCounty, LandSite, Note, Parcel, Project, Task, normalize_key
from . import layers, score
from .lookup import identify, run_checks
from .sources import LIMIT_LAYERS, STORED_LAYERS

MAX_SHAPES = 2500
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
    if params.get("out_of_state"):
        qs = qs.exclude(mail_state__in=["TX", ""])
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
        "statuses": Parcel.Status.choices, "zoning_classes": score.ZONING_CLASSES,
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
    zoom = int(request.GET.get("zoom") or 10)
    as_points = zoom < 12
    fields = ["pk", "county__name", "key", "prop_id", "owner_name", "acres", "score", "status", "killers", "lat", "lon",
              "situs_address", "near", "in_city", "state_land_use", "market_value", "zoning_class", "zoning"]
    rows = qs.order_by("-score", "-acres").only(*[f for f in fields if "__" not in f], "county__name",
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
                           "ppa": p.value_per_acre})
    return JsonResponse({"type": "FeatureCollection", "features": features, "total": total,
                         "truncated": total > len(features), "points": as_points, "list": listed})


def _card(request, parcel, live=None):
    checks = (parcel.checks or {}) if parcel else {}
    near_rows = []
    if parcel:
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
            near_rows.append({"label": spec["label"], "miles": n["miles"], "name": name})
    return render(request, "crm/partials/land_card.html", {
        "p": parcel, "live": live, "checks": checks, "near_rows": near_rows,
        "land_use": score.land_use_label(parcel.state_land_use) if parcel else "",
        "unconfirmed": score.ALWAYS_UNCONFIRMED, "statuses": Parcel.Status.choices,
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
    if status == Parcel.Status.PURSUE and not parcel.company:
        _pursue(parcel, request.user)
    parcel.save()
    return _card(request, parcel)


def _owner_company_name(owner):
    name = " ".join(owner.split()) or "Unknown landowner"
    return name.title() if name.isupper() else name


def _pursue(parcel, user):
    """Make the landowner a company (category Landowner), add a note and a task to contact them."""
    name = _owner_company_name(parcel.owner_name)
    company = Company.objects.filter(name_key=normalize_key(name)).first()
    if not company:
        company = Company.objects.create(
            name=name, categories=["Landowner"], owner=user, created_by=user,
            hq_location=", ".join(x for x in [parcel.mail_city.title(), parcel.mail_state] if x),
        )
    elif "Landowner" not in (company.categories or []):
        company.categories = [*(company.categories or []), "Landowner"]
        company.save(update_fields=["categories"])
    parcel.company = company
    link = parcel.get_absolute_url()
    summary = (f"Land search: {parcel.acres:,.0f} ac in {parcel.county.name} County "
               f"(property {parcel.prop_id or parcel.key}), screen score {parcel.score}.")
    body = "\n".join(x for x in [
        summary,
        f"Address: {parcel.situs_address}" if parcel.situs_address else "",
        f"Owner mailing: {parcel.mailing}" if parcel.mailing else "",
        f"Map: {link}",
    ] if x)
    Note.objects.create(body=body, author=user, company=company)
    Activity.objects.create(kind=Activity.Kind.NOTE, occurred_at=timezone.now(), actor=user, company=company,
                            summary=f"Parcel marked Pursue: {parcel.acres:,.0f} ac, {parcel.county.name} County",
                            body=body)
    Task.objects.create(
        title=f"Contact landowner: {name} ({parcel.acres:,.0f} ac, {parcel.county.name} Co.)",
        notes=body, due_date=timezone.localdate() + timedelta(days=7), assignee=user, company=company, created_by=user,
    )


@require_POST
def parcel_notes(request, pk):
    parcel = get_object_or_404(Parcel.objects.select_related("county", "company", "status_by"), pk=pk)
    parcel.notes = request.POST.get("notes", "").strip()
    parcel.save(update_fields=["notes", "updated_at"])
    return _card(request, parcel)


def identify_view(request):
    """Click on the map: our stored parcel if we have it, otherwise the live county/state record."""
    try:
        lat, lon = float(request.GET["lat"]), float(request.GET["lon"])
    except (KeyError, ValueError):
        raise Http404
    hit = identify(lat, lon)
    if not hit:
        return HttpResponse('<div class="land-card"><p class="muted">No parcel record here.</p></div>')
    prop = hit["fields"].get("prop_id")
    stored = Parcel.objects.filter(county__fips=hit["county"]["fips"], prop_id=prop).select_related(
        "county", "company").first() if prop else None
    if stored:
        return _card(request, stored)
    return _card(request, None, live={**hit, "lat": lat, "lon": lon,
                                      "land_use": score.land_use_label(hit["fields"].get("state_land_use")),
                                      "geometry_json": json.dumps(hit["geometry"]) if hit["geometry"] else ""})


@require_POST
def parcel_save(request):
    """Save the parcel at a point into land search (for parcels under the size cut-off, or outside loaded counties)."""
    from .parcels import owner_key

    try:
        lat, lon = float(request.POST["lat"]), float(request.POST["lon"])
    except (KeyError, ValueError):
        raise Http404
    hit = identify(lat, lon)
    if not hit or not hit["geometry"]:
        raise Http404
    c = hit["county"]
    county, _ = LandCounty.objects.get_or_create(fips=c["fips"], defaults={
        "name": c["name"], "state": c["state"], "source": c.get("source", ""),
        "cad_search_url": c.get("cad_search_url", "")})
    geom = shape(hit["geometry"])
    fields = hit["fields"]
    key = fields.get("prop_id") or fields.get("geo_id") or f"pt-{lat:.5f},{lon:.5f}"
    parcel = Parcel.objects.filter(county=county, key=key).first() or Parcel(county=county, key=key)
    for name, value in fields.items():
        f = Parcel._meta.get_field(name)
        setattr(parcel, name, value[: f.max_length] if isinstance(value, str) and getattr(f, "max_length", None) else value)
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
    run_checks(parcel)
    return _card(request, parcel)


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
                "Military base (mi)", "Latitude", "Longitude", "Link"])
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
                    m("highway"), m("rail"), m("military"), round(p.lat, 6), round(p.lon, 6),
                    base + p.get_absolute_url()])
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
    results, summary = qd.evaluate(whole, buildings, positions)
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
        keep = ("name", "kind", "sqft", "hd", "newqd", "pes", "barricaded", "mce", "k", "lat", "lon")
        target.qd_design = {"buildings": [{k: b.get(k) for k in keep} for b in out],
                            "saved_at": timezone.now().isoformat(), "saved_by": request.user.username}
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
    parcels = list(site.parcels.select_related("county"))
    owners = sorted({p.owner_name for p in parcels if p.owner_name})
    return {
        "id": site.pk, "name": site.name, "status": site.status, "status_label": site.get_status_display(),
        "campus": site.campus_id, "project": site.project_id, "home": site.home_label, "notes": site.notes,
        "parcels": [p.pk for p in parcels], "acres": round(sum(p.acres for p in parcels), 1),
        "owners": owners, "has_qd": bool((site.qd_design or {}).get("buildings")),
        "url": site.get_absolute_url(),
    }


def sites_json(request):
    """Saved land sites, plus the campus and project choices for the save dialog."""
    sites = LandSite.objects.select_related("campus", "project").prefetch_related("parcels")
    return JsonResponse({
        "sites": [_site_json(s) for s in sites],
        "campuses": [{"id": c.pk, "label": f"{c.code} · {c.name}"} for c in Campus.objects.all()],
        "projects": [{"id": p.pk, "label": p.name, "campus": p.campus_id} for p in Project.objects.filter(active=True)],
        "statuses": LandSite.Status.choices,
    })


def site_json(request, pk):
    site = get_object_or_404(LandSite, pk=pk)
    data = _site_json(site)
    parcels = list(site.parcels.all())
    from shapely.geometry import mapping
    data["geometry"] = (mapping(
        _union_geometry(parcels)) if parcels else None)
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
        site.parcels.set(Parcel.objects.filter(pk__in=[int(i) for i in data["parcels"] if str(i).isdigit()]))
    return JsonResponse(_site_json(site))


@require_POST
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
