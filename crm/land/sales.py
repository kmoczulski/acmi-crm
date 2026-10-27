"""Last transfer (date, price, how conveyed, deed) for each stored parcel, where the county publishes it, and the sale
comps made from them.

Sources were checked field by field in Oct 2026 (docs/HANDOFF.md, "Built 2026-10-26: land-sales"):
- Charles, MD: the statewide MD iMAP parcel layer (SDAT), joined on ACCTID. It also fills the owner mailing address
  (the county layer leaves it blank on vacant rural parcels) and the land / improvement / total values.
- Hays, TX: Hays County's own parcel layer (Hays CAD data): deed date and volume / page / document number. Texas is a
  non-disclosure state, so there is never a price.
- Madison and Morgan AL, Cumberland / Harnett / Moore / Hoke NC, Maricopa and Pinal AZ: fields on the layer each
  county already loads from.
Nowhere else publishes a sale or deed date we could find (TxGIO StratMap's DATE_ACQ is when TxGIO got the file).

Blank values never overwrite stored ones. `refresh_county` is what `load_parcels` runs after a load and what
`manage.py refresh_sales` runs on its own (no geometry is downloaded).
"""
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone as dt_tz

from django.db import transaction

from ..models import Parcel, SaleComp
from . import counties
from .sources import arcgis_query

MD_PARCELS = "https://mdgeodata.md.gov/imap/rest/services/PlanningCadastre/MD_ParcelBoundaries/MapServer/0"
HAYS_PARCELS = "https://services5.arcgis.com/bVphnK8rPe5MHUSr/arcgis/rest/services/Hays_County_Parcels/FeatureServer/0"
SALE_FIELDS = ["last_sale_date", "last_sale_price", "last_sale_type", "last_sale_arms_length", "last_sale_deed",
               "last_sale_source"]
# A price under this is a nominal deed ($10 and other good and valuable consideration), not a sale.
MIN_PRICE = 1000
# Maricopa's sale date and its latest deed are separate fields: the price belongs to the deed only when they're this close.
SAME_TRANSFER_DAYS = 90

# SDAT "how conveyed" (CONVEY1), from the Maryland Assessment Procedure Manual: 1-3 are arms-length (market) sales,
# 4 is everything else (gifts, estates, foreclosures, tax sales, related parties).
MD_CONVEY = {
    1: ("Arms-length, improved", True),
    2: ("Arms-length, vacant land", True),
    3: ("Arms-length, multiple accounts", True),
    4: ("Not arms-length (gift, estate, related parties...)", False),
}
# Madison County, AL deed types. Inherited or given land is a non-arms-length transfer; the rest say nothing either way.
MADISON_DEEDS = {
    "WARR": ("Warranty deed", None), "SURV": ("Survivorship deed", None), "DEED": ("Deed", None),
    "QTCL": ("Quitclaim deed", False), "LIFE": ("Life estate deed", False), "LIFE ESTATE": ("Life estate deed", False),
    "PREP": ("Personal representative's deed (estate)", False), "EXEC": ("Executor's deed (estate)", False),
    "WILL": ("By will (inherited)", False), "DIST": ("Distribution deed (estate)", False),
    "AFFD": ("Affidavit (heirship or similar)", False), "GIFT": ("Gift deed", False),
    "TRUST": ("Trustee's deed", None), "TRUS": ("Trustee's deed", None), "FORC": ("Foreclosure deed", False),
    "CORR": ("Correction deed", None), "PLAT": ("Plat recorded", None),
}


# --- Parsing ---------------------------------------------------------------------------------------------------

def _t(rec, k):
    v = rec.get(k)
    return "" if v is None else str(v).strip()


def _valid(d):
    return d if d and date(1901, 1, 1) < d <= date.today() + timedelta(days=31) else None


def ymd(value):
    """'19910111' or 20160214.0 -> date."""
    digits = re.sub(r"\D", "", str(value or "").split(".")[0])
    try:
        return _valid(datetime.strptime(digits, "%Y%m%d").date()) if len(digits) == 8 else None
    except ValueError:
        return None


def mdy(value):
    """'02/14/2024' -> date."""
    try:
        return _valid(datetime.strptime(str(value or "").strip()[:10], "%m/%d/%Y").date())
    except ValueError:
        return None


def iso(value):
    try:
        return _valid(date.fromisoformat(str(value or "").strip()[:10]))
    except ValueError:
        return None


