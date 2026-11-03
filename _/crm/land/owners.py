"""Join owner / mailing / value data from a file (county assessor export, Regrid, a WTHGIS Excel export...) onto
parcels already loaded for a county, matching by parcel number.

For counties whose public parcel services carry no owner data (Hoke NC, Greene IN, east Pinal AZ). The file's
columns are recognised by common names (Regrid's `parcelnumb`, `owner`, `mailadd`...); anything else is mapped
with --map. Blank cells never overwrite data a parcel already has, and a later `load_parcels` keeps owner data too.
"""
import csv
import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

from ..models import Parcel
from . import score
from .parcels import _owner_totals, _text, owner_key

# Parcel field -> header names (compared lowercase with every non-alphanumeric stripped), best first.
ALIASES = {
    "key": ["parcelnumb", "parcelnumbernoformatting", "parcelnumbnoformatting", "parcelnumber", "parcelno", "parcelid",
            "parcel", "apn", "pin", "parno", "taxid", "stateparcelid", "altparcelnumb1", "accountnumber", "account"],
    "owner_name": ["owner", "ownername", "owner1", "ownername1", "firstownername", "ownnam", "owners", "taxpayer",
                   "taxpayername", "name"],
    "care_of": ["owner2", "ownername2", "secondownername", "careof", "additionalowner"],
    "mail_address": ["mailadd", "mailingaddress", "mailaddress", "mailaddr1", "mailingstreet", "mailstreet",
                     "owneraddress", "owneraddr", "mailingaddr1"],
    "mail_city": ["mailcity", "mailingcity", "ownercity"],
    "mail_state": ["mailstate2", "mailstate", "mailingstate", "ownerstate"],
    "mail_zip": ["mailzip", "mailingzip", "ownerzip", "mailzipcode"],
    "situs_address": ["saddress", "siteaddress", "situsaddress", "situs", "propertyaddress", "propaddress"],
    "situs_city": ["scity", "sitecity", "situscity", "propertycity"],
    "situs_zip": ["szip", "sitezip", "situszip", "propertyzip"],
    "land_value": ["landval", "landvalue", "landassessed", "totallandvalue"],
    "improvement_value": ["improvval", "improvementvalue", "improvementsvalue", "buildingvalue", "totalimpvalue"],
    "market_value": ["parval", "totalvalue", "marketvalue", "totalassessedvalue", "assessedvalue", "totalappraised"],
    "acres": ["llgisacre", "gisacre", "gisacres", "acres", "acreage", "deededacres", "calcacres", "totalacres"],
}
TEXT_FIELDS = ["owner_name", "care_of", "mail_address", "mail_city", "mail_state", "mail_zip",
               "situs_address", "situs_city", "situs_zip"]
NUMBER_FIELDS = ["land_value", "improvement_value", "market_value"]


