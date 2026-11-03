"""Download Greene County, IN's parcel / owner table from the county's WTHGIS viewer (greenein.wthgis.com) to a CSV
that `manage.py load_owners Greene_IN <csv> --commit` understands.

The viewer's Index > Parcels2 pages the table (26,550 parcels: owner, mailing and property address, acreage,
assessment class) through index.ashx. This asks for the same pages, one at a time with a pause between them.
Usage: .venv/Scripts/python scripts/fetch_greene_in_owners.py [out.csv]
"""
import csv
import sys
import time
from pathlib import Path

import requests

BASE = "https://greenein.wthgis.com/tgis/index.ashx"
DSID = 675  # Parcels2
PAGE = 200
PAUSE = 0.5  # seconds between requests

# Column order of the Parcels2 table after the two leading row-number fields (from action=layerIndex).
COLUMNS = ["County", "Area", "Section", "Block", "Parcel", "Split", "TaxID", "OldParcelNumber", "taxPROVALParcelId",
           "taxMVPParcelID", "taxInstrumentNumber", "taxOwnerName", "taxOwnerName2", "taxOwnerStreet", "taxOwnerCity",
           "taxOwnerState", "taxOwnerZipCode", "taxPropertyStreet", "taxPropertyCity", "taxPropertyState",
           "taxPropertyZipCode", "taxDeedBook", "taxDeedPage", "taxLegalAcreage", "taxLegalDescription",
           "taxAssessmentClass", "taxNeighborhood", "taxFoundMVP", "taxFoundProval", "Comments", "Calculated Acreage",
           "taxMVPLegalDesc", "taxMVPLegalAcreage", "taxProvalOwnerName", "LastProvalTransfer", "PrivRec"]
# Output headers use Regrid-style names so load_owners recognises them without --map.
OUT = [("parcelnumb", "taxPROVALParcelId"), ("owner", "taxOwnerName"), ("owner2", "taxOwnerName2"),
       ("mailadd", "taxOwnerStreet"), ("mail_city", "taxOwnerCity"), ("mail_state2", "taxOwnerState"),
       ("mail_zip", "taxOwnerZipCode"), ("saddress", "taxPropertyStreet"), ("scity", "taxPropertyCity"),
       ("szip", "taxPropertyZipCode"), ("ll_gisacre", "taxLegalAcreage"), ("assessment_class", "taxAssessmentClass"),
       ("legal", "taxLegalDescription")]


def main(out_path):
    session = requests.Session()
    head = session.get(BASE.replace("index", "Index"), params={"action": "layerIndex", "dsid": DSID, "name": "Parcels2"},
                       timeout=60).text
    total = int(head.split("<bigTable><![CDATA[")[1].split("|")[2])
    print(f"{total:,} parcels to fetch")
    rows = []
    for start in range(0, total, PAGE):
        for attempt in range(3):
            try:
                r = session.get(BASE, params={"action": "getFtrs", "dsid": DSID, "i1": start, "cnt": PAGE}, timeout=90)
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(5)
        for rec in filter(None, r.text.split("\x1e")):
            parts = rec.split("\x1f")
            if len(parts) != len(COLUMNS) + 2:
                print(f"  skipped a row with {len(parts)} fields at offset {start}")
                continue
            rows.append(dict(zip(COLUMNS, parts[2:])))
        print(f"  {min(start + PAGE, total):,} / {total:,}", end="\r")
        time.sleep(PAUSE)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([o for o, _ in OUT])
        for rec in rows:
            writer.writerow([" ".join(rec[src].split()) for _, src in OUT])
    print(f"\nWrote {len(rows):,} rows to {out_path}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/land/raw/greene_in_owners.csv"))