def epoch_ms(value):
    """ArcGIS date (milliseconds since 1970, UTC) -> date. Default '1900-01-01' fill values come back as None."""
    if not isinstance(value, (int, float)):
        return None
    try:
        return _valid((datetime(1970, 1, 1, tzinfo=dt_tz.utc) + timedelta(milliseconds=value)).date())
    except (OverflowError, ValueError):
        return None


def month_year(value):
    """Harnett's '8/2006' (month and year only) -> the 1st of that month."""
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", str(value or "").strip())
    try:
        return _valid(date(int(m.group(2)), int(m.group(1)), 1)) if m else None
    except ValueError:
        return None


def price(value):
    if isinstance(value, (int, float)):
        n = value
    else:
        digits = re.sub(r"[^\d.]", "", str(value or ""))
        try:
            n = float(digits) if digits else 0
        except ValueError:
            n = 0
    return int(n) if n >= MIN_PRICE else None


def _book_page(book, page, word="Book"):
    book, page = (str(x or "").strip().lstrip("0") for x in (book, page))
    return f"{word} {book} page {page}" if book and page else ""


# --- Per-county record readers: rec -> fields ----------------------------------------------------------------------

def _charles(rec):
    """MD iMAP statewide parcel (SDAT) record: last transfer, owner mailing address and values. No owner names."""
    label, arms = MD_CONVEY.get(rec.get("CONVEY1"), ("", None))
    deed = _book_page(rec.get("GR1LIBR1"), rec.get("GR1FOLO1"), word="Liber").replace(" page ", " folio ")
    street = " ".join(x for x in [_t(rec, "OWNADD1"), _t(rec, "OWNADD2")] if x)
    zipcode = _t(rec, "OWNERZIP")[:5]
    out = {
        "last_sale_date": ymd(rec.get("TRADATE")), "last_sale_price": price(rec.get("CONSIDR1")),
        "last_sale_type": label, "last_sale_arms_length": arms, "last_sale_deed": deed,
        "land_value": rec.get("NFMLNDVL") or None, "improvement_value": rec.get("NFMIMPVL") or None,
        "market_value": rec.get("NFMTTLVL") or None,
    }
    if street and (_t(rec, "OWNCITY") or zipcode):  # a whole mailing address, or none of it
        st = _t(rec, "OWNSTATE").upper()
        out.update(mail_address=street, mail_city=_t(rec, "OWNCITY"), mail_zip=zipcode,
                   mail_state=st if re.fullmatch(r"[A-Z]{2}", st) else counties.zip_state(zipcode))
    return out


def _hays(rec):
    deed = (f"Vol {_t(rec, 'Volume')} pg {_t(rec, 'Page')}" if _t(rec, "Volume") and _t(rec, "Page")
            else (f"Doc {_t(rec, 'Number').replace('DOC#', '')}" if _t(rec, "Number") else ""))
    return {"last_sale_date": mdy(rec.get("Deed_Date")), "last_sale_deed": deed}


def _madison(rec):
    code = _t(rec, "DeedType").upper()
    label, arms = MADISON_DEEDS.get(code, (code.title() if code else "", None))
    return {"last_sale_date": epoch_ms(rec.get("DeedDate")), "last_sale_type": label, "last_sale_arms_length": arms,
            "last_sale_deed": _book_page(rec.get("DeedBook"), rec.get("DeedPage"))}


def _morgan(rec):
    """Morgan County: last sale date and price, and whether the assessor used the sale in its sales study."""
    amount = price(rec.get("SoldTotalPrice"))
    used = rec.get("UseOnSales")
    label = ("Qualified sale (used in the assessor's sales study)" if used == 1
             else "Not used in the assessor's sales study" if amount else "")
    return {"last_sale_date": epoch_ms(rec.get("LastSalesDate")), "last_sale_price": amount, "last_sale_type": label,
            "last_sale_arms_length": True if used == 1 and amount else None, "last_sale_deed": _t(rec, "LastDeed")}


def _nc(rec):
    """NC OneMap: Harnett gives the sale month and year, Moore a sale date, Cumberland neither; all give the deed book
    and page (sourceref). transfdate is when NC OneMap got the file, not a sale."""
    m = re.search(r"(\d+)\s*/\s*(\d+)", _t(rec, "sourceref")) if "deed" in _t(rec, "sourceref").lower() else None
    return {"last_sale_date": epoch_ms(rec.get("saledate")) or month_year(rec.get("saledatetx")),
            "last_sale_deed": _book_page(m.group(1), m.group(2)) if m else ""}


