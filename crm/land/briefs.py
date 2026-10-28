"""One-page site briefs: what a site is, what's around it, what to worry about and what is still unverified, plus
aerial images with the site outline drawn on them.

A brief covers a saved LandSite (one parcel or an assemblage). Everything comes from data the app already holds
(parcel roll, zoning overlays, distances to power / roads / rail, live flood / wetland / utility / incentive checks);
checks that haven't been run yet are run when the brief is built. Aerials are Esri World Imagery, rendered here so
the page prints and saves as a PDF with the outline in place.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from collections import defaultdict

import requests
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from shapely.geometry import shape

from . import brief_options, comps as comps_mod, listing as listing_mod, score, signals, trim, zoning_ref
from .lookup import checks_for, run_checks_many
from .sources import STORED_LAYERS, fema_flood_url
from .views import near_rows

SITE_SIZE = (1400, 760)
CONTEXT_SIZE = (1000, 900)
STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "crm"
ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services"
ATTRIBUTION = "Imagery © Esri, Maxar, Earthstar Geographics"
_to_merc = Transformer.from_crs(4326, 3857, always_xy=True).transform
_CACHE = {}  # (bbox, size, labels) -> (time, jpeg bytes)
CACHE_SECONDS = 3600


# --- Aerials -------------------------------------------------------------------------------------------------------

def _bbox_merc(geoms, mode, ratio):
    """Web-Mercator bbox with the picture's aspect ratio: tight (the site fills it) or context (about 5 mi across)."""
    xs, ys = [], []
    for g in geoms:
        minx, miny, maxx, maxy = g.bounds
        for x, y in ((minx, miny), (maxx, maxy)):
            mx, my = _to_merc(x, y)
            xs.append(mx)
            ys.append(my)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    if mode == "context":
        width = 9000
    else:
        width = max((max(xs) - min(xs)) * 1.35, (max(ys) - min(ys)) * 1.35 * ratio, 1400)
    height = width / ratio
    return cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2


def _fetch(layer, bbox, size, transparent=False):
    params = {"bbox": ",".join(f"{v:.2f}" for v in bbox), "bboxSR": 3857, "imageSR": 3857, "size": f"{size[0]},{size[1]}",
              "format": "png32" if transparent else "jpg", "f": "image"}
    if transparent:
        params["transparent"] = "true"
    resp = requests.get(f"{ESRI}/{layer}/MapServer/export", params=params, timeout=30)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    return img if img.size == tuple(size) else img.resize(tuple(size))


def _label_font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


# FEMA's own flood-hazard layer (NFHL layer 28), switched on as a transparent overlay. FEMA paints it at about 30%
# opacity (cyan = 100-year Zone A / AE, orange = shaded Zone X, white labels), which is too faint on aerial photos,
# so the fills are strengthened here; nothing is redrawn.
FEMA_FILLS = {"100": (0, 229, 255), "x": (255, 129, 0)}  # the colors FEMA uses, as served
FEMA_LEGEND = {"100": "FEMA 100-year floodplain (Zone A / AE)", "x": "FEMA shaded Zone X (500-year / reduced risk)"}
FEMA_ALPHA = 130
ROAD_LAYERS = ("Reference/World_Transportation", "Reference/World_Boundaries_and_Places")


