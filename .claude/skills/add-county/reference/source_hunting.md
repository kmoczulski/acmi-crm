# Finding a county's parcel and zoning data

## Probe commands
```bash
# service directory (try /arcgis/rest, /server/rest, /webapps/rest, /mapping/rest on the county host)
curl -sL "https://HOST/arcgis/rest/services?f=json" | head -c 600

# a layer: fields, max records, geometry
curl -sL "$LAYER?f=json" | python -c "import sys,json; d=json.load(sys.stdin); print(d['name'], d.get('maxRecordCount'), d['geometryType']); print([f['name'] for f in d['fields']])"

# size, a sample, and coverage of owners on big parcels
curl -sL "$LAYER/query" -d "where=1=1&returnCountOnly=true&f=json"
curl -sL "$LAYER/query" -d "where=1=1&outFields=*&resultRecordCount=3&returnGeometry=false&f=json"
curl -sL "$LAYER/query" --data-urlencode "where=<acre field> >= 16" -d "returnCountOnly=true&f=json"
curl -sL "$LAYER/query" --data-urlencode "where=<acre field> >= 16 AND (<owner> IS NULL OR <owner> = '')" -d "returnCountOnly=true&f=json"
```
(`curl` with a POST body uses `-d`; wrap URL-encoded `where` clauses with `--data-urlencode`.)

## Where a county's real host hides
- Search ArcGIS Online (`https://www.arcgis.com/sharing/rest/search?q=<county> parcels&f=json&num=40`) for the assessor
  viewer's **web map**, then `.../sharing/rest/content/items/<id>/data?f=json` and read `operationalLayers[].url`.
  That is how Pinal's live host (`gis.pinal.gov/mapping/rest`) was found after the old one stopped resolving.
- County GIS portal ("open data") and the planning department's zoning web map use the same trick.
- Statewide: NC OneMap `NC1Map_Parcels` (owners often blank), IndianaMap `Parcel_Boundaries_of_Indiana_Current` (no owners),
  ADWR `General/Parcels` (AZ, owners partial), TxGIO (Texas, shapefiles).
- Old tgis / WTHGIS viewers: `tgis/Index.ashx?action=layerIndex&dsid=<id>&name=<layer>` lists the table and
  `index.ashx?action=getFtrs&dsid=<id>&i1=<start>&cnt=<n>` pages it (fields split on `\x1f`, rows on `\x1e`). Greene IN
  is done this way in `scripts/fetch_greene_in_owners.py`.
- Cities: a city GIS host (e.g. `rogue.casagrandeaz.gov`) or the city's own ArcGIS Online org (Eloy) often has the
  zoning layer and water / sewer mains.

## What to record about a candidate layer
| Item | Why |
|---|---|
| Record count | tells you whether windows are needed (over ~300k, yes) |
| `maxRecordCount` | paging size |
| Owner, mailing, situs, value, land-use, acres field names | the `fields_fn` mapping |
| Share of 16+ ac parcels with blank owner | whether this source is good enough |
| Key format and placeholders (`NAP`, `NO ID`, blank) | `key_fn`, duplicate keys merge into one huge parcel |
| Acreage units (acres, sq ft, square degrees) | `acres_fn`; check against geometry |
| Spatial reference | arcgis_query asks for WGS84 (`outSR=4326`) |

## Zoning code text
Official first: American Legal (`codelibrary.amlegal.com`), Municode (`library.municode.com`), eCode360. If automated
fetches return 403, open the page in the browser tool or find the county's PDF. A Zoneomics mirror is a last resort and
must be tagged as such in the JSON.