def _hoke(rec):
    return {"last_sale_date": ymd(rec.get("DEED_DATE")) or mdy(rec.get("DEED_DATE2")),
            "last_sale_deed": _book_page(rec.get("DEED_BOOK"), rec.get("DEED_PAGE"))}


def _maricopa(rec):
    """The latest deed (date, number) and the last sale (date, price) are separate: the price is the last transfer's
    only when the two dates are within SAME_TRANSFER_DAYS. The assessor doesn't say whether a sale was arms-length."""
    deed_on, sold_on, amount = epoch_ms(rec.get("DEED_DATE")), mdy(rec.get("SALE_DATE")), price(rec.get("SALE_PRICE"))
    deed = f"Doc {_t(rec, 'DEED_NUMBER')}" if _t(rec, "DEED_NUMBER") else ""
    if sold_on and not (deed_on and (deed_on - sold_on).days > SAME_TRANSFER_DAYS):
        return {"last_sale_date": sold_on, "last_sale_price": amount, "last_sale_deed": deed,
                "last_sale_type": "Sale (not validated by the assessor)" if amount else "Sale (no price recorded)"}
    # A later deed with no sale recorded (a gift, a transfer into a trust...): the old price isn't this transfer's.
    return {"last_sale_date": deed_on, "last_sale_price": None, "last_sale_type": "Deed (no sale recorded)" if deed_on else "",
            "last_sale_deed": deed}


def _pinal(rec):
    """Pinal: last sale date and price; REASONTOEXCLUDE is the assessor's code for leaving a sale out of its study
    (blank = used). The codes' meanings aren't published with the layer, so an excluded sale is 'not known'."""
    amount, code = price(rec.get("SALEPRICE")), _t(rec, "REASONTOEXCLUDE")
    label = (f"Left out of the assessor's sales study (code {code})" if code
             else "Used in the assessor's sales study" if amount else "")
    return {"last_sale_date": iso(rec.get("SALEDATE")), "last_sale_price": amount, "last_sale_type": label,
            "last_sale_arms_length": True if amount and not code else None,
            "last_sale_deed": f"Reception {_t(rec, 'RECEPTIONNO')}" if _t(rec, "RECEPTIONNO") else ""}


def _base(fips, read, *, unvalidated_comps=False, note=""):
    """A source that is the same layer the county's parcels load from."""
    cfg = counties.COUNTIES[fips]
    return {"label": cfg["source"], "url": cfg["parcels_url"], "where": lambda a: counties.big_where(cfg, a),
            "scope": lambda w: counties.in_county(cfg, w), "windows": cfg.get("full_windows") or [],
            "key": lambda rec: counties.record_key(cfg, rec), "read": read, "unvalidated_comps": unvalidated_comps,
            "note": note}


SOURCES = {
    "24017": {
        "label": "MD iMAP statewide parcels (SDAT)", "url": MD_PARCELS, "page_size": 1000,
        "where": lambda a: f"JURSCODE = 'CHAR' AND ACRES >= {a:.1f}", "scope": lambda w: f"JURSCODE = 'CHAR' AND ({w})",
        "key": lambda rec: _t(rec, "ACCTID"), "key_in": lambda keys: "ACCTID IN (" + ", ".join(f"'{k}'" for k in keys) + ")",
        "fields": ["ACCTID", "OWNADD1", "OWNADD2", "OWNCITY", "OWNSTATE", "OWNERZIP", "NFMLNDVL", "NFMIMPVL",
                   "NFMTTLVL", "TRADATE", "CONSIDR1", "CONVEY1", "GR1LIBR1", "GR1FOLO1"],
        "read": _charles, "replace": {"mail_address", "mail_city", "mail_state", "mail_zip", "land_value",
                                      "improvement_value", "market_value"},
        "note": "Sale price, how conveyed (SDAT code) and deed liber/folio; also mailing address and values",
    },
    "48209": {
        "label": "Hays County parcels (Hays CAD)", "url": HAYS_PARCELS, "page_size": 2000,
        "where": lambda a: f"legal_acreage >= {a:.1f}", "scope": lambda w: w,
        "key": lambda rec: _t(rec, "prop_id"),
        "key_in": lambda keys: "prop_id IN (" + ", ".join(k for k in keys if k.isdigit()) + ")",
        "fields": ["prop_id", "Deed_Date", "Volume", "Page", "Number"], "read": _hays,
        "note": "Deed date and volume / page or document number; no price (Texas doesn't disclose sale prices)",
    },
    "01089": _base("01089", _madison, note="Deed date, deed type and book / page; no price"),
    "01103": _base("01103", _morgan, note="Last sale date and price, and whether the assessor used the sale"),
    "37085": _base("37085", _nc, note="Sale month and year, deed book / page; no price"),
    "37125": _base("37125", _nc, note="Sale date (some parcels), deed book / page; no price"),
    "37051": _base("37051", _nc, note="Deed book / page only; no date or price"),
    "37093": _base("37093", _hoke, note="Deed date and book / page; no price"),
    "04013": _base("04013", _maricopa, unvalidated_comps=True,
                   note="Deed date and number, last sale date and price (not marked arms-length or not)"),
    "04021": _base("04021", _pinal, note="Last sale date and price, the assessor's exclusion code; no deed date"),
}