def _fetch_fema(bbox, size):
    """FEMA flood overlay as a transparent RGBA image, with retries (FEMA's server drops connections now and then)."""
    base, layer = fema_flood_url().rsplit("/", 1)  # FEMA renumbers its layers now and then, so look the id up (cached)
    params = {"bbox": ",".join(f"{v:.2f}" for v in bbox), "bboxSR": 3857, "imageSR": 3857, "size": f"{size[0]},{size[1]}",
              "format": "png32", "transparent": "true", "layers": f"show:{layer}", "f": "image"}
    for attempt in range(3):  # short timeouts: a slow FEMA server must not hold the whole report up
        try:
            resp = requests.get(f"{base}/export", params=params, timeout=25)
            resp.raise_for_status()
            return Image.open(io.BytesIO(resp.content)).convert("RGBA")
        except (requests.RequestException, OSError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def _strengthen_flood(img):
    """(image with FEMA's fills made opaque enough to see, {kinds present}). Labels and edges keep their own alpha."""
    r, g, b, a = img.split()
    present = set()
    pixels = img.load()
    for kind, (cr, cg, cb) in FEMA_FILLS.items():  # sample on a grid: cheap, and the polygons are large
        for x in range(0, img.width, 6):
            for y in range(0, img.height, 6):
                px_ = pixels[x, y]
                if px_[3] and abs(px_[0] - cr) < 6 and abs(px_[1] - cg) < 6 and abs(px_[2] - cb) < 6:
                    present.add(kind)
                    break
            if kind in present:
                break
    boosted = a.point(lambda v: FEMA_ALPHA if 0 < v <= 90 else v)
    return Image.merge("RGBA", (r, g, b, boosted)), present


def render_aerial(parcel_geoms, site_geom, mode="site", size=(1200, 720), flood=True):
    """JPEG bytes: Esri imagery with each parcel outlined thin and the site outline thick. mode: 'site' | 'context'."""
    bbox = _bbox_merc([site_geom], mode, size[0] / size[1])
    key = (tuple(round(v) for v in bbox), size, mode)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    flood_kinds = set()
    fit = lambda im: im if im.size == tuple(size) else im.resize(tuple(size))  # noqa: E731
    # The imagery, the FEMA layer and the road / place layers are separate requests: fetch them all at once.
    with ThreadPoolExecutor(max_workers=2 + len(ROAD_LAYERS)) as pool:
        f_img = pool.submit(_fetch, "World_Imagery", bbox, size)
        f_fema = pool.submit(_fetch_fema, bbox, size) if flood else None
        f_roads = [pool.submit(_fetch, layer, bbox, size, True) for layer in ROAD_LAYERS]
        try:
            img = fit(f_img.result())
        except (requests.RequestException, OSError):
            img = Image.new("RGBA", size, (60, 60, 60, 255))
            ImageDraw.Draw(img).text((20, 20), "Aerial unavailable (Esri imagery did not respond)", fill="white",
                                     font=_label_font(22))
        if f_fema is not None:
            try:
                fema, flood_kinds = _strengthen_flood(fit(f_fema.result()))
                img = Image.alpha_composite(img, fema)
            except (requests.RequestException, OSError):
                flood = None  # FEMA's server is down: the aerial is still useful, and the legend says so
        for fut in f_roads:  # road symbols and names, place names
            try:
                img = Image.alpha_composite(img, fit(fut.result()))
            except (requests.RequestException, OSError):
                pass
    minx, miny, maxx, maxy = bbox

    def px(coords):
        out = []
        for x, y in coords:
            mx, my = _to_merc(x, y)
            out.append(((mx - minx) / (maxx - minx) * size[0], (maxy - my) / (maxy - miny) * size[1]))
        return out

    def polygons(g):
        return list(getattr(g, "geoms", [g]))

    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for g in parcel_geoms:
        for poly in polygons(g):
            if poly.geom_type == "Polygon":
                draw.line(px(poly.exterior.coords), fill=(255, 255, 255, 200), width=1)
    for poly in polygons(site_geom):
        if poly.geom_type != "Polygon":
            continue
        pts = px(poly.exterior.coords)
        if not flood_kinds:  # with the flood layer on, an outline only: a tint would turn cyan / orange green
            draw.polygon(pts, fill=(255, 212, 0, 55 if mode == "site" else 110))
        draw.line(pts + pts[:1], fill=(255, 212, 0, 255), width=5 if mode == "site" else 4, joint="curve")
    img = Image.alpha_composite(img, overlay)
    # Attribution (required by Esri) and, on the context map, a north-up note.
    draw = ImageDraw.Draw(img)
    font = _label_font(15)
    for dx, dy in ((1, 1), (-1, -1), (1, -1), (-1, 1)):
        draw.text((10 + dx, size[1] - 24 + dy), ATTRIBUTION, fill="black", font=font)
    draw.text((10, size[1] - 24), ATTRIBUTION, fill="white", font=font)
    if flood_kinds:  # legend, bottom left above the attribution
        rows = [FEMA_LEGEND[k] for k in ("100", "x") if k in flood_kinds]
        top = size[1] - 36 - 22 * len(rows)
        draw.rectangle([6, top - 6, 372, size[1] - 32], fill=(0, 0, 0, 165))
        for i, (kind, label) in enumerate((k, FEMA_LEGEND[k]) for k in ("100", "x") if k in flood_kinds):
            y = top + i * 22
            draw.rectangle([12, y, 32, y + 14], fill=FEMA_FILLS[kind] + (FEMA_ALPHA,), outline="white")
            draw.text((40, y - 1), label, fill="white", font=_label_font(15))
    elif flood is None:
        draw.text((12, size[1] - 52), "FEMA flood layer unavailable right now", fill="white", font=_label_font(14))
    elif flood and mode == "site":
        draw.text((12, size[1] - 52), "No FEMA flood zones mapped in this view", fill="white", font=_label_font(14))
    if mode == "site":  # scale bar
        meters_per_px = (maxx - minx) / size[0] * math.cos(math.radians(site_geom.centroid.y))
        for feet in (500, 1000, 2000, 5000, 10000):
            if feet * 0.3048 / meters_per_px > 140:
                break
        bar = feet * 0.3048 / meters_per_px
        x0, y0 = size[0] - bar - 20, size[1] - 34
        draw.rectangle([x0 - 6, y0 - 22, x0 + bar + 6, y0 + 14], fill=(0, 0, 0, 140))
        draw.line([x0, y0, x0 + bar, y0], fill="white", width=3)
        draw.line([x0, y0 - 5, x0, y0 + 5], fill="white", width=3)
        draw.line([x0 + bar, y0 - 5, x0 + bar, y0 + 5], fill="white", width=3)
        draw.text((x0, y0 - 20), f"{feet:,} ft" if feet < 5280 else f"{feet / 5280:g} mi", fill="white", font=font)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=88)
    data = buf.getvalue()
    if len(_CACHE) > 40:
        _CACHE.clear()
    _CACHE[key] = (time.time(), data)
    return data


