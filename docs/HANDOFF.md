# ACMI CRM: Handoff (as of 2026-10-14, night)

Read this first when picking the project up in a new chat, along with
`attio_spec.md` in this folder.

## Goal

One program for the ACMI team that replaces the outreach workbook, the send
scripts, the news review app and the Trello hand-offs, and adds Granola meetings
and defense-industrial land search. Kellen chose to **build everything** rather
than buy HubSpot, Attio or Pipedrive.

## Decisions made

| Topic | Decision |
|---|---|
| Stack | Django (Python) + Postgres/PostGIS; SQLite on the laptop for development |
| Users | ACMI team, hosted |
| Hosting | Google Cloud (Cloud Run + Cloud SQL) under ACMI's Google Workspace org |
| Sign-in | Google sign-in, internal Workspace app, acmigroup.com accounts only |
| Code | Private GitHub repo; Cloud Run deploys from it |
| Email (phase 2) | Per-user Gmail API via Google sign-in, replacing the shared app password |
| Granola (phase 3) | Granola API if the plan has one; otherwise a daily scheduled Claude task pushes meetings in |
| Land search (phase 4) | **Built 2026-10-14.** Texas Triangle (suburban/rural counties, not the big metros) + Charles Co. MD. Parcels are a local copy (state publishes once a year); utilities, flood, wetlands, HUBZone, EPA are live per parcel |
| Attio | Used as a design reference only (trial workspace `acmi-properties`). Copy behaviour, never code |

## Phases

1. **CRM core: built 2026-10-13, running locally on SQLite.** See "Phase 1 status" below.
2. Outreach: Gmail, templates, send queue, reply/bounce detection, news review inside the app.
   **Gmail sync (read side) built 2026-10-13.** See "Phase 2 status" below.
3. Granola + follow-up queues + tasks.
4. Land search: parcels, overlays, buy-box scoring, map, "Pursue". **Built 2026-10-14**, see below.

## Existing systems (leave untouched until replaced)

- `C:\Users\Kemoc\Desktop\Prospecting\ACMI_Outreach_Engine.xlsx`: the only live
  copy. Sheets: Master_Contacts (~2,670), Outreach_Log (~20k), Email_Templates,
  Send_Queue, News_Outreach_Queue, News_Review_Log, Settings, Dashboard, Audit,
  Weekly_Report. This is the Phase 1 import source.
- `Desktop\Prospecting\send_emails_v4.py`, `scan_responses.py`,
  `send_weekly_report.py`: the logic to port in Phase 2.