def source_for(county):
    return SOURCES.get(county.fips if hasattr(county, "fips") else str(county))


# --- Refresh ------------------------------------------------------------------------------------------------------

def _fetch(src, stored_keys, min_acres, log):
    """{key: fields} from the source: the big parcels (as the loader asks for them), the load-everything windows,
    then any stored parcel still missing, by key where the source can be asked by key."""
    out = {}

    def take(feats):
        for f in feats:
            rec = f.get("properties") or f.get("attributes") or {}
            k = src["key"](rec)
            if not k:
                continue
            fields = src["read"](rec)
            old = out.get(k)
            # Several rows for one parcel (Hays's deed history, Morgan's duplicates): keep the latest transfer.
            if old is None or (fields.get("last_sale_date") or date.min) > (old.get("last_sale_date") or date.min):
                out[k] = fields

    fields = src.get("fields", ["*"])
    page = src.get("page_size", 1000)
    take(arcgis_query(src["url"], where=src["where"](min_acres * 0.8), fields=fields, return_geometry=False,
                      page_size=page))
    for west, south, east, north in src.get("windows") or []:
        take(arcgis_query(src["url"], where=src["scope"]("1=1"), fields=fields, return_geometry=False,
                          bbox=(west, south, east, north), page_size=page))
    missing = sorted(set(stored_keys) - set(out))
    if missing and src.get("key_in"):
        log(f"  {len(missing):,} stored parcels not in the first pass: asking for them by key")
        for i in range(0, len(missing), 100):
            where = src["key_in"](missing[i:i + 100])
            if where.endswith("IN ()"):
                continue
            take(arcgis_query(src["url"], where=src["scope"](where), fields=fields, return_geometry=False,
                              page_size=page))
    return out


def changes_for(p, fields, src):
    """{field: new value} for one parcel. Sale fields and the source's `replace` fields take the source's value
    when it has one; other fields only fill blanks. A blank never overwrites."""
    replace = set(SALE_FIELDS) | src.get("replace", set())
    out = {}
    for f, v in fields.items():
        if v in ("", None):
            continue
        if f in replace or getattr(p, f) in ("", None):
            if getattr(p, f) != v:
                out[f] = v
    if fields.get("last_sale_date") and p.last_sale_source != src["label"]:
        out["last_sale_source"] = src["label"]
    # A different transfer replaces all of the old one: its price, type and deed don't carry over to the new one.
    if "last_sale_date" in out:
        for f in ("last_sale_price", "last_sale_type", "last_sale_arms_length", "last_sale_deed"):
            new = fields.get(f, "" if f in ("last_sale_type", "last_sale_deed") else None)
            if new in ("", None) and getattr(p, f) not in ("", None):
                out[f] = new
    return out