# --- Brief data ----------------------------------------------------------------------------------------------------

INDUSTRIAL = ("industrial",)
FLOOD_WARN_PCT = 20
WETLAND_WARN_PCT = 10


def _zone_label(parcel):
    z = (parcel.overlays or {}).get("zoning") or {}
    code = (parcel.zoning or "").split(" / ")[0].strip() or z.get("value") or ""
    return code, z


def _summarise_killers(parcels):
    """Deal-killers across a site: one line per kind ('3 parcels over the limit for ... (worst 99%)'), not one per parcel."""
    import re

    grouped, plain = {}, set()
    for p in parcels:
        for k in p.killers or []:
            m = re.match(r"(\d+)%\s+(.*)", k)
            if m:
                g = grouped.setdefault(m.group(2), {"parcels": set(), "worst": 0})
                g["parcels"].add(p.pk)
                g["worst"] = max(g["worst"], int(m.group(1)))
            else:
                plain.add(k)
    lines = [f"{len(g['parcels'])} of {len(parcels)} parcels are over the deal-killer limit for \"{what}\" (worst {g['worst']}%)"
             if len(parcels) > 1 else f"{g['worst']}% {what}" for what, g in grouped.items()]
    return sorted(lines) + sorted(plain)


def _checks_missing(parcel):
    have = parcel.checks or {}
    wanted = set(checks_for(parcel.county.state, parcel.county.name))
    return bool(wanted - set(have)) or any(isinstance(v, dict) and (v.get("error") or v.get("refresh_error"))
                                           for v in have.values())


def sfha_zones(zones):
    """The 100-year (Special Flood Hazard Area) zones among FEMA zone codes: A, AE, AH, AO, AR, A99, V, VE.
    Zone X (500-year / minimal hazard) and D (undetermined) are not."""
    return [z for z in zones or [] if z[:1] in ("A", "V")]


def maps_url(query):
    """A Google Maps link for a lat,lng pair or an address."""
    from urllib.parse import quote_plus

    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)


