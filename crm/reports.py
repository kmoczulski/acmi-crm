"""The tenant pipeline report's columns, and its Excel export laid out like the Fortress pipeline tracker
(G: drive, 1 Projects/Fortress Pipeline Meetings/2026.09.25/ACMI_Properties_Pipeline_Tracker.xlsx).

One column list drives the report page, its print/PDF and the Excel file, so a column someone hides (Win %, cost...)
is left out of all three. Each person's choice is kept in their preferences ("report_cols").
"""
from dataclasses import dataclass
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

NAVY = "002060"  # the tracker's header fill
INPUT_BLUE = "0000FF"  # hard-coded inputs are blue, formulas black (ACMI's model standard)
FONT = "Aptos Narrow"
SF = '#,##0" SF";(#,##0" SF");"-"'
MONEY = '"$"#,##0;("$"#,##0);"-"'
PSF = '"$"#,##0" PSF";("$"#,##0" PSF");"-"'
NUMBER = '#,##0;(#,##0);"-"'
ACRES = '#,##0.0;(#,##0.0);"-"'
MONTH = "mmm-yy"
THIN = Side(style="thin", color="BFBFBF")


@dataclass(frozen=True)
class ReportColumn:
    key: str
    label: str  # on the page
    excel: str  # the tracker's header
    width: float
    default: bool = True
    group: str = ""  # "cost" and "probability" columns are the ones people hide before sending the report out


COLUMNS = [
    ReportColumn("site", "Site", "Campus / Project", 16),
    ReportColumn("deal_type", "Deal type", "Deal Type", 16),
    ReportColumn("stage", "Stage", "Stage", 20),
    ReportColumn("tenant", "Tenant", "Tenant / Counterparty", 30),
    ReportColumn("asset_type", "Asset type", "Asset Type", 22),
    ReportColumn("acres", "Acres", "Acres", 9),
    ReportColumn("size", "Size (sf)", "Estimated Size (SF)", 15),
    ReportColumn("cost", "Project cost", "Estimated Project Cost ($)", 19, group="cost"),
    ReportColumn("psf", "$ PSF", "Estimated Cost PSF", 15, group="cost"),
    ReportColumn("rent", "Rent / NOI", "Year-1 Rent / NOI ($)", 17, default=False, group="cost"),
    ReportColumn("roc", "ROC", "Return on Cost", 10, group="cost"),
    ReportColumn("lease_doc", "Lease / LOI", "Lease / LOI", 12),
    ReportColumn("win", "Win %", "Win %", 8, group="probability"),
    ReportColumn("weighted", "Weighted", "Weighted Cost ($)", 17, group="probability"),
    ReportColumn("lead", "Lead / source", "Lead / Source", 16),
    ReportColumn("owner", "Owner", "ACMI Lead", 14, default=False),
    ReportColumn("target_close", "Target close", "Target Close", 12),
    ReportColumn("est_delivery", "Est. delivery", "Est. Delivery", 12),
    ReportColumn("notes", "Next step / notes", "Next Step / Notes", 48),
]
BY_KEY = {c.key: c for c in COLUMNS}
DEFAULT_KEYS = [c.key for c in COLUMNS if c.default]


def chosen_columns(keys):
    """The columns to show, in report order. Unknown keys are dropped; nothing chosen means the defaults."""
    keys = [k for k in (keys or []) if k in BY_KEY]
    keys = keys or DEFAULT_KEYS
    if "tenant" not in keys:  # a row needs a name
        keys = ["tenant", *keys]
    return [c for c in COLUMNS if c.key in keys]


def _owner(user):
    return (user.get_full_name() or user.username) if user else ""


def cell_value(o, key):
    """A deal's raw value for a column (numbers stay numbers for Excel)."""
    return {
        "site": o.site_label,
        "deal_type": o.get_deal_type_display(),
        "stage": o.get_stage_display(),
        "tenant": o.company.name,
        "asset_type": o.get_asset_type_display(),
        "acres": float(o.acreage_need) if o.acreage_need else None,
        "size": o.sq_ft_need,
        "cost": float(o.value) if o.value else None,
        "psf": float(o.cost_psf) if o.cost_psf else None,
        "rent": float(o.annual_rent) if o.annual_rent else None,
        "roc": float(o.roc) if o.roc else None,
        "lease_doc": o.lease_doc_label,
        "win": o.win_probability / 100,
        "weighted": float(o.weighted_value) if o.value else None,
        "lead": o.lead_source,
        "owner": _owner(o.owner),
        "target_close": o.target_close,
        "est_delivery": o.est_delivery,
        "notes": o.next_step or o.timeline,
    }[key]