- `Desktop\Prospecting\news_review\`: Flask news review app (Gmail IMAP, Trello REST).

## Tooling status

- Installed: Python 3.13, Git.
- Kellen installed or created: Node.js + REA (`npx rea-agents setup`), Attio
  trial, and "a project" (2026-10-13; confirm whether that's the Google Cloud
  project, the GitHub repo or something else).
- Still to confirm: GitHub CLI (`gh`) + `gh auth login`, Google Cloud CLI.

## Phase 1 status (2026-10-13)

Built in this folder (see `README.md` for commands and layout):

- Django 5.2 project (`config/`, app `crm/`), SQLite locally, Postgres via env vars.
- Sign-in: allauth Google sign-in limited to acmigroup.com (switches on when
  `GOOGLE_CLIENT_ID` is set), plus local username login. Every page needs login.
- Models: everything in spec section 8, plus `EmailTemplate` and `NewsDraft`.
  `FieldHistory` + stage-change timeline events on Company status/priority/owner/fit,
  Person priority/owner/company/title, Opportunity stage/value/owner/campus.
- Importer `import_workbook` committed to the local DB: 1,746 companies, 2,830
  people, 21,405 activities (20k outreach sends + responses + news), 14 templates,
  146 news drafts, 5 users (kellen, simon, dave, dowell, joe), campuses NSIH and MEIH.
  Re-run creates nothing new.
- Screens: Home, Companies/People tables (filters, sort, columns, saved views,
  pagination), record pages (Attio layout, highlight cards, timeline, tasks, notes,
  lists, prev/next), Opportunity kanban (drag to change stage), Tasks, Lists (board/table).
- Smoke-tested every page and write action through the Django test client.

Import mapping decisions worth knowing:
- Company priority = highest priority among its contacts. Company flag set only
  when *every* contact shares it (so 100 companies are flagged Exclude; 270 people are).
- Company stage derived once: any send → Contacted, Responded/Meeting/Referred →
  Engaged, Not Interested → Not interested. Only advances Prospect/Contacted.
- Workbook dates are stored as local midnight (America/Chicago).

Not done yet / next steps:
- Duplicate companies from workbook name variants: 67 email domains are shared by 2+
  companies (e.g. L3Harris has 7 variants). Need a merge tool + Kellen's call on
  which are true subsidiaries.
- No automated test suite yet (only the smoke script).
- No git repo yet (`.gitignore` is ready). Deployment files (Dockerfile, Cloud Run)
  not written.
- Kellen's local account has no password: `manage.py changepassword kellen`.

## Phase 2 status (2026-10-13)

Gmail sync, read side (`crm/mail/`, Mail page, `sync_mail` command, 23 tests passing):
- **Connection: IMAP + Gmail app password** (Kellen's call, matches the existing
  scripts; no Google Cloud project needed). Password comes from
  `.env` → `ACMI_IMAP_PASSWORD_<USERNAME>`, never the database. Reads "All Mail"
  read-only, resumes by IMAP UID, first sync reads `backfill_days` (default 1 year).
  Switching to the Gmail API via Google sign-in later only replaces `crm/mail/imap.py`.
- Classification ports `scan_responses.py`: bounce (mailer-daemon/postmaster or
  bounce subject; failed address from X-Failed-Recipients or body), out-of-office
  (subject or Auto-Submitted header), reply = reply in a thread ACMI started
  (matched by Message-ID, Gmail thread id, or template/news-draft subject), with
  "Not Interested" phrases checked only above the quoted text.
- Matching: person by email → company by domain (creates the person) → otherwise the
  domain goes to the review queue on the Mail page (create company / attach to
  existing / ignore / protect). Free-mail, internal ACMI and newsletter mail is skipped.
- One Activity per email per company, keyed on Message-ID, so the same email in two
  mailboxes logs once. Stages auto-advance (sent → Contacted, reply → Engaged,
  not interested → Not interested) only from Prospect/Contacted, with history.
- Not run against a real mailbox yet: waiting on Kellen's app password in `.env`.
- Not built yet: scheduling locally (Windows Task Scheduler) or in prod (Cloud
  Scheduler → `/internal/sync-mail/`), sending email, templates/send queue, news review.

## Built 2026-10-13 (evening), all on top of Phase 2

- Full year+ of Kellen's Gmail synced (54.6k messages, ~27k timeline emails). Sync is
  parallel (8 IMAP connections, adaptive), reconnects on drops, guarded against two
  copies running at once; SQLite in WAL mode with IMMEDIATE transactions.
- Review queue auto-attaches domains that spell a company name; replies re-detected
  after a domain is approved (`reclassify_replies`).
- Team connections: `Activity.team_members`, strongest teammate + Weak/Medium/Strong on
  Company/Person, "Kellen → Ben Allen" on timelines.
- Documents: `Attachment` (names/sizes from IMAP BODYSTRUCTURE, never file bytes),
  auto-categorized (PDA, LOI, NDA, Contract, Marketing...). `sync_attachments` backfill.
- Names: `fill_names` command (Gmail display names → workbook → first.last@).
- Fit score 1–5 with definitions; campus of interest (NSIH/MEIH/own/none) as chips.
- Read-only Details panel (empty fields hidden) + Edit pop-up; Delete on records.
- AI summary panel above Recent activity (`crm/ai.py`, Claude Opus 5.5, structured
  output, server-side refusal fallback). **Off until ANTHROPIC_API_KEY is in .env.**
- Home: positive replies show the opening of what was said. White ACMI logo in sidebar.
- 44 tests: `manage.py test crm`.

## Built 2026-10-13 (late)

- Mail review queue: Ignore/Protect moved to the second column (was off-screen).
- Automatic sync: Windows scheduled task "ACMI CRM mail sync" runs
  `scripts/sync_mail_hidden.vbs` -> `scripts/sync_mail.cmd` every 15 min while signed
  in; log in `logs/sync_mail.log`.
- 70 empty people deleted (backup: `backups/db-2026-10-13-before-cleanup.sqlite3`).
- Duplicate merge tool: Companies -> Possible duplicates (97 groups; name variants
  pre-ticked, shared-domain-only groups left unticked); "Merge another company into this
  one" on each company page. `crm/merge.py`.
- Project vs prospecting (`crm/projects.py`, Projects page): per Gmail thread.
  Prospecting = workbook campaigns, template/news subjects, subjects sent to 8+
  companies (invites exempt), and pitches nobody answered. Project = thread's own text
  (not quotes/signatures) matches a project keyword. Projects: MEIH, NSIH,
  "4390 Indian Head Hwy" (site under MEIH; Kellen said 43490, mail says 4390, both match).
- News review moved into the CRM (`crm/news/`, News review page). Parser copied from
  Prospecting/news_review/scan_parser.py; Gmail draft list/edit/trash ported.
  **Kellen's decisions:** writes to the CRM only (no workbook), and CRM tasks instead of
  Trello ("Find contact at X", "Research new lead: X"). Apply runs once (locked after).
  The old Flask app should no longer be used (it writes the workbook and Trello).
- Home: automatic stage changes hidden from Recent activity; project chips shown.
- 58 tests.

## Built 2026-10-14: everything from Desktop/Prospecting now lives in the CRM

- **Outreach sending** (`crm/outreach/`, Outreach page) replaces send_emails_v4.py:
  OutreachSettings (the old Settings sheet + two safety rules: skip late-stage
  companies, skip anyone who replied in 30 days), templates with a `campaign` field
  (current: "September v1" -> T014), same template matching, `{first_name}` now works,
  queue preview + skip reasons, test send to yourself, typed SEND confirmation, daily
  cap, background send (`manage.py send_batch <id>`, spawned from the page) with stop,
  Message-ID on every email, logged on the timeline with the same key the Gmail sync uses.
  Attachments: media/outreach/ (copied from Prospecting; ~16 MB per email).
- **Weekly report** (Outreach -> Weekly report) replaces send_weekly_report.py: any
  week, same sections + positive replies, "Email this report" to Simon/Dave/David.
- **Fixed double counting:** 17,668 workbook campaign sends were also logged from Gmail
  Sent. `dedupe_campaign_sends` kept the Gmail copy (with campaign/outcome). Backup:
  `backups/db-2026-10-14-before-dedupe.sqlite3`. Queue went 711 -> 1,087 eligible.
- Old bugs not carried over: greeting "To whom it may concern,," (double comma),
  T006 name with a line break, T009 duplicated, Send_Queue formulas off by one row.
- Retired (leave in place, stop using): send_emails_v4.py, scan_responses.py,
  send_weekly_report.py, news_review/, and the workbook as a live system.
- **Security:** both scripts contain the Gmail app password in plain text; Kellen should
  rotate it and update .env.

## Built 2026-10-14 (later): pipeline, from the VP-of-BD review

- Sign-in page shows the white ACMI logo on a navy band.
- **Deal stages now match the Fortress tracker** (Prospect 10%, Qualified 25%, LOI/PDA negotiation 50%,
  LOI/PDA signed 75%, Lease negotiation 85% (not in the tracker; my assumption), Signed lease/DMA 100%,
  On hold 5%, Cold/lost 0%). `STAGE_PROBABILITY` in models.py; a deal's own "Win %" overrides it.
- New deal fields: site (for non-campus deals), deal type, asset type, priority, deal team, lead/source,
  target close, est. delivery, next step + due date, "on investor report", Trello link.
- **`import_pipeline`** (`crm/pipeline_import.py`): 106 deals from the tracker (G: drive, 2026.09.25) +
  the Trello Properties-Prospecting board (snapshot in `data/imports/`, gitignored). Tracker rows are on
  the investor report. Where tracker and Trello disagree, the further-along stage wins (11 differences,
  listed when the command runs). Owner = the deal-team member with the strongest email relationship.
  Trello "New Leads", "Cold" and admin lists were not imported. Users `paul` and `john` created (no
  password). 40 new companies; 16 of them picked up their email via `auto_resolve_queue`. Re-running
  updates the same deals. Backup: `backups/db-2026-10-14-before-pipeline.sqlite3`.
- Home: pipeline tiles (open, weighted, signed, by campus), My day (replies waiting on us, tasks,
  my deals without a next step, this week's meetings), pipeline by stage, deals that need attention,
  this week's stage moves, possible deals found in email. Outreach funnel collapsed at the bottom.
  Recent activity hides calendar RSVPs, bounces, out-of-office and company stage moves.
- Opportunities: board + table, Other/TBD and "Needs attention" filters, search, value-sorted columns,
  On hold/Cold collapsed. Suggestions from LOI/PDA/NDA/lease/proposal attachments (`crm/pipeline.py`):
  "Create deal" or "Not a deal".
- Deal page: next step, cost/weighted/$psf, stakeholders with roles (`OpportunityContact`), deal team,
  documents, company + deal timeline. Delete moved into a collapsed section (company page too).
- Documents (`Document`, `crm/documents.py`): paste a Google Drive link (default, nothing stored twice)
  or upload a file (local media for now). **Hosting TODO:** `store_upload` should push uploads to the
  shared drive (Drive API, `ACMI_DRIVE_FOLDER_ID`) and keep only the link.
- Investor report (`/reports/pipeline/`): tracker layout, print/PDF, Excel download (`crm/reports.py`,
  live formulas for $psf/weighted/totals). Replaces the Fortress tracker.
- Companies table: tick rows -> set owner (a person, or "whoever knows them best"); Stage/Priority
  filters are checkbox dropdowns; "Last reply" column replaces "Last response" by default; "Best deal" column.
- Timelines show only the new part of an email (no quoted thread, no signature).
- Meetings (`crm/meetings.py`, `import_meetings <json>`): Granola notes + Google Calendar events become
  Meeting activities on matched companies (past -> "Meeting held", future -> "Next meeting").
  Loaded once from Claude's Granola/Calendar connectors (65 Granola notes, last 30 days; calendar next
  30 days): 33 meetings matched. Granola summaries not pulled yet. Ongoing refresh needs a scheduled
  Claude task (not set up; Kellen to decide).
- 78 tests at this point.

## Built 2026-10-14 (evening): meetings calendar, task cards, comments, notifications, sidebar

- **Meetings calendar** (`/meetings/`, `Meeting` model, `crm/meetings.py`): month grid, Everyone / Mine /
  With companies. Click a meeting -> pop-up with Granola's action items ("Make task" turns one into a
  task, owner guessed from the notes), the full summary (safe markdown renderer) and attendees.
  Calendar events within 15 min of a Granola note with a similar title merge into it; solo blocks
  ("Gym") skipped. Loaded: 65 Granola notes (last 30 days, free plan limit) + calendar next 30 days =
  117 meetings, 60 with notes, 223 action items (`data/imports/granola_notes.json`, `meetings.json`).
  Re-import: `manage.py import_meetings data/imports/granola_notes.json data/imports/meetings.json`.
  Still manual: a daily scheduled Claude task should refresh those JSON files (not set up).
- **Task cards** (`/tasks/<id>/`): edit title/due/assignee/description, repeat (weekday, weekly,
  2 weeks, monthly, quarterly, optional end date; ticking done creates the next one), comments.
- **Comments** (`Comment`) on companies, deals and tasks; `@Name` mentions. Company page shows all
  comments on the company and its deals/tasks, plus its recent meetings.
- **Notifications** (`Notification`, `crm/notify.py`, bell at the top of the sidebar): task assigned to
  you, @mention, comment on something you own/are on, your deal moved stage, a company you own replied
  (mail from the last 3 days only), and a once-a-day due/overdue sweep (runs on first page view of the day).
  In-app only; an email digest could reuse the outreach Gmail sending later.
- **Sidebar**: collapsible tree (Dashboard; CRM > Companies > Possible duplicates, People,
  Opportunities > Investor report, Meetings, Tasks, Lists, Projects; Outreach > Send queue >
  Templates/Weekly report, News review, Mail review; Development > Land search, Contract negotiations
  (not built yet, no links); Settings > Admin). Open/closed remembered per browser.
- **Possible duplicates** page: one card per group with fixed column widths so every group lines up,
  stage pills, kept row highlighted, Keep disables that row's Merge box.
- 89 tests.

## Built 2026-10-14 (night): Google Calendar API

- Google Cloud project `sincere-passage-511621-j3`, OAuth web client (redirect
  `http://localhost:8000/accounts/google/login/callback/`). Keys in `.env` (GOOGLE_CLIENT_ID/SECRET), so
  **Google sign-in is now on** (local username login still works).