def refresh_county(county, *, commit=True, min_acres=None, log=print, rebuild_groups=True):
    """Fill last-transfer fields (and Charles's mailing and values) for one stored county. Returns a stats dict;
    nothing is written unless commit. With commit, re-scores the county's signals and re-makes its county comps."""
    from . import portfolio, signals

    src = source_for(county)
    if not src:
        return None
    parcels = list(Parcel.objects.filter(county=county).select_related("county"))
    log(f"{county.name} {county.state}: reading {src['label']}")
    recs = _fetch(src, [p.key for p in parcels], float(min_acres or county.min_acres or 20), log)
    changed, per_field, matched = [], Counter(), 0
    for p in parcels:
        fields = recs.get(p.key)
        if fields is None:
            continue
        matched += 1
        diff = changes_for(p, fields, src)
        for f, v in diff.items():
            setattr(p, f, v)
            per_field[f] += 1
        if diff:
            changed.append(p)
    stats = {"county": f"{county.name} {county.state}", "records": len(recs), "parcels": len(parcels),
             "matched": matched, "changed": len(changed), "fields": dict(per_field),
             "with_date": sum(1 for p in parcels if p.last_sale_date),
             "with_price": sum(1 for p in parcels if p.last_sale_price)}
    if not commit:
        return stats
    with transaction.atomic():
        Parcel.objects.bulk_update(changed, sorted({f for p in changed for f in per_field}) or ["last_sale_source"],
                                   batch_size=500)
    signals.apply_county(county)
    stats["comps"] = sync_comps(county)
    if rebuild_groups:
        portfolio.rebuild()
    return stats


# --- County comps -----------------------------------------------------------------------------------------------

def improved(p):
    """Buildings worth enough that the price isn't for land alone."""
    imp, land = p.improvement_value or 0, p.land_value or 0
    return imp >= 25000 and imp >= 0.1 * (imp + land)


def comp_ok(p, src=None):
    """A parcel's last transfer makes a county comp when it has a price and looks arms-length: marked so by the
    county, or (Maricopa, which never says) not marked otherwise. Multiple-account SDAT sales are left out: the
    price covers accounts that may not be stored here."""
    src = src or source_for(p.county) or {}
    if not (p.last_sale_date and p.last_sale_price and p.last_sale_price >= MIN_PRICE and p.acres):
        return False
    if "multiple" in (p.last_sale_type or "").lower():
        return False
    return p.last_sale_arms_length is True or (p.last_sale_arms_length is None and src.get("unvalidated_comps"))


def sync_comps(county):
    """Make county comps from the parcels' last transfers. Parcels sharing a sale (same date and deed, or same date
    and price) become one comp with their acres added up. Comps that no longer come out are removed unless the team
    has put notes, a site or a flyer on them. Returns the number of county comps for the county."""
    src = source_for(county) or {}
    groups = defaultdict(list)
    for p in Parcel.objects.filter(county=county, last_sale_price__gte=MIN_PRICE).select_related("county"):
        if comp_ok(p, src):
            groups[f"{p.last_sale_date.isoformat()}|{p.last_sale_deed or p.last_sale_price}"].append(p)
    keep = set()
    for key, ps in groups.items():
        ps.sort(key=lambda p: -p.acres)
        lead, acres = ps[0], sum(p.acres for p in ps)
        owners = list(dict.fromkeys(p.owner_name for p in ps if p.owner_name))
        place = lead.situs_address or lead.legal_desc or f"Parcel {lead.prop_id or lead.key}"
        defaults = {
            "parcel": lead, "sale_date": lead.last_sale_date, "price": lead.last_sale_price, "acres": round(acres, 2),
            "place": (place + (f" and {len(ps) - 1} more parcels" if len(ps) > 1 else ""))[:200],
            "buyer": "; ".join(owners)[:255], "arms_length": lead.last_sale_arms_length,
            "conveyance": lead.last_sale_type[:80], "deed": lead.last_sale_deed, "improved": any(improved(p) for p in ps),
            "parcels_count": len(ps), "lat": sum(p.lat * p.acres for p in ps) / acres,
            "lon": sum(p.lon * p.acres for p in ps) / acres, "source_note": lead.last_sale_source[:255],
        }
        comp = SaleComp.objects.filter(source=SaleComp.Source.COUNTY, county=county, record_key=key).first()
        if comp is None:
            comp = SaleComp(source=SaleComp.Source.COUNTY, county=county, record_key=key)
        for f, v in defaults.items():
            setattr(comp, f, v)
        comp.save()
        keep.add(key)
    stale = (SaleComp.objects.filter(source=SaleComp.Source.COUNTY, county=county).exclude(record_key__in=keep)
             .filter(notes="", site__isnull=True, flyer=""))
    stale.delete()
    return SaleComp.objects.filter(source=SaleComp.Source.COUNTY, county=county).count()