def build(site, refresh=False):
    """Everything the brief page prints, as plain dicts."""
    pairs = trim.parts(site)  # [(deal, shape)] included parcels, trimmed to the portion the site needs
    if not pairs:
        raise ValueError("This site has no parcels")
    deals = [d for d, _ in pairs]
    parcels = [d.parcel for d in deals]
    stale = [p for p in parcels if refresh or _checks_missing(p)]
    try:
        run_checks_many(stale)  # all the parcels at once; each check inside is already parallel
    except Exception:  # an agency being down shouldn't stop the brief
        pass
    acres = {p.pk: d.acres for d, p in zip(deals, parcels)}
    gross = sum(acres.values())
    ck = lambda p, name: (p.checks or {}).get(name) or {}  # noqa: E731

    def weighted(name, key):
        have = [(acres[p.pk], ck(p, name).get(key)) for p in parcels if isinstance(ck(p, name).get(key), (int, float))]
        covered = sum(a for a, _ in have)
        return round(sum(a * v for a, v in have) / covered, 1) if covered else None

    flood = weighted("flood", "sfha_pct")
    wet = weighted("wetlands", "pct")
    net = None
    if all(ck(p, "net_usable") for p in parcels):
        net = round(sum(acres[p.pk] * (1 - ck(p, "net_usable").get("lost_pct", 0) / 100) for p in parcels), 1)

    # Owners
    owners = {}
    for p in parcels:
        o = owners.setdefault(p.owner_name or "Owner not listed", {"name": p.owner_name or "Owner not listed", "acres": 0,
                                                                   "parcels": 0, "mail": ""})
        o["acres"] += acres[p.pk]
        o["parcels"] += 1
        o["mail"] = o["mail"] or ", ".join(x for x in [p.mail_city, p.mail_state] if x)
    owners = sorted(({**o, "acres": round(o["acres"], 1)} for o in owners.values()), key=lambda o: -o["acres"])

    # Zoning, grouped by what the map says
    zoning = {}
    for p in parcels:
        code, z = _zone_label(p)
        name = (z.get("name") or "").strip()
        if name.upper().startswith(code.upper()):
            name = name[len(code):].strip()
        authority = z.get("authority") or ""
        label = code or "No zoning data"
        row = zoning.setdefault((label, authority), {
            "code": label, "name": name, "authority": authority, "acres": 0,
            "bucket": score.ZONING_LABELS.get(p.zoning_class, "") if p.zoning_class else "",
            "industrial": p.zoning_class in INDUSTRIAL})
        row["acres"] += acres[p.pk]
    zoning = sorted(({**r, "acres": round(r["acres"], 1), "pct": round(100 * r["acres"] / gross) if gross else 0}
                     for r in zoning.values()), key=lambda r: -r["acres"])
    industrial_pct = round(100 * sum(r["acres"] for r in zoning if r["industrial"]) / gross) if gross else 0

    # Nearest infrastructure across the site's parcels
    best = {}
    for p in parcels:
        for row in near_rows(p):
            if row["layer"] not in best or row["miles"] < best[row["layer"]]["miles"]:
                best[row["layer"]] = row
    near = [best[k] for k in STORED_LAYERS if k in best]

    def union_names(name, key="providers"):
        seen, out = set(), []
        for p in parcels:
            for x in ck(p, name).get(key) or []:
                label = x["name"] if isinstance(x, dict) else x
                if label and label not in seen:
                    seen.add(label)
                    out.append(x)
        return out

    electric = union_names("electric")
    water = union_names("water", "details")
    sewer = union_names("sewer", "details")

    def nearest_line(name):
        lines = [ck(p, name).get("nearest_line") for p in parcels]
        lines = [x for x in lines if x]
        return min(lines, key=lambda x: x["miles"]) if lines else None

    epa, seen_epa = [], set()
    for s in sorted((s for p in parcels for s in ck(p, "epa").get("sites") or []), key=lambda s: s["miles"]):
        if s["name"] not in seen_epa:  # every parcel reports the same nearby site
            seen_epa.add(s["name"])
            epa.append(s)
    incentives = {}
    for p in parcels:
        for prog in ck(p, "incentives").get("programs") or []:
            incentives.setdefault(prog["key"], prog)
    hubzone = any(ck(p, "hubzone").get("qualified") for p in parcels)

    # Score and reasons (largest parcel speaks for the site)
    big = max(parcels, key=lambda p: acres[p.pk])
    scores = [p.score for p in parcels]
    avg_score = round(sum(acres[p.pk] * p.score for p in parcels) / gross) if gross else 0
    killers = _summarise_killers(parcels)

    jurisdictions = sorted({p.in_city or f"Unincorporated {p.county.name} County" for p in parcels})
    counties = sorted({f"{p.county.name} County, {p.county.state}" for p in parcels})
    values = [p.market_value for p in parcels if p.market_value]
    land_values = [p.land_value for p in parcels if p.land_value]

    t = best.get("transmission")
    hwy = best.get("highway")
    flags = []

    def flag(kind, text):
        flags.append({"kind": kind, "text": text})

    if industrial_pct >= 80:
        flag("good", f"{industrial_pct}% of the acreage is zoned industrial ({', '.join(r['code'] for r in zoning if r['industrial'])}) "
                     f"per {next(r['authority'] for r in zoning if r['industrial'])}")
    elif zoning and zoning[0]["code"] != "No zoning data":
        flag("bad" if industrial_pct < 20 else "warn",
             f"Only {industrial_pct}% of the acreage is zoned industrial on the public map. Largest zone: "
             f"{zoning[0]['code']}{(' ' + zoning[0]['name']) if zoning[0]['name'] else ''} ({zoning[0]['pct']}%). "
             "Check against what the broker is claiming.")
    else:
        flag("warn", "No zoning data on the public layers for this land: confirm the zoning with the jurisdiction.")
    zones = sorted({z for p in parcels for z in ck(p, "flood").get("zones") or []})
    hundred_year = sfha_zones(zones)
    # Parcels the live checks couldn't cover: the site's flood and wetland shares are over the other parcels only.
    no_flood = [p for p in parcels if not isinstance(ck(p, "flood").get("sfha_pct"), (int, float))]
    no_wet = [p for p in parcels if "wetlands" in checks_for(p.county.state, p.county.name)
              and not isinstance(ck(p, "wetlands").get("pct"), (int, float))]
    if flood is not None and flood >= FLOOD_WARN_PCT:
        approx = " Zone A is FEMA's approximate mapping (no base flood elevations): an engineering study or LOMR can change it."             if hundred_year and set(hundred_year) <= {"A"} else ""
        in_zones = f" (zone {', '.join(hundred_year)})" if hundred_year else ""
        flag("bad", f"{flood:.0f}% of the site is in the FEMA 100-year floodplain{in_zones}"
                    + (f"; net usable about {net:,.0f} of {gross:,.0f} ac." if net is not None else ".") + approx)
    elif flood is not None and not no_flood:
        flag("good", f"Little or no FEMA flood exposure ({flood:.0f}% in the 100-year zone)")
    if no_flood or no_wet:
        parts = [f"FEMA flood data for {len(no_flood)} of {len(parcels)} parcels "
                 f"({sum(acres[p.pk] for p in no_flood):,.0f} ac)" if no_flood else "",
                 f"wetland data for {len(no_wet)} of {len(parcels)} parcels" if no_wet else ""]
        flag("warn", "Live checks incomplete: missing " + " and ".join(x for x in parts if x)
                     + ". The flood and wetland figures cover the other parcels only; re-run live checks.")
    if any(ck(p, name).get("refresh_error") for p in parcels for name in ("flood", "wetlands")):
        flag("warn", "FEMA or the wetlands service didn't respond on the last re-check: flood and wetland figures are "
                     "from the check before.")
    if wet is not None and wet >= WETLAND_WARN_PCT:
        flag("warn", f"{wet:.0f}% mapped wetlands / waters")
    if t:
        flag("good" if t["miles"] <= 2 else "warn" if t["miles"] <= 5 else "bad",
             f"Nearest transmission line {t['miles']:.1f} mi ({t['name']})")
    if hwy:
        flag("good" if hwy["miles"] <= 2 else "warn", f"{hwy['miles']:.1f} mi to {hwy['name']}")
    near_epa = [s for s in epa if s["miles"] <= 0.5]
    if near_epa:
        flag("warn", f"EPA cleanup site within half a mile: {near_epa[0]['name']} ({near_epa[0]['miles']:.2f} mi)")
    if hubzone:
        flag("good", "In a HUBZone (SBA): useful for small-business defense tenants")
    oz = [v["label"] for k, v in incentives.items() if k == "oz"]
    if oz:
        flag("good", "In a federal Opportunity Zone")
    for k in killers:
        if flood is not None and flood >= FLOOD_WARN_PCT and "floodplain" in k.lower():
            continue  # already flagged above
        flag("bad", k)
    order = {"bad": 0, "warn": 1, "good": 2}
    flags.sort(key=lambda f: order[f["kind"]])

    to_verify = list(score.ALWAYS_UNCONFIRMED)[:4]
    authority = next((r["authority"] for r in zoning if r["authority"]), "the jurisdiction")
    to_verify.insert(0, f"Zoning and allowed uses: confirm with {authority} planning (public map shown here, not a zoning verification letter)")

    parcel_rows = []
    for p in sorted(parcels, key=lambda p: -acres[p.pk]):
        z = (p.overlays or {}).get("zoning") or {}
        parcel_rows.append({
            "apn": p.prop_id or ("No parcel number yet" if p.key.startswith("NAP") else p.key), "owner": p.owner_name,
            "acres": round(acres[p.pk], 1), "zoning": (p.zoning or "").split(" / ")[0] or "-",
            "authority": z.get("authority", ""), "use": score.land_use_label(p.state_land_use) or "",
            "mail": ", ".join(x for x in [p.mail_city, p.mail_state] if x), "value": p.land_value or p.market_value,
            "address": p.situs_address, "map": maps_url(f"{p.lat:.6f},{p.lon:.6f}"),
        })
    biggest = max(parcels, key=lambda p: acres[p.pk])
    # Recorded land sales nearby (outside-safe: no comps marked internal, no notes, no buyer or seller names) and
    # the owners' off-market signals (team view only: the template keeps them off the PDF and the printout).
    center_lat = sum(p.lat * acres[p.pk] for p in parcels) / (gross or 1)
    center_lon = sum(p.lon * acres[p.pk] for p in parcels) / (gross or 1)
    sale_comps = comps_mod.nearby(lat=center_lat, lon=center_lon, county=biggest.county, outside=True, limit=8,
                                  exclude_parcels=[p.pk for p in parcels])
    owner_signals = [(p, signals.chips(p)) for p in sorted(parcels, key=lambda p: -acres[p.pk]) if p.signals]
    return {
        "sale_comps": sale_comps, "owner_signals": owner_signals,
        "parcel_rows": parcel_rows, "listing": listing_mod.display(site.listing),
        "map_url": maps_url(f"{biggest.lat:.6f},{biggest.lon:.6f}"), "hide": brief_options.hidden(site), "extras": brief_options.extras(site),
        "site": site, "parcels": parcels, "n_parcels": len(parcels), "gross": round(gross, 1), "net": net,
        "flood": flood, "flood_zones": zones, "wetlands": wet, "owners": owners, "zoning": zoning, "industrial_pct": industrial_pct,
        "near": near, "electric": electric, "water": water, "sewer": sewer,
        "water_line": nearest_line("water"), "sewer_line": nearest_line("sewer"),
        "epa": epa[:3], "incentives": list(incentives.values()), "hubzone": hubzone,
        "score": avg_score, "score_range": (min(scores), max(scores)), "reasons": (big.score_reasons or [])[:8],
        "killers": killers, "flags": flags, "to_verify": to_verify,
        "jurisdictions": jurisdictions, "counties": counties,
        "market_value": sum(values) or None, "land_value": sum(land_values) or None,
        "tax_year": next((p.tax_year for p in parcels if p.tax_year), ""),
        "checked_at": max((p.checked_at for p in parcels if p.checked_at), default=None),
        "built_at": timezone.now(),
        "bad": sum(1 for f in flags if f["kind"] == "bad"), "warn": sum(1 for f in flags if f["kind"] == "warn"),
        "good": sum(1 for f in flags if f["kind"] == "good"),
        "ranking": (len(killers) + sum(1 for f in flags if f["kind"] == "bad"),
                    sum(1 for f in flags if f["kind"] == "warn"), -(net if net is not None else gross)),
    }