- Sign-in asks for `calendar.readonly` with offline access; allauth stores tokens
  (`SOCIALACCOUNT_STORE_TOKENS`). Each person clicks **Meetings -> Connect Google Calendar** once.
- `crm/gcal.py` + `manage.py sync_calendar`: refreshes the token, reads 90 days back / 60 ahead
  (`ACMI_CALENDAR_DAYS_BACK/AHEAD`), feeds `crm/meetings.import_meetings` (merges with Granola notes,
  company timelines), removes cancelled events. `CalendarSync` keeps last sync / error per person.
  Runs inside the existing 15-minute "ACMI CRM mail sync" task (`scripts/sync_mail.cmd`). "Sync now" on Meetings.
- Team emails set so Google sign-in links to existing accounts: simon=sshewmaker@, dave=ddenardo@,
  dowell=ddowell@, paul=pkadzielski@. **john and joe have no email yet**: set them before they sign in,
  or Google creates duplicate users.
- When hosted: add the Cloud Run URL's `/accounts/google/login/callback/` to the OAuth client.
- 94 tests.

## Dashboard (2026-10-14, late)

- Week calendar at the top of the Dashboard: Mon–Fri (weekend days only when something is on),
  Mine / Everyone, previous / next week, click a meeting for the same pop-up as the Meetings page
  (`crm/templates/crm/partials/meeting_dialog.html`, shared). "Meetings this week" list removed from My day.
