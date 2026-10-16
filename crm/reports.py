"""Excel export of the investor pipeline report, laid out like the Fortress pipeline tracker."""
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

NAVY = "1F3A5F"
FONT = "Aptos Narrow"
MONEY = '_($* #,##0_);_($* (#,##0);_($* "-"_);_(@_)'
NUMBER = '#,##0;(#,##0);"-"'
DATE = "m/d/yyyy"

TENANT_COLUMNS = [
    ("Site", 14), ("Deal Type", 16), ("Stage", 20), ("Tenant / Counterparty", 30), ("Asset Type", 24),
    ("Acres", 9), ("Estimated Size (SF)", 14), ("Estimated Project Cost ($)", 18), ("Cost PSF", 11),
    ("Win %", 8), ("Weighted Cost ($)", 16), ("Owner", 14), ("Target Close", 12), ("Est. Delivery", 12),
    ("Next Step / Notes", 50),
]


def _banner(ws, row, text, width):
    ws.cell(row=row, column=1, value=text)
    for col in range(1, width + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF")


def _header(ws, row, columns):
    for i, (label, width) in enumerate(columns, start=1):
        cell = ws.cell(row=row, column=i, value=label)
        cell.font = Font(name=FONT, bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="bottom")
        cell.border = Border(bottom=Side(style="thin"))
        ws.column_dimensions[get_column_letter(i)].width = width


def _owner(user):
    return (user.get_full_name() or user.username) if user else ""


def pipeline_workbook(data):
    wb = Workbook()
    ws = wb.active
    ws.title = "Tenants"
    width = len(TENANT_COLUMNS)
    ws["A1"] = "ACMI PROPERTIES: TENANTS / BUILDINGS"
    ws["A1"].font = Font(name=FONT, bold=True, size=14)
    ws["A2"] = f"As of {data['as_of']:%B %d, %Y}"
    ws["A2"].font = Font(name=FONT, italic=True)
    row = 4
    for section in data["sections"]:
        _banner(ws, row, section["title"], width)
        row += 1
        _header(ws, row, TENANT_COLUMNS)
        row += 1
        first = row
        for o in section["rows"]:
            values = [
                o.site_label, o.deal_type, o.get_stage_display(), o.company.name, o.asset_type,
                float(o.acreage_need) if o.acreage_need else None, o.sq_ft_need,
                float(o.value) if o.value else None, None, o.win_probability / 100, None, _owner(o.owner),
                o.target_close, o.est_delivery, o.next_step or o.timeline,
            ]
            for col, value in enumerate(values, start=1):
                ws.cell(row=row, column=col, value=value).font = Font(name=FONT)
            # Cost PSF and weighted cost stay live formulas.
            ws.cell(row=row, column=9, value=f'=IF(AND(ISNUMBER(G{row}),G{row}>0,ISNUMBER(H{row})),H{row}/G{row},"")')
            ws.cell(row=row, column=11, value=f'=IF(ISNUMBER(H{row}),H{row}*J{row},"")')
            for col, fmt in ((6, NUMBER), (7, NUMBER), (8, MONEY), (9, MONEY), (10, "0%"), (11, MONEY), (13, DATE), (14, DATE)):
                ws.cell(row=row, column=col).number_format = fmt
                ws.cell(row=row, column=col).font = Font(name=FONT)
            row += 1
        last = row - 1
        total = ws.cell(row=row, column=1, value=f"TOTAL - {len(section['rows'])} Tenants")
        total.font = Font(name=FONT, bold=True)
        if section["rows"]:
            for col, fmt in ((7, NUMBER), (8, MONEY), (11, MONEY)):
                letter = get_column_letter(col)
                cell = ws.cell(row=row, column=col, value=f"=SUM({letter}{first}:{letter}{last})")
                cell.number_format = fmt
                cell.font = Font(name=FONT, bold=True)
                cell.border = Border(top=Side(style="thin"))
        row += 2
    ws.freeze_panes = "A4"

    sites = wb.create_sheet("Sites")
    columns = [("Site Name", 22), ("Total Acres", 11), ("# Tenants", 10), ("Total SF", 12),
               ("Total Project Cost ($)", 18), ("Weighted Cost ($)", 16), ("Avg $ PSF", 11)]
    sites["A1"] = "ACMI PROPERTIES: DEVELOPMENT SITES"
    sites["A1"].font = Font(name=FONT, bold=True, size=14)
    _header(sites, 3, columns)
    r = 4
    for s in data["sites"]:
        values = [s["site"], float(s["acres"]) or None, s["tenants"], s["sf"], float(s["cost"]), float(s["weighted"])]
        for col, value in enumerate(values, start=1):
            sites.cell(row=r, column=col, value=value).font = Font(name=FONT)
        # Only deals with both a cost and a size count toward $/sf, so this is a value, not E/D.
        sites.cell(row=r, column=7, value=float(s["psf"]) if s["psf"] else None).font = Font(name=FONT)
        for col, fmt in ((2, NUMBER), (3, NUMBER), (4, NUMBER), (5, MONEY), (6, MONEY), (7, MONEY)):
            sites.cell(row=r, column=col).number_format = fmt
        r += 1
    if data["sites"]:
        sites.cell(row=r, column=1, value=f"TOTAL - {len(data['sites'])} sites").font = Font(name=FONT, bold=True)
        for col, fmt in ((2, NUMBER), (3, NUMBER), (4, NUMBER), (5, MONEY), (6, MONEY)):
            letter = get_column_letter(col)
            cell = sites.cell(row=r, column=col, value=f"=SUM({letter}4:{letter}{r - 1})")
            cell.number_format = fmt
            cell.font = Font(name=FONT, bold=True)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