# --- Notes & definitions -------------------------------------------------------------------------------------------

FLOOD_NOTES = [
    ("sfha", "FEMA 100-year floodplain (Special Flood Hazard Area)",
     "Land FEMA maps as having a 1% chance of flooding in any given year (the \"base flood\"). Federally backed mortgages "
     "require flood insurance there, and building usually needs a floodplain development permit. It is shown in cyan."),
    ("A", "Zone A",
     "100-year floodplain mapped by approximate methods: FEMA has not published base flood elevations. A site-specific "
     "engineering study can refine the line, and FEMA can then revise the map (see LOMR / LOMA)."),
    ("AE", "Zone AE",
     "100-year floodplain from a detailed study, with base flood elevations (the water level to build above) published."),
    ("floodway", "Floodway",
     "The channel and the adjoining land that must stay clear so the base flood does not rise more than a set height "
     "(normally 1 foot). Building in a floodway is very restricted."),
    ("X", "Zone X (shaded orange on the maps)",
     "Moderate flood risk: the 0.2% annual-chance (500-year) flood, or areas where 1% flooding is shallow (under 1 foot) "
     "or the drainage area is under 1 square mile, or land protected by a levee. Flood insurance is not federally required. "
     "Unshaded Zone X (minimal hazard) is not drawn."),
    ("lomr", "LOMR / LOMA",
     "Letter of Map Revision / Amendment: FEMA's formal change to a flood map or to one property's designation after an "
     "engineering study or fill, which can remove land from the 100-year floodplain."),
]