- Recent activity on the Dashboard shows the last 7 days; "Show 7 more days" widens it (`?days=14`, up to 365).
- Dashboard lists (replies waiting, my deals, positive responses, deals needing attention, possible deals,
  pipeline changes, recent activity, tasks) show 10 and collapse the rest behind "Show N more" / "Show less"
  (`.clist[data-limit]`, script in base.html, works on any page).
- 96 tests.

## Built 2026-10-14 (night): Land search, QD site planning, sites, Projects by campus

**Data** (`crm/land/`; commands `load_land_layers`, `load_parcels <county...>`, `score_parcels [--remeasure]`):
- Texas: TxGIO StratMap 2026 release (published 2026-10-01), one zip per county, downloaded with a browser
  user agent (their CDN 403s scripts), parcels of 20+ ac kept, zip deleted. The state's live parcel service allows
  map images and point lookups (identify) but NOT searching, which is why searchable parcels are a local copy.
  Loaded (Kellen: test area around Kyle/San Marcos): Hays, Caldwell, Bastrop, Guadalupe, Comal, Blanco.
  `DATE_ACQ` in the file is the state's collection date, not a deed date: no "years held" signal for Texas.
- Maryland: Charles County's own Tax_Parcels service (74k parcels, 2,088 of 20+ ac), plus county zoning, water/sewer
  service categories, Chesapeake Bay Critical Area, Military Awareness Zone, Navy noise, Priority Funding Area,
  Technology Corridor and septic tiers stamped on each parcel (`crm/land/counties.py`). MD iMAP was down for
  maintenance; SDAT has no assessed values in the county layer (card links to the SDAT record).
