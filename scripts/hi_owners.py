"""Honolulu (Oahu) owner join: fetch the City's OWNALL table for the stored parcels and load it with load_owners' join.

The City's tax-parcel polygons carry no owners; CadastralTables/FeatureServer/5 ("OWNALL") is keyed by TMK. Run it
where the database is (it uses the app's settings):

    manage.py shell -c "exec(open('scripts/hi_owners.py').read())"

On the hosted service run it through the migrate job (the file is in the image):
    gcloud run jobs execute acmi-crm-migrate --args="^@^manage.py@shell@-c@exec(open('scripts/hi_owners.py').read())" --wait

Keeps the whole-parcel row (suffix 0000) with the lowest ownseq per TMK. Blank cells never overwrite existing data.
"""
import csv
import re
import tempfile
import time

import requests

from crm.land.owners import join_owners
from crm.models import LandCounty, Parcel

OWNALL = "https://services.arcgis.com/tNJpAOha4mODLkXz/arcgis/rest/services/CadastralTables/FeatureServer/5/query"
county = LandCounty.objects.get(fips="15003")
tmks = sorted(set(Parcel.objects.filter(county=county).values_list("prop_id", flat=True)))
rows = {}
for i in range(0, len(tmks), 100):
    where = "tmk IN (" + ",".join("'%s'" % t for t in tmks[i:i + 100]) + ")"
    r = requests.post(OWNALL, data={"where": where, "f": "json", "resultRecordCount": 2000,
                                    "outFields": "tmk,suffix,ownseq,own1,own2,paddr1,paddr2,paddr3"}, timeout=90)
    r.raise_for_status()
    for f in r.json().get("features", []):
        a = f["attributes"]
        rank = (0 if (a.get("suffix") or "") == "0000" else 1, a.get("ownseq") or 0)
        if a["tmk"] not in rows or rank < rows[a["tmk"]][0]:
            rows[a["tmk"]] = (rank, a)
    time.sleep(0.3)
path = tempfile.mktemp(suffix=".csv")
with open(path, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["tmk", "owner", "owner2", "mailadd", "mailcity", "mailstate", "mailzip"])
    for key, (_, a) in sorted(rows.items()):
        p1, p2, p3 = (a.get("paddr1") or "").strip(), (a.get("paddr2") or "").strip(), (a.get("paddr3") or "").strip()
        care = " ".join(x for x in [(a.get("own2") or "").strip(), p1 if p2 else ""] if x)
        m = re.match(r"^(.*?)[ ,]+([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", p3)
        city, state, zipc = (m.group(1).strip(" ,"), m.group(2), m.group(3)) if m else (p3, "", "")
        w.writerow([key, (a.get("own1") or "").strip(), care, p2 or p1, city, state, zipc])
stats = join_owners(county, path, overrides={"key": "tmk"}, commit=True, log=print)
print("Honolulu owners:", stats["matched"], "matched of", stats["parcels"], "stored; owners on file now", stats["owners_after"])