GENERAL_NOTES = {
    "net": ("Net usable acres",
            "Gross acres minus the mapped FEMA 100-year floodplain and National Wetlands Inventory wetlands. Easements, "
            "setbacks, steep slopes and safety (QD) arcs are not deducted. Zone A land counts as lost even though a flood "
            "study might recover it."),
    "wetlands": ("Wetlands (NWI)",
                 "U.S. Fish & Wildlife Service National Wetlands Inventory: mapped from aerial photos. Large lakes are left "
                 "out. It is a screening layer, not a jurisdictional wetland delineation."),
    "score": ("Buy-box score",
              "ACMI's 0 to 100 screen of how well a site fits a defense-industrial campus: size, zoning, power, access, "
              "owner, utilities and incentives, with penalties for deal-killers. Acre-weighted across a site's parcels. "
              "A screening aid, not a valuation or a recommendation."),
    "zoning": ("Zoning (public map)",
               "The zoning shown on the jurisdiction's published GIS layer, not a zoning verification letter. \"I-2 / CR\" is "
               "I-2 with an overlay district. A city's name in a county layer means that city zones the land. \"No zoning data\" "
               "means the layers do not cover that parcel. \"% industrial\" is the share of acres in industrial districts."),
    "distances": ("Distances to power, roads and rail",
                  "Straight-line distance from the site edge to the nearest mapped feature (federal HIFLD and state data). "
                  "Nearness does not mean capacity or an available connection: confirm with the utility."),
    "providers": ("Utility providers",
                  "Electric: federal HIFLD retail territories overlap at their edges, so the list is a starting point, and "
                  "irrigation districts and federal power marketers are left out. An electrical district is a local public "
                  "power district. Water and sewer: private companies holding an Arizona Corporation Commission (ACC) "
                  "Certificate of Convenience and Necessity, plus distance to city mains where the city publishes them. "
                  "City and district service areas are not mapped, so \"none mapped\" can still mean city service."),
    "value": ("Assessed / land value",
              "Values from the county assessor's roll. They are not market prices and often lag sales, especially for "
              "farmland valued on current use."),
    "hubzone": ("HUBZone",
                "SBA program: small businesses based in a Historically Underutilized Business Zone can win federal contract "
                "preferences, which helps small defense suppliers."),
    "imagery": ("Aerial imagery and outlines",
                "Esri World Imagery (image dates vary). Parcel and site outlines come from county parcel data and are "
                "approximate: they are not a survey."),
}