- Stored layers (Texas Triangle + Southern MD boxes): military bases, 100 kV+ transmission, substations, gas
  pipelines, rail, NHS highways, city/town limits. Distances measured in EPSG:5070.
- Counties grouped State -> Region -> County (`LandCounty.region`, `sources.COUNTY_REGIONS`).
- Refresh: Texas yearly when TxGIO publishes (re-run `load_parcels`; team status/notes/QD plans are kept).
  Charles: any time (live county service). Nothing else needs refreshing (utilities etc. are live).

**Screen** (`crm/land/score.py`): 0-100 from the defense-site buy-box: size (net usable once checked), zoning bucket
or tax-roll land use, power (line + substation distance), access (highway, rail), defense driver (base distance,
HUBZone, Navy zone), water/sewer, owner signals. Deal-killers cap the score at 25: 30%+ floodplain, 10%+ floodway,
20%+ wetlands, Superfund within 0.5 mi, tax-exempt owner, 30%+ Critical Area RCA.
- Zoning buckets (`ZONING_CLASSES`): industrial, business park/employment, commercial, mixed use, PUD, ag/rural,
  residential, conservation, public, town (not mapped), unzoned (unincorporated TX). Charles codes mapped by code
  (`ZONING_CODES`; the county files BP/PEP under "Mixed use"). Add new counties' codes there.