FORMATS = {"acres": ACRES, "size": SF, "cost": MONEY, "psf": PSF, "rent": MONEY, "roc": "0.0%", "win": "0%", "weighted": MONEY,
           "target_close": MONTH, "est_delivery": MONTH}


def _font(**kw):
    return Font(name=FONT, **kw)


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _input(ws, row, column, value):
    """A typed-in value. Text that starts like a formula (a deal note "=HYPERLINK(...)", "-50 SF") is kept as plain
    text, so opening the file never runs it; Excel's quote prefix keeps it text if someone edits the cell too."""
    cell = ws.cell(row=row, column=column, value=value)
    if isinstance(value, str) and value.startswith(FORMULA_START):
        cell.data_type = "s"
        cell.quotePrefix = True
    return cell


def _header_row(ws, row, labels_widths):
    for i, (label, width) in enumerate(labels_widths, start=1):
        cell = ws.cell(row=row, column=i, value=label)
        cell.font = _font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = Border(bottom=THIN, top=THIN, left=THIN, right=THIN)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[row].height = 30


def _title(ws, text, as_of):
    ws["A1"] = text
    ws["A1"].font = _font(bold=True, size=14)
    ws["A2"] = "ACMI Properties"
    ws["A2"].font = _font(bold=True)
    ws["A3"] = as_of
    ws["A3"].number_format = "mmmm d, yyyy"
    ws["A3"].font = _font(italic=True)
    ws["A3"].alignment = Alignment(horizontal="left")


def _print_setup(ws, header_row):
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = f"1:{header_row}"
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.oddFooter.center.text = "ACMI Properties · Confidential · Page &P of &N"