def definitions(briefs):
    """[(term, text)] for the terms that appear in these briefs, flood terms first."""
    out = []
    zones = {z for b in briefs for z in (b.get("flood_zones") or [])}
    have_flood = any(b.get("flood") is not None for b in briefs)
    if have_flood:
        wanted = {"sfha", "lomr"}
        if "A" in zones:
            wanted.add("A")
        if "AE" in zones:
            wanted.add("AE")
        if any((p.checks or {}).get("flood", {}).get("floodway_pct") for b in briefs for p in b["parcels"]):
            wanted.add("floodway")
        wanted.add("X")  # the aerials draw shaded Zone X wherever FEMA maps it
        out += [(term, text) for key, term, text in FLOOD_NOTES if key in wanted]
    out.append(GENERAL_NOTES["net"])
    if any(b.get("wetlands") is not None for b in briefs):
        out.append(GENERAL_NOTES["wetlands"])
    out += [GENERAL_NOTES["score"], GENERAL_NOTES["zoning"], GENERAL_NOTES["distances"], GENERAL_NOTES["providers"]]
    if any(b.get("market_value") or b.get("land_value") for b in briefs):
        out.append(GENERAL_NOTES["value"])
    seen = set()
    for b in briefs:  # incentive programs found on these sites
        for prog in b.get("incentives") or []:
            if prog["key"] not in seen and prog.get("summary"):
                seen.add(prog["key"])
                out.append((prog["label"], prog["summary"]))
    if any(b.get("hubzone") for b in briefs):
        out.append(GENERAL_NOTES["hubzone"])
    out.append(GENERAL_NOTES["imagery"])
    return out


# What a site's `listing` JSON may hold (all optional); the brief's second page prints whatever is there.
LISTING_KEYS = ("source", "price", "taxes", "size", "zoning_claim", "contacts", "highlights", "utilities", "access",
                "drive_times", "market", "nearby", "documents", "concept_plan", "claims_check")

