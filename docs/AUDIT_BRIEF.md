# Audit brief: ACMI CRM and land search

For an independent reviewer (human or AI). Read this first, then `docs/HANDOFF.md` for the build history. The reviewer
has the whole working folder, including the local `db.sqlite3` (real parcels, sites, CRM records) and `.env`, so it can
check the code against the real data as well as read it. The test suite itself runs on a throwaway SQLite database and
mocks the network, so it never touches that data.

## What it is
A Django 5.2 app (single app `crm`) used by ACMI Properties to run its CRM (companies, people, deals, outreach, mail
sync, meetings) and a **land-search module** (`crm/land/`): parcels from county GIS services scored against ACMI's
defense-industrial "buy-box", saved sites, client searches with shareable report links, and one-page site briefs with
aerials, flood layers, zoning definitions and a PDF export. Deployed (intended) on Cloud Run with Cloud SQL (Postgres);
local dev is SQLite.

## Run it
```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt     # or .venv/bin/pip on macOS / Linux
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py test crm        # 182 tests, a few minutes; no network needed
```

## Map of the land module (`crm/land/`)
| File | Role |
|---|---|
| `counties.py` | Per-county config: parcel service, field mapping, key rules, zoning overlays, "full windows" |
| `parcels.py`, `owners.py` | Loading counties; joining owner files onto stored parcels (`load_owners`) |
| `sources.py`, `layers.py` | Stored map layers (power, rail, highways, city limits), distance measuring |
| `lookup.py` | Live checks per parcel: FEMA flood, wetlands, utilities, HUBZone, EPA, incentives |
| `score.py` | Buy-box score, deal-killers, zoning buckets, `methodology()` (documented in reports) |
| `zoning_ref.py` + `zoning_ref/*.json` | Researched zoning code definitions and permitted uses per jurisdiction |
| `briefs.py`, `brief_views.py`, `listing.py` | Site briefs, comparison, aerials, PDF export, flyer details |
| `assignments.py`, `assignment_views.py` | Client searches and the tokenised guest view (`/g/<token>/`) |
| `tracker.py`, `trim.py`, `siteplan.py`, `qd.py` | Site deal tracking, parcel trimming, site planning, safety-arc layouts |
| `views.py` | Map, parcel card, assemblage, saved sites JSON |
| `management/commands/` | `load_parcels`, `load_land_layers`, `load_owners`, `score_parcels`, `check_county` ... |

## Where to look hardest (trust boundaries and risk)
1. **Access control.** `crm/middleware.py` `LoginRequiredMiddleware` lets `PUBLIC_PREFIXES` through
   (`/accounts/`, `/static/`, `/healthz`, `/internal/`, `/g/`). Check `/g/<token>/` (guest reports): tokens
   (`assignments.new_token`, `guest_assignment`), expiry and switch-off, and that no internal field (owner names, site
   notes, scores, listing details) reaches a guest page. Check `/internal/sync-mail/` is protected by something stronger
   than being unlinked.
2. **PDF export** (`briefs.html_to_pdf`, `brief_views._pdf`): the server renders HTML containing user-entered text (site
   notes, flyer fields) and prints it with headless Chrome via a `file://` temp page. Look for HTML / script injection,
   Chrome reading local files, `--no-sandbox` use, temp-file handling, and what a malicious site name does to the
   download filename (`briefs.export_name`).
3. **Outbound requests.** The code calls many third-party services (county ArcGIS hosts, FEMA, Esri, USFWS, SBA, EPA,
   TIGERweb, an LLM in `ai.py`). Check timeouts and retries, that no URL is built from user input, and that failures
   degrade rather than 500 (`lookup.compute_checks`, `briefs.render_aerial`).
4. **Data loading.** `parcels._save` prunes stored parcels not seen in a reload (unless they belong to a site, have notes,
   a status, a company or a QD design). Check that cannot delete wanted work. Parcels with no parcel number are keyed
   `NAP<objectid>` (Pinal) which can change when the county republishes. `counties.full_windows` loads tens of thousands of
   small parcels.
5. **Scoring.** `score.screen` against `score.methodology()` (a test pins the headline numbers, not all of them).
   Site ranking (`briefs.build` "ranking") ignores the score by design; confirm that is acceptable.
6. **Forms and writes.** CSRF and permissions on `site_listing`, `site_save`, `assemble_point`, `parcel_save`
   (the last two fetch from county services and write rows from a clicked point).
7. **Settings.** `config/settings.py`: `DEBUG` defaults to True, `SECRET_KEY` has an insecure development default that is
   rejected only when `DEBUG` is false, and `LOCAL_LOGIN_ENABLED` defaults to True. Confirm production sets
   `DJANGO_DEBUG=false`, a real key, and turns local login off if it should be.

## Checks that need the real database
- `manage.py check_county <County_ST>` for every loaded county (Cumberland / Harnett / Hoke / Moore NC, Greene IN,
  Maricopa / Pinal AZ, Charles MD, Limestone / Madison / Morgan AL, six Texas counties): owner, mailing and zoning
  coverage and zoning codes with no definition.
- Spot-check 3 to 5 parcels per county against the county's own public viewer (owner, acres, zoning).
- Sites 29 to 32 (the three Arizona sites and Goodyear NC) have saved listing details; open their briefs and compare the
  numbers with the flyers in `Downloads` if available.
- Look for duplicate parcels (same county and key), parcels with acres of 0, and stale `SiteParcelDeal` rows.

## Data trust (a code audit can't settle these)
- **Zoning reference:** Eloy and Maricopa were read from the official code. **Casa Grande, Charles County MD,
  Huntsville AL and Pinal County came from a third-party mirror** and are tagged "check source" in the reports; the
  use-permission tables for Casa Grande B-1..I-2 and Charles County are incomplete. A person should confirm against the
  official codes. Codes whose meaning was inferred (Huntsville AC, HM, DS, M, M2, RO2; Pinal PM/RV, MH-435) are noted
  in the JSON `caveats`.
- **Flyer details** on the three Arizona sites were typed in from broker flyers and are the brokers' statements.
- **Owner data:** Hoke NC, Pinal AZ, Cumberland / Harnett / Moore NC and Maricopa AZ have owners and mailing
  addresses from county services; Greene IN from the county's WTHGIS viewer; Charles MD mailing address coverage is 66%.
  `manage.py check_county <County_ST>` prints the numbers.
- Zoning layers exist for Maricopa County, Casa Grande, Eloy, Pinal County, Charles County and Huntsville only. The Texas,
  NC and Greene IN counties show no zoning district.

## Findings already known
- **`requirements.txt` was missing shapely, pyproj, pyshp, numpy and Pillow**, which `crm/land` imports at startup. Fixed in
  this commit; worth confirming with a clean install and a Docker build.
- The Dockerfile now installs `chromium` for PDFs; the image has not been rebuilt or tested.
- Tests for zoning-data content only check structure, not the legal accuracy of the text.
- The server's PDF export waits on slow third parties; the first package takes minutes cold (cached for an hour per aerial).

## Suggested questions for the reviewer
1. Can an unauthenticated visitor reach anything but guest reports and the health check? Can one guest see another's data?
2. Can any stored text reach the PDF page or a download name unescaped?
3. Which code paths could silently show wrong or stale numbers (acreage units, key collisions, parcels merged by key)?
4. Is the buy-box methodology text still true of `score.screen`? List any drift.
5. What breaks on Postgres that works on SQLite (JSON fields, `bulk_update` limits, concurrency in `run_checks_many`)?
6. Anything that would fail on a fresh deploy (missing requirements, migrations 0025 to 0030, static files, Chromium)?