- Wetlands count streams, ponds and freshwater wetlands only; lakes, bays and estuary left out (Kellen's call).

**Live checks per parcel** (`crm/land/lookup.py`, run when a card opens, ~2 s): FEMA flood, NWI wetlands, water and
sewer CCN + mapped lines (PUC Texas; TX only), electric provider (PUC Texas in TX, HIFLD national elsewhere),
SBA HUBZone, EPA cleanup sites within 1 mi. Net usable acres = gross minus floodplain and wetlands.

**Map** (`/land/`, Leaflet + esri-leaflet from unpkg): satellite/streets/topo; filters (where, acres, score, $/ac,
zoning, land use, distance to power/highway/rail, outside city limits, out-of-state owner, hide deal-killers,
status); list; CSV export; search (our parcels, lat/lon, addresses via Esri geocoder); live layers (all parcel lines,
power, substations, electric territories, gas, water/sewer areas and lines, flood, wetlands, zoning, Critical Area,
rail, bases, Navy zones, city limits); click ANY parcel for the live record ("Add to land search" saves small ones).
- Card: owner + holdings, property, zoning, buy-box reasons, utilities & site, county layers, "still to confirm",
  notes, Watch / Pursue / Pass. **Pursue** makes the owner a Company (category Landowner) + a note + a task.
- Tools: distance (mi/ft/yd/km/m), area (acres/sq ft/sq mi/ha/sq m), drive time A <-> B both ways, drive-time map
  (bands, from or to a point). Routing: public Valhalla server (OpenStreetMap, free fair-use, 4 bands per request
  so more are batched; free-flow times, no traffic) via `/land/drive/...` (`crm/land/drive.py`).
- **Assemblages**: Assemble button or Ctrl/Shift-click parcels -> totals bar (acres, owners, contiguous, value) ->
  "Save as site" (`LandSite`: name, campus, project, status, notes, parcels, QD plan). "Saved sites" picker.
- **QD site plan** (bottom drawer, drag to resize; `crm/land/qd.py`): port of "QD CALCS v20 - ACMI Site Screen.xlsx"
  (DoD 6055.09-M V3.E3). Buildings with SF footprint, type (magazine = IMD spacing, operating = ILD), HD, NEWQD,
  PES, barricade, MCE, K. Arcs measured from the footprint edge. Auto-fit places buildings so IBD arcs stay on the
  parcel/assemblage and spacing holds; reports encumbered / off-site / free acres. Saved on the parcel or site.
  - Checked against "Site Plan 2026.09.30" (4250 Middletown Rd, Pomfret): 300 / 1,250 / 419 ft all match. The 419
    needed the HFD **Open** column (Table V3.E3.T2); the site-screen tab's "Aboveground or Open" row uses the
    Structure column (200 ft at 5 lb). PES now has both "Open (no structure)" and "Aboveground structure".
  - On the 139-ac parcel alone ERG A's arc spills ~18 ac off-site (deepest point is 959 ft from the line); the plan's
    197.28 ac includes parcels under 20 ac (click them on the map to add).
  - Screening only: 360-degree arcs (ECM arcs are really directional), not a DDESB submission.

**Projects page** now Campus -> projects in it -> prospective land sites; "New campus" also creates its campus project
for email tagging; a project with a campus is hung off that campus project automatically.

- REA (Kellen's `npx rea-agents setup`) is a reverse-engineering tool; deliberately NOT used on LandGlide (licensed
  LightBox data). Same rule as Attio: copy behaviour, not code.
- Kellen's own dev server on :8000 was serving stale Python (didn't reload models.py): restart it.
- 111 tests (`crm/tests/test_land.py` added).