# --- PDF export ----------------------------------------------------------------------------------------------------
# The server renders the page itself (aerials and logo embedded, so no login is needed) and has headless Chrome / Edge
# print it. Without a browser on the machine the page's Print button still works.

class PdfUnavailable(RuntimeError):
    pass


def chrome_path():
    """A Chrome / Chromium / Edge executable: ACMI_CHROME, then PATH, then the usual install folders."""
    env = os.environ.get("ACMI_CHROME")
    if env and os.path.exists(env):
        return env
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if os.path.exists(path):
            return path
    return None


def html_to_pdf(html, timeout=180):
    """PDF bytes for a standalone HTML page (letter pages come from its @page rule)."""
    chrome = chrome_path()
    if not chrome:
        raise PdfUnavailable("no Chrome or Edge is installed on the server (set ACMI_CHROME to its path)")
    with tempfile.TemporaryDirectory() as folder:
        src, out = Path(folder) / "brief.html", Path(folder) / "brief.pdf"
        src.write_text(html, encoding="utf-8")
        cmd = [chrome, "--headless=new", "--disable-gpu", "--disable-dev-shm-usage", "--no-pdf-header-footer",
               f"--user-data-dir={Path(folder) / 'profile'}", f"--print-to-pdf={out}"]
        # Chrome's sandbox needs user namespaces, which root and Cloud Run's container sandbox don't give it.
        if os.environ.get("ACMI_CHROME_NO_SANDBOX") == "1" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            cmd.append("--no-sandbox")
        try:
            subprocess.run(cmd + [src.as_uri()], capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PdfUnavailable(f"Chrome would not run ({exc})")
        if not out.exists():
            raise PdfUnavailable("Chrome did not produce a PDF")
        return out.read_bytes()


def export_name(site_name=None, day=None):
    """PDF file name: ACMI_Site_Brief_<Site_Name_Words>_2026.10.16.pdf (or ACMI_Site_Briefs_2026.10.16.pdf for a package)."""
    import re

    stamp = (day or timezone.localdate()).strftime("%Y.%m.%d")
    if not site_name:
        return f"ACMI_Site_Briefs_{stamp}.pdf"
    words = "_".join(w for w in re.split(r"[^A-Za-z0-9]+", site_name) if w)
    return f"ACMI_Site_Brief_{words}_{stamp}.pdf"


def data_uri(data, mime):
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def logo_data_uri():
    return data_uri((STATIC_DIR / "logo_blue.png").read_bytes(), "image/png")


def pdf_css():
    return (STATIC_DIR / "brief.css").read_text(encoding="utf-8") + (
        "\nbody { margin: 0; font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }"
        "\n.report-logo { display: block; height: auto; margin: 0 0 12px; }\n")


def embed(brief):
    """Put the site's two aerials into the brief as data URIs, for the standalone PDF page."""
    geoms, outline = site_geometries(brief["site"])
    with ThreadPoolExecutor(max_workers=2) as pool:  # the close-up and the context map at once
        near = pool.submit(render_aerial, geoms, outline, "site", SITE_SIZE)
        wide = pool.submit(render_aerial, geoms, outline, "context", CONTEXT_SIZE)
        brief["aerial_site"] = data_uri(near.result(), "image/jpeg")
        brief["aerial_context"] = data_uri(wide.result(), "image/jpeg")
    return brief


def zoning_sections(briefs):
    """Zoning districts to print for these briefs (deduplicated), from the researched code reference."""
    merged = {"districts": [], "undocumented": [], "no_layer": {}}
    seen = set()
    for b in briefs:
        # Codes that cover under 1% of a site's acreage (slivers) are left out of the write-up.
        major = {z["code"].upper() for z in b["zoning"] if z["pct"] >= 1}
        parcels = [p for p in b["parcels"] if not major or zoning_ref._code_of(p).upper() in major
                   or not zoning_ref._code_of(p)]
        ref = zoning_ref.references(parcels)
        for d in ref["districts"]:
            ident = (d["key"], d["district"]["code"])
            if ident not in seen:
                seen.add(ident)
                merged["districts"].append(d)
        merged["undocumented"] += [u for u in ref["undocumented"] if u not in merged["undocumented"]]
        merged["no_layer"].update(ref["no_layer"])
    return merged


def site_geometries(site):
    """(parcel geometries, site outline) for the aerials: included parcels, trimmed to the site's portions."""
    pairs = trim.parts(site)
    geoms = [g for _, g in pairs]
    return geoms, trim.union(geoms)
