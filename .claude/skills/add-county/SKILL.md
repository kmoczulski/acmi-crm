---
name: add-county
description: Add a new county (or city zoning) to the ACMI land-search app and get all of its data: parcels with owner, mailing address and acreage, the zoning layer, the zoning code definitions and permitted uses from the county or city code, and utility layers. Use whenever the user wants a new county, state, or city's zoning added to land search, or finds owner or zoning data missing for a county.
---

# Add a county to ACMI land search

The goal for every county is the same set of facts per parcel: **parcel number, owner + mailing address, acreage,
assessed value, land use, zoning district**, and for every zoning district a plain-language **definition, permitted uses
and standards from the official code**. Where a fact truly isn't published, the app shows N/A. It never guesses.

Read first: `docs/HANDOFF.md` (land sections), `crm/land/counties.py` (existing configs are the templates: Cumberland NC
= statewide layer filtered by county; Hoke NC = county's own service; Pinal AZ = county service + zoning + windows;
Greene IN = statewide layer with owners added from a file), `crm/land/zoning_ref.py`.

Work through the steps in order and report what you found at each one. Don't skip the verification step.

## 1. Find the parcel source (and prove it carries what we need)
Try in this order; stop at the first that has **owner names and mailing addresses**:
1. The county's own ArcGIS REST service. Find the host from the assessor's parcel viewer: search ArcGIS Online for the
   viewer's web map, then read its `operationalLayers` urls (this is how Pinal's real host was found after its old one died).
2. A state-wide layer (NC OneMap, IndianaMap, ADWR). These often leave **owner blank** for some counties: test it.
3. A city or neighbouring-county mirror of the assessor roll.
4. A viewer with an index/export (WTHGIS `tgis/index.ashx`) or the assessor's bulk file / Regrid CSV, joined with
   `load_owners`.
See `reference/source_hunting.md` for the probe commands and what to look at in the field list.

Before writing any code, record for the candidate layer: record count, `maxRecordCount`, geometry type, the **field that
holds owner / mailing / acres / value / land-use**, the key format, and the **share of parcels over 16 acres with a blank
owner**. Pitfalls we hit: junk acreage fields (NC `gisacres` is square degrees: use recorded acres or shape area),
placeholder parcel numbers (Pinal "NAP" shared by many parcels), counties with owner and mailing withheld in a state layer.

## 2. Add the config
Add an entry to `COUNTIES` in `crm/land/counties.py` (template: `reference/county_config_template.py`): `fips`, `name`,
`state`, `bbox`, `aliases`, `parcels_url`, `key_field`/`key_fn`, `owner_field`, `count_fields`, `acres_fn`,
`big_where_fn`, `fields_fn` (map to Parcel fields), `base_where` for shared layers, `count_where` if the county has
huge numbers of tiny lots, and `full_windows` for areas where every parcel of any size is wanted (sites being assembled).
Also wire the state/region: `sources.STATE_COUNTY_REGIONS`, `REGIONS` bbox + a `town_xx` city-limits layer (TIGERweb
places by state FIPS), the substation state filter, `models.LandCounty.state_name`, `land_layers.js` state list,
`load_land_layers --region`. A new state means touching all of those; grep an existing state (e.g. `'AZ'`) to find them.

## 3. Zoning layer
Find the county's zoning layer, and each city's if cities zone themselves. Check for coded-value domains (numbers that
stand for codes: use `value_labels`) and for city names inside the county layer (use `refines` + `when_value` so the city's
own layer replaces it). Map codes to scoring buckets in `score.py` (`ZONING_CODES[<fips>]`). If there is no public zoning
layer, say so: the county goes in `zoning_ref.NO_LAYER` (or the state does) and the report shows a note, not a guess.

## 4. Zoning code definitions and permitted uses (the part people forget)
For each jurisdiction that has zoning data, write `crm/land/zoning_ref/<key>.json` (schema:
`reference/zoning_json_schema.md`) covering **every code that actually occurs** in the stored parcels
(`check_county` lists the ones still undefined). For each district: name, purpose, **permitted uses by right**,
conditional / special-permit uses, lot size, height, setbacks, coverage, the code section, the source URL.
- Use the official code (American Legal, Municode, eCode360, the county's PDF). If a site blocks automated fetching, read
  it in the browser tool, or use the PDF. If only a third-party mirror (Zoneomics) is reachable, **say so in `edition` /
  `code_url`** so the reports tag it "check source", and put every gap in `caveats`.
- Never invent. Unknown = `null`. Say when a use (data center, explosives / energetics manufacturing, solar, outdoor
  storage, truck terminal) is "not addressed" rather than assuming.
- Several jurisdictions at once: run one research subagent per jurisdiction in parallel, each writing its own JSON; then
  spot-check one industrial district yourself against the source before accepting the batch.
- Register the jurisdiction in `zoning_ref.AUTHORITY_FILES` (the overlay `authority` text) or `COUNTY_FILES`.

## 5. Utilities
Water / sewer / electric provider layers go in `lookup.AZ_PROVIDERS`-style config or the state's existing check
(Texas uses PUC CCNs; elsewhere HIFLD). Look for the state utility commission's certificate-area layers and the county's
electrical or water district layers. City-owned utilities usually aren't mapped; say so in the notes.

## 6. Load and verify
```
.venv/Scripts/python manage.py load_parcels <County_ST>          # 2 to 30 min; --min-acres 0 for everything
.venv/Scripts/python manage.py load_land_layers --region <region>  # power, rail, highways, city limits
.venv/Scripts/python manage.py score_parcels --remeasure
.venv/Scripts/python manage.py check_county <County_ST>           # coverage report: owner %, mailing %, zoning %, undefined codes
```
`check_county` is the gate. Aim for owner and mailing coverage near 100% for parcels you can reach, zoning on every
parcel if a layer exists, **zero undefined zoning codes**, and no "no scoring bucket" codes. Anything short of that gets
fixed or written down as a known gap in `HANDOFF.md`. Spot-check two real parcels you know against the county's own viewer.

## 7. Finish
- Tests in `crm/tests/test_land.py` (pattern: `FayettevilleCountyTests`, `ArizonaCountyTests`): field mapping, key
  format, acres, region, zoning bucket, and anything unusual (placeholder keys, windows).
- `.venv/Scripts/python manage.py test crm` must be clean.
- Update `docs/HANDOFF.md` (source, quirks, coverage numbers, what is missing and why). Don't commit unless asked.

## Gotchas
- Windows shell: `\n` and `\\` in heredocs get mangled. Write Python patch scripts to a file with the Write tool.
- SQLite "database is locked": don't run two loads, or a load and `score_parcels`, at once.
- FEMA's NFHL layer id changes (28 / 35); always go through `sources.fema_flood_url()`.
- Don't scrape beyond what the public viewer's own paging does; keep a pause between requests.
- Loading a whole 1M-parcel county isn't practical: keep the 20 ac cutoff and use `full_windows` around real sites.