def pipeline_workbook(data, columns=None):
    columns = columns or chosen_columns(None)
    keys = [c.key for c in columns]
    col_of = {k: i for i, k in enumerate(keys, start=1)}
    width = len(columns)
    wb = Workbook()
    ws = wb.active
    ws.title = "Tenants"
    _title(ws, "ACMI PROPERTIES: TENANTS / BUILDINGS", data["as_of"])

    row = 5
    first_header = None
    for section in data["sections"]:
        ws.cell(row=row, column=1, value=section["title"]).font = _font(bold=True, size=12)
        row += 1
        _header_row(ws, row, [(c.excel, c.width) for c in columns])
        first_header = first_header or row
        row += 1
        first = row
        for o in section["rows"]:
            for c in columns:
                cell = _input(ws, row, col_of[c.key], cell_value(o, c.key))
                cell.font = _font(color=INPUT_BLUE)
                cell.border = Border(bottom=THIN)
                cell.alignment = Alignment(vertical="top", wrap_text=c.key in ("notes", "tenant"))
                if c.key in FORMATS:
                    cell.number_format = FORMATS[c.key]
            # Derived columns stay live formulas when their inputs are on the sheet (black, per the standard).
            size, cost, win = (get_column_letter(col_of[k]) if k in col_of else None for k in ("size", "cost", "win"))
            if "psf" in col_of and size and cost:
                cell = ws.cell(row=row, column=col_of["psf"], value=f'=IFERROR({cost}{row}/{size}{row},"")')
                cell.font = _font()
            rent = get_column_letter(col_of["rent"]) if "rent" in col_of else None
            if "roc" in col_of and rent and cost:
                cell = ws.cell(row=row, column=col_of["roc"], value=f'=IFERROR({rent}{row}/{cost}{row},"")')
                cell.font = _font()
            if "lease_doc" in col_of and o.lease_doc_url:
                cell = ws.cell(row=row, column=col_of["lease_doc"])
                cell.hyperlink = o.lease_doc_url
                cell.font = _font(color=INPUT_BLUE, underline="single")
            if "weighted" in col_of and cost and win:
                cell = ws.cell(row=row, column=col_of["weighted"], value=f'=IF(ISNUMBER({cost}{row}),{cost}{row}*{win}{row},"")')
                cell.font = _font()
            row += 1
        last = row - 1
        label = ws.cell(row=row, column=1, value=f"TOTAL - {len(section['rows'])} Tenant{'s' if len(section['rows']) != 1 else ''}")
        label.font = _font(bold=True)
        for c in range(1, width + 1):
            ws.cell(row=row, column=c).border = Border(top=Side(style="thin"), bottom=Side(style="double"))
        if section["rows"]:
            for key in ("acres", "size", "cost", "rent", "weighted"):
                if key in col_of:
                    letter = get_column_letter(col_of[key])
                    cell = ws.cell(row=row, column=col_of[key], value=f"=SUM({letter}{first}:{letter}{last})")
                    cell.number_format = FORMATS[key]
                    cell.font = _font(bold=True)
            if "roc" in col_of and "rent" in col_of and "cost" in col_of:
                r_, c_ = get_column_letter(col_of["rent"]), get_column_letter(col_of["cost"])
                cell = ws.cell(row=row, column=col_of["roc"], value=f'=IFERROR(SUMIFS({r_}{first}:{r_}{last},{c_}{first}:{c_}{last},">0")/SUMIFS({c_}{first}:{c_}{last},{r_}{first}:{r_}{last},">0"),"")')
                cell.number_format = FORMATS["roc"]
                cell.font = _font(bold=True)
            if "psf" in col_of and "size" in col_of and "cost" in col_of:
                s, c_ = get_column_letter(col_of["size"]), get_column_letter(col_of["cost"])
                cell = ws.cell(row=row, column=col_of["psf"], value=f'=IFERROR(SUMIFS({c_}{first}:{c_}{last},{s}{first}:{s}{last},">0")/SUMIFS({s}{first}:{s}{last},{c_}{first}:{c_}{last},">0"),"")')
                cell.number_format = PSF
                cell.font = _font(bold=True)
        row += 3
    ws.freeze_panes = ws.cell(row=(first_header or 6) + 1, column=2)
    _print_setup(ws, first_header or 6)

    # Development sites: one row per site, the totals of its deals.
    sites = wb.create_sheet("Sites")
    _title(sites, "ACMI PROPERTIES: DEVELOPMENT SITES", data["as_of"])
    site_cols = [("site", "Site Name", 20), ("location", "Location (City, ST)", 18), ("acres", "Total Acres", 12),
                 ("tenants", "# Tenants", 10), ("sf", "Total SF", 14)]
    if "cost" in keys:
        site_cols.append(("cost", "Total Project Cost ($)", 18))
    if "weighted" in keys and "cost" in keys:
        site_cols.append(("weighted", "Weighted Cost ($)", 17))
    if "psf" in keys:
        site_cols.append(("psf", "Avg $ PSF", 13))
    site_cols.append(("notes", "Status / Notes", 40))
    fmt = {"acres": ACRES, "tenants": NUMBER, "sf": SF, "cost": MONEY, "weighted": MONEY, "psf": PSF}
    _header_row(sites, 5, [(label, w) for _, label, w in site_cols])
    r = 6
    for s in data["sites"]:
        values = {
            "site": s["site"], "location": s["campus"].location if s["campus"] else "",
            "acres": float(s["acres"]) or None, "tenants": s["tenants"], "sf": s["sf"] or None,
            "cost": float(s["cost"]) or None, "weighted": float(s["weighted"]) or None,
            "psf": float(s["psf"]) if s["psf"] else None,
            "notes": (s["campus"].status if s["campus"] else ""),
        }
        for i, (key, _, _) in enumerate(site_cols, start=1):
            cell = _input(sites, r, i, values[key])
            cell.font = _font(color=INPUT_BLUE)
            cell.border = Border(bottom=THIN)
            if key in fmt:
                cell.number_format = fmt[key]
        r += 1
    if data["sites"]:
        sites.cell(row=r, column=1, value=f"TOTAL - {len(data['sites'])} sites").font = _font(bold=True)
        for i, (key, _, _) in enumerate(site_cols, start=1):
            sites.cell(row=r, column=i).border = Border(top=Side(style="thin"), bottom=Side(style="double"))
            if key in ("acres", "tenants", "sf", "cost", "weighted"):
                letter = get_column_letter(i)
                cell = sites.cell(row=r, column=i, value=f"=SUM({letter}6:{letter}{r - 1})")
                cell.number_format = fmt[key]
                cell.font = _font(bold=True)
    sites.freeze_panes = "B6"
    _print_setup(sites, 5)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