## Built 2026-10-14 (late night): Land search UI rework (Kellen didn't like the floating layout)

- Layout is now docked, nothing floats over the map: a **toolbar across the top** of the page (title, Filters panel
  toggle, search, base map, Layers, Saved sites, Assemble, Measure, Drive time, QD plan, Locate | Views, Tools);
  below it the filters/results panel, the map, the **parcel card as its own column** on the right (scrolls, never cut
  off), and the **QD drawer docked under the map** (drag to resize; the map shrinks, so the imagery credit stays on the
  map). A ResizeObserver keeps Leaflet sized. On narrow screens opening a card folds the filters panel away.
- **Tools menu (⚙ Tools)**: switch any toolbar tool, the filters panel or the legend on/off.
- **Views menu**: save what you see (map position, base map, layers, filters, tools shown, panels open, QD drawer
  height) as a named view; share with the team; one per person can be the default, which opens on a plain /land/
  visit. Without a default the page reopens where you left it (localStorage). Links with filters still win.
  Stored as `SavedView(object_type="land")` with the state JSON in `query` + new `is_default`; endpoints
  `/land/views.json`, `/land/views/save/`, `/land/views/<id>/delete/` (owner-only changes). Lists form hides "land".
- 114 tests.

## Backlog

1. AI summaries: need an Anthropic API key in .env (console.anthropic.com).
2. Never sent a real batch from the CRM yet: first run = test send, then a small batch.
3. Land search next: more Texas Triangle counties (`load_parcels <names>`, ~1 min each for rural ones); zoning
   maps for Texas cities and other MD counties; directional ECM arcs; QD building rotation and custom footprints;
   hosted version needs the parcel table in Postgres (PostGIS optional) and the Valhalla server choice revisited
   (self-host or a paid router for heavy use).
4. **After the CRM is final:** separate project-management tool for contract
   negotiations (not part of the CRM). Kellen explicitly deferred it.

## Open items

- Attio trial is syncing Kellen's mailbox and calendar. He may want it turned off.
- Confirm what "the project" Kellen created is, and get its ID or URL.