def _norm_header(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def norm_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _number(value):
    s = re.sub(r"[^\d.\-]", "", str(value if value is not None else ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


class _TableParser(HTMLParser):
    """Excel 'exports' from older web viewers are usually an HTML table saved as .xls."""

    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None


def _dict_rows(header, body):
    header = [str(h or "").strip() for h in header]
    for row in body:
        yield {h: ("" if v is None else v) for h, v in zip(header, row)}


def read_rows(path):
    """Rows of a .csv / .xlsx / HTML-table .xls / .json / .geojson file as {header: value} dicts."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".json", ".geojson"):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        items = data.get("features", data) if isinstance(data, dict) else data
        return (dict(i.get("properties", i)) for i in items)
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        sheet = load_workbook(path, read_only=True, data_only=True).worksheets[0]
        it = sheet.iter_rows(values_only=True)
        return _dict_rows(next(it), it)
    raw = path.read_bytes()
    head = raw[:2048].lstrip().lower()
    if head.startswith((b"<", b"\xef\xbb\xbf<")):
        parser = _TableParser()
        parser.feed(raw.decode("utf-8", errors="replace"))
        return _dict_rows(parser.rows[0], parser.rows[1:]) if parser.rows else iter(())
    if suffix == ".xls":
        raise ValueError("Real binary .xls files aren't supported: re-save it as .xlsx or .csv")
    text = raw.decode("utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|") if text else csv.excel
    return csv.DictReader(text.splitlines(), dialect=dialect)


def find_columns(headers, overrides=None):
    """{field: header} from recognised names, with explicit --map overrides (field=Header) on top."""
    by_norm = {}
    for h in headers:
        by_norm.setdefault(_norm_header(h), h)
    found = {}
    for field, names in ALIASES.items():
        header = next((by_norm[n] for n in names if n in by_norm), None)
        if header:
            found[field] = header
    for field, header in (overrides or {}).items():
        if header not in headers:
            raise ValueError(f"No column {header!r} in the file (columns: {', '.join(headers)})")
        found[field] = header
    return found


def row_fields(row, columns):
    """Parcel fields from one file row: text trimmed, numbers parsed, blanks dropped."""
    out = {}
    for field in TEXT_FIELDS:
        if field in columns:
            value = _text(row.get(columns[field]))
            if value:
                out[field] = " ".join(value.split())
    for field in NUMBER_FIELDS:
        if field in columns:
            number = _number(row.get(columns[field]))
            if number:
                out[field] = int(round(number))
    if "mail_state" in out:
        out["mail_state"] = out["mail_state"][:2].upper()
    return out


def key_index(parcels):
    """{normalised id: parcel} over each parcel's key, prop_id and geo_id (and a zero-stripped form of each)."""
    index = {}
    for p in parcels:
        for raw in (p.key, p.prop_id, p.geo_id):
            n = norm_key(raw)
            if n:
                index.setdefault(n, p)
                index.setdefault(n.lstrip("0"), p)
    return index


def join_owners(county, path, *, overrides=None, commit=False, log=print):
    """Match the file's rows to the county's stored parcels. Returns a stats dict; writes only with commit=True."""
    rows = read_rows(path)
    first = next(rows, None)
    if first is None:
        raise ValueError("The file has no data rows")
    headers = list(first.keys())
    columns = find_columns(headers, overrides)
    if "key" not in columns or "owner_name" not in columns:
        raise ValueError(f"Couldn't find the parcel-number and owner columns. Columns in the file: {', '.join(headers)}. "
                         "Name them with --map key=<column> --map owner_name=<column>.")
    log("Columns used: " + ", ".join(f"{f} <- {h}" for f, h in columns.items()))
    parcels = list(Parcel.objects.filter(county=county))
    index = key_index(parcels)
    matched, changed, unmatched, per_owner = {}, defaultdict(dict), 0, defaultdict(lambda: [0.0, 0])
    total = 0
    for row in [first, *rows]:
        total += 1
        fields = row_fields(row, columns)
        acres = _number(row.get(columns["acres"])) if "acres" in columns else None
        owner = owner_key(fields.get("owner_name", ""))
        if owner and owner != "NULL":
            per_owner[owner][0] += acres or 0
            per_owner[owner][1] += 1
        n = norm_key(row.get(columns["key"]))
        p = index.get(n) or index.get(n.lstrip("0"))
        if not p or not fields:
            unmatched += 0 if p else 1
            continue
        matched[p.pk] = p
        for f, v in fields.items():
            if f.startswith("situs") and getattr(p, f):
                continue  # keep the service's address; the file only fills blanks there
            if v != getattr(p, f):
                changed[p.pk][f] = v
    stats = {"rows": total, "matched": len(matched), "unmatched_rows": unmatched, "parcels": len(parcels),
             "parcels_changed": len(changed), "owners_before": sum(1 for p in parcels if p.owner_name),
             "samples": []}
    for pk, fields in list(changed.items())[:3]:
        stats["samples"].append({"key": matched[pk].key, **fields})
    if not commit:
        return stats
    has_acres = "acres" in columns and any(v[0] for v in per_owner.values())
    for p in parcels:
        for f, v in changed.get(p.pk, {}).items():
            setattr(p, f, v)
        totals = per_owner.get(owner_key(p.owner_name)) if has_acres else None
        if totals:
            p.owner_acres, p.owner_parcels = round(totals[0], 1), totals[1]
        score.apply(p)
    Parcel.objects.bulk_update(parcels, TEXT_FIELDS + NUMBER_FIELDS + ["owner_acres", "owner_parcels", "zoning_class",
                                                                        "score", "score_reasons", "killers", "signals",
                                                                        "motivation"], batch_size=500)
    if not has_acres:  # no acreage column: count owners over the parcels stored here
        totals = _owner_totals((p.owner_name, p.acres) for p in parcels)
        for p in parcels:
            o = totals.get(owner_key(p.owner_name))
            p.owner_acres, p.owner_parcels = (round(o[0], 1), o[1]) if o else (None, None)
        Parcel.objects.bulk_update(parcels, ["owner_acres", "owner_parcels"], batch_size=500)
    stats["owners_after"] = sum(1 for p in parcels if p.owner_name)
    from . import portfolio

    portfolio.rebuild()
    return stats
