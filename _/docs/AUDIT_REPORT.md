# Audit report: ACMI CRM and land search

Commit f764116, 2026-10-16. Follows `docs/AUDIT_BRIEF.md`.

## Fix status (branch `worktree-audit-fixes`, 2026-10-16)
All fixes below are on the branch; 224 tests pass (182 existing + 42 in `crm/tests/test_audit_fixes.py`). The map page
was checked in a browser with the new Content-Security-Policy and integrity hashes (no console errors).

| Item | Status |
|---|---|
| S1 password sign-in | **Fixed.** `crm.auth.PasswordBackend` / `AllauthBackend` refuse passwords when `ACMI_LOCAL_LOGIN=false`; `/admin/login/` hands over to Google sign-in; password reset and sign-up pages 404. Everyone is signed out once (the backend paths changed). |
| S2 roles | **Fixed (adjustable):** a *Managers* group (`crm/permissions.py`) is needed to send outreach batches, change outreach settings / templates, delete or merge records, delete land sites and turn client links on. Migration 0032 put every existing account in it, so nobody loses anything today; first-time Google sign-ins start without it until an admin adds them (Admin › Groups). |
| S3 / finding 1 outreach double-send | **Fixed.** Batch and each email are claimed with conditional updates; starting a batch is one locked step; new status "Sending (unconfirmed)"; `send_batch --resume`; Stop closes a batch whose process died. Migration 0031. |
| S4 logging | **Fixed.** `LOGGING` sends errors to the console (Cloud Logging). |
| S5 CDN / CSP | **Fixed.** SRI hashes on all unpkg files; `Content-Security-Policy` header (scripts from this site and unpkg only, no plugins, no framing). |
| S6 lower-risk | **Fixed:** `?next=` uses Django's host check; News draft HTML rebuilt by a parser-based allowlist (the old regex let `<svg/onload>` through) and cleaned again when shown; Google tokens removed from admin; "Re-run live checks" is a POST; site reads no longer write. |
| Deploy | **Fixed in config, not yet run:** Cloud Build migrates (Cloud Run job) before deploying and wires env vars + Secret Manager; Dockerfile runs as a non-root user (Chrome via `ACMI_CHROME_NO_SANDBOX`), corrected worker comment; HSTS 1 year + subdomains, SSL redirect (health check exempt), 50+ character secret key enforced; SQLite side files ignored. Still open: background sends on Cloud Run, uploaded files on Cloud Run (see improvements). |
| Findings 2-10 | **Fixed** (each has a test). |
| Public land scoring | **Fixed in code:** federal owners held at 40, state / local / institutional owners −5 with a warning (`score.public_owner`); methodology page updated. **Stored scores change only after `manage.py score_parcels` is run.** |
| Data notes | **Fixed:** loads skip slivers under 0.01 ac, store each identical outline once (noted in the kept record's legal description), and move a parcel whose object-id key changed to its new key instead of pruning it; `manage.py clean_parcels` applies the first two to what is stored. **Open:** Hays values (42%) and Charles mailing addresses (66%) need assessor files. |
| Outreach attachments / uploads | **Fixed:** a missing attachment file fails the batch before anything is sent (it used to send without it); uploaded documents are served through a signed-in view (`/documents/<id>/file/`; only PDFs and images open in the browser). Storage that survives Cloud Run restarts is still open. |
| CI | **Added:** `.github/workflows/tests.yml` runs migrations check, the tests and `check --deploy` on Postgres once the repo is on GitHub. |

---
Original report (findings as of f764116):

## How it was checked
- **Tests:** all 182 pass (46 s) with the Anthropic key and Gmail password blanked.
- **Security review** of the whole tree. `/security-review` can't run here because it diffs against `origin/HEAD` and the repo has no remote, so its method (high-confidence, exploitable issues only) was applied by hand to every trust boundary in the brief.
- **Code review** (`/code-review high .`): 10 findings, indexed in section 4.
- **Live database**, opened read-only (SQLite `mode=ro`; writes are refused): data checks, `check_county` for every county, and a column-length check for Postgres.
- **Live services:** 68 parcels spot-checked against the county / state GIS services, a `pip-audit` of every installed package, and one server-made PDF (site 29).
- **Fresh install** from `git archive HEAD`: clean venv, `pip check`, `collectstatic`, `migrate` on an empty database, `check --deploy` with production settings.
- **Not checked:** the Docker image build (Docker isn't installed), a run on Postgres, and the sites 29–32 flyer comparison (no flyer files were provided).

## 1. Security: fix before go-live

| # | Issue | Evidence | Fix |
|---|---|---|---|
| S1 | **Password sign-in can't be switched off.** `ACMI_LOCAL_LOGIN=false` only hides the form (`templates/account/login.html:17`). `/accounts/login/` and `/admin/login/` still accept a username and password, and `/accounts/password/reset/` is open. | Verified with a test: with local login off, both POSTs return 302 and a signed-in session. | Override `AccountAdapter.authenticate()` to return `None` when local login is off. Send `/admin/login/` to Google sign-in. Disable password reset. Set unusable passwords on production accounts. |
| S2 | **No roles.** Anyone signed in can send outreach batches, delete or merge records, create client share links and use admin pages they're staff for. | No permission checks in any view (only the sidebar's admin link checks `is_staff`). | Decide who may send, delete and share. Add one or two Django groups and a decorator on those views. |
| S3 | **Outreach can double-send** (code-review finding 1). | `sender.py:159` accepts a batch that is already SENDING; `outreach/views.py:92` is check-then-create. | Claim the batch atomically (`UPDATE … WHERE status='queued'`) and claim each item before sending it. |
| S4 | **Production would hide errors.** There's no `LOGGING` setting, so with `DEBUG=false` Django logs crashes only to a console handler filtered to DEBUG and to an `AdminEmailHandler` with no `ADMINS`. | Confirmed in the fresh install (`RequireDebugTrue` console, `RequireDebugFalse` mail). | Add a `LOGGING` config that sends `django.request` errors to stdout (Cloud Logging picks it up). |
| S5 | Map scripts load from unpkg with no SRI hash, and there's no Content-Security-Policy. A compromised CDN file would run with full access to the CRM session. | `land_search.html` and the site and guest pages | Self-host Leaflet and esri-leaflet in `crm/static/`, or add `integrity=`. Add a CSP (django-csp). |
| S6 | Lower risk: `_safe_next` lets `/\evil.com` through, and browsers treat it as `//evil.com`. The News page renders Gmail draft HTML with `|safe` (your own drafts). Google Calendar refresh tokens are stored in plain text and shown to staff in Django admin. Several GETs write to the database (brief refresh, `ensure_deals` on every read, guest view counter). | `views.py:38`, `partials/news_card.html:55`, `SOCIALACCOUNT_STORE_TOKENS` | Reject `\` in next URLs. Sanitise or iframe the draft HTML. Unregister `SocialToken` from admin. |

**Already sound:** `/internal/sync-mail/` refuses all requests when no secret is set and compares it in constant time. Guest links use 192-bit tokens; expiry, switch-off and regenerate work; and guest pages only reach that assignment's entries marked "show to client" (no owner names, notes, deal status or scores). An anonymous request to every route returned only the sign-in pages, `/healthz` and 404s. CSRF protects every write except the cron endpoint. PDF HTML is autoescaped (the only `|safe` is the bundled CSS), and the download name is cut to `[A-Za-z0-9_]`. Outbound requests have timeouts. The parcel `IN (...)` query only takes digits or letters. `pip-audit` found no known vulnerabilities.

## 2. Hosting and deploy readiness
- **Python side passes:** clean install, `pip check`, 135 static files collected, all migrations apply to an empty database, no missing migrations. No stored text is longer than its column, so moving the data to Postgres won't fail on varchar limits.
- `cloudbuild.yaml` says "build, push, migrate, deploy" but has **no migrate step**, and sets no environment variables or Secret Manager secrets.
- The **Dockerfile starts 2 gunicorn workers**, but its own comment says the mail sync and send batches share state with one web process.
- **Background work doesn't fit request-based Cloud Run:** outreach batches start as a detached subprocess, `/internal/sync-mail/` runs the whole mail and calendar sync inside one request, and a cold PDF package takes minutes. Use Cloud Run Jobs or Cloud Tasks, or at least CPU-always-allocated and a longer request timeout.
- **Uploads:** `media/` isn't served when `DEBUG=false`, and Cloud Run's disk is wiped on restart (the Drive upload in `documents.store_upload` is still a stub).
- `check --deploy`: set `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD` and `SECURE_SSL_REDIRECT` (or redirect at the load balancer), raise HSTS above 1 hour, and use a 50+ character secret key. The image runs as root, so Chrome gets `--no-sandbox`; add a non-root `USER`.
- `.gitignore` covers `*.sqlite3` but not `db.sqlite3-wal` / `-shm`, which appear while the mail sync runs.

## 3. Data (live database, read-only)

| County | Parcels | Owner | Mailing | Zoning | Notes |
|---|---|---|---|---|---|
| Limestone / Madison / Morgan AL | 3,925 / 4,751 / 4,166 | 100% | 100% | 6% / 18% / 0% | Codes with no scoring bucket: AC, HM, DS |
| Maricopa AZ | 15,397 | 100% | 100% | 66% | AD-1/2/3 and "R1-6 RUPD" have no bucket |
| Pinal AZ | 63,438 | 99% | 99% | 100% | 44 parcels with 0 acres; "Undesignated" / "Multiple" have no bucket |
| Greene IN | 4,545 | 99% | 99% | 0% | owners from the WTHGIS file (the live layer has none, as expected) |
| Charles MD | 2,148 | 100% | **66%** | 100% | code **WPC** (3 parcels) has no zoning definition |
| Cumberland / Harnett / Hoke / Moore NC | 3,103 / 4,092 / 1,333 / 4,192 | 98–100% | 96–100% | 0% | |
| Bastrop / Blanco / Caldwell / Comal / Guadalupe / Hays TX | 2,418–4,852 each | 96–100% | 96–99% | 0% | Hays assessed value only **42%**; `check_county` fails for Texas (finding 7) |

- **Spot-check, 62 of 68 match the live source.** The differences: 4 Greene IN parcels (expected, owners came from a file), Moore NC 758500768894 (deed acres 95.8 vs mapped 27.4), Hays 13247 (the live service returns sibling id 13248, finding 4), plus the wrong-county label found for Harnett (finding 3). NC OneMap is in NC State Plane feet, so `Shape__Area / 43,560` is correct.
- **Duplicates and keys:** no duplicate (county, key) pairs. **40 groups (85 parcels) share an identical outline:** 25 in Pinal, 14 in Cumberland (OID-keyed, i.e. no PIN), 1 in Hays. Fifteen of those groups are 20+ ac, so they inflate counts and owner totals. 988 parcels have unstable `NAP` / `OID` keys; none is on a site yet.
- **Public land ranks as top buy-box land.** 809 parcels with public-looking owners (United States, TVA, State of …, arsenal, county, city) score 60 or more, and 139 score 75 or more; for example Redstone Arsenal scores 76 and TVA tracts about 80. Only Texas rolls carry the tax-exempt code `X` that the scorer looks for, and a zoning layer overrides it anyway. Add an owner-name check for public owners as a warning or a points penalty.
- **Methodology vs `score.screen()`:** the thresholds and points match. Two kinds of drift. Undocumented in the methodology: a zoning code with no bucket scores 4 points. Outdated in the docs: HANDOFF line 290 still calls a tax-exempt owner a deal-killer, but the code treats it as a warning.
- **Sites and deals:** no stale `SiteParcelDeal` rows, and no parcel-level site plans at risk from the prune today.
- **Site 29 PDF:** 6 pages, made in about 8 s (aerials 6.6 s, Chrome 1.6 s), with the logo and the flyer-claims table. It prints "100-year floodplain (zone A, AE, X)" (finding 6).

## 4. Code review findings (`/code-review high`)
1. Outreach batch can be sent twice (`crm/outreach/sender.py:159`)
2. A failed re-check erases the flood deal-killer and lifts the score (`crm/land/lookup.py:324`)
3. Map click labels NC parcels with the wrong county (`crm/land/lookup.py:369`), verified live
4. Click on a merged Hays parcel stores a duplicate (`crm/land/views.py:277`), verified live
5. Reload prune ignores site plans and partial loads (`crm/land/parcels.py:321`)
6. Brief calls Zone X part of the 100-year floodplain (`crm/land/briefs.py:379`), seen in the site 29 PDF
7. `check_county` fails for all Texas counties (`crm/management/commands/check_county.py:24`)
8. Every site read writes to the database, including guest pages (`crm/land/tracker.py:36`)
9. Live-saved parcels get click-position keys and overwrite file-loaded owners (`crm/land/views.py:299`)
10. A bad expiry date silently makes a share link permanent, and the guest view count loses updates (`crm/land/assignment_views.py:68`)

## 5. Answers to the brief's questions
1. **What can an anonymous visitor reach?** Only the sign-in pages, `/healthz`, the secret-protected cron URL and guest links. A guest can't see another assignment. The real gap is S1: password sign-in can't be turned off.
2. **Can stored text reach the PDF or download name unescaped?** No: templates autoescape, and the filename is sanitised. Keep `|safe` out of `land_briefs_pdf.html`, because Chrome renders it as a `file://` page.
3. **Which paths could show wrong or stale numbers?** Findings 2 to 6 and 9, public land ranked high, outline duplicates, NAP / OID keys, and mapped-vs-deed acre outliers.
4. **Does the methodology match `score.screen()`?** Yes on thresholds and points. The drift is listed in section 3.
5. **What breaks on Postgres?** Nothing found statically: no raw SQL, `bulk_update` batches stay under the parameter limit, no over-length text, and the live-check threads don't touch the database. The race in finding 1 exists on both. It has not been run on Postgres.
6. **What fails on a fresh deploy?** The Python side is fine. Missing: a migrate step, secrets wiring, logging, the worker count, a non-root user, persistent media, and a test of the Chromium image layer.
