# ACMI CRM: Handoff (as of 2026-11-04)

Read this first when picking the project up in a new chat, along with
`attio_spec.md` in this folder. Start with "Pick up here"; the dated sections after it are the history.

**Update, 2026-11-04 (late; side session started from the county-adding session; Kellen: "I want to be able to put in a warehouse,
magazine, and other hazard buildings that can all be autofited based on the site constraints and zoning etc."): one Auto-fit planner
for flex-industrial AND hazard buildings is built and tested on the branch `claude/nice-thompson-1a5b68`, NOT committed, NOT deployed.**
Before, `qd.py` (QD) and `siteplan.py` (zoning layout) were separate tools that did not know each other.
- **Engine: `crm/land/autofit.py`, `plan()`.** One program list: flex types (`warehouse`, `manufacturing`, `flex`, `office`, `lab`) and
  hazard types (`magazine` = IMD spacing, `operating` = ILD) with HD, NEWQD, PES, barricade, MCE, K, footprint sf. Order: (1) hazard
  buildings first, inside the zoning envelope (setback, flood / wetland exclusion, via `siteplan.envelope`), each at a spot whose IBD arc
  stays on the property (largest IBD first, `qd.footprint`, spacing to the other hazard buildings); (2) each flex line then runs
  `siteplan.layout` with the hazard arcs, earlier buildings and courts carved out. The carve-out distance is the line's **Exposed at**:
  default IBD (an unrelated inhabited building), `lab` defaults to ILD, or PTRD / ILD / none per line. Hazard buildings count toward
  FAR / coverage / ISR (`layout(start=...)`, new) and the zone's open space is checked as the pervious share; (3) every hazard building
  gets **Max NEWQD given the final layout** (IBD to the property line, spacing to the other hazard buildings, and the distance each flex
  building needs from it; `qd.max_newqd` now takes `(name, ft, key)` gaps). Zone height / stories cap each line (notes say so).
- **What limited each building** is reported per line: `zoning` (FAR / coverage / ISR from `layout`'s new `stops`, or setback), `qd`
  (arc runs N ac off the property, spacing, or the arcs took X ac of the envelope), `exclusion` (flood / wetland: deepest spot with vs
  without it). "Cut NEWQD to fit" lowers a hazard building's NEWQD to what its spot allows when the arc won't fit.
- **UI:** an **Auto-fit** button in the land toolbar opens a drawer (same family as QD plan / Site plan; one drawer at a time): zone
  picker (rezone scenarios), setback, program table with hazard columns, results (verdict, hazard table with Max NEWQD, flex table,
  limits, acres lost to setback / exclusion / arcs), arcs and footprints on the map. Endpoint `POST /land/autofit/`
  (`views.auto_fit`); "Save program" keeps it on the site / parcel in `site_plan["autofit"]` (a normal Site plan save keeps it; no
  migration). `site_plan` now shares `_std_with_overrides` and `_flood_wetland_exclusion` with it. The old QD plan and Site plan
  drawers are unchanged and still work.
- **New zoning standards in `siteplan.STANDARDS`** (from `crm/land/zoning_ref/`, same sources, "check source" caveats kept, no numbers
  invented; a field the file leaves null keeps the generic 50 / 25 / 25 ft, 45 ft / 2 story default and is listed in `generic`, which
  the drawer shows): **Miami-Dade 12086** AU (also Homestead's AU), GU (all generic: the article has no numbers), IU-1 / IU-2 / IU-3
  (front 20, side 15 and rear 20 as the conservative end of the 33-51 ranges, open space 10%, no height), BU-2 (FAR / coverage 0.40),
  Homestead I-1 / I-2 (front 20, side 10, **rear generic**, 70 ft / 6 stories, 90% impervious as ISR), PUD (setbacks generic, 150 ft
  cap, 35% open space). **Emery 49015** Cleveland A-1 / R-1 / C-1 / I-1 (I-1: 25 ft all round, 30 ft / 2 stories) and Green River
  I-1 / I-2 / I-3 (25 ft from the easement line, no height) coded **`GR I-1`..`GR I-3`** because Cleveland's I-1 shares the zoning
  layer's plain "I-1": a Green River parcel opens as Cleveland's I-1 until you pick the GR zone in the drawer (the layer doesn't say
  which town; a better fix would carry the authority into `_dominant_zone`). Not added: Florida City, the other Utah towns, Miami-Dade
  residential / business districts other than BU-2. `BUILDING_TYPES` gained a `height` (clear height, ft) for the zone-height check.
- **Limits to know:** screening only, never a DDESB submission (wording kept in the drawer and the API); 360-degree arcs; hazard
  buildings get no access road or apron; the 15 ft margin `siteplan.envelope` puts around exclusions also pads the arcs; a flex line
  with no count fills what is left (the default program sets counts); flex lines are laid out one after another in program order, so
  the first greedy line takes the room.
- **Tests:** `AutoFitTests` in `crm/tests/test_land.py` (standards, `layout(start)` / stops, big site clears every arc, hazard sf
  counts toward FAR, small site reports the arc limit and "cut to fit" repairs it, exclusion, zone height cap, endpoint + save);
  97 land tests pass. Checked in the browser on a scratch database (`ACMI_SQLITE_PATH` in the scratchpad): the real Miami-Dade flood
  layer answered, the program fitted and the limits read correctly. Not deployed: no hosted-service, migration or `db.sqlite3` change.

**Update, 2026-11-04 (evening; Claude on the laptop; Kellen: "Can we add this LOI/PSA trackline capability? It should be used to
align on proposed closing timelines and deposits etc.", with the Lewis Place "LOI Tracker" workbook, then "Merge"): the LOI / PSA
terms tracker is built, tested and merged into `main` (commit `c9edb51`, fast-forwarded after CI on Postgres went green), NOT
deployed.** Branch `loi-tracker` (worktree `.claude/worktrees/loi-tracker`), cut from `6d597b7` and rebased onto `origin/main`
`4eb5f5e`, then `main` (`fc7b0ec`) merged in. Per the entry below, live is `acmi-crm-00032-hlr` (`5ed007f`, photos / phones); `main` is
ahead of it by the effective-date recalculation (`b705c40`, no migration) and this tracker, **neither deployed**: check the live
image before any deploy. One new table, **migration `0079_termsround`** (it was `0078` until
`0078_comment_photo` reached `main` first; it now depends on that one): a plain CREATE TABLE, no change to existing tables, so
older code can run against it.
- **What Kellen gets.** A **Terms** page on every contract (`/contracts/<id>/terms/`): a button and a panel on the contract page,
  and "Terms (n)" beside the contract on the land site page. A *round* is one proposal (ACMI initial offer, Seller redline, ACMI
  counter, ...). It holds the terms the workbook held: per phase the acres and price per acre, the feasibility period and its
  extensions (days, options available, options used, deposit per extension), the initial deposit and its non-refundable part,
  the deposit after feasibility and its non-refundable part, 30 / 60 / 90-day deposits, days to close counted from final zoning
  approval or from the end of feasibility, closing extensions (days, options, used, deposit, non-refundable part); and for the
  deal the days to PSA signing (or a fixed date), months to final zoning approval, outside date (months) and subdivision.
  The page shows, side by side (up to four rounds, chips pick which): a card per round (price, deposits, what is lost if ACMI
  walks away when feasibility ends, PSA / feasibility / closing dates, cash to close); **where ACMI and the seller stand** (the
  latest round from each side, the gap = seller less ACMI, "Aligned" where equal); the timeline lanes (the contract timeline's
  partial, ticks thinned on long spans); **the schedule** (each event's date, the deposit and its non-refundable part, the
  cumulative "walk away here: lose $X, get back $Y", cash to close, extension rows say where the date moves to); the inputs
  grid with differing rows tinted; print / save as PDF (logo prints; the on-screen logo is hidden). Adding or editing a round is
  one vertical form with a **live preview** (the server does the arithmetic: `terms.js` posts the form to `.../terms/preview/`
  as you type). A new round starts from the previous round's terms and is named for the other side. **Mark agreed** (one per
  contract); once the contract is signed with an effective date the agreed round counts from the real date, labelled "PSA
  signed". **Import** a team LOI Tracker workbook (.xlsx): one round per column under "LOI INFO", rows read by their wording.
- **Where the code is.** Engine `crm/contracts/terms.py` (pure functions; `ROUND_FIELDS` / `PHASE_FIELDS` drive the form, the
  stored JSON, both grids and the importer, so adding a term is one line there), `terms_views.py`, `terms_import.py`,
  `forms.py` (`TermsRoundForm`, `TermsImportForm`), model `TermsRound` in `crm/models.py`, templates `contract_terms.html`,
  `contract_terms_edit.html`, `partials/terms_preview.html`, `partials/terms_field.html`, the `lt-*` CSS at the end of
  `app.css`, `static/crm/terms.js`; hooks in `contracts/views.py` (`terms_panel`), `land/views.py` (`_site_contracts`),
  `contract_detail.html`, `land_site_detail.html`, `urls.py`. Tests: `test_terms.py` (engine), `test_terms_views.py` (pages,
  form, permissions, import).
- **How it was checked.** The engine reproduces the Lewis Place workbook to the dollar and the day for all three rounds (PSA,
  feasibility end, zoning approval, closing, outside date, deposits, the refundable / non-refundable aggregates, cash to close);
  the expected values in the tests are the workbook's own cached values. The importer read the real workbook
  (`G:\Shared drives\1 ACMI Properties\1 Projects\MD\MEIH\01. Land Acquisition\Lewis Place\LOI Tracker - MD - Lewis Place.xlsx`)
  with nothing skipped, and the imported rounds gave the same numbers. In the browser pane on a scratch database (never the
  real one): the tracker with those three rounds, the edit form and its live preview recalculating, saving a round, validation
  errors, the empty state, the contract page, light and dark, and a 375 px phone with no sideways scroll. **Tests:** the whole
  suite on SQLite before the rebase (1,272 tests, 8 minutes) was green, 1 skipped; the tracker, contract, land, diligence,
  static-integrity and phone-layout modules (239 tests) again after four small cleanups; `makemigrations --check` clean, also
  after the rebase and the renumbering. The combination with `4eb5f5e` (comment photos, effective-date recalculation) was
  checked by CI on Postgres before `main` moved. **Not checked:** a real phone, printing to a real PDF (the print CSS is small
  and was read, not rendered), anyone but one test user on a scratch copy.
- **Where it differs from the workbook, on purpose.** (1) A deposit for an extension is dated the day the extension has to be
  exercised (the end of the period it extends), not the extended date; amounts, the aggregates, closing date and cash to close
  are identical, only the dates of the two closing-extension deposits move (Apr 30 and Jun 29, 2028, not Jun 29 and Aug 28).
  (2) Feasibility extension deposits are one row per extension (the workbook adds them up on the first date). (3) The
  workbook's "Deposits Check" is true by construction here, so the page warns instead: deposits over the price, an extended
  closing past the outside date (closing is held at the outside date), more extensions used than available. (4) The labels'
  spelling slips are fixed. (5) Phase II and later start `start_days` after the previous phase closes and close after their own
  feasibility, like the workbook's blocks; the workbook's Phase II / III aggregate formulas pointed at the wrong rows (they were
  zero, so nobody saw it).
- **Not built, ask Kellen.** Pushing the agreed round's dates into Critical dates (reminders, calendar, site timeline): left out
  on purpose, since a signed PSA's dates are read from the document and two sources would duplicate. Claude reading the terms
  out of an LOI draft. A view across all deals. An Excel export (print to PDF is there). Whether he wants extension deposits
  dated the workbook's way.
- **To ship.** Kellen's go-ahead right before, then the usual deploy steps (read the deploy note in memory first). The migrate
  job will apply `0079_termsround` (one new table; 00032 already applied `0078_comment_photo`). It only adds, so rolling the
  image back is safe. Shipping `main` ships the effective-date recalculation too. **Merge note:** the uncommitted land work
  in the main folder (branch `task-groups`) edits `crm/models.py`; if it adds a migration it has to be numbered after `0079`
  and depend on it.

**Update, 2026-11-04 (evening; Claude on the laptop; Kellen: "I thought I added the ability to upload photos to comments on
company cards? also within the company cards, people needs to add phone numbers if we have them", then "deploy"): both are
built, checked and DEPLOYED: live is revision `acmi-crm-00032-hlr`, image `5ed007f`, since 4:21 pm Central.** Branch
`card-photos-phones` (worktree `.claude/worktrees/card-photos-phones`, cut from `origin/main` `6d597b7`), commit `5ed007f`, CI
green on it (run 41921163980). **`main` now holds `5ed007f` AND another session's critical-dates fix (`b705c40`, the next
entry), which reached `main` while I waited for CI. I deployed only `5ed007f` (that fix was not part of what Kellen asked for
here), so live has the photos and the phone numbers and NOT the effective-date recalculation. The next entry was written when
live was 00031 / `a3048b0`; its fix is still not deployed.**
- **The deploy (4:16-4:22 pm Central).** `main` moved under me (that other session's merge), so I merged `main` into the branch
  (`4eb5f5e`; only this file conflicted, both entries kept), got CI green on the merge (run 41922613914), fast-forwarded `main`
  to it, then built exactly `5ed007f` from a clean `git archive` with `cloudbuild.yaml` (`_TAG=5ed007f`; before building, the
  file's settings matched the live env for all four resources). Cloud Build `30ee45b8-afb9-421c-aaba-da45acae9ca8`, 5 min 38 s;
  a manual Cloud SQL backup first (id `1793829914222`, "before card-photos-phones 5ed007f"). After: the migrate job ran
  `crm.0078_comment_photo... OK`; the service and the three jobs have **identical env / command / args / resources / Cloud SQL
  / account** before and after (only the image changed); no WARNING and no 5xx on the new revision; the login page is 200,
  `/companies/1/` and `/comments/1/photo/` answer 302 to sign-in, and the served `app.css` carries the new rules. **Rollback:**
  `gcloud run services update-traffic acmi-crm --to-revisions=acmi-crm-00031-ptp=100 --region us-central1 --project
  sincere-passage-511621-j3` (safe on the schema: the new column is nullable and old code ignores it). **Not checked on the live
  copy:** anything behind sign-in (Claude cannot sign in as Kellen): attaching a real photo (its first upload goes through the
  Cloud Storage bucket, the path documents use) and the people's numbers on the company card. Ask Kellen to try both.
- **Why the photos were not there.** The upload had been built on 2026-11-03 as commit `7cf25af` on `origin/photo-comments`
  (cut from `960e79c`, 67 commits behind `main`) and never merged, and no note here mentioned it. It could not have shipped as
  it stood: its migration `0077_comment_photo` and main's `0077_deal_lease_roc` both depend on `0076`, so the migrations would
  have two leaf nodes (`makemigrations --check` in CI fails and `migrate` refuses); it linked the file with `photo.url`, but
  production runs DEBUG off (Django serves no media) with a private Cloud Storage bucket, which is why documents go through
  an app view; and it never checked what was uploaded. That branch is left alone (not merged, not deleted); this one redoes
  the work.
- **Photos.** `Comment.photo` (a nullable `ImageField`, so code from before it can still insert comments after a rollback;
  migration `0078_comment_photo`, one nullable column, no backfill). `crm/comment_photos.py` checks the upload is a real
  JPG / PNG / GIF / WebP (the type the browser claims means nothing), at most 15 MB and 64 megapixels, stores it under a name
  of its own, and scales anything over 2400 px down once (camera rotation baked in; animated GIFs untouched).
  `views.comment_add` takes the optional `photo`; a bad file posts nothing and says why (the typed text is not kept).
  `views.comment_photo` (`comments/<pk>/photo/`) streams the file to signed-in users, as `document_file` does, with a private
  one-day cache; `comment_delete` removes the file too. "Add a photo" is on both comment forms (the company page's Activity
  box and the comments panel on deals, tasks and sites); the picture shows in the feed, the panels and "Latest comments" on
  the **company and deal cards** (`partials/comment_photo.html`, `comment_attach.html`). One photo per comment; the text is
  still required. Not supported: iPhone HEIC files sent from a computer (a phone's own picker converts them to JPG).
- **Phone numbers.** `phone_numbers` filter (`crm_tags.py`) and `partials/person_phones.html`: each saved number shows as a
  tap-to-call `tel:` link with its office / mobile label beside it (`tel:` carries no label or extension; a saved value with no
  digits, like "call the main line", shows as plain text). On the **company card** the People rows keep opening the person
  (the name link is stretched over the row) while the numbers stay their own links; also a **Phone column on the company page's
  People tab** and the numbers under each name in the Overview's "Who we know" widget. Nobody without a number gets an empty line.
- **Checked.** `test_comment_photos.py` (16 tests: upload, serving, sign-in, delete, bad / huge / cut-off / SVG files,
  rotation, GIF, the filter, the card, tab and widget); the whole suite 1203 OK (1 skipped) before a CSS-only tweak, and the 62
  related tests again after it; `makemigrations --check` clean. In the browser pane on a scratch copy (own database, fake
  data): a real upload through the form (3200 x 2400 stored as 2400 x 1800 and shown), a refused text file, the card popup,
  the People tab, the deal page, dark mode and a phone width (no sideways scroll; the photo control drops under the button).
  **Not checked:** the real Cloud Storage bucket (the same `open` / `FileResponse` path documents use), a real phone camera.
- **Possible follow-ups, none asked:** several photos per comment (a second model), HEIC (needs `pillow-heif` in the image),
  editing a comment, the numbers on the deal card's People list. The person card (`cards/person.html`) and
  `person_details.html` still put the whole saved text, label included, in their `tel:` link; the new filter fixes that in
  two lines if wanted.

**Update, 2026-11-04 (evening; Claude on the laptop; Kellen: "I uploaded a contract then updated the effective date but the
dates that Claude calculated didn't update. Was this fix deployed?"): the effective-date recalculation is fixed, tested and
merged into `main` (`b705c40`, fast-forwarded after CI went green), NOT deployed. Live is still `acmi-crm-00031-ptp`, image `a3048b0`.** The fix was commit `b363621`
(2026-11-03, branch `contract-dates-recompute`, one commit). It was never merged or deployed, and nothing in this file
mentioned it, so it sat on its branch for a day. **It would not have worked if deployed:** `_post` in
`crm/contracts/views.py` read `old_effective` after `form.is_valid()`, and `is_valid()` already copies the posted values
onto the contract, so "the effective date changed" was never true. A test that posts a new effective date through the real
page failed on the original (feasibility stayed May 1) and passed once the old date was read before the form is built.
- **What it does now:** saving a contract's details with a new effective date calls
  `timeline.recompute_dates(contract, old_effective, user)` (in `crm/contracts/timeline.py`; before `dates.sync_site`, so
  the land site's timeline gets the new dates). It moves every stored critical date that has a rule (anchor + offset),
  including Claude's suggestions nobody has kept and dates that were blank waiting for the effective date; chains follow
  (closing = feasibility + 30). Fixed dates stay. A date that is done, or that a person moved (a push or an exercised
  extension: a history entry not marked `auto`), stays where it is, and the dates that count from it work from where it
  stands. A suggestion that shares a key with a kept date cannot move the kept date. Each move is in the date's history
  (the "Moved N×" fold on the contract page) with the reason, marked `auto` so the next change of the effective date still
  moves it. "Saved." says how many were recalculated and how many stayed. No model change, no migration.
- **Not changed:** a date a person edited by hand that still carries a rule follows the next effective-date change (clear
  its rule to pin it); old hand-added dates with no parsed rule (Trello imports) do not move.
- **Tests:** `crm/tests/test_contract_recalc.py` (9 tests: the move and the chain, a second change, same-date save leaves
  hand-set dates, blank dates fill in, done / pushed dates, key clash, site timeline, no effective date, idempotence).
  Against `b363621` alone 6 of the 9 fail (the page test among them); with the fix all 9 pass. Full suite on the laptop:
  1196 tests OK (1 skipped, 470 s). CI on Postgres green on PR 21 (https://github.com/kmoczulski/acmi-crm/pull/21, runs
  41920844132 push and 41920890112 pull_request, head `b705c40`). Clicked through on a scratch copy (a contract with a done
  date, a pushed date, a fixed date and one unkept Claude suggestion; effective date Mar 2 to Apr 1): the Saved message, the
  new dates and the "Moved 1×" reason all showed. Not tried against real Google or the live data.
- **To ship:** Kellen's go-ahead right before, then the usual deploy steps (live image and ancestor check, fingerprint
  before / after, migrate job finds nothing, curl, logs). Rollback target `acmi-crm-00031-ptp`. Local `main` (checked out in
  `.claude/worktrees/mail-booking`) is behind `origin/main`: do not deploy from it.
- **Branches:** PR 21 (`contract-dates-fix`, merged by fast-forward) holds the work. `contract-dates-recompute` (the original
  commit) is an ancestor of `main` through merge `6cc28ce` and can be deleted. The worktree
  `.claude/worktrees/contract-dates-fix` can go once this is deployed.

**Update, 2026-11-04 (afternoon, after 00030; Claude on the laptop; Kellen: "ok deploy", then "Yes, task groups"): task groups
(PR 20, the Tasks page grouped by deal / campus, land site, prospecting, general) is merged into `main` and DEPLOYED: live is
revision `acmi-crm-00031-ptp`, image `a3048b0` (= `main` `a3048b0`).** PR 20 was another session's work: 4 files (`views.py`
`task_list`, `task_list.html`, a few lines of `app.css`, `test_task_groups.py`), no model or migration change, CI green on its
own head `434a3b2`. **Its branch did not contain the tabs and top bar fixes (live 00030 was not an ancestor of it), so a deploy
of the PR head as it stood would have dropped them**: I merged `main` and the PR on a fresh branch (`deploy-task-groups`, merge
`a3048b0`), CI green on Postgres (run 41909238297), fast-forwarded `main`, and deployed from a clean export. After: the
migrate job found nothing to apply; the service and the three jobs fingerprint **identical** to before (the News folder
default in `cloudbuild.yaml` did its job), only the image changed; no WARNING or 5xx logs; the served CSS has the task-group
rule and the solid top bar. Rollback: traffic to `acmi-crm-00030-hb8` (`902944a`). **Not checked on the live copy:** the Tasks
page itself (behind sign-in). **Leave alone:** the detached worktree `.claude/worktrees/task-groups-deploy` (at `434a3b2`, a
checkout prepared for deploying the PR head without the fixes: do not deploy from it) and the uncommitted land-layers edits in
the main folder (on branch `task-groups`, not part of any PR; not touched).

**Update, 2026-11-04 (afternoon, after 00028; Claude on the laptop; Kellen: "merge and deploy"): the company-tabs fix and the
solid top bar are merged into `main` and DEPLOYED: live is revision `acmi-crm-00030-hb8`, image `902944a` (= `main`
`902944a`, with a config-only commit on `main` after it).** Branch `ux-fixes-2026-11-04` (`f3b9c7b` tabs, `e35f344` top bar)
plus `main` at `c8e3208`; CI green on Postgres (run 41904995749, 6 min); `main` fast-forwarded to `902944a`; no migrations;
no backup (nothing to migrate); the migrate job found nothing to apply; the new CSS and `nav.js` are served; no WARNING logs.
Rollback: traffic to `acmi-crm-00028-mzv` (`97be770`, has everything but these two fixes).
- **A slip, put right in about six minutes (revision 00029 → 00030).** I built from `main`'s `cloudbuild.yaml`, whose
  `_NEWS_FOLDER` default was blank, so revision 00029 had `ACMI_NEWS_DRIVE_FOLDER` empty and the News page lost its Drive
  folder. The before / after fingerprint caught it (the service's hash changed, the three jobs' did not); I read the value
  from revision 00028 and set it back with `gcloud run services update --update-env-vars` (00030), and the fingerprint is
  identical to before my deploy again. The entry below says the default lived "on the laptop branch only"; I had not read it.
  **Kept on `main`: `_NEWS_FOLDER` now defaults to the folder id** (`cloudbuild.yaml`, one line), so a plain pipeline run keeps it.
- **Rule for the next deploy:** the pipeline file's defaults are not always what is live. Before building, compare the live
  service's env with what the file would set, and read the top of this file for deploy notes from other sessions; after
  building, the fingerprint (env, command, args, resources, Cloud SQL, account) must be identical except the image. If it is not,
  find the variable by comparing the two revisions' env (names and value hashes, not values) before anything else.
- Still open from this session: the proposed-task edit / delete (design below, not built), and the questions to Kellen below.

**Update, 2026-11-04 (night, later): merged into `main` and DEPLOYED: live is revision `acmi-crm-00028-mzv`, image `97be770` (= `main` locally, NOT pushed to origin yet).** Main now holds the mail Times panel work, the Schedule page colours (one colour per person, busy cells split into stripes), and the Granola .txt branch (`393ee4f`, which was live as 00027 but never in `main`). Before: 00027 / 393ee4f. Rollback target: `acmi-crm-00027-8dw`. Full suite 1186 tests OK; manual Cloud SQL backup taken first; env/command fingerprints of the service and 3 jobs identical before and after; no migrations; no WARNING logs after.

**Update, 2026-11-04 (night): mail Times panel, branch `mail-booking-availability` (from `main`, not merged, not deployed, no migration).** The inline reply box never got the availability / people / create URLs (`mail_reader.html`), so nobody else's calendar loaded there; fixed. The panel now draws every person's busy time in their own colour, splits overlapping meetings into lanes (`compose.js`), lists ACMI teammates as toggles (`schedule/people/` also returns `team`), adds everyone on To / Cc automatically (and as they are typed), and has an Optional toggle per guest (sent to Google as `optional`). `slots.availability` returns each person's merged `busy` blocks. Others' blocks say only "Busy" (free/busy has no titles). Checked in the browser pane on a scratch DB with fake Google (compose page and inline reply); not checked against real Google. The Schedule page does not have the colours / lanes yet.

**Update, 2026-11-04 later: live is revision `acmi-crm-00025-2rh`, image `94f65da` (= `main`), not `00022`.** Changes since: the
News page reads the Drive folder `1Oz5t51WMcGwFBU2FCM1PEwgKA0EsYgGZ` (`ACMI_NEWS_DRIVE_FOLDER`, now also the default of
`_NEWS_FOLDER` in `cloudbuild.yaml` on the laptop branch only, pass it with `--substitutions` otherwise); the folder must
be shared as Viewer with the runtime service account `46824423159-compute@developer.gserviceaccount.com` (Mel is adding
it to the "1 ACMI Properties" shared drive). New: **Upload a scan** on the News page (`/news/upload/`) for when Drive
isn't reachable. Fixed: a scan whose second line was a "Prepared for ..." sentence broke the import (scan date now comes
from the title; every imported text value is cut to its field limit). Kellen runs the Google Cloud project himself.
Open: `/chat/poll/` returns 502 in the logs (not looked at). The `ux-fixes` and other branches below are still not deployed.

**[Update: the two fixes below shipped in revision 00030, see the first entry above; the proposed-task edit / delete is
still NOT built.]** **Pick up here, 2026-11-04 after the deploy (Claude on the laptop; Kellen asked for this handoff so work can resume later).
Live was then revision `acmi-crm-00022-fbx`, image `a799272` (the entry below is that deploy). Since then Kellen reported
three things from using the live app: two are fixed on a branch and NOT deployed, one is not built.** Resume in this
order: read CI on the branch, ship the two fixes (Kellen's OK to deploy), build the proposed-task edit / delete from the
design below, then one more deploy.
- **1. The company page tabs showed the same popup** (Kellen: "within a tenant company page - clicking overview through
  deals just displays the same popup"). **Fixed, not deployed:** branch `ux-fixes-2026-11-04` (worktree
  `.claude/worktrees/ux-fixes`, from `main` `79eb8b1`, pushed), commit `f3b9c7b`. The record-card handler in `nav.js` sent
  every click on a link to a company, deal or person page to the card popup, including the page's own tabs
  (`?tab=meetings`), paging and filters, so Overview, Meetings, Action items, Activity, People and Deals all showed the
  company card. A link to the page you are already on is plain navigation now; links to other records still open their
  card, and links inside a card still chain. The cause is old: the handler has been that way since the cards arrived
  (2026-10-27) and is unchanged from what production ran before today, so it was live before the merge too. Checked on a
  scratch copy: the Meetings and Deals tabs switch, a deal link still opens its card.
- **2. "The top bar selection is transparent. I dont like that."** **Fixed, not deployed:** same branch, commit `e35f344`.
  Two things on the bar were see-through: the selected item (a 16% white overlay) and the menus (frosted glass at 82%, so
  the page showed through the Calendar dropdown). The selected item is now a solid blue pill (`--nav-active`), hover and
  open items a darker solid (`--nav-hover`), and the nav dropdowns, + New and the profile menu are opaque (`--panel`, no
  blur); dark mode has its own values. Checked on a scratch copy in light and dark. Kellen did not say which of the two
  was meant, so both are done. **Still translucent on purpose; ask whether they should be solid too:** the Ctrl K
  palette, the home page's search results (`.hero-results`), the mail pop-overs (`.mail-adv-pop`, `.addr-suggest`), the
  map controls and legend.
- **3. "I want the ability to edit a proposed task or delete one." NOT BUILT; the design is here.** What "proposed task"
  means is my reading and **not confirmed with Kellen**: a **meeting action item that has no task yet**. The Task model has
  no proposed state. Ask Kellen one line first: other things in the app are "proposed" too (AI-proposed critical dates on a
  contract, `ct-proposed` forms with Confirm; an entitlement's proposed conditions; a build team member with status
  Proposed; the Needs filing cards).
  - **Where they live:** `Meeting.action_items`, a JSON list of `{title, owner, detail, task_id}` read from the notes'
    "Next Steps / Action Items" section (`meetings.action_items()`). An item without `task_id` shows a grey dot and a
    **Make task** form (assignee, date, time). Drawn by `crm/templates/crm/partials/action_item.html`, which the company
    page's Action items tab (`company_detail.html`, `partials/company_actions.html`), the Overview widget and the meeting
    cards (`partials/company_meeting_card.html`, compact mode) and the meeting pop-up (`partials/meeting_panel.html`) include.
  - **The trap:** a Granola refresh rebuilds the whole list from the notes and keeps only `task_id`, matched by title
    (`meetings._meeting_for`, `kept = {...}` around line 399). A plain edit would be undone and a deleted item would come back.
  - **Design (no migration; a NOT NULL column would again break old code on a rollback, see item 3 under "To do next"
    below):** keep the list order, because the index is the item's identity in the forms. Add per-item keys: `key` (the
    title as the notes wrote it), `edited` (title, owner and detail overrides) and `deleted` (a tombstone: hidden
    everywhere, skipped by "Make tasks for all"). On a refresh, match parsed items by `key` or title and carry over edits,
    tombstones and `task_id`.
  - **Code to change:** `crm/meetings.py` (`_meeting_for`'s merge, `make_task` ~l.496, `make_all_tasks` ~l.525, new
    `edit_item` / `delete_item`); `crm/company_page.py` (`_cards` ~l.54, `_item` ~l.70, the loop in `meeting_context`
    ~l.90); `crm/views.py` (`meeting_panel` ~l.1439 and its `untasked` count, two POST views beside `meeting_make_task`
    ~l.1471); `crm/urls.py` (beside `meetings/<pk>/task/`, e.g. `meetings/<pk>/item/edit/` and `.../delete/`, with `index`
    in the POST as `make_task` does); `action_item.html` (for items with no task only: **Edit** as an inline `<details>`
    form with title, owner and detail; **Delete** as a POST button with a confirm; both redirect to `next` through `_safe_next`).
  - **Tests to write** (existing ones that touch these items: `test_company_page.py`, `test_collab.py`, `test_capture.py`,
    `test_meeting_dedupe.py`): an edit changes title / owner / detail and keeps the index; a delete hides the item and lowers
    the "Make tasks for all N items" count; Make tasks for all skips tombstones; a re-import keeps an edit, a tombstone
    and a `task_id`; an item that already has a task cannot be edited or deleted here (its task has its own edit and delete).
- **Where everything is.** `main` is `79eb8b1` (docs only on top of what is live; this handoff is one more docs commit).
  Branch `ux-fixes-2026-11-04` holds the two fixes and nothing else. CI run 41880113308 was queued on it when this was
  written and **not read**; the full suite was not run on it (one JS file and CSS changed; `test_static_integrity` should
  still hold). To ship: read CI, fast-forward `main` to the branch, deploy image-only with Kellen's OK (the usual checks: the
  live image and the ancestor check, fingerprint before and after, the migrate job, curl, logs; no migration, so a backup is
  optional). Build the proposed-task feature on the same branch or a new one cut from it. The two fixes are small, checked
  and fix things Kellen sees in daily use, so do not hold them back for the feature.
- **Housekeeping, done after the deploy by a side session** (report with every deleted branch's old SHA:
  `C:\Users\Kemoc\AppData\Roaming\Claude\side-session-notes\local_32850d5c-a743-48d8-89df-c0f70f3d7ead\cleanup-report.md`).
  It removed 20 clean worktrees whose branches were already in `main` (18 under `.claude/worktrees` plus
  `ACMI_CRM_deploy_154e56a` and `ACMI_CRM_person_phones`) and 43 local branches with the safe `git branch -d`; nothing
  pushed, no remote branch touched, the main folder untouched. **What is left:** the main folder (`calendar-fixes-v2`: still
  the old HANDOFF.md and the stray `_/` files; fast-forwarding it to `main` is a decision, since a laptop sync task may
  still run its code); worktree `merge-chat-calendar` (clean, in `main`: can go); `ux-fixes` (the branch above, in use);
  `ux-polish-2026-11` (**12 uncommitted files from another session: look, do not delete**); `deploy-lease-roc` and
  `lease-roc` (their content is in `main`; deleting `deploy-lease-roc` leaves commit `ea95d55`, the source of revision
  00020, only in the reflog); `gmail-push` (branch `push-defaults`: **not safe to drop yet**, it holds
  `scripts/gcp_gmail_push.ps1`, the one-time Gmail push setup script, which `main` lacks: copy it into `main` first). The
  branches `data-room` and `email-client-design` are fully in `main` but the safe delete refused them (their upstreams
  lack local commits): `git branch -D` is fine. Also left alone: `C:\Users\Kemoc\Desktop\ACMI_CRM_deploy_ea95d55` (not a
  registered worktree, probably the 00020 deploy export) and seven empty shell folders under `.claude/worktrees`. The two
  `backup/*` branches are deliberate backups: keep.

**Earlier on 2026-11-04 (Claude on the laptop; Kellen: "pick up on handoff", then "push and deploy everything"): the merge
of Chat with the calendar line is done, tested, pushed and DEPLOYED: revision `acmi-crm-00022-fbx`, image `a799272`, live
since 11:24 am Central.** Branch `merge-chat-calendar` (worktree `.claude/worktrees/merge-chat-calendar`, cut from
`calendar-fixes-v2` `1bf04b3`); `main` was fast-forwarded to it after the deploy. Kellen's main folder is still on
`calendar-fixes-v2`, clean, and its HANDOFF.md is the old one: read this file from `main` or the worktree.
- **The deploy (11:12–11:25 am Central).** CI on the pushed commit (run 41874959891, Postgres) green in 6½ minutes; manual
  Cloud SQL backup "before merge-chat-calendar a799272"; Cloud Build `c92c5fc9-8f8d-4d47-b0df-0750b6ed805a` (7 min, from a
  clean `git archive` of `a799272`, `cloudbuild.yaml` with `_TAG=a799272`); the live image was re-read right before
  (`5742155`, an ancestor of what shipped, so nothing live was dropped). After: the migrate job found **no migrations to
  apply**; the service and the three jobs have **identical env / command / args / Cloud SQL settings** before and after
  (md5 of each, only the image differs); no WARNING or ERROR logs on the new revision; the login page is 200, the pages
  behind sign-in answer 302, and the served `app.css` carries the Apple layer and the restored Times rule. **Rollback:**
  `gcloud run services update-traffic acmi-crm --to-revisions=acmi-crm-00021-86x=100 --region us-central1 --project
  sincere-passage-511621-j3` (00021 is `5742155`, the calendar line; the schema is the same either way). **Not checked on
  the hosted copy:** anything behind sign-in (Claude cannot sign in as Kellen): Chat, scheduling, the Apple look, the
  add-to-directory card. Ask Kellen to click through them.
- **What the deploy changed for everyone:** the Apple-style look and dark mode (first time live); Chat (the Chat link, and the
  💬 button in the top bar for anyone who has turned it on) and Scheduling (Calendar menu and the Meetings header) are back;
  add-a-person-from-an-email, inbox times and the phone fixes are back; the buy-box score is gone from the brief PDFs;
  pipeline cards link to company and deal cards; the Meet / Teams choice in scheduling. **Chat and scheduling still need
  the Google steps in the Cloud console** (Kellen runs it: `docs/FOR_MEL_GOOGLE_CHAT.md`, `docs/FOR_MEL_SCHEDULING.md`, the
  file names are old: the `calendar.events` scope on the consent screen, then each person presses Allow scheduling once;
  until then "Open in Google Calendar" works). Whether they were done is not known here; neither has been run against
  real Google as far as these notes say.
- **Before it, live from 10:21 am Central: revision `acmi-crm-00021-86x`, image `5742155`** (just after the entries below
  were written; they still say 00020 / `ea95d55`). That is `calendar-fixes-v2` at `5742155`: calendar
  views, lease / ROC, phone land-search filters, data room pipeline. **It had no Chat, no Scheduling, no "add a person from
  an email" and no inbox times.**
- **Features that had been live and were silently dropped.** Images `154e56a`, `417defa` and `f431408` (branch `person-phones`)
  ran as revisions 00012–00014 from 2026-10-30 until the 2026-11-03 redeploys (Grok's `main`, then the calendar line). They
  are: a name in an email adds that person to the directory (with Office and Cell phone boxes), the time beside the date in
  the inbox list, the notification settings table and ten wide pages fitting a phone, and folded dock boxes no longer
  covering the end of the page. The earlier notes called `154e56a` "not on either line, decide whether it goes in"; the
  Artifact Registry tags show it shipped, so it goes in.
- **`merge-chat-calendar` is `calendar-fixes-v2` plus**, in order:
  1. `60c475c` removes the 598-file `_/` copy and the stray root files (`Hosting Costs.*`, `inbox_response.*`,
     `ACMI_CRM_cmd_commands.txt`). They stay in history at `a06f139`.
  2. `ccc4f85` merges `main` (Chat, scheduling, Apple-style look). One conflict, the Meetings header: the Day / Week / Month
     buttons and main's Schedule a meeting button both stay. `0077_deal_lease_roc` now depends on
     `0076_chatconnection_can_create`, so the migrations have one leaf (`makemigrations --check` clean; the database
     records migrations by name, so nothing re-runs). 0074–0076 are already applied in production.
  3. `8fcfe65` merges `person-phones`. Ten conflicts; where its older Chat code met main's newer, main wins: the
     scheduling form with Google Meet / Microsoft Teams, the Calendar menu with Critical dates, and the Chat list as a 💬
     button in the top bar (so the phone-only button and its pinned-box CSS are dropped and `test_phone_layout` follows).
     app.css keeps the Apple layers last and appends person-phones' who-card and phone rules after them.
  4. `15cd68f`, `6f3e919` merge PR 18 (buy-box score removed from the brief PDFs) and PR 19 (company / deal card links,
     deal card stage selector). No conflicts.
  5. `9a454f9` the top menus fold into ☰ at 1230 px. person-phones folded at 1400 px, which hid them on 1280 and 1366 px
     laptops; the Apple pass's tightened bar fits there (measured on a scratch copy with Mail, Chat and Settings showing:
     fits at 1280 and 1366, overflows ~23 px at 1200). `app.css`, `split.js` `NARROW` and the check in `base.html` agree.
  6. `31f3705` fixes found by the full suite (below).
  7. `ca635b2` restores `recipients.js` and the `.times-panel` rule head, and adds `test_static_integrity` (below).
- **Bugs found and fixed on the way** (all came in with main's lines, none with the merge itself):
  - main's scheduling ignored the old `meet` flag when the form sent no `video`: the three `test_schedule` Meet-link failures.
  - **PR 17 was squash-merged on GitHub as `e76e155`, and its `crm/views.py` arrived truncated** (Grok's three "Enrich
    person_create from email" commits on the PR branch emptied or cut it to 352 lines). The hand restore (`3a9ac22`) came from
    a version that predates PR 17's changes to `views.py`, so `company_detail` had lost `tenant_facts` (and the import of
    `tenant_rows`) and the Tenant & financials panel never rendered; put back.
    Grok's person_create enrichment never landed (`person-phones` does that job: the card, with phones).
  - **The same push left two more files damaged that no test looks at**, found by diffing the squash against its base:
    `crm/static/crm/recipients.js` (address suggestions in compose and reply) was a one-word `PLACEHOLDER` on `main`, so
    suggestions would have stopped working; and the first line of the `.times-panel` rule in `app.css` (the Times side panel
    in compose) was gone, leaving loose declarations and a stray `}` that also cost the next rule. Both restored from the
    version production runs, and `crm/tests/test_static_integrity.py` now fails on an empty or placeholder script / stylesheet
    or unbalanced braces in `app.css`. Everything else the squash removed was checked line by line (replacements, not
    drops); every Python file compiles, every JS file parses and all 221 templates compile.
  - Two PR 17 tests were wrong, not the code: `test_same_zoning_keeps_only_matching_parcels` had been made by renaming
    `test_summary_and_value_range` and putting new setup in front of its body (split back into two); the Tenant & financials
    test asked the Overview, which is a widget grid with no side column (it asks the People tab now). **The Overview does
    not show Tenant & financials at all, only the other tabs' side column; a widget for it would be the fix if Kellen wants
    it on the first screen (`COMPANY_WIDGETS` in `crm/prefs.py`, `crm/widgets/company/`).**
- **Checked:** `manage.py check`, `check --deploy --fail-level ERROR` as CI runs it, `makemigrations --check`, and the full
  suite run serially, twice: the first run (1,168 tests, 7½ minutes) had 2 failures, the two PR 17 tests above, both listed
  in this file as failing before the merge; after `31f3705` the second run (1,169 tests, about 10 minutes) is all green, 1
  skipped. `ca635b2` came after that run and only restores static files and adds two tests, which pass alone together with the
  other tests that read `app.css`. In the browser pane on a scratch database at 1280, 1366 and 375 px (light and dark):
  home, Meetings header, the Chat panel opening under the 💬 button (it shows Google's refusal there, as expected without
  Google), the inbox with times, the add-to-directory card with Office / Cell phone boxes, a team board with cards, the phone
  land search (map, "N parcels" pill, the Filters & results sheet), the compose page with the Times side panel docked at the
  right, and no sideways scroll on a phone. **Not checked:** real Google Chat or Gmail, a real phone, Postgres (CI runs it;
  two earlier fixes were for Postgres ordering).
- **To do next** (the push, CI and deploy that were here are done, see above):
  1. **Have Kellen click through the hosted copy** (Chat, Scheduling, the Apple look in light and dark, the add-to-directory
     card, the compose Times panel) and watch the logs for a day: Chat and scheduling were off from the afternoon of
     2026-11-03, and the Apple look has never run on the hosted copy.
  2. **The Google steps** for scheduling and Chat (above; Kellen's, in the Cloud console), if not done. A person pressing Allow scheduling before the
     `calendar.events` scope is on the consent screen gets Google's "app blocked" screen.
  3. **A latent problem this deploy ends (inferred from how Django adds columns, not seen failing):** while 00019–00021 ran,
     the code did not know `CalendarSync.can_schedule`, which migration 0075 had already added to the database. Django adds
     such a column NOT NULL and drops the temporary default, so that code could not have inserted a new `CalendarSync` row
     (a person's first calendar connection); existing rows were fine, and a search of the production error logs for the
     last three days found nothing. Rolling back to 00021 puts the same mismatch back. Old code does not run safely on a
     schema that has gained a NOT NULL column.
  4. **Housekeeping: done after the deploy by a side session, see "Pick up here" above.** PRs 15, 18 and 19 show merged.
  5. **Ask Kellen:** whether Tenant & financials should be an Overview widget (above); whether the Chat button in the top bar
     (main's) or the phone-only 💬 (person-phones') is the one wanted; whether `docs/GEMINI_*` and the other Gemini / Grok
     docs stay in the repo.

Newest before that (2026-11-04): **Apple-style look, passes 1 and 2: merged to `main` and pushed to GitHub (`e77ee52`); deployed later the same day with the merge above (revision 00022).**
Kellen: "I want it to feel like apple designed it"; keep navy and the top bar.

**Its pick-up list (status at the end of the day in brackets):**
1. **Deploy when Kellen says so** (ask right before; see the deploy note in memory). No migration and no Python changes, only
   `app.css` and template colours, so it is an image-only deploy and rollback is traffic back to the previous revision.
   [Deployed, as part of the merged deploy above.]
2. **CI on `main` is red, and was red before the redesign**: run `41697036626` on `9f8eb56` ("Restore inbox.html") failed. The 5
   failing tests (`test_land_sales.CompTests.test_same_zoning_keeps_only_matching_parcels`, three in `test_schedule`,
   `test_company_page…test_the_side_column_shows_tenant_and_financials`) came in with the "Restore …" commits (views.py, cards.py,
   inbox.html were truncated or overwritten and restored by hand). Fix those first; they are logic, not styling.
   [Fixed on `merge-chat-calendar`, see above.]
3. **Not seen on screen yet:** the inbox reading pane (the preview's sign-in didn't carry into its frame) and team boards
   with cards. Look at both, light and dark, on the hosted copy or a scratch preview. [Both seen on a scratch copy at the end
   of the day, in light mode: the thread opens and the sender buttons work; a board shows grey list trays with white cards.
   Dark mode was only looked at on the add-to-directory card.]
4. **Kellen's main folder is still on `calendar-fixes-v2`** with uncommitted edits to `app.css`, `board.js` and
   `land_search.html`. That branch carries a stray `_/` copy of the whole repo; clean it out, then merge it onto the new
   `main` (expect small `app.css` conflicts with the redesign). [Done on `merge-chat-calendar`; the edits had been committed
   as `b5495fe`; the merge needed no `app.css` conflict resolution against the redesign, only against person-phones.]
5. **Possible pass 3**, only if Kellen wants more: the ~570 inline `style=` spacing attributes, the contract reader, the
   site-card dialog, data room and capital pages, the phone layout.

How to preview a worktree safely (scratch DB, fake records, FakeGmail for the inbox): the memory note on previewing; this time
the server was started from Bash in the background because the session couldn't edit the main checkout's `launch.json`.

What changed. Pass 1: branch `worktree-apple-ui` (worktree `.claude/worktrees/apple-ui`, from `main` `9f8eb56`; CSS
and template colours only, no migration, no Python changes). In `crm/static/crm/app.css`: a new palette at the top (Apple's
system greys, ACMI navy kept for the bar and main buttons, system font stack incl. Segoe UI Variable on Windows), about 320
hard-coded neutral colours swapped for variables (`--panel`, `--panel-2`, `--fill`, `--line`, `--text`, `--muted` …), pale tints
rewritten as see-through rgba (same pixel over white, darkens over a dark panel), dark text colours wrapped in
`color-mix(… var(--ink-mix), var(--text))`, and an "Apple-style layer" at the end of the file (capsule buttons, focus rings,
shadow-not-outline panels, grouped-list tables, segmented tabs, frosted top bar / menus / search palette, sheet dialogs, stat
tiles). **Dark mode is now a real dark palette** (`html[data-theme="dark"]` at the end of app.css); the old whole-page
`filter: invert()` is gone, so maps, photos and email frames are no longer double-inverted. Inline colours in the 83 page
templates and two partials got the same treatment; PDF, email and guest templates were left alone on purpose.
**Pass 2** (same branch): every small all-caps label is sentence case now (43 rules; only the meeting card's month stays
caps), and an "Apple-style layer, pass 2" at the end of app.css covers underline tabs, the company page (frosted sticky tabs,
fact strip, meeting cards with a Calendar-icon date), deal board columns titled in their stage colour (no coloured top
border; at-risk cards get a red dot, not a stripe), team board tiles/lists/cards, the month and week calendars (today in a
red circle, events as tinted bars), the inbox (Mail-like sidebar, blue unread dots, frosted reader header and pop-overs) and
land search (frosted Leaflet controls, legend and pop-ups). Between 1181 and 1440 px the top bar tightens and search shows
as its icon, so the bar fits with Mail and Chat on. The ~570 inline `style=` attributes are almost all spacing (margins,
widths) and were left; the few colour ones are in print, email and PDF templates. Checked in the browser pane on scratch
databases (light and dark): home, companies, a company, the deal board, the calendar, land search, the inbox (with the test
suite's FakeGmail); the inbox reading pane and team boards with cards were not seen.
Tests: 1,142 run, 5 fail (land comps zoning filter, three scheduling tests, the company page's "Tenant & financials"
column); none of them looks at styling, and CI on `main` was already failing before the redesign (run
`41697036626`). Map marker colours inside `<script>` blocks were left as hex on purpose (Leaflet can't read CSS
variables).

[It did ship, as revisions 00012–00014 the same afternoon, and was dropped by the 2026-11-03 redeploys; it is in
`merge-chat-calendar`. The Chat list is main's top-bar button there, not the phone-only 💬 button described in point 3.]
**Newest, not deployed (2026-10-30, afternoon): branch `claude/beautiful-albattani-6xp3qi`, on top of `chat-parity`
`1bc8005` (the live code), pushed, not merged.** Kellen, from his phone, asked for three things. All 1,157 tests pass,
there is no migration, and nothing is new in settings. To ship it, deploy this branch as `chat-parity` was deployed:
only the image changes.
1. **A name in an email adds them to the directory** ("click an email and [add] them to the directory directly. Anne is
   a land broker"):
   - From, To and Cc names in the reading pane and the dock's email box are buttons (`partials/mail_to_line.html`,
     `crm/static/crm/who.js`).
   - A click opens a card (`directory_views.who`, `partials/who_card.html`). It says what the hub already knows: an
     ACMI address, a person in the CRM, a contact at a directory firm, or whose the domain is.
   - Otherwise the card has a form: name, firm, type of firm ("Real estate broker" fits a land broker), title and
     phone. The firm is prefilled with the one that owns the domain, else a name made from the domain.
   - `who_add`, through `logic.add_contact`, puts the contact on the firm, creating it or matching a name in any case.
     It gives a new firm the email domain, unless the domain is personal email, a company's or another firm's. Then it
     files the domain's mail waiting in the review queue there.
   - Adding the same address again only fills blanks. Tests in `crm/tests/test_who.py`.
2. **Time with the date in the inbox list**: the `mail_when` filter in `crm_tags.py` gives "Oct 30, 9:12 a.m.", with
   the year added when it isn't this year.
3. **Phone layout** ("the interface on mobile is bad... Chat is blocking the view"). Checked in Chromium at 390 px on
   made-up data with Chat on:
   - Below 700 px the pinned Chat box is gone. A 💬 button in the top bar carries the unread count and opens the list
     full screen (`dock.js` `data-chat-open`). "+ New" shows as "+" to make room.
   - Folded boxes no longer cover the end of the page (`--dock-h`).
   - Notification settings' device table stacks instead of widening the page to 485 px.
   - The ten pages that still scrolled sideways now fit: the redline compare, project build and capital, firm,
     Needs filing, client searches and their site report editor, site manage and diligence. 97 pages were checked at
     390 px.
   - The Back link no longer hides under the top bar on Mail, and the reader's buttons are smaller.
   - Also on desktop: with Chat added, the top menus ran 10 px past a 1440 px window (the avatar was cut off), and up
     to 1,348 px wide between 1181 and 1366 px. Search is now its icon below 1500 px, and the menus fold into ☰ at
     1400 px instead of 1180 px (`app.css`, `split.js` `NARROW`, the inline check in `base.html`).
   - Not tried: a real iPhone, and real Google Chat or Gmail.

Before it, (2026-10-30): branch `chat-parity` (worktree `.claude/worktrees/chat-parity`, on `schedule`, pushed to GitHub, not merged;
**live since 10:38 am Central on 2026-10-30**: revision `acmi-crm-00011-dlb`, image `c370d25`, built by `cloudbuild.yaml` in 5
minutes, migration `0076` applied, the service and the three jobs have the same settings as before and only the image changed,
Cloud SQL backup `1793374290231` taken first, rollback is traffic back to `acmi-crm-00010-pfw`): Kellen asked for everything
Google Chat's web app does, so Chat now has **emoji reactions, quote in reply, file
attachments (send, drop, paste; pictures shown, files downloaded through the hub), edit and delete your own messages, @mentions
and :emoji: suggestions, formatting buttons, starting direct messages / group chats / spaces, searching messages, and it
catches reactions and edits by others without a reload**; see "Like Google Chat's own web app" in `docs/CHAT.md`. One new
optional Google permission (`chat.spaces.create`, in `docs/FOR_MEL_GOOGLE_CHAT.md`), migration `0076`. Everything sent now
says `markupSyntax: MARKUP_SYNTAX_CHAT` (the live Chat code didn't; a project made lately reads text as Markdown by default).
Nothing has run against real Google. gcloud's Google sign-in had expired by the next morning (Google's organization asks for a
password check about every 14 hours); it was renewed through the browser pane, see the deploy note in memory.

Before it (2026-10-29): the **mail dock**, email boxes along the bottom of the page (`docs/MAIL_DOCK.md`), is merged to `main`
(PR #14). On top of it, **live since 9:03 pm Central on 2026-10-29 but not yet merged to `main`** (revision `acmi-crm-00010-pfw`,
image `f0be55f`, built with `cloudbuild.yaml`; migrations `0074` and `0075` applied; the service and the three jobs have the same
settings as before, only the image changed): branch `schedule` (worktree `.claude/worktrees/schedule`, code at `f0be55f`, on
`main` `41044b8`, 1,081 tests pass): the dock's superset (`dock.js` also carries Chat), **Chat in the hub** (`docs/CHAT.md`; coworkers
only, no tie to companies or the CRM; waits on Mel's Google setup, `docs/FOR_MEL_GOOGLE_CHAT.md`) and **Scheduling**
(`docs/SCHEDULING.md`; see when teammates are free and put a meeting on the calendar; `docs/FOR_MEL_SCHEDULING.md`). Neither has
run against real Google yet: the first real "Turn on Chat" and "Allow scheduling" prove them. Before the migrations a Cloud SQL
backup was taken (`1793324941228`, 8:49 pm Central). Rollback is traffic back to `acmi-crm-00009-db8` (the migrations only add
a table and a column, so the old code runs on the new schema). What e-signature would take is in `docs/ESIGN_AND_CHAT.md`
(nothing built).

## Pick up here (2026-10-25)

**Done 2026-11-04 (Claude on the laptop, after Kellen had Grok and Gemini work on the code): review of that work, phone
layout, two PRs. Nothing from this entry is deployed.**
- **Grok's pushes to `main` are sound.** `9f8eb56` has Chat, scheduling and the restores of `views.py` / `inbox.html`
  (truncated and overwritten by a large-file push); both files now equal their pre-push versions, `manage.py check` is
  clean, and `main` matches branch `chat-parity` except newer scheduling (video: Meet / Teams).
- **Gemini's output was never usable and is on the calendar line by mistake.** `a06f139` ("Improve mobile UX...")
  added a 598-file copy of an old project under `_/` (its own `base.html`, `nav.js`, `board.js`, `board.html`,
  `land_search.html`, `app.css`: different apps, missing dark mode, the mail dock, Chat, the search palette; its board
  posts to `/crm/pipeline/move-stage/`, which does not exist) plus `Hosting Costs.docx/.pdf`, `inbox_response.*`
  (a failed curl log) and `ACMI_CRM_cmd_commands.txt`. The real `crm/` tree was not touched by it except one test.
  **To do: `git rm -r _` and those stray files from `calendar-fixes-v2`** (keep the folder on disk if wanted).
  [Done on `merge-chat-calendar` (`60c475c`); `calendar-fixes-v2` itself still has them until it is fast-forwarded.]
- **Phone layout, done in the real files (`b5495fe`, `calendar-fixes-v2`, not deployed):** land search on a phone is a
  full-height map with a "N parcels" pill that opens the filters and results as a slide-up sheet (`.land-fab`,
  `.land-side-bar`, `side-open`; the ☰ Filters button does the same; split-view `html.embedded` keeps side by side);
  board cards lift on a 250 ms press-and-hold, drag between lists with edge auto-scroll and save through the existing
  move endpoint (`board.js` touch section, `.bl-card.touch-lifted`). Checked on a scratch database at 375 px with
  simulated touch events, not on a real phone. Gemini's bottom nav bar was left out on purpose: the app already has a
  phone menu button, and a bottom bar would collide with the mail dock.
- **PR 18** (`remove-buybox-score`): the buy-box score is gone from the brief PDFs (score box, comparison row and its
  toggle, "How the buy-box score is calculated", the definition, the footer mention); scoring still works on the land
  search and site cards. Replaces Grok's "optional score" version, whose Notes template used `selectattr` (not a Django
  filter) and failed to render. `test_land` 89 green.
- **PR 19** (`card-nav-redesign`): pipeline cards link the company to its card and the deal to its card; company card
  shows open deals with stage / value / next step and a "View in pipeline" button (`?q=`); deal card has a stage
  selector. Fixes on top of Grok's pass: the handler is in `nav.js` (scripts in fetched card HTML never run), the stage
  endpoint returns JSON only for `X-Requested-With: fetch` or `json=1` (the board's drag sends `json=1`), plain posts
  redirect through `_safe_next` (it was an open redirect). Collab, pipeline, boards, cards, land suites green (116).
- **Both PRs are based on `main` (the Chat line), so merging and deploying `main` alone would drop the calendar views
  and lease / ROC that production has.** Fold them in with the merge described under "To do next" below, then deploy
  once. A deploy was asked for and not run: the PRs were unmerged, their CI was pending, and gcloud's sign-in had
  expired. (I first assumed production was revision 00011 from an old note; the entry below is the current one.)
- Local branches `fix-buybox-score` and `fix-card-nav` (worktrees `.claude/worktrees/fix-buybox`, `fix-cardnav`) were
  pushed to the two PR branches and can be removed after the merge.

**Built 2026-11-04: data room pipeline in the report layout** (Kellen: "I want the fortress data room to have the
same excel style view - no need for the other views - they should still have the option to export to excel"). Branch
`calendar-fixes-v2`, not committed, no migration. The room's home (`crm/room/home.html`) drops the Table / Board tabs
(`?view=board` is ignored) and is laid out exactly like the investor report (Kellen: "should be exactly the same as
the report view"): the report's column order (Site, Deal type, Stage, Tenant, ...), stage pills in the report's
colours, logo and "ACMI Properties pipeline as of", Print / PDF; ROC added to the room allow-list (cost group, so
`show_costs` off hides it). Still never shown: lead / source, owner, next step / notes, rent, the Lease / LOI link,
the deal-name line under the tenant. Layout: Signed agreements / Active discussions / Prospects
(`REPORT_SECTIONS` in `crm/views.py`), navy header rows, section totals, then Development sites. **Download Excel**
(`/room/pipeline.xlsx`, `room_xlsx`) is the same tracker workbook as the internal report (`pipeline_workbook(...,
partner=True)`), with only the room's allow-listed columns; the Sites sheet leaves out campus location and status notes.
Downloads are logged (page `excel`, "downloaded the pipeline as Excel" on the Manager's log). Built in
`logic.sections()` / `logic.sites()`, which only total columns the room shows. Tests in `test_data_room.py`.

**[DONE on `merge-chat-calendar`, see "Pick up here" at the top; the text below is what was asked for, and production has
moved on to 00021 / `5742155`.] To do next, before any other deploy (2026-11-04): merge Chat (`main`) with the calendar
line.** Production
(revision `acmi-crm-00020-jgs`, image `ea95d55`, local branch `deploy-lease-roc`) is `a06f139` (calendar Day / Week /
Month, mobile UX; branch `calendar-fixes-v2`) plus lease / ROC, and has **no Chat code**, though the database already
has Chat's migrations 0074–0076 (from `main` `9f8eb56`, live ~2 h on 2026-11-03 before `a06f139` was redeployed).
Deploying `main` as it is would drop the calendar views; deploying the calendar line leaves Chat off.
- Merge `main` with `deploy-lease-roc` into one branch. Take `0077_deal_lease_roc` from branch `lease-roc` (it depends
  on `0076_chatconnection_can_create`); the DB records migrations by name, so its dependency can change safely.
- Decide whether `b5495fe` (phone land-search filters, on `calendar-fixes-v2`, never deployed) and `154e56a` (add a
  person from an email, inbox times, phone layout; folder `ACMI_CRM_deploy_154e56a`, not on either line) go in.
- `makemigrations --check`, full suite run serially (parallel hides errors). Failures already there before the merge:
  `test_person_phones` (2) on the calendar line; `test_company_page` side column, `test_land_sales` same-zoning and 3
  `test_schedule` Meet-link tests on `main`.
- Then deploy the merged commit (check the live image first; Kellen's OK right before).

**Built 2026-11-04: lease / LOI and ROC on deals** (Kellen: "update the company card so we can have the lease or LOI
on there which is then pulled to the IR report - I want it to share the ROC metric"). Branch `calendar-fixes-v2`;
main and production already use 0074–0076 for Chat, so it is numbered `0077_deal_lease_roc` but depends on 0073 here; when the Chat line is merged in, point its dependency at 0076 (done on `merge-chat-calendar`). Migration `0077_deal_lease_roc`: `Opportunity.lease_doc_kind` (LOI / Lease), `lease_doc_name`,
`lease_doc_url`, `annual_rent` (year-1 rent / NOI). `Opportunity.roc` = annual rent ÷ project cost (`value`).
- Company page › Drive files: each current file has **Use as lease / LOI** (pick the deal; kind guessed from the name).
  Or paste the link on the deal's Edit form, where the year-1 rent is typed too.
- Company page › Opportunities tab: Lease / LOI link and ROC columns.
- Investor report: new columns **ROC** and **Lease / LOI** (on by default; "Rent / NOI" off by default). ROC is in the
  "cost" group, so hiding costs before sending hides it too. Excel: ROC is a live `rent / cost` formula when the
  Rent column is shown, section total = weighted ROC; the Lease / LOI cell links to Drive.
- Not in the partner data room (its allow-list is unchanged).

**Done on the laptop 2026-10-28 (branch `claude/beautiful-albattani-6xp3qi`, merged fast-forward to `main` `b691c6d`, CI green, 904 tests; real database migrated through 0072, backup `backups/db-2026-10-28-before-review-fixes-to-main.sqlite3`; the laptop's `.venv` still has the old `pypdf` / `pillow`, CI and the image use the new pins):** steps 1, 2 and 4
below, and W1–W13 in code (Kellen's instruction was to do W1–W13 on the hosted copy; they're code fixes, so they ship
with the first deploy instead). Real database backed up to `backups/db-2026-10-28-before-review-merge.sqlite3`.
- Merge of `main` (`dcc0b9e`): the 9 conflicts resolved as in worklist step 0; data room migration renumbered
  `0072_data_room_login_attempts` (the read-only check showed it was never applied). CI green.
- W1–W13, each with a test in `crm/tests/test_wl_g1.py` … `test_wl_g7.py`, each diff reviewed separately. Pipeline
  table: the extra rule that picking the On hold or Lost stage still shows those deals. W11 still needs one real site
  brief PDF rendered on the new `pypdf` / `pillow` pins.
- **News and Granola from Drive** (Kellen chose a Drive folder for Granola): `ACMI_NEWS_DRIVE_FOLDER` makes the News
  page list and import from that folder (`NewsScan.path` is `drive:<file id>`); `ACMI_GRANOLA_DRIVE_FOLDER` makes
  `sync_all` import new Granola JSON files once each (`ImportBatch.sheet` `granola:<file id>`; a file that fails is
  logged by name and tried again next run). With neither set, the laptop folders work as before. `cloudbuild.yaml`
  passes them as `_NEWS_FOLDER` / `_GRANOLA_FOLDER` with `ACMI_DRIVE_ADC=true`; its header lists the one-time setup.
  **The daily Granola task (acmiproperties account) has to save its JSON into that Drive folder.**
- **Granola text export from Drive, as Kellen (built 2026-11-04, branch `worktree-granola-drive-txt`, not deployed):**
  `crm/granola_text.py` reads the daily export (`=== MEETING ===` blocks: ID, TITLE, DATE with CST/CDT, URL, ATTENDEES, notes)
  into the rows `import_meetings` takes. ALL-CAPS lines become headings and `- Title (Owner): detail` under NEXT STEPS become
  action items (194 from the real 53-meeting export). Attendees are names only: team by name or first-initial+last, outside
  by one matching Person, and a `(Company)` label links the company when exactly one has that name. `import_from_drive` takes
  `.json` or `.txt`, skips the index file (no meetings), and re-reads a `.txt` whose modified time changed.
  `ACMI_GRANOLA_DRIVE_USER` reads the folder with that person's Google token (`drive._user_session`) and adds Drive
  read-only to the sign-in scope; cloudbuild passes `_GRANOLA_USER`. **To switch on:** add the Drive scope to the OAuth
  consent screen, set `_GRANOLA_FOLDER` (`1qREDx3CbuEVHkWTEyK344jFQ5cHG1HYn`) and `_GRANOLA_USER`, deploy, Kellen signs in
  again once. Merge Chat (main) with the calendar line first.
- 901 tests green.
- **Not done, needs Kellen:** the whole cutover in step 3.
- **Secret Manager is Mel's** (Kellen, 10-28). Neither ACMI project (`sincere-passage-511621-j3`, which has the OAuth
  client, or `acmi-properties-web-app`) has billing or Secret Manager on. `docs/FOR_MEL_SECRET_MANAGER.md` lists what
  Mel sets up. Once Mel gives Kellen Secret Manager Admin, Claude on the laptop uploads `ACMI_TOKEN_KEY` and the
  VAPID pair from `.env` (piped, never printed) and compares checksums.
- Follow-ups the reviews raised: CSV exports in `crm/land` (`comp_views.py`, `letters.py`, `views.py`) have no formula
  guard; Trello-imported attachment URLs aren't scheme-checked; old push subscriptions / card links that the new rules
  would refuse are still stored; read-only mailbox grantees can still add CRM notes (Kellen's call).

**NEXT, for the ACMI Claude on the laptop (2026-10-28): finish the review work in `docs/WORKLIST_2026-10-28.md`.**
Kellen: "combine everything and check against what you found. Then update handoff and have my acmi claude finish the
work". The worklist combines today's security audit and UI review, Gemini's report, the 10-26 review and the 10-22 UX
pass. Each item was checked against `main` (`b254b6d`) and this branch, with Gemini's wrong items rejected with proof.
**Kellen decided the same day: deploy everything to the cloud first, then audit and fix the hosted copy.** The laptop
stops being the working copy. Worklist section 5 checks the laptop Claude's hosting-readiness list. In order:
1. **Merge `main` into branch `claude/beautiful-albattani-6xp3qi`, before the first deploy.** This branch's
   `cloudbuild.yaml` already passes `ACMI_TOKEN_KEY`, the VAPID pair, the Anthropic key to the sync job and the Google
   client to the send job, which are the first three "confirmed" readiness gaps. Deploying `main` as it stands ships
   them. The merge brings 9 conflicts, each with its resolution in worklist step 0. Take `main`'s mail layout and
   `attach.js`, and drop the branch's duplicate attachment list.
2. **Renumber the branch's migration** `0068_data_room_login_attempts` to `0072`, after a read-only check that it was
   never applied. Then suite, CI, and fast-forward `main`.
3. **Cutover** (worklist section 5):
   - Secret Manager gets the laptop's own `ACMI_TOKEN_KEY` and VAPID pair, or the copied tokens and subscriptions
     are unreadable.
   - Copy `media/` to the bucket (the image leaves it out).
   - Turn off the laptop's mail sync task before the final database copy.
   - Rehearse the 1.1 GB copy into a scratch Postgres first.
   - Add the OAuth redirect URIs.
4. **Still to build for the cloud:** the news watcher reading a Drive folder (plus `ACMI_DRIVE_ADC`), and a way for
   Granola meetings to reach the hosted database (today the import writes the laptop's).
5. **Then, on the hosted copy:** W1–W13, each with a test. Improvements I1–I16 are Kellen's pick, with the quick wins
   marked.

**Record cards 2026-10-27** (Kellen: "check the clickability between things... on the investor report to a company
then to their page. Is that necessary? Should it not be their card? ... It should be much more fluid and easy to
navigate"). A plain click on any company, deal or person link now opens that record's card over the page you are on:
`crm/cards.py` renders `crm/cards/{company,deal,person}.html` at `<record url>card/`, `nav.js` ("peek") fetches it
into the `#peek` dialog in `base.html`. The card has the essentials (facts strip, next step or AI headline, recent
activity, latest comments, people, deals, agreements, sites, open tasks) and an **Open page** button that keeps the
list position (`?from=`) the link carried. Links inside a card open the next card with **Back**; Esc or the backdrop
closes. Links go straight to the page when the click carries a modifier (new tab), the link is in a breadcrumb, a
record header, the record nav or search results, or it has `data-page`. Sites keep their own card on the Sites page.
Also linked on the way: a deal's campus and land sites on the deal page and in the company page's Open deals widget.
Tests in `crm/tests/test_cards.py`; checked in Chromium (report row, chained cards, Back, Open page, Esc, list
position, Ctrl-click); 736 tests green. Tasks, contracts and sites could get cards the same way (`CARD` in nav.js, a view and a
template each).

**Company page: a widget grid and one chronological feed 2026-10-26** (Kellen: "the company card should be
customizable like the dashboard and be more so like Trello where comments and meeting updates/notes are in
chronological order"; branch `claude/beautiful-albattani-6xp3qi`, with the review work below). The Overview tab is
now a grid of widgets laid out per person, the dashboard's own machinery: `dashboard.js` reads the grid's
`data-layout` key, `crm/prefs.py` keeps a registry per page (`LAYOUTS`: "dashboard", "company"), and the frame
markup is one partial (`crm/partials/widget_grid.html`) with each widget's body in `crm/widgets/company/<id>.html`.
Customize page (top right of the tabs) drags, resizes, hides, adds back and resets, saved under the "company" key
for every company. The widgets, in default order: At a glance, AI overview (Kellen: "move the ai overview to the top so it's more
prominent"), Overview, Activity, Details, Meetings, Action items and tasks, Who we know, Open deals, Files, Team
connections, Boards and lists. **Overview** (Kellen: "the company overview
section should be more apparent and have proper formatting") leads the page under At a glance: the description as
prose (`prose` filter in `crm_tags.py`, the meeting-notes renderer: paragraphs, `#` headings, `-` bullets,
`**bold**`), the sector, HQ, website and LinkedIn in a row above it, categories and flags under it, and an Edit link;
the edit dialog lives once at page level so any widget can open it. **Activity** is the Trello-style feed
(`company_page.feed`): comments, notes, meetings (with their Granola notes and action items, Make task included)
and the conversation (email, replies, calls, stage moves) in one list, newest first, a comment box at the top, a
fold for a note, and chips for comments and notes / meetings / email. A meeting in the feed is high level (Kellen: "Activity
should mention a meeting with high level details"): title, when, who from outside, a one-line gist of the notes
(`company_page.gist`) and a line of action items; its title jumps to the meeting's card in the Meetings widget on the
same page (`data-jump` in `company_detail.html`, with a flash), or opens the Meetings tab when the card isn't on the
page. Meetings and notes come from their own rows; their echoes on the activity timeline (`FEED_ECHOES`) are left
out so nothing shows twice. The other tabs keep the
two-column layout with the side panels (`team_panel.html`, `lists_panel.html` are shared). Also fixed on the way:
"+ Add widget" on the dashboard did nothing while the URL already carried `?customize=1` (a `#widgets`-only
change is a fragment jump, not a reload). 731 tests green. Person and deal pages still have the old
notes and comments panels; the same feed would fit them next.

**Security audit and UI review of `main` 2026-10-28** (Kellen handed over the two Gemini prompts: "Ignore Gemini... pick up the task for me"; report only, no code
changed; both against `main` at `b254b6d` on made-up data). `docs/AUDIT_REPORT_2026-10-28.md`: 11 verified findings,
none above Medium. Findings 1, 2, 5 and 8 are already fixed on this branch; the new ones are formula injection in the
Excel exports, a read-only shared-mailbox grantee who can file, partner passwords on the team sign-in, push endpoints
and "Not a deal" working from a GET. `docs/UI_REVIEW_2026-10-28.md` (screenshots in `docs/ux-review-3/`): the parcel
card 500s once the user who set a status is deleted, five inbox actions 500 when Google refuses the token, 23 phone
pages are too wide (mostly `app.css:685`), and the pipeline table lists closed deals first under an open-deal count.
The parcel card and four of the five inbox actions turned out to be fixed on this branch already (10-26 items 10
and 12). What's still to do is in `docs/WORKLIST_2026-10-28.md`.

**Code review and audit of the week's changes 2026-10-26** (Kellen: "review the changes made since 10/21, audit and
optimize everything"; branch `claude/beautiful-albattani-6xp3qi`, not merged; findings and a checklist of what to try
on the laptop in `docs/REVIEW_2026-10-26.md`). Fixed there, each with a test, 715 tests green: the data room lockout
now lives in the database and follows the real client behind Cloud Run's proxy (migration
`0068_data_room_login_attempts`; before, a per-process cache keyed on the proxy's address); board card attachments
download unless they're PDFs or pictures (an uploaded web page could run on the hub's origin); a line break in a
compose header is a form message, not a 500; Gmail worker threads close their database connections; partner accounts
never show as "ACMI" teammates in address suggestions; `cloudbuild.yaml` passes `ACMI_TOKEN_KEY`, the VAPID pair and
the Google client to every job (the send job had no Google client at all); `cryptography` and `google-auth` pinned;
`.env.example` lists the new variables. Also on the branch (Kellen, 2026-10-26: "I can only attach one document in
email?"): the compose page and the inline reply keep every file picked (`compose.js`): each pick adds to a list under
the Attach button, with a ✕ per file and a 25 MB total warning; before, picking again replaced the first file.
**Second pass, same branch** (Kellen: "go through each section of the web app and double check everything", and
"in the QD arc when I put in 10,000 it shows 10000"): every number field in the hub takes and shows thousands
separators (`comma_numbers` in `crm/forms.py`, `acmiNum` in `nav.js`, the QD planner and site plan, the hand-written
inputs); every URL was fetched on a scratch database as a Manager, a plain member and signed out, and the main pages
opened in Chromium. Fixed from that: five pages that crashed on a missing user (a `|default:` argument on None
raises), the Tasks page in database order (Meta.ordering is dropped on a GROUP BY), Gmail failures after the inbox
loaded showing as 500s in the reading frame, repeated per-row queries on the campus, home, company and deal pages,
map overlays asked for every region on every move (and the Huntsville parcel layer never drew: no CORS on that
server). Details and a checklist in the report's "Second pass" section. 726 tests green.
**Merge:** CI, then `git merge --ff-only`, back up the real database and `manage.py migrate crm`.

**Split view 2026-10-27** (Kellen: "run the email client at the same time I have my land map open... being able to open any component of the web app at the same time"): ◫ in the top bar opens a second hub page beside the one you're on (`crm/static/crm/split.js`, markup at the end of `base.html`). The side is a same-origin frame of the page drawn without the top bar: `?embed=1`, and pages opened inside it are known by the browser's `Sec-Fetch-Dest: iframe` (`context_processors.is_embedded`, `EMBED`; a head script also hides the bar whenever a page is framed). Pick any page in the side's dropdown, or **Alt+click** a link to open it on the other side. Drag the divider (or arrow keys on it) to resize; ⇄ swaps the two pages, ⤢ keeps only the side page, ✕ closes the side, ← and ↻ act on the side. Remembered per browser (`localStorage.hubSplit`); the main side folds its menus into ☰ under 1180 px (`html.split-narrow`); a side Land map keeps its filters beside the map down to 560 px; phones (900 px and under) show one page. Framing: `X_FRAME_OPTIONS = "SAMEORIGIN"` and the CSP's `frame-ancestors 'self'` (both were "deny"); the side runs the same middleware, so sign-in, partner-only and manager rules apply. Only the main page polls for alerts; the side keeps its own Back trail. Checked in a browser on a local test database (Land beside Tasks, Boards, swap, resize, close, Alt+click, phone); **the live inbox beside the map isn't checked against real Gmail**, only by Django tests (`crm/tests/test_split_view.py`). No migration.

**Mail and home fixes 2026-10-27** (Kellen's feedback): the Times panel is wider (drag its left edge; remembered), stretches 7 AM to 8 PM to the panel height and wraps event titles; scrollbars keep their own room (`scrollbar-gutter`) in the Times grid and the inbox columns. Each message has Reply / Reply all / Forward buttons and a bar under the thread replies to the latest (the reply box opens there). The formatting bar has text colour and highlight (inline styles, kept by the sanitizer). My day: a waiting reply links to the email (`inbox_open?m=`) and × takes it off your list (UserPrefs `my_day_replies_dismissed`; a new reply on the thread brings it back). Dashboard: a dragged widget now lands above or below another depending on which half it's dropped on (it used to always go on top), and ↑ ↓ on ⠿ step past the next widget. No migration.

**Offer meeting times from an email 2026-10-27** (Kellen: "have my calendar pop on the side so I can pick timeslots for a meeting that then get added to my email"): the formatting bar on the compose page and the reply box has 📅 Times. It opens a side panel with your week from Google Calendar (`gcal.week`, read live with the calendar.readonly permission the Meetings sync already has, nothing stored; free and declined events left out), weekdays 7 AM to 8 PM, with a length picker, weekends and week paging. Click free time to pick a slot (a slot over a meeting is flagged). Insert puts "Would any of these times work for you?" and the slots as a list, in your time zone, where the cursor was. Route `inbox/compose/calendar/?start=YYYY-MM-DD`. No migration.

**Labels in the inbox 2026-10-27** (Kellen: "collapse labels, delete them, or modify them"): the folder pane shows labels as Gmail nests them (`index.label_tree`), each parent and the whole Labels group collapsible (remembered in the browser). The ⋯ menu on a label renames it (`actions.rename_label`; labels nested under it follow) or deletes it (`actions.delete_label`, after a confirm; it and its sub-labels come off their emails in Gmail, no email deleted). The hub's own ACMI/ labels can't be changed there. Route `inbox/labels/edit/`. No migration.

**Faster inbox 2026-10-27** (Kellen: "the email component is slow"): clicking a conversation opens it in the reading pane without reloading the page (`X-Mail-Pane` answers `crm/partials/mail_reader.html` only, so Gmail isn't asked for the list again; the page's reader scripts re-run via `initReader()` and compose.js `initInlineReply()`; anything failing falls back to a normal load). Resting the mouse on a row for 150 ms reads that conversation into the cache (`/inbox/warm/`, read only, nothing marked read). Each message is parsed once per thread (`render.thread_texts`, cached with the thread), the CRM tags are worked out once per page, and the "other parts of this conversation" Gmail search is cached 10 minutes. Measured on the real mailbox: opening a conversation went from 1.5 to 2.8 s to 0.12 to 0.18 s cached, about 0.9 s when Gmail must send it.

**Email formatting 2026-10-27** (Kellen: "I should have the tools to adjust the body of the email so italics, bold, numbering etc."): the compose page and the reading pane's reply box have a formatting bar over the body (bold, italic, underline, strikethrough, bulleted and numbered lists, indent, link with Ctrl+K, clear formatting) in `crm/static/crm/compose.js`. It posts `body_html`; `compose.formatted` cleans it with the news-draft sanitizer and `compose.plain_text` makes the plain part (numbered lists numbered). Drafts keep the formatting; an empty editor falls back to the plain `body`. No migration.

**Trello history, Campus Pursuits and Capitalization 2026-10-27: the Trello import is finished** (Kellen took the five recommended answers in `docs/TRELLO_MAPPING.md`; merged fast-forward `6f9d242`, CI green, 810 tests, no migration; applied on the real database, backup `backups/db-2026-10-27-before-trello-pursuits-capital.sqlite3`): `import_trello <folder> --board pursuits` made the **Campus pursuits** team board (High / Medium / Low priority, Holding / overflow; 55 cards, 185 comments, 12 cards on their deals: Elbit, Niron, Thales, SAIC, Johns Hopkins, Long Wall, North Wind, Saab, Specter, Castelion; the Exploration Park / Nassau Bay and Volare (Avio) pursuit cards came over). `--board capital` made the **Capital partners** team board (Intro conversations, Active discussions, Term sheet / negotiation, Committed / closed, Passed; 16 cards, all on directory firms, 41 comments, 29 attachments). Wellington, Temerity, Silver Peak, Davis Companies, L&B Capital, Blue Owl and JRE added as investors. **The directory's "Fortress" is now "Fortress Investment Group" and is the Fortress data room's partner firm.** All seven boards Kellen kept are in (Prospecting, Maryland / MEIH, NSIH, General, Campus Pursuits, Capitalization; the second MEIH board skipped): about 3,000 Trello comments with their authors and dates. `import_trello` is safe to re-run against a newer fetch: it adds only what isn't there.

**Trello history, Properties-General 2026-10-27** (Kellen took the recommended answers in `docs/TRELLO_MAPPING.md`, "Properties-General", except the NSIH dates: "I can upload the PSA and related docs"; merged fast-forward `8c73905`, CI green, 791 tests, no migration; applied on the real database, backup `backups/db-2026-10-27-before-trello-general.sqlite3`): `import_trello <folder> --board general` made the **ACMI general** team board (Press / postings, Administrative, Other markets, Airport Commerce, and an archived Project North Star – Niron list; 59 cards, Spinner and Supernova cards on their deals, Niron's on the Niron deal) and an archived **Properties-General archive** board (102 cards: Avio, Strategic, RRAD, Propulsion Park, Precision Park, King Ranch, TexAmericas, the Army partnership, Prospecting, Tenant Followups). The 2025 "NSIH (Bloomfield, IN)" cards went onto the NSIH board's Administration (archived) and ARA - Lease Negotiation onto MEIH's ARA list (archived). 455 comments, 41 checklists, 145 attachments, 314 history entries. **The 43 NSIH and Hostetler date cards weren't imported**; their 5 comments and 54 attachments went onto the NSIH board's Land PSA - Hostetler, Land PSA - NFP, Prometheus - Ground Lease and Prometheus - DMA cards. **Kellen: upload the Hostetler PSA, the NFP / Prometheus agreements and the ARA PDA** as contracts so their dates reach the calendar. Exploration Park and Volare lists skipped (87 cards, 233 comments), 31 mirror cards skipped. **Next:** Campus Pursuits and Capitalization.

**Trello history, Properties-NSIH 2026-10-27** (Kellen took the six recommended answers in `docs/TRELLO_MAPPING.md`, "Properties-NSIH"; merged fast-forward `eca3056`, CI green, 776 tests, no migration; applied on the real database, backup `backups/db-2026-10-27-before-trello-nsih.sqlite3`): `import_trello <folder> --board nsih` made a new **NSIH** board (campus and project NSIH; lists Administration, Design & construction, Due diligence, Infrastructure / utilities, Land contracts, Parcel owners, Tenants and leases, Capitalization, Models & decks, and an Open tasks feed; the team as members) with 113 cards and all 521 comments (David DeNardo 455; Patrick Dillmann 18, Olivia Jaskula-Dybka 2 and Eli Miller 1 named in their comments), 42 checklists, 61 attachments, 180 history entries. 9 cards link to the Prometheus, iRocket and Spinner deals (the archived Kratos LOI cards onto Prometheus), 29 to directory firms. 14 firms added: Ryan LLC, Drewry Simmons Vornehm, Earthsource, SMS Energetics, TRC Companies, Johnny's Signs, APT Solar Solutions, Weddle (services); Fortress, Strongbox, Browning, OneIM, Cerberus (investors), CTL Capital (lender). **The Prometheus (Kratos / Rafael) deal is now on campus NSIH** (it had none). The land PSAs (NFP, Hostetler) and the Prometheus ground lease / DMA are plain cards until they're uploaded as contracts. 8 mirror and link cards skipped. **Next:** Properties-General (its two NSIH critical-dates lists join this board), Campus Pursuits, Capitalization.

**Trello history, Maryland / MEIH 2026-10-27** (Kellen took the plan in `docs/TRELLO_MAPPING.md`, "The two MEIH boards", with changes: Lewis Place is Bryans Road, NSWC - IHD is the base relationship (a Consultants card), ARA's dates wait for the final PDA, the second MEIH board is skipped; merged fast-forward `576fdaf`, CI green, 773 tests, no migration; applied on the real database, backup `backups/db-2026-10-27-before-trello-meih.sqlite3`): `import_trello <folder> --board meih` put 50 of the 73 cards on the MEIH board (Land sites, Critical dates, The Forge, ARA Facility deal and sites and design, Consultants), placed by hand (`MEIH_CARDS` in `crm/boards/trello_import.py`) and linked to 17 sites, the 4390 PSA, the ARA deal and 4 directory firms; all 118 comments (44 also on site pages, 6 on the 4390 PSA, 3 on the ARA deal), 55 checklists, 58 attachments, 97 history entries. The 10 4390 date cards (pre-extension dates) and ARA's 13 invoice / lease / PDA date cards weren't imported; their comments and attachments went onto the 4390 PSA and ARA "Contact List & Agreement" cards. **Kellen: upload the final ARA PDA** as a contract so its dates reach the calendar. Kimley Horn (engineer), Maryland Department of Commerce (agency) and Powers Brown (architect) added to the directory. **Next:** NSIH, General, Campus Pursuits, Capitalization, each with a mapping first.

**Trello history, Prospecting 2026-10-26** (Kellen: "pull all the comments and boards from trello"; merged fast-forward `195d697`, CI green, 718 tests, no migration; applied on the real database, backup `backups/db-2026-10-26-before-trello-prospecting.sqlite3`): Kellen ran `trello_fetch.py` (15 boards, full history, `data/imports/trello/2026-10-26/`). `manage.py import_trello <folder> [--apply]` (`crm/boards/trello_import.py`, dry run by default) put Properties-Prospecting onto the Tenant pipeline board: all 1,661 comments with their author and date (1,132 on 103 deal cards, also on the deal page; 361 on cards linked to a company, also on the company page; 168 on cards with no record), 200 checklists (564 items), 175 attachment links, 807 card-history entries, 12 labels, 260 cards for Trello cards with no deal on 15 unbound lists (New Leads, Cold, Partners, Outreach Summary / Admin open; Targets and the old lists archived). The CRM wins: deal cards keep their title, stage, description and dates; only labels and members were added. Outsiders (Joe Don Bowling 96, Olivia Jaskula-Dybka 30, four others) have their name at the start of the comment. Nobody was notified; a re-run adds nothing. 13 mirror cards skipped. **Next:** the other boards (both MEIH boards, NSIH, General, Campus Pursuits, Capitalization), each with a mapping for Kellen first; skipped boards are in `docs/TRELLO_MAPPING.md`.

**Fortress data room started 2026-10-26** (Kellen: "build out the room so it can be seen"): room 1 "Fortress" on the real database (backup `backups/db-2026-10-26-before-fortress-room.sqlite3`), active, costs shown, win % off, every campus, no partner firm (Fortress isn't in the directory). It shows all 27 investor-report deals; none has a partner note yet, no files, no partner invited. Manager page `/data-rooms/1/`, preview `/room/?room=1` (not logged); both render on the real data.

**Client searches 2026-10-26** (Kellen: "I can't delete a client search"; merged fast-forward `56d3693`, CI green, 712 tests, no migration): **Delete** on the search page and on each row of Client searches and a company's Site searches tab, for the search's creator or a Manager. Its reports go with it; the sites stay in Land. A **+ Land search** button in the company page header starts one for that company.

**David walkthrough fixes 2026-10-26** (Kellen: "start on bugs 1-9 from the David review in a worktree"; branch
`worktree-david-review-fixes`, migration `0067_assignment_company`. **Merged 2026-10-26** fast-forward `4101657`, CI
green, 710 tests; real DB migrated through 0067, backup `backups/db-2026-10-26-before-david-review-fixes.sqlite3`;
branch and worktree removed). From the walkthrough with David DeNardo on Oct 26:
- **Map fills the window** (`.land` was 100vh under the 52 px top bar, and the 1680 px page cap applied): no page
  scroll, nothing cut off at the bottom. Sticky bars that slid under the top bar now sit below it: company tabs,
  Sites compare bar, contract reader side panel, email template preview.
- **QD planner:** a parcel opened outside the loaded assemblage/site now wins (`planTarget()` in `land_search.html`;
  before, a site opened earlier caught the auto-fit, the "jumped to the airport side" bug). Clear also removes the
  saved plan (asks first) and can't be undone by a check still in flight; saving no buildings stores `{}`. Buildings
  take a length (width = area / length) and a turn in degrees (`qd.footprint`); auto-fit allows for the corners.
  New **Max NEWQD** column (`qd.max_newqd`): the most the building can hold where it stands, keeping its IBD arc on the
  property and its own separation to each neighbour, with a "use" button. Screening only, same as the rest.
- **Site plan:** re-runs on every edit (zoning numbers, program, parking, setback); typed zoning numbers stay as
  overrides until the zone changes (they used to be dropped after each run, so the next run lost them). The flood /
  wetland lookup now waits 20 s with no retries (it was 90 s x 3 tries per service) and is cached per outline for a day.
- **Client searches:** `Assignment.company` (the client name defaults to it). Pick the company on the list page or the
  details form; a company page has a **Site searches** tab (linked searches, plus older ones whose client name matches)
  with New site search. The Add a saved site picker is grouped by project.
- Checked in the browser on a scratch DB with made-up parcels; tests added in `test_land.py` and `test_assignments.py`.
- **Mail search** (`crm/mail/search.py`, Kellen: "needs to be much more robust"): plain words that name a company,
  directory firm or person also find mail from/to/cc their domains or addresses (Gmail `{...}` OR; a note says what was
  added, with "just the words"). Queries with Gmail operators pass through as typed. A ⚙ search-options panel (from,
  to, subject, words, doesn't have, dates, attachment, unread, this folder only) builds the operators. The search box
  suggests people as you type (picking one searches mail with them both ways).
- **To / Cc / Bcc autocomplete** (`static/crm/recipients.js`, `/inbox/suggest/`) on the compose page and the inline
  reply: teammates, CRM people, directory firm contacts and people who have written to you, by name or address.
  Inserts `Name <address>, `. Only addresses the hub knows; Google Contacts isn't read (no People API scope).
- Kellen's corrections to the review list: Raven Space's owner (David) is right; #15 (each person's own Claude) skipped.
- Not reproduced: Kellen's "can't see the bottom" on a card. The Sites card scrolls inside its dialog; most likely
  it was the map page overflow fixed above. Check on the laptop.

**Where things stand**
- `main` has everything below; the last code commit is `7a36c20` (browser notifications), pushed to `kmoczulski/acmi-crm`.
  515 tests pass and CI is green on Postgres. **Merged 2026-10-25** (fast-forward): PR #1 `boards-bound` and PR #2
  `data-room` (data room and Drive folders). The email client design is in its own session and branch
  (`email-client-design`, `docs/EMAIL_CLIENT.md`).

**Browser notifications 2026-10-25** (Kellen asked; PR #3 `web-push`, **merged** fast-forward `7a36c20`, CI green;
migration `0060_push_subscriptions`, the email client numbers from 0061). The real database is migrated (backup
`backups/db-2026-10-25-before-web-push.sqlite3`), VAPID keys are in the laptop's `.env` (old `.env` in
`backups/env-2026-10-25-before-vapid.bak`), and the new requirements are installed in `.venv`:
- Web Push (`crm/push.py`, `crm/push_views.py`, `static/crm/push.js`): the service worker is served at `/sw.js` (public
  path, but partner accounts are redirected away from it); a `PushSubscription` per person and browser; `notify.send`
  pushes after commit to every browser of that person for the kinds they left on. Clicking one opens the record
  (through `notification_open`, which marks it read). Gone subscriptions (404/410) are deleted. `pywebpush==2.5.0`.
- **Notification settings** page (`/notifications/settings/`, user menu and the Notifications page): a switch per
  kind for "In the hub" and "Browser" (`UserPrefs` "notify"; everything on by default), Turn on browser notifications
  (asks the browser), Send a test notification, Turn off on this device, and the list of browsers with Remove. A kind
  off in the hub is still saved (`Notification.in_hub=False`, read) so its key stops repeats, but never shows in the
  bell. New kind `mail` ("New email"): the email client calls `notify.new_mail(user, sender, subject, url, message_id)` from
  `crm.mail.client.on_new_mail`.
- `manage.py notify_sweep` runs the due and timed reminders for the whole team (`notify.sweep_everyone`), so they
  reach browsers with the hub closed. It rides with the 15-minute mail sync (`scripts/sync_mail.cmd`, `sync_all`, the
  cron endpoint), so a task's "15 minutes ahead" reminder can arrive up to 15 minutes late when nobody has the hub open.
- **To turn it on:** `manage.py vapid_keys` prints `ACMI_VAPID_PUBLIC_KEY` / `ACMI_VAPID_PRIVATE_KEY` for `.env`
  (and Secret Manager on Cloud Run); optional `ACMI_VAPID_SUBJECT` (default `https://hub.acmigroup.com`). Changing the
  keys later means everyone turns notifications on again. Install the new requirements first.
- **HTTPS only, except localhost:** works on the laptop at http://localhost:8000 now; on phones and other computers
  only once the hub is hosted on https (hub.acmigroup.com). iPhones need the hub added to the home screen first.
- Checked on a scratch database: the settings page, Save, the test button, the service worker registering at `/`,
  and a real encrypted push decrypted back to the alert. The built-in browser blocks notification permission, so the
  pop-up itself still needs one click-through in Chrome on the laptop.
- The real database (`db.sqlite3`, about 600 MB) is migrated through `0059_drive_folders` (2026-10-25). Latest backup:
  `backups/db-2026-10-25-before-boards-bound-data-room.sqlite3`. `boards_setup --apply` ran: the Tenant pipeline
  record board (8 stage lists, 110 deal cards) and the MEIH board (7 lists, 3 filled from records; Kellen, Simon,
  David, Dave). Both pages and the data room page render on the real data. No data room is started yet, no Drive
  folder is mapped, and the Drive service account isn't set up (`docs/DATA_ROOM.md`).
- `ANTHROPIC_API_KEY` is set in `.env`, so the Claude features run for real. `ACMI_TRANSCRIPT_DIR` is not set yet.
- Built since the audit, each with its own section below: roadmap phase 1 (GitHub and CI, the transcript archive, the
  campus summary with Claude's weekly draft, Cloud Run plumbing); contracts and critical dates, modeled on Notessa;
  due diligence and the closing checklist; the entitlement tracker; build records; tenant site fit, lease terms and
  the rent roll; flyer intake and landowner letters. `docs/ROADMAP.md` has the plan these came from.

**Merged 2026-10-19** (`1ea4ecb`, CI green, no migration): campus Edit button on the Projects page (a campus had no way to
rename it); a company's Opportunities tab shows deal details and an Add to / Remove from investor report button; the
investor report tables share one column grid; **Pursue on a parcel now only sets its status** (Kellen: landowners
don't belong in the CRM, so no company, note or task; the 6 companies and 7 tasks made earlier were deleted, backup
`backups/db-2026-10-19-before-landowner-cleanup.sqlite3`); "Add to project" on the parcel card and a Land sites panel
on the project page. Granola: the connector is on Granola's free plan, so transcripts aren't available, only notes
and summaries. Imported 2026-10-19: the last 30 days (60 meetings, Sep 21 to Oct 16) from `data/imports/granola/2026-10-19-b*.json`
(59 on the calendar; summaries condensed, Next Steps verbatim; backup `backups/db-2026-10-19-before-granola-import.sqlite3`).
No daily task yet. 25 attendee domains (Greystar, Fortress, Hoar, Walton, the county...) have no company, so those
meetings don't reach a company timeline. Also merged (`4721171`): tick sites on the Sites page and click Compare
selected; the briefs page has a Choose sites panel.

**Granola import 2026-10-21** (Kellen asked): Oct 16 to Oct 20, 7 meetings (6 new, Greystar refreshed) from
`data/imports/granola/2026-10-21.json`, full summaries; backup `backups/db-2026-10-21-before-granola-import.sqlite3`.
Booz Allen matched a company. Then (Kellen): `fortusfederal.us` added to Fortus Technologies (1781); Cozen O'Connor
added (1795, our outside counsel, Jonathan Ciriello; flagged Exclude; later the same day moved to the Directory, see "the directory" below); "MBI B101" added to
the ARA Facility project's keywords, so the Cozen call (and later MBI B101 mail) tags to ARA Facility / MEIH. Backups
`backups/db-2026-10-21-before-fortus-domain.sqlite3`, `...-before-cozen.sqlite3`. greystar.com and terracon.com still
have no company.

**UX overhaul 2026-10-21** (Kellen asked; branch `ux-overhaul`, details in `docs/UX_REVIEW.md`): CoStar-style top
navigation instead of the sidebar, search everywhere (Ctrl K and a big box on the dashboard), a + New menu, a
customizable dashboard (drag, resize, hide, add widgets; saved per person), a saved-views bar on every list, the
Investor report merged into one **Tenant pipeline** page (Board / Table / Report), Companies opening on Active, stage
and due-date colours, breadcrumbs, and duplicate due alerts fixed. Migration `0055_user_prefs` (UserPrefs table,
SavedView.object_type widened, unique keyed notifications after removing duplicates). **Merged 2026-10-21**
(`88ad5e0`, CI green, 446 tests) and migrated on the real database (it had no duplicate alerts, so nothing was
removed). Backup `backups/db-2026-10-21-before-ux-overhaul.sqlite3`.

**Tenant pipeline report columns and the Fortress export 2026-10-21** (Kellen asked): `crm/reports.py` holds one
column list for the report page, its print/PDF and the Excel download. **Columns** on the Report tab hides any of them
("Hide costs" drops cost, $ PSF and weighted; "Hide win %" drops Win % and weighted), saved per person
(`UserPrefs` "report_cols"); `?cols=a,b` gives a one-off link. Hidden columns are gone from all three, including the
section totals and the Sites sheet. The Excel now follows the Fortress tracker (G: `1 Projects/Fortress Pipeline
Meetings/2026.09.25/ACMI_Properties_Pipeline_Tracker.xlsx`): its headers (Campus / Project ... Lead / Source ...),
navy header rows, blue inputs and black formulas, SF / $ / PSF / mmm-yy formats, a Sites sheet with Location and
Status / Notes, landscape fit-to-width printing with repeated headers. Owner ("ACMI Lead") is off by default. The page
puts the three sections in one table so their columns line up. No migration. Formulas have no saved values until
Excel opens the file, so phone and email previews show those cells blank.

**The Forge lenders 2026-10-21** (Kellen asked): two lender calls that morning weren't on any lender page, because
neither bank was in the CRM (Shore United's 10 emails sat in Mail review; B&H's call was a forwarded invite, not on
the synced calendar). Added both to the directory as banks (Shore United with `shoreunitedbank.com` and 3 contacts,
which filed its 10 queued emails; B&H Bank with Jeff Rouse and Paul Covill, **domain unknown: add it on the firm
page**), and both as construction-loan sources on The Forge (B&H at Term sheet, $2M sought, terms in its notes; Shore
United In discussion). Imported both Granola notes from `data/imports/granola/2026-10-21-lenders.json`; Shore United's
had no Granola summary, so it carries B&H's next steps (Kellen: the same tasks for both). Four tasks for Kellen (9-12),
linked from both meetings. Shore United still shows a second calendar row for the same call (the bank's own invite,
different title). Backup `backups/db-2026-10-21-before-forge-lenders.sqlite3`.

**UX second pass 2026-10-21** (Kellen asked; run in a cloud session on a scratch database of made-up data at live scale,
nothing changed in the repo or the real database): 29 findings with screenshots, timings and an axe-core scan, as a web page
https://claude.ai/artifact/7MeJEVyTfnFBXXDgVefAtj. Headlines: the capture gap is structural (meetings link only to
Companies, never to directory firms; a bank's "medium" verdict never auto-files; search can't see the review queue), there
is no daily Granola path, same-time meetings only merge on title, drag-and-drop has no keyboard path, five pages repeat one
query per row, 11 phone pages render wider than the screen, and errors and setup messages are written for a developer.
The "Second pass" section for `docs/UX_REVIEW.md` and the screenshot set for `docs/ux-review-2/` are drafted and wait
for Kellen's answers to the eight scope questions in the page's last section. Fixes start after that, one branch per area.

**Boards, step 1 (`boards-core`) 2026-10-21** (Kellen: "go with the defaults and start boards-core"): Trello-style boards on
the CRM's records, built as designed in the last section of this file. Migration `0056_boards`. **Merged 2026-10-21** (CI
green, 470 tests) and **migrated on the real database** the same night on the laptop (backup
`backups/db-2026-10-21-before-boards.sqlite3`; the new tables start empty; the dashboard and Boards render). Details in
"Built 2026-10-21: boards-core" below. Kellen also took the defaults on the UX second pass's eight questions: its
"Second pass" section and screenshots are now in `docs/UX_REVIEW.md` and `docs/ux-review-2/`.

**Trello review 2026-10-22** (Kellen asked; read-only, nothing written to the database or Trello): Kellen's JSON
exports of Maryland / MEIH, Properties-Prospecting and Properties-General (the Trello connector didn't load in the
session) are converted to the `trello_fetch.py` layout in `data/imports/trello/2026-10-22/` (gitignored; the originals
are in `raw/`). The per-board mapping and the open decisions are in `docs/TRELLO_MAPPING.md`. **Trello's export keeps
only the newest 1,000 actions per board:** MEIH's 118 comments are complete, but Prospecting has 186 of 1,660 and
General 55 of 697, so run `trello_fetch.py` before importing those two. Mirror cards show at least one more board that
wasn't exported. Recommendation: `boards-bound` before `boards-import`. **Kellen decided the same day:** the CRM wins
wherever it and Trello disagree, but every Trello comment comes over with its author and date; one MEIH board (The
Forge and ARA Facility are lists on it; the layout and how attorney / designer work is tracked are in
`TRELLO_MAPPING.md`, "Decided"). **ARA merged** on the real database: "ARA (Applied Research Associates)" folded into
"Applied Research Associates" (backup `backups/db-2026-10-22-before-ara-merge.sqlite3`; 1,769 companies). The other
decisions in `TRELLO_MAPPING.md` are still open.

**Boards, step 2 (`boards-bound`) 2026-10-22, first part** (Kellen: "Yes", start it): bound and feed lists, on branch
`boards-bound` (not merged, not pushed; the real database is not migrated). Migration `0057_board_record_type`
(one blank column). Details in "Built 2026-10-22: boards-bound, part 1" at the end of this file. Still to do in this
step: the Tenant pipeline and Sites tabs drawn by the boards component, Lists → Boards with redirects, and the My
cards widget.

**Capital partner data room 2026-10-22** (Kellen: Fortress only; partners make an account and sign in; every deal
on the investor report with an on / off switch; win % off; files only when approved): built on branch `data-room`,
stacked on `boards-bound` (merge that first). Migration `0058_data_room`. Details in "Built 2026-10-22: data room" at
the end of this file; decisions in `docs/DATA_ROOM.md`. Partners can only reach it once the hub is on the internet.
**Drive folders 2026-10-25** (same branch, migration `0059_drive_folders`): a Drive files panel on company and project
pages (map the tenancy or project folder; current versions, archive skipped), "Share in data room" from there, newer
versions flagged with a swap, and the room serving Drive files through the hub. **Waiting on Kellen:** the Drive
service account (steps in `docs/DATA_ROOM.md`, "Setting up Drive access"); until then the panels say "Drive isn't
connected" and folders can still be mapped.

**Email in the hub, designed 2026-10-25** (Kellen asked; design only, no code): an Outlook-style mail client
(labels, threads, reading pane, compose / reply / forward, Gmail search, File to a record and Open in Mail back) over
the Gmail API with per-person sign-in (`gmail.modify`, Internal app so no Google verification), **optional per person**
(Kellen: "it shouldn't be required"). Design, build order and Kellen's
questions are in `docs/EMAIL_CLIENT.md`. Kellen took all nine recommended answers and added a long-thread reading
pane (newest first, one attachments panel with versions grouped, outline, find in thread; step 3b). PRs #1 and #2 merged 2026-10-25.
**Step 1 (`mail-connect`) merged 2026-10-25** (PR #4, `eb7ca51`, CI green) and **migrated on the real database**
(backup `backups/db-2026-10-25-before-mail-connect.sqlite3`). `ACMI_TOKEN_KEY` is in `.env` (generated, never printed;
`.env` backed up first to `backups/env-2026-10-25-before-token-key.bak`), so the stored Google token is now
encrypted. **Kellen: copy the key into the password manager.** Mail can't be turned on until Google is set up (the
"Before Mail can be turned on" list in "Built 2026-10-25: mail-connect"). **Kellen turned Mail on the same night**
(connected, no error). **Step 2 (`mail-api-reader`) merged 2026-10-26** (PR #5, `3b32a33`, CI green), migrated
(backup `backups/db-2026-10-26-before-mail-api-reader.sqlite3`), and **Kellen's mailbox now syncs through the Gmail
API** (first run 9:17: 10 messages, no error, no duplicates). **Step 3 (`mail-inbox`)**: the inbox on screen,
see "Built 2026-10-26: mail-inbox" at the end of this file (merged `a6bd5e9`, migrated, backup
`backups/db-2026-10-26-before-mail-inbox.sqlite3`). **Step 3b (`mail-long-threads`) merged 2026-10-26** (PR #7, `aadf190`,
no migration): "Built 2026-10-26: mail-long-threads". The 10:17 sync finished the inbox fill (303 inbox threads
indexed, no error) and sent 6 new-mail alerts. **Step 4 (`mail-actions`) merged 2026-10-26** (PR #8, `bf602f0`, migrated, backup
`backups/db-2026-10-26-before-mail-actions.sqlite3`): "Built 2026-10-26: mail-actions". **Step 5
(`mail-compose`) merged 2026-10-26** (PR #9, `ca4bb4f`, no migration): "Built 2026-10-26: mail-compose" (Kellen, 2026-10-26: "I can't respond to an email?"). **Inline reply and the
Gmail signature** merged (PR #10). **Step 6 (`mail-filing`) merged 2026-10-26** (PR #11, `b5cd494`, migrated,
backup `backups/db-2026-10-26-before-mail-filing.sqlite3`): "Built 2026-10-26: mail-filing". The 11:47 sync put CRM
labels on 24 of Kellen's threads (12 labels, e.g. ACMI/Projects/MEIH, ACMI/Tenants/RTX; no error). **Step 7
(`mail-shared`) merged 2026-10-26** (PR #12, `90f077f`, migrated, backup
`backups/db-2026-10-26-before-mail-shared.sqlite3`): "Built 2026-10-26: mail-shared". No shared mailbox connected yet:
the redirect URI has to be added in Google Cloud first. **Step 8
(`mail-outreach-api`) merged 2026-10-26** (PR #13, `8e222bd`, no migration): "Built 2026-10-26: mail-outreach-api". Left: step 9, hosting (billing).

**Next, in order**
1. **Granola (time-sensitive: Granola only keeps 30 days).**
   - Kellen connects the Granola connector in Claude (Settings › Connectors). Its tools are `list_meetings`,
     `get_meetings`, `get_meeting_transcript` and `query_granola`.
   - Then backfill the last 30 days into the CRM and set up a daily scheduled task. The task pulls Granola meetings
     and transcripts into `data/imports/granola/<date>.json` (the format is at the top of `crm/meetings.py`), then
     runs `manage.py import_meetings <file>` and `manage.py export_transcripts`. Details are under "roadmap phase 1" ›
     Transcript archive.
   - **The daily task, and so the Granola connector it uses, goes on Kellen's acmiproperties Claude account**, not the
     account these Code sessions use (Kellen, 2026-10-21), so don't set it up from here. The import writes the real
     database on the laptop, so the task has to run where it can reach this folder (`data/imports/granola/`, `.venv`).
   - A direct sync from the CRM, with no Claude in the middle, needs the Granola API, which needs Granola's Business
     or Enterprise plan ([docs](https://docs.granola.ai/help-center/sharing/integrations/granola-api)). That's
     Kellen's call.
2. **Trello boards:** the import is done (2026-10-27; boards-core, boards-bound and boards-import merged). What's left is `boards-polish` (design at the end of this file). To pick up later Trello activity, re-run `scripts/trello_fetch.py` at the laptop and `import_trello <new folder> --board <name>` for each board; it only adds what's new. Attribution is by full name, never `pipeline_import.TEAM` (its old lowercase usernames would make duplicate users).
3. **UX second pass fixes** (`docs/UX_REVIEW.md`, "Second pass"; Kellen took the defaults): one branch per area in this
   order: capture gaps and Granola; duplicates and undo; accessibility, phone, states and wording; roles, outside-facing
   output, rough edges.
4. **Sale comps per area, and off-market signals** (long holds, estates, tax-delinquent where counties publish it):
   the rest of roadmap item 7. Built on branch `land-sales` (2026-10-26, see its section at the end); merge, migrate
   and run `refresh_sales` on the real database.
5. **Owner-data gaps:** Charles mailing addresses and values come from the statewide MD layer on branch `land-sales`
   (`refresh_sales Charles`). Hays market value stays blank where Hays CAD has none (don't derive it; land value is on
   99%). This matters for landowner letters: an owner with no mailing address can't be written to.
6. **Go-live once billing is on:** Cloud Run jobs and Scheduler, Cloud Storage, hub.acmigroup.com (`docs/ROADMAP.md`
   section 1).

**Decided; don't reopen**
- The pro forma stays in Excel, separate from the CRM. Never build or propose one.
- Investor updates are skipped for now.
- The contracts module emulates Notessa (notessa.ai): cited answers, the amendment chain, defined terms, templates.

**Waiting on Kellen**
- Connect Granola and set up the daily task, on the acmiproperties Claude account (above).
- Who "Dave" is in the tracker's Lead column: 9 deals are owned by David DeNardo and 1 by David Dowell, because the
  Oct 14 import read "Dave" as DeNardo. The hub's `Dave` account is David Dowell.
- Confirm who stays a Manager (Admin › Groups). Everyone with an account is one today.
- Pick a restricted shared-drive folder for `ACMI_TRANSCRIPT_DIR`. Transcripts can hold NDA material, and Maryland
  needs all-party consent to record.
- Set a retention rule for `backups/`: 32 full copies, about 15 GB (2026-10-21).
- Delete the old Gmail app password in Google Account › Security › App passwords (it's out of the hub's `.env` since
  2026-10-26), and turn on device encryption on the laptop. (Google tokens are encrypted at rest
  since 2026-10-25; copy `ACMI_TOKEN_KEY` from `.env` into the password manager.)
- Turn billing on in Google Cloud so the app can be deployed.
- Read the built-in landowner letters (`BUILTINS` in `crm/land/letters.py`) before the first mailing. They describe
  ACMI as developing "industrial campuses for advanced manufacturing companies" and don't name defense or
  energetics.
- The first real flyer upload is the first live test of Claude reading a flyer (the tests mock the API).
- From earlier and still unconfirmed: the Attio trial syncs Kellen's mailbox and calendar (turn it off?), and what
  "the project" Kellen created on 2026-10-13 was.

**How to work in this repo**
- **Code changes go in a worktree.** The Windows task "ACMI CRM mail sync" runs the main folder's code against the
  real database every 15 minutes. Work on a branch in `.claude/worktrees/<name>`, never in the main folder.
  Docs-only notes like this one can go straight on main.
- **Commit, CI, merge:**
  1. Commit as Kellen (`git -c user.name="Kellen Moczulski" -c user.email="kmoczulski@acmigroup.com" commit ...`)
     with a `Co-Authored-By: Claude ...` line.
  2. Push the branch and wait for CI (`gh run watch <id> --repo kmoczulski/acmi-crm --exit-status`).
  3. On main, `git merge --ff-only`, then push.
  4. Remove the worktree and the branch, locally and on GitHub.
- **Before a migration or any data write on the real database,** back it up with SQLite's backup API, which is safe
  while the mail sync writes:
  ```
  .venv/Scripts/python.exe -c "import sqlite3; s = sqlite3.connect('file:db.sqlite3?mode=ro', uri=True); d = sqlite3.connect('backups/db-YYYY-MM-DD-before-WHAT.sqlite3'); s.backup(d); d.close(); s.close()"
  ```
  Then run `manage.py migrate crm` and add a "Merged" line to the module's section here.
- **Tests:** `.venv/Scripts/python.exe manage.py test crm` takes about a minute. The tests mock the Anthropic client.
  In a test, read a `FileResponse` out (`b"".join(r.streaming_content)`); calling `.close()` on it ends the Postgres
  connection in CI.
- **Standing rules:**
  - Query the real database read-only, and report counts, not contents.
  - Never print `.env` values.
  - Every report carries the ACMI logo (`crm/partials/report_logo.html`).
  - Transcripts are Managers-only.
  - Writes to live data, and anything that goes out (email, posts), wait for Kellen's OK.
  - Say what a `db.sqlite3` file is before deleting it.
  - Keep tool installs out of `.venv`.
- **Browser checks:** point a throwaway settings file at a scratch database with made-up data (plus a middleware that
  signs a demo user in), add a temporary `.claude/launch.json` entry, and remove all of it afterwards. Templates only
  reload when the server restarts under `--noreload`. Download buttons (PDF, CSV) are easier to check with Django's
  test client than in the browser.
- **Claude in the app** (`crm/contracts/ai.py` is the model to copy):
  - Model `claude-opus-5-5`, streamed, with `get_final_message()`.
  - Effort is set through `output_config` (medium).
  - The server-side refusal fallback beta is on.
  - Citations don't mix with structured outputs, so structured calls drop citations and anchor their quotes in code.

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
| Granola (phase 3) | Granola API if the plan has one; otherwise a daily scheduled Claude task pushes meetings in. 2026-10-18: the API needs Granola Business or Enterprise, so for now it's the Claude connector plus a daily task (waiting on Kellen to connect it) |
| Land search (phase 4) | **Built 2026-10-14.** Texas Triangle (suburban/rural counties, not the big metros) + Charles Co. MD. Parcels are a local copy (state publishes once a year); utilities, flood, wetlands, HUBZone, EPA are live per parcel |
| Address | **hub.acmigroup.com** (chosen 2026-10-14); needs a DNS record, the Cloud Run domain mapping, and the URL added to the OAuth client + `DJANGO_ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` |
| Go-live | In progress 2026-10-14: gcloud + gh signed in; project is under the acmigroup.com org but **billing is off** (no billing account; budget doc `docs/ACMI_CRM_Hosting_Budget.docx` written for team approval, ~$30-45/mo). Prod edits made (WhiteNoise, Dockerfile, cloudbuild.yaml, cron endpoint also syncs calendar). Since 2026-10-16 the code is on GitHub with CI; as of 2026-10-18 **nothing is deployed** (billing still off) |
| Attio | Used as a design reference only (trial workspace `acmi-properties`). Copy behaviour, never code |
| Pro forma | Stays in Excel, separate from the CRM (2026-10-18); the CRM's build records stay at the summary level |
| Contracts | Built inside the CRM, modeled on Notessa (2026-10-16), instead of the separate tool deferred earlier |
| Investor updates | Skipped for now (2026-10-18) |

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
- GitHub CLI (`gh`) and the Google Cloud CLI are installed and signed in (2026-10-14).

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
20%+ wetlands, Superfund within 0.5 mi, 30%+ Critical Area RCA. (A tax-exempt / public owner is a warning, not a
deal-killer; since 2026-10-16 federal owners are held at 40 and other public owners lose 5: `score.public_owner`.)
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

## Built 2026-10-14 (late night): MEIH land pipeline imported from the shared drive

- Source: `G:/Shared drives/1 ACMI Properties/1 Projects/MD/MEIH/01. Land Acquisition` (Aug 2026 "Maryland Land"
  deck, Land Research 2026.07.10, Greer land list, Assemblage 22 review, Outside-the-Gate sheet, deal folders).
  Read only; nothing written to the drive.
- `data/imports/meih_land_sites.json` (gitignored) holds 28 sites with their Charles County SDAT accounts, status and
  notes; `manage.py import_land_sites <json>` creates/updates them by name (re-runnable) under campus MEIH.
  `crm/land/parcels.ensure_parcels` fetches any account from the county service, any size (105 parcels; many under
  20 ac). `normalize_account` turns 906022677 / 06022677 / 7043589 / 09-003134 into 10-digit SDAT ids.
- Statuses: 4390 Indian Head Hwy (ETC) under contract; LOI/negotiating: Lake Acton (PSA agreed 10/14, Phase I 15.33 +
  Phase II 47.86 ac of CRIP's 5 parcels), Livingston Rd airport land, Lewis Place, Rosewick, Billingsley WCD
  (Assemblage 22, 10 parcels), Outside the Gate (8 tiny parcels), 12 Dogwood, Berry/Tara, Garner (stale? not in Aug
  deck); review: Ray Dr, PSM Pomfret Rd, Hughesville; outreach: Copart/Fox Tail, Rockhill (Prince George's, no
  shape: PG parcels not loaded), Civista, DeMarr IG, Rosewick East, Bardon, Pavlo, Howard/Santay; dropped: the passes.
  Parcels in those sites got map status Pursue / Watch / Pass (only where unreviewed; no companies or tasks made).
- GGCAL's Land Research ids were re-platted (now 0906360616–23). Hughesville's Lieberman file says 09-00544, matched to
  0909005455: confirm. Newburg folder has only a map (no site made).
- Map: new "Saved sites & assemblages" layer (outlines by status, click to open); `/land/sites.geojson`.
- Charles loader now skips shapes without an account ("NO ID", e.g. an 11,475 ac private right-of-way).
- **Shared-shape records merged (fixed 2026-10-14):** some Texas appraisal files give several property ids the same
  polygon and GIS_AREA (Hays "HAY HILL HAY LLC": 11 ids on one 3,654.87 ac shape, legal acres 0–3,236). The Texas
  loader now spots kept records with the same shape (rounded bounds + area, `_shape_sig`) and stores ONE parcel:
  key = the id with team work on it, else the largest legal area; mapped acres = the shape's; deed acres = that id's;
  legal_desc ends "[same shape as prop ids …; also owned by …]". Owner totals count a shared shape once per owner.
  The other ids are pruned like any vanished parcel (never ones with status/notes/landowner/site/QD plan). Re-loaded
  all six TX counties: Hays 4,150 -> 2,915 parcels (1,235 ids merged), Blanco 1; no two parcels share a shape now.
  Searching a merged-away prop id finds the parcel via legal_desc. Backup: `backups/db-2026-10-14-before-shared-shape-merge.sqlite3`.
  Loader also now closes the shapefile reader (temp dirs were left behind on Windows) and prune keeps QD plans.
- **Known data issue (not fixed):** a few Caldwell/Guadalupe parcels are unique shapes whose mapped acres dwarf their
  deed acres (Caldwell 51626 MARTINEZ TERESA ESTATE 4,727 ac mapped vs 126 deed; 51677/51711 WELLS EVELYN; Guadalupe
  167688 FREUDIGER 5,674 vs 198, 167685 VERDEJA "8.34% UDI"). Not shared shapes, so the merge doesn't touch them;
  likely oversized/undivided-interest polygons in the CAD file. Treat mapped acres there with suspicion.
- 119 tests.

## Built 2026-10-14/15 (overnight): incentives, Sites page + deal timelines, quick site planner

- **Incentive programs** (`crm/land/incentives.py`, one registry): Opportunity Zones (HUD), OZ 2.0 eligible tracts,
  NMTC tracts (CDFI 2025), HUBZones (map; card check stays live SBA), Foreign-Trade Zones (points + service areas),
  MD: Priority Funding Areas (statewide, mdgeodata), Enterprise Zones (+ admin contacts), Sustainable Communities,
  RISE zones, Charles Development Districts; TX Enterprise Zones (Governor's office, 2020 block groups). Each has a
  short what / how-we'd-use-it / source / official link: Layers menu "Incentives & programs" (ⓘ), map popups and a
  parcel-card "Incentive programs" section (live check `incentives`). Score: up to +5 (OZ 3, NMTC 2, EZ 2, FTZ
  covering the county 1, SC 1, RISE 1); PFA stays in the entitlement score. Links checked 2026-10-14; Maryland
  pages had moved (now planning.maryland.gov/Pages/OurProducts/pfaMap.aspx, commerce .../enterprise-zone-tax-credit-ez,
  dhcd .../neighborhood-revitalization/sustainable-communities). BRAC zones skipped (none near Charles).
- **FEMA renumbered NFHL** (Flood Hazard Zones 28 → 35) mid-session: `sources.fema_flood_layer_id()` now finds it by
  name (cached 6 h); the map layer uses the same id. ArcGIS bbox queries now send a JSON envelope (FEMA rejects the
  "x,y,x,y" form).
- **Sites page** (`/land/sites/`, sidebar Land search › Sites): campus → project groups sorted by deal stage
  (`LandSite.STATUS_ORDER`: acquired, contract, LOI, outreach, review, prospect, dropped), totals, owners, zoning,
  best score, flags; **Deal timelines** chart (dots per milestone, bar to today). Projects page and the map's site
  picker sort by status too.
- **Site page** (`/land/sites/<id>/manage/`): edit name/status/campus/project/notes, milestone dates
  (`LandSite.MILESTONES`: identified, contacted, LOI sent/signed, PSA sent/signed, feasibility end, approvals,
  closed; later milestones move the status forward, never back; dropped stays dropped), stage track with days
  between steps, parcels (remove), activity log (`LandSiteEvent`: status, milestone, edit, note). Groundwork for the
  Contract negotiations module.
- MEIH dates seeded from file names (e.g. Livingston LOI 1/23 → signed 5/6 → PSA draft 7/31; ETC PSA 6/22; Lake Acton
  LOI 4/7–4/10, PSA draft 5/18). `import_land_sites` is now non-destructive: existing sites only gain missing parcels
  and dates; `--overwrite` replaces. Garner marked Dropped (seller passed, Kellen). Public owners are a warning, not a
  deal-killer (counties sell land).
- **Quick site planner** (`crm/land/siteplan.py`, `/land/siteplan/`, map toolbar "Site plan" drawer): zone picker
  ("As zoned now" or "Rezone to …"), editable setbacks / FAR / impervious / coverage / height, building program
  (warehouse, manufacturing, flex/R&D, office, lab/energetics: count or target SF, depth, stories), parking either
  stalls per 1,000 sf (typical market ratios, editable) or the Charles code formula (1 / 300 sf office + 1 / employee
  at max shift; office % and staff per 1,000 sf editable), optional floodplain + wetland exclusion (live FEMA/NWI),
  auto layout in rows (buildings + truck court/parking bands, 60 ft between buildings) stopping at the zone's FAR /
  coverage / ISR, yield summary; plans save on the site or parcel (`site_plan`).
  Charles standards transcribed from Ch. 297 Figs. VI-1/2/5/6/10, VII-4 and parking Fig. XX-1 (Supp 15, Jul 2025;
  PDFs from eCode360). Texas parcels default to an editable "no zoning" set. Examples: Billingsley (Assemblage 22)
  as WCD ≈ 1.7M sf (8% ISR binds) vs rezoned IG ≈ 11.2M sf with flood/wetlands kept clear; Lewis Place WCD 216K sf
  vs IG 2.6M sf.
- Third-party tools Kellen listed (TestFit, Forma, InQI, mnml, Aino, ARCHITEChTURES) are paid products without open
  feeds; the planner above is our own TestFit-style screen. Not built: drag-to-edit buildings, financial pro forma
  in the planner, bufferyards between zones (Fig. XXII) and road-corridor buffers (Fig. X-1).
- 128 tests.

## Built 2026-10-15: site archive, per-parcel deal tracking

- **Archive:** dropped sites leave the campus/project groups and sit in a collapsed Archive at the bottom of the
  Sites page (date dropped, why), the Projects page (per campus) and the map's Saved sites picker.
- **Per-parcel deals** (`SiteParcelDeal`, `crm/land/tracker.py`): every parcel in a site has its own status,
  milestone dates and notes; edit from the site page grid (dialog, "apply to all parcels of this owner" on by
  default; dates move a parcel's status forward, never back; logged as `parcel` events). Single-owner sites copy
  the site's status/dates into their parcels; multi-owner parcels start at Under review.
- **Global tracker** on the site page: acres-by-stage bar, secured acres (contract + acquired), furthest / slowest
  parcel, a hint when the site status is ahead of its slowest parcel (an assemblage is only as far along as the last
  owner), and the owner-grouped parcel × milestone grid with first–last dates per milestone. Sites page shows a
  mini progress bar for multi-owner sites and small per-parcel dots on the Deal timelines chart.
- Seeded from the files (import JSON `parcel_deals`, non-destructive): Outside the Gate (Wamsley LOI/term sheet,
  Kroc LOI → PSA 8/21, Focus On LOI → PSA 7/24, Infinite Design outreach), Billingsley (Wald, Walton, Filiaggi LOIs
  9/28; other 7 parcels under review), Berry/Tara (Chestnut + Tara Jenkins negotiating, 5 contacting).
- **Site page mini map** (`/land/sites/<pk>/manage/`, under the stage track): pan/zoom, base map switch, Fit to
  site, the full layers list (incl. incentive programs) — no tools. Parcels colored by their per-parcel deal stage,
  yellow dashed outline around the whole site. Base + layer picks remembered per browser (`site-map:*`).
  Layer factories now live in `crm/static/crm/land_layers.js` (`window.LandLayers`), shared with land search —
  add new overlays there (and to `LABELS`) so both maps get them.
- 132 tests.

## Built 2026-10-15: exclude and trim parcels in a site

- **Exclude** (site page Parcels table): the parcel stays on the site, greyed, with its deal history, but counts for
  nothing — acres, outline, Saved sites layer, QD, site plan, tracker rollup. Include brings it back. Saving the
  site from the map keeps excluded parcels (the map only selects included ones; `_site_json` lists `excluded`).
- **Trim** (`SiteParcelDeal.portion` GeoJSON + `portion_acres` + `portion_cuts`; `crm/land/trim.py`; endpoint
  `land/sites/<pk>/trim/`): "✂ Trim" on a parcel row or the map popup puts the mini map in cut mode — click along
  the trail/creek/road (start and end outside the parcel; lines are stretched 60 m past the end clicks), double-click
  to finish, more lines cut again. Pieces touching the rest of the site are kept by default (green); click a piece
  to flip it; Save portion / Whole parcel / Cancel. Portion acres are scaled to the tax-roll acreage. Everything
  that measures a site (`trim.site_geometry`, `trim.site_acres`, `SiteParcelDeal.acres`) uses the portion.
- For Billingsley: Kellen draws the trail cut on Cottrell Adam (0906035698) himself; verified with a test line, then
  reset (site back to 813.4 ac).
- 140 tests (incl. the Huntsville session's).

## Built 2026-10-15: Huntsville, AL counties (from Initial Land Research Overview - 2026.07.09.pdf)

- **Counties:** Limestone (01083), Madison (01089), Morgan (01103), region "North Alabama (Huntsville)".
  `load_parcels Limestone_AL Madison_AL Morgan_AL` (bare "Limestone" / "Madison" are still the Texas counties;
  `region_for(name, state)` is now state-aware). ~4 min for all three; live ArcGIS services, no download.
- **Sources:** Limestone and Morgan from the City of Huntsville's `Boundaries/CombinedParcels` (layers 7, 10). The
  county's own Limestone layer leaves out Huntsville-annexed land (the whole Greenbrier / I-565 corridor), so don't
  switch back. Madison from the City of Madison's copy (has acres and values). Parcel keys are digits only.
- **Zoning:** City of Huntsville `Zoning/ZoningDistricts/5` stamped on every parcel (covers its annexed Limestone
  land: PI, CIP, AIP...). Codes → buckets in `score.HUNTSVILLE_CODES` (M, M2, HM, AC, DS left unmapped: names not
  confirmed). Decatur, Athens, Madison city and the towns publish no zoning service (Decatur only via MapGeo), so
  those parcels show their city from Census TIGER places (`town_al` layer). Unincorporated AL = "No zoning" (+5,
  with the annexation-for-utilities caveat from the PDF).
- **Land use:** Alabama assessment class (1 utility, 2 commercial/other, 3 farm/forest/homes) → TX-style codes.
- `load_land_layers --region north_al` refreshes one region only (others kept). Electric providers and
  substations now include AL/TN (TVA distributors). Map: Huntsville zoning in the Zoning layer, AL parcel lines,
  AL city limits.
- Checked against the PDF: the top tracts line up (9000 Greenbrier Pkwy PI 578 ac, 27341 Old Hwy 20 PI 518,
  Powell Rd PI, 2522 County Line Rd PI 422, 7049 Greenbrier Pkwy CIP). 84 industrial-zoned tracts of 100+ ac.
- No site-plan standards for Huntsville codes yet (planner falls back to generic setbacks).

## Built 2026-10-15: client land searches (Assignments) and the guest view

For land-search engagements (Elbit 26 Defense, Niron). Modeled on the Word reports and `Site_Assessment_Elbit_2026.09.17.xlsm`
in the shared drive (Tenancy > Elbit 26 Defense > Site Assessment).

- `Assignment` = one client search; `AssignmentSite` = a saved `LandSite` inside it plus its diligence report (JSON).
  Migration 0029. Logic in `crm/land/assignments.py` (report structure, ranking, guest lookup), views in
  `crm/land/assignment_views.py`. Nav: Development > Land search > Client searches.
- Report = ten rated categories (Low Risk / Moderate / Elevated / N/A), executive summary, priority actions, overview
  (prefilled from parcel data), net acreage, utilities gap table, permit matrix, critical dates, ROM to pad-ready,
  44-item master checklist. Edit page: `/land/assignments/<id>/sites/<entry>/edit/`.
- Ranking is the workbook's rule: Elevated sorts last, then fewest Moderates, then fewer deadline-critical Moderates,
  then ROM net of "Yes" incentive-eligible lines (lower first), then acreage headroom over the program floor. Staff can pin a
  site to a position. The guest page states the rule and says it is judgement, not a calculation.
- Guest view: `/g/<token>/` (ranking + comparative matrix) and `/g/<token>/<entry>/` (report). No login; `/g/` is in
  `PUBLIC_PREFIXES`. Token is `secrets.token_urlsafe(24)`; link can be switched off, expired, or regenerated; each site has a
  "show to client" flag; responses are `noindex`/`no-store`. Guests never see owner names, site notes, deal status/milestones or
  the buy-box score (tests assert this). Only entries of that assignment are reachable.
- Not built: Word/PDF export (print-to-PDF works from the guest page), passcode on the link, per-client branding, a
  "refresh from CRM" button (overview fields left blank fall back to live CRM data automatically).

## Backlog

1. ~~AI summaries: need an Anthropic API key in .env.~~ The key is set (checked 2026-10-18).
2. Never sent a real batch from the CRM yet: first run = test send, then a small batch.
3. Land search next: more Texas Triangle counties (`load_parcels <names>`, ~1 min each for rural ones); zoning
   maps for Texas cities and other MD counties; directional ECM arcs; QD building rotation and custom footprints;
   hosted version needs the parcel table in Postgres (PostGIS optional) and the Valhalla server choice revisited
   (self-host or a paid router for heavy use).
4. ~~**After the CRM is final:** separate project-management tool for contract
   negotiations (not part of the CRM).~~ Superseded: Kellen chose to build contracts inside the CRM (2026-10-16).

## Open items

The current list is under "Pick up here" at the top (2026-10-21).

## Built 2026-10-16: Fayetteville, NC (Cumberland County)

- **County:** Cumberland (37051), region "Fayetteville, NC". `load_parcels Cumberland_NC` (~2 min; live service).
  Aliases "fayetteville" / "cumberland nc". Loaded locally: 3,082 tracts of 20+ ac, 406 inside city limits.
  Fort Liberty also touches Hoke (093), Harnett (085) and Moore (125): not added yet.
- **Source:** NC OneMap statewide parcels (`NC1Map_Parcels/FeatureServer/1`), filtered by the new per-county
  `base_where` config key (`counties.in_county` scopes the count, big-parcel and ensure_parcels queries). Adding
  another NC county is one more `COUNTIES` entry with a different `cntyname`.
- **Gotchas:** the layer's `gisacres` is junk (square degrees); acres come from `recareano`, falling back to
  `Shape__Area`/43,560. ~10% of parcels have no PIN: they're keyed `OID<objectid>` (not stable across reloads).
  No tax-roll year is exposed (`tax_year` blank).
- **Zoning:** none yet. No public Fayetteville / Cumberland zoning service found (the city and county GIS hosts I
  tried don't answer), so parcels show "town" inside Census city limits (`town_nc` layer, region `fayetteville_nc`)
  and no zoning elsewhere. NC is not in `NO_COUNTY_ZONING` since NC counties do zone.
- `load_land_layers --region fayetteville_nc` refreshes this region only. Substations now include NC.
- **Added same day:** Hoke (37093), Harnett (37085), Moore (37125) via `_nc_county()`: `load_parcels Hoke_NC Harnett_NC Moore_NC`
  (bare "Moore" is still the Texas county). Region layers now span the four counties.

## Built 2026-10-16: Arizona I-10 corridor (Maricopa, Pinal) and Greene County, IN

For three listings (Western Land Co. Wintersburg Rd & I-10, Tonopah = Maricopa; Land Advisors Barnes Farm, Casa
Grande and I-10/I-8 Business Park, Eloy = Pinal) plus Greene County, IN (NSWC Crane).

- **Maricopa (04013):** County Assessor service (`gis.mcassessor.maricopa.gov/.../Parcels/MapServer/0`): owner,
  mailing address, full cash value, use code. 1.76M parcels, so owner totals count only parcels of 1+ acre
  (`count_where`; `total_parcels` = that count, 164k). Acres = `LAND_SIZE`/43,560. `CITY_ZONING` is almost always
  "CONTACT LOCAL JURISDICTION", so no zoning overlay. PUC → land use only checked on the first digits (see
  `_az_land_use`). Loaded: 11,268 tracts.
- **Pinal (04021):** county's own service (`gismaps.pinalcountyaz.gov/webapps/...Parcels_Webservice`) did not
  resolve, so this uses ADWR's copy (`azwatermaps.azwater.gov/.../General/Parcels/MapServer/11`): owner, APN, acres,
  site address only (no mailing address or values). Parcels with no APN (State Trust / BLM / tribal) are skipped.
  Loaded: 7,377 tracts. Swap the URL + `_pinal_fields` if the county service comes back.
- **APNs:** keys are letters+digits without dashes ('506-45-029H' → `50645029H`); `normalize_account` handles AZ.
- Brochure parcels found: Wintersburg 50645028 (160 ac), 029H (49), 029J (50) = 313 Wintersburg LLC (029Q/R/S under
  20 ac, not loaded); Barnes 50326020E (141.5), 020F (180) = Barnes Vernon Lester (020D under 20 ac).
- **Greene, IN (18055):** IndianaMap statewide layer (`gisdata.in.gov/.../Parcel_Boundaries_of_Indiana_Current`)
  filtered by `county_fips`. It has NO owner names, acreage or values: acres come from the geometry, owner / value
  stay blank. Needs a county source (WTHGIS viewer `greenein.wthgis.com` has no REST service). 4,545 tracts.
  DLGF class → land use is by hundreds digit (mostly blank for Greene).
- Regions `central_az`, `crane_in`; `load_land_layers --region central_az|crane_in`. Zoning: none for any of these.

### Owner-data status (2026-10-16)

| County | Owner | Mailing | Source note |
|---|---|---|---|
| Cumberland, Harnett, Moore NC; Maricopa AZ | 98-99% | 96-99% | county assessor data, fine |
| Pinal AZ | 87% | 24% | ADWR layer enriched from the Casa Grande mirror of the assessor roll (`PINAL_ENRICH`, covers Casa Grande / Eloy / Coolidge only: owners, mailing, land value). The rest of Pinal (east side) has owner only where ADWR has it. County's own host `gismaps.pinalcountyaz.gov` is dead (DNS). |
| Hoke NC | 0% | 0% | NC OneMap's Hoke records have the owner and mailing fields blank (the county withholds them); no open Hoke service found |
| Greene IN | 0% | 0% | IndianaMap carries no owner data; county viewer `greenein.wthgis.com` has no REST service |

- Loader now supports `enrich` in a county config: a second service whose records fill blanks and add parcels the base
  layer lacks; owner totals use the enriched owners (`parcels._enrichment_records`).
- Hoke / Greene / east Pinal owner data needs the assessor's bulk file or a paid parcel dataset (Regrid, Dynamo Spatial,
  Land Records); a file could be joined by parcel number with a small `enrich`-style loader.
- **Update 2026-10-26 (branch `land-sales`):** real-DB coverage before it (owner / mailing / market / land value):
  Charles 2148 / 1409 / 0 / 0, Hays 2897 / 2841 / 1237 / 2895, the rest fine. `refresh_sales Charles` fills Charles's
  mailing and values from the statewide MD layer (2,460 of 2,465 of its 16+ acre records have a whole mailing
  address, 2,359 a value). Hays's blank market values are blank at Hays CAD too and are left blank (its market figure
  isn't land + improvements, so it can't be derived).

### load_owners: join an owner file onto loaded parcels (2026-10-16)

`manage.py load_owners <County_ST> <file> [--map field=Column ...] [--commit]` (code: `crm/land/owners.py`).
Dry run by default (prints columns used, match rate, samples). Reads .csv / .xlsx / HTML-table .xls (what older
viewers like WTHGIS export) / .json / .geojson. Recognises Regrid's names (`parcelnumb`, `owner`, `mailadd`,
`mail_city`, `mail_state2`, `mail_zip`, `parval`, `ll_gisacre`...) and common assessor headers; use `--map` otherwise.
Matches by parcel number ignoring punctuation / leading zeros against each parcel's key, prop_id and geo_id. Blanks
never overwrite; fills owner, mailing, values, owner portfolio totals; re-scores. `load_parcels` now keeps owner data
a file added (blank service values don't wipe it). Needed for Hoke NC, Greene IN, east Pinal AZ.
Greene IN's viewer (`greenein.wthgis.com`) has no REST API, only search + an Excel export button.
- **Greene IN owners solved (2026-10-16):** the WTHGIS viewer's Index > Parcels2 pages the whole county table
  (26,550 parcels: owner, mailing + property address, acreage, assessment class, via `tgis/index.ashx?action=getFtrs`).
  `scripts/fetch_greene_in_owners.py` pulls it politely (~130 requests) into `data/land/raw/greene_in_owners.csv`;
  then `load_owners Greene_IN <csv> --commit`. 4,494 / 4,545 parcels matched (99%). No land/improvement values in that
  table (Parcels1 has a few). Re-run both commands to refresh. Other WTHGIS counties work the same way (change host/dsid).
- **Hoke NC owners solved (2026-10-16):** Hoke now loads from the county's own service
  (`maps.hokecounty.org/server/rest/services/Parcels/MapServer/0`: owner, mailing, acres, `COST_TOTAL` as value);
  NC OneMap's Hoke records have owner/mail blank. 1,307 of 1,333 parcels have owners (98%). `TWN_PIN` = same
  12-digit number as NC OneMap, so keys are unchanged. No land-use class in that layer.
- Remaining gap: east Pinal AZ (owner only where ADWR has it; mailing only in the Casa Grande-mirror area).

- **Pinal AZ fixed (2026-10-16):** found the county's current host `gis.pinal.gov/mapping/rest/services`
  (via the Assessor viewer's web map on ArcGIS Online; `gismaps.pinalcountyaz.gov` is dead). Pinal now loads from
  `TaxParcels/MapServer/3` (owner, mailing, use class, land value, acres: 100% owners and mailing for the 6,492
  tracts) and stamps the county zoning layer (`Zoning_2010/MapServer/0`, numeric coded values translated through
  `value_labels`; CI-B/CI-1/CI-2/I-1..3 = industrial, city names = "town", see `score.PINAL_CODES`). The ADWR copy and
  the Casa Grande mirror enrichment were removed. `stamp()` gained `value_labels` for coded-value layers.
- Owner coverage now: every county we added is 98-100% except none; the `load_owners` file loader stays for any future
  county whose public service lacks owners.

## Built 2026-10-16: Arizona sites for Dave's ACA request + site briefs

Dave Dowell (Oct 15, "FW: ACMI Overview Materials + Arizona Sites") asked Kellen for a brief, high-level dive with an
aerial on three ACA sites, to say which look interesting (ACA wants 2 or fewer): Wintersburg & I-10 (Tonopah, Maricopa),
Barnes Farm (Casa Grande, Pinal), I-10 | I-8 Business Park / Petersen Park (Eloy, Pinal).

- **Saved sites** (Land > Sites): the three, built from the brochure APNs. Small parcels under the 20 ac load cutoff are
  pulled in with `ensure_parcels` (e.g. `ensure_parcels('Maricopa AZ', ['506-45-029H', ...])`). Eloy holds only the Petersen
  Eloy 501 core (502 ac); the rest of the 1,173 ac is other owners' adjoining parcels, listed in the site notes
  (core + Royal Estates + Jericho + Sonntag + TES Farms is about 1,179 ac): confirm with the ALTA survey, then add them.
- **Zoning added:** Maricopa County (`gis.maricopa.gov .../PlanNet/MapServer/11`, unincorporated land), City of Casa
  Grande (`rogue.casagrandeaz.gov .../Planning/Zoning`) and City of Eloy (Eloy's ArcGIS org `EloyZoning_view_Public`).
  Pinal parcels that the county layer hands to a city ("CASA GRANDE", "ELOY") take the city's own code: `stamp()` now
  supports `refines` / `when_value` overlays. Buckets in `score.py` (`PINAL_CODES`, `ZONING_CODES["04013"]`). `"I-2 / CR"`
  style codes use the part before " / ".
- **Findings:** Wintersburg IND-3 (98%), Barnes I-2, both match ACA. Eloy does NOT: its public map has the Petersen core as
  R1-6 / OSR / C-2 (ACA/brochure say I-2); I-1 on Royal Estates and Jericho; I-2 only on City of Eloy's 160 ac. FEMA
  zone A (approximate) covers all of the Eloy core and about half of Wintersburg.
- **AZ utilities** (`lookup.py`): water / sewer = ACC-certified private companies from the county layers (Maricopa
  PlanNet 48 / 50, Pinal `Water_Providers` / `Sewer_Providers`) plus distance to Eloy's water and sewer mains; electric =
  HIFLD with irrigation districts and federal marketers split out, and Pinal's own Electrical District layer. City and
  district service areas aren't published, so "no company mapped" means city / district / well.
- **Site briefs:** `crm/land/briefs.py` + `brief_views.py`. `/land/sites/<id>/brief/` is a one-page letter brief (aerial with
  the outline, 5-mile context map, owners, zoning by authority, power / roads / utilities, flood, flags, still-to-verify,
  broker notes); `/land/briefs/?ids=1,2,3` (Sites page > "Compare & print briefs") adds a comparison cover page ranked by
  red flags, cautions, usable acres. Print / save as PDF from the browser. Live checks run on first view (about a minute);
  "Re-run live checks" refreshes. Aerials: Esri World Imagery rendered server-side with Pillow (`/land/sites/<id>/aerial.jpg`,
  `?mode=context`), with the required Esri attribution.
- Not done: county zoning for Hoke/Harnett/Moore/Greene, utility service areas for cities, price (never in the roll).

### Brief maps and logo (2026-10-16, later)
- Brief aerials now switch on FEMA's own NFHL flood layer (transparent export, retried; FEMA's fill is 30% opacity so
  `_strengthen_flood` raises it to about 50%: cyan = Zone A/AE, orange = shaded Zone X) and Esri's road symbols / names /
  places on both the tight and context maps, with an on-image legend. The site is outline-only when flood shows.
  `sources.fema_flood_layer_id()` no longer caches a failed lookup (fallback 28; FEMA flips between 28 and 35).
- **Logo rule:** every report carries the ACMI Properties logo. Include `crm/partials/report_logo.html`
  (`crm/static/crm/logo_blue.png`, a recolor of the white sidebar `logo.png`). On: site briefs / comparison, DD reports
  (`dd_report.html`, internal + client share pages), pipeline and weekly reports. Add it to any new report template.
- **Notes & definitions page:** `briefs.definitions()` builds a glossary (FEMA 100-year floodplain, Zone A / AE / X, floodway,
  LOMR / LOMA, net usable acres, NWI wetlands, buy-box score, zoning, distances, utility providers, assessed value, the
  incentive programs found, HUBZone, imagery) limited to terms that appear. It follows the single brief and ends the
  multi-site package (`partials/brief_notes.html`). Add a term in `FLOOD_NOTES` / `GENERAL_NOTES`.
- **Save as PDF vs Print are separate buttons** on `/land/sites/<id>/brief/` and `/land/briefs/`. "Save as PDF" (`?format=pdf`)
  downloads a PDF made on the server: the page is rendered standalone (`land_briefs_pdf.html`, aerials and logo embedded as
  data URIs so no login is needed) and printed by headless Chrome / Edge (`briefs.html_to_pdf`; finds the browser via
  `ACMI_CHROME`, PATH or the usual install folders). The Dockerfile now installs `chromium`: rebuild the image before
  relying on it in production. First PDF for a package takes about 2 minutes (FEMA / Esri aerials, cached for an hour);
  later ones take seconds. Without a browser it redirects back with a message and the Print button still works.

### Small parcels and assembling (2026-10-16)
- **Why parcels were missing:** Pinal's assessor gives land with no parcel number the placeholder "NAP" (new splits, state /
  federal / tribal land, rights-of-way). They shared one key, got merged and dropped; the 20 ac cutoff hid the small ones.
  `_pinal_key` now keys each as `NAP<objectid>` (not stable if the county republishes).
- **Load everything in a window:** `counties.PINAL_FULL_WINDOWS` / `MARICOPA_FULL_WINDOWS` (west, south, east, north). Every
  parcel of any size inside is loaded (`cfg["full_windows"]`, `in_windows`); the 20 ac cutoff applies elsewhere (all of
  Maricopa's 1.76M parcels isn't practical). Add a window, then `load_parcels <County>`.
- **Map:** the Min acres filter no longer defaults to 50. In Assemble mode the map ignores the acres / score / $ per acre /
  status filters so every stored parcel in view has an outline and can be clicked, and clicking land that isn't stored yet
  (needs zoom 14+) looks the parcel up live, stores it and adds it (`land_views.assemble_point`, `_store_parcel_at`).
- **Petersen Park fixed:** the site now holds the 19 Petersen Arizona Land & Entitlement Fund LLC parcels at the I-10 / Hwy 84
  interchange near Shedd Rd (667 ac, zoned I-2 on Eloy's map, matching the brochure). The earlier Petersen Eloy 501 LLC parcels
  (south Eloy, near Battaglia Dr) were the wrong tract. About 507 ac of the brochure's 1,173.72 is other owners / NAP tracts
  (listed in the site notes); no exact combination found, confirm with the ALTA survey.
- **Land search filter:** states and regions fold (caret), the choice is remembered in the browser (`land:folded`), and a
  folded row shows how many counties are ticked.

### Zoning reference, flyer details, methodology (2026-10-16, later)
- **Zoning code reference:** `crm/land/zoning_ref/*.json` (Maricopa County, Casa Grande, Eloy, Pinal County, Charles County
  MD, Huntsville AL), loaded by `crm/land/zoning_ref.py`. Each district has purpose, permitted uses, conditional uses,
  standards, code section and source; unknown fields are null and print as "not captured". Eloy and Maricopa were read from
  the official code (American Legal; the county's PDF). Casa Grande, Charles County, Huntsville and Pinal came from the
  Zoneomics third-party mirror, so they carry a "check source" tag and have gaps (Casa Grande's B-1..I-2 use columns, Charles
  County's table of permissible uses, Huntsville LI / HI / RA, Pinal MH-435). Briefs print the districts on a site
  ("Zoning districts on these sites"); the parcel card has a "What <code> allows" fold-out. Counties with no zoning layer
  (Texas, the NC counties, Greene IN) say so instead of showing a district.
  To add a jurisdiction: add its JSON, map its overlay authority in `AUTHORITY_FILES`. Check the mirror-sourced ones against
  the official code before relying on them for a decision.
- **Flyer facts:** `LandSite.listing` (migration 0030) holds the broker flyer's price, taxes, contacts, utilities, drive times,
  nearby employers, documents and a "claims against the app" table; each brief's second page prints it with a parcel table
  (APN, owner, acres, zoning). The three Arizona sites are loaded from the Land Advisors / Western Land flyers.
- **Buy-box methodology** is on the notes page, built from the scorer's own constants (`score.methodology()`); keep it in
  step with `score.screen()`.
- Multi-parcel deal-killers print one line per kind (`briefs._summarise_killers`). Export names:
  `ACMI_Site_Brief_<Site_Name_Words>_YYYY.MM.DD.pdf`.
- **Flyer details are editable on the site page** (Site page > "Listing details", or "Edit listing details" on the brief):
  `/land/sites/<id>/listing/`, `crm/land/listing.py`. Location, size, price, taxes, zoning claim, flyer parcel numbers,
  concept plan, utilities (electric, water, sewer, natural gas, telecom, irrigation + custom rows), access, highlights,
  market, nearby employers, documents, brokers, drive times, and a flyer-claims table (confirmed / partly confirmed /
  differs / note / not checked). Anything left blank prints as N/A on the brief's second page. PDFs are made from the
  app's Save as PDF button; none are kept in the repo.

### Audit (2026-10-16, night)
- Independent audit per `docs/AUDIT_BRIEF.md`; results in **`docs/AUDIT_REPORT.md`** (report only, no code changed). 182 tests pass.
- Top items: password sign-in can't be switched off (`ACMI_LOCAL_LOGIN=false` only hides the form); no roles; outreach
  batches can double-send; a failed live re-check drops the flood deal-killer; map clicks mislabel NC counties / duplicate
  merged Hays parcels; no production logging; Cloud Build has no migrate step; public land (Redstone, TVA) scores 60-80.
- **Fixes made and merged (same night):** main at `aef3d2b`, 225 tests; see the "Fix status" table at the top of
  `docs/AUDIT_REPORT.md`. On the real DB: backup `backups/db-2026-10-16-before-audit-fixes.sqlite3`, migrations
  0031-0032 applied (all 7 accounts in Managers), `clean_parcels --commit` removed 89 parcels (44 Pinal + 1 Maricopa
  slivers, 25 Pinal + 19 Cumberland duplicate outlines), `score_parcels` rescored 132,193 (no federal parcel at 60+ now,
  was 238). Things to know:
  - Password sign-in is checked by `crm.auth.PasswordBackend` / `AllauthBackend`; everyone is signed out once.
  - Site deal rows are written when parcels join or leave a site (`tracker.ensure_deals`), never on reads; pages use
    `tracker.site_deals()`.
  - Live checks keep their last good result when an agency fails (`refresh_error`); briefs warn when checks are missing.
  - `load_parcels` won't prune when a reload returns < 80% of stored parcels (`--force-prune` to override); site plans
    are protected. `send_batch --resume` takes over a batch whose process died.
  - Scores change only after `manage.py score_parcels` (public-owner rule).
  - **Roles:** the *Managers* group (`crm/permissions.py`) sends batches, edits outreach settings / templates, deletes,
    merges, deletes sites and turns client links on. Every account that existed at migration 0032 is in it; new Google
    sign-ins start without it (add them in Admin › Groups).
  - Loads skip slivers (< 0.01 ac), store identical outlines once and re-key object-id parcels by outline;
    `manage.py clean_parcels [County_ST] [--commit]` tidies what is stored (dry run by default).
  - A missing outreach attachment fails the batch up front; uploads are served by `/documents/<id>/file/`.
  - `.github/workflows/tests.yml` runs the tests on Postgres once the repo is on GitHub.
  - Still open (improvements review, `docs/ROADMAP.md`): background work and file storage on Cloud Run.
- The 15-minute mail sync runs the main folder's code against the real DB, so work on branches in a worktree.

### Speed and the add-county skill (2026-10-16, evening)
- Reports were slow because everything ran in sequence: the aerial's four map layers (Esri imagery, FEMA, roads, places)
  and each parcel's live checks. They now run in parallel (`briefs.render_aerial`, `briefs.embed`,
  `lookup.run_checks_many` + `compute_checks`), FEMA waits at most 25 s per try, and the FEMA flood query asks for
  simplified geometry (about 10x faster, the floodplain share moves under 1%). Goodyear (NC, 2 parcels): fresh live checks
  31 s -> 8 s; the two aerials 7 s.
- **Skill:** `.claude/skills/add-county/` is the playbook for adding a county (find the parcel source, owners, acreage,
  zoning layer, zoning code definitions and permitted uses, utilities, load, verify, tests). `manage.py check_county
  <County_ST>` prints owner / mailing / zoning coverage and the zoning codes that still lack a definition.

## Built 2026-10-16 (night): roadmap phase 1 (Kellen: "go with your recommended answers")

Decisions taken from `docs/ROADMAP.md`: keep every current account a Manager; Cloud Run jobs + Cloud Scheduler for
background work; Cloud Storage for app files; repo on GitHub with CI; transcripts via a daily scheduled Claude task
with the Granola connector, Kellen + Dave + Simon's meetings, kept in the CRM + `.txt` files; the campus summary built
archive first, then the Summary tab, then the weekly Claude draft; next module after that: contract & critical dates.

- **GitHub:** private repo `kmoczulski/acmi-crm` (origin). `.github/workflows/tests.yml` runs migrations check, the
  tests and `check --deploy` on Postgres for every push.
- **Transcript archive** (`crm/transcripts.py`, `MeetingTranscript`, migration 0033): `import_meetings` now accepts a
  `transcript` (text, or Granola's `[{speaker, text, start}]`) per meeting, keeps it for good (a later import without
  one, or a cut-off one, never replaces it) and writes one `.txt` per meeting to `ACMI_TRANSCRIPT_DIR/<campus or
  General>/YYYY-MM-DD <title>.txt` (header, notes, transcript). `manage.py export_transcripts [--to <folder>]`
  rewrites missing / changed files. Meeting pop-up: "Transcript (.txt)" download, Managers only.
  **Still needed:** Kellen connects the Granola connector (claude.ai Connectors; its tools include
  `get_meeting_transcript`), then a daily scheduled task pulls the last 3 days: Granola list / get meetings + transcript →
  JSON in `data/imports/granola/<date>.json` (format at the top of `crm/meetings.py`) → `manage.py import_meetings
  <file>` → `manage.py export_transcripts`. Do the first run over the full 30 days straight away. Dave's and Simon's
  meetings need their own connector (or Granola sharing). Point `ACMI_TRANSCRIPT_DIR` at a restricted shared-drive
  folder (transcripts can hold NDA material; Maryland needs all-party consent to record).
- **Campus summary** (`/campuses/<code>/`, sidebar Development › Campus summary; `crm/campus.py`, `crm/campus_views.py`,
  `CampusUpdate`): tenants (the campus's open deals: stage, last email each way, meetings, next step, flags "no reply in
  N days", "next step overdue", "no activity in N days"), land (the campus's sites, owner by owner from the per-parcel
  deals, last site event), projects (the campus's projects except the campus-code one: last tagged email), "Other",
  and a 7-day feed (emails, meetings, land events, updates; `?days=14`). "Add update" on every item: text, ball in
  court (us / them / third party), promised-for date, date; the latest shows on the line, earlier ones fold away.
  "Weekly update email" gives the whole tab in Simon's bullet format to paste. Checked on a copy of the real DB: MEIH
  shows 14 tenants, 16 sites, 2 projects (e.g. ERG "no reply in 22 days (we wrote Sep 24)").
- **Weekly update drafted by Claude** (`crm/campus_ai.py`, `CampusReport`, migration 0034): "Draft this week's update
  with Claude" on the Summary tab sends the tab's facts (items, this week's updates, flags, the week's activity, meeting
  notes, last week's sent update) to Claude Opus 5.5 (structured JSON, effort medium, server-side refusal fallback on)
  and saves one draft per campus per week in the team's bullet format. Edit it in place; a Manager sends it from their
  own Gmail (HTML with the logo + plain text; recipients default to last week's, else the outreach report list); sent
  weeks are kept read-only and printable (`/campuses/<code>/reports/<id>/`, logo on the page).
- **Go-live plumbing:** `ACMI_SEND_JOB` makes Send start a Cloud Run job (`crm/cloudrun.py`, REST + metadata-server
  token, no new library) instead of a local process; a job that won't start fails the batch instead of blocking sends.
  `manage.py sync_all` (mail, then calendar) is what the Cloud Run sync job runs; `cloudbuild.yaml` deploys
  `acmi-crm-send` and `acmi-crm-sync` jobs and documents the Cloud Scheduler line. `ACMI_GCS_BUCKET` puts uploads and
  outreach attachments in Cloud Storage (`django-storages[google]`, only imported when the bucket is set). None of this
  has run in Google Cloud yet (billing is still off).

## Built 2026-10-16 (late night): contracts and critical dates, modeled on Notessa

Kellen: build the contract module inside the CRM, and "add in Notessa functionality; that's the program I want to
emulate". Notessa (notessa.ai) reads commercial real-estate agreements: it links amendments to the original, answers
with citations to the exact language, shows defined terms and cross-references in place, and runs reusable review
templates (lease / loan / JV abstracts). This module does that inside the CRM and adds what a developer needs on top:
critical dates with reminders, negotiation rounds and redlines, and the tie-in to land sites and campuses.

- **Contracts** (`/contracts/`, sidebar Development › Contracts; `crm/contracts/`; models `Contract`,
  `ContractDocument`, `ContractDate`, `ContractAnswer`, `ContractTemplate`, migration 0035): one per LOI, PSA, option,
  lease, PDA / DMA, JV, loan, easement, design / construction contract or NDA: counterparty, our entity, land site (and
  which owners of an assemblage it covers), campus, tenant deal, price, acres, effective date, ball in court and since
  when, next step, counsel, who runs it. "New contract" on a site's page starts one for that site. The list shows each
  contract's next date, how long the ball has sat with someone, and the next 60 days of deadlines.
- **Agreement file:** upload PDF or Word; the text is read on upload with page numbers (`crm/contracts/text.py`; a
  scan keeps only the PDF, which Claude can read but the reader can't). Signed documents form the chain in date order
  (original, amendments, amended and restated (earlier ones show as superseded), side letters, notices, exhibits,
  estoppels). Drafts are negotiation rounds, marked sent by us or them.
- **Reader** (`/contracts/<id>/read/<doc>/`): page by page, defined terms open their definition (and any later
  redefinition) in a side panel; "Section 5.2" links open that section beside the text (in an amendment, "Section 5.2 of
  the Agreement" opens the original's); "§ 5.2 changed by First Amendment" badges on the original; a section outline;
  `?hl=start-end` marks the passage a citation points to. All pattern matching, no AI.
- **Ask Claude:** a question box with quick questions. Claude Opus 5.5 reads the signed documents (or the latest draft
  while negotiating) as plain-text documents with the Citations API on, in order, each labeled with its type, date and
  whether it was superseded; CRM facts and today's date go after the cached documents (5-minute prompt cache). The
  citations come back as character ranges in our own text, so the numbered chips show the quote and open the reader at
  the exact passage. Answers are kept (pin, open, print) with the tokens used.
- **Abstracts:** 8 built-in templates in `crm/contracts/methods.py` (land PSA, LOI, lease, development agreement, JV,
  loan, termination and exit rights, risk review). A template is a list of points plus how to write it up, like
  Notessa's methods; "Copy" on the Abstract templates page makes ACMI's own. The abstract page is printable, with the
  logo and every source quoted.
- **Critical dates:** by hand, or "Find dates with Claude" (structured JSON at effort high; citations can't be combined
  with structured output, so each quote is matched back to the text for its "source" link, or marked not found). Claude's
  dates wait as suggestions until someone keeps them. Kept dates show on the contract, the list, the dashboard (next 14
  days and missed), the land site page and the campus summary (facts, plus flags within 14 days or missed, so the weekly
  Claude draft mentions them). Whoever runs the contract (else whoever created it) gets a bell reminder 30, 14, 7 and 1
  days ahead, on the day, and once when missed (`contracts/dates.py reminders`, from the daily `notify.due_sweep`); a
  moved date is reminded again.
- **Site timeline:** for an LOI, PSA or option on a site, our first draft out, signing (status Signed + effective date),
  the feasibility end and the closing date (once Closed) fill the site's milestones, or the covered owners' parcel
  deals, logged "(from <contract>)". The status only moves forward (`tracker.apply_milestones`).
- **Redline** (`/contracts/<id>/compare/`): any two documents, word by word (`crm/contracts/compare.py`), laid out on
  the newer one's lines so PDF re-wrapping isn't a change; "Have Claude explain the changes" asks about just those two.
- **Permissions:** everyone signed in reads, uploads, asks and adds dates; Managers delete contracts and documents; a
  kept date can be deleted by whoever added it or a Manager.
- **Cost:** about $0.15-0.30 for a first question on a 60-page agreement at Opus 5.5 prices, a few cents for follow-ups
  within five minutes; abstracts run longer (1-3 minutes, within the 600 s gunicorn / 900 s Cloud Run timeouts).
- **Checked** in a browser on a scratch database with made-up contracts (list, contract page, citation pop-up, reader
  terms and section links, amendment badge, redline, templates, dashboard and site panels); Claude calls are covered by
  tests with a mocked API. No real agreement has gone through Claude yet.
- **Not yet:** a portfolio view across contracts (Notessa's stacking plan and covenant report), a calendar (.ics) feed
  of critical dates, OCR for scans, and auto-numbered Word headings (Word draws the numbers, so cross-references link
  less often in .docx than in PDFs).
- **Merged 2026-10-17:** main at `8fb1640`, 268 tests (CI green on Postgres). On the real DB: backup
  `backups/db-2026-10-17-before-contracts.sqlite3`, migration 0035 applied (new tables only; no contracts yet).

## Built 2026-10-17: due diligence and the closing checklist (next roadmap item; Kellen: "yes, start it")

- **Due diligence** per site (`/land/sites/<id>/diligence/`, "Due diligence" panel on the site page and the contract
  page; `crm/land/diligence.py`, `diligence_views.py`, model `DiligenceItem`, migration 0036). Starts from the same
  checklist the client reports use (`assignments.CHECKLIST`): "Standard" (14 items: ALTA survey, title commitment,
  easements, existing leases, zoning letter and use, Phase I, wetlands, species, cultural resources, borings, electric,
  water / sewer will-serves, road access) or the full 44. Each item: status (Not started → Ordered → Received →
  Reviewed: clear / issue, or Waived), consultant, who chases it, budget and invoiced, ordered / due / received dates
  (filled in when the status moves), findings, the report (PDF / Word, text read for Claude) or a link. Status changes
  are logged on the site.
- **The deadline** is the end of the feasibility period: the kept "Feasibility ends" date on the site's open PSA or
  option, else the site's milestone. Items due after it, or late, or anything still open within 14 days of it, list as
  "At risk". The strip shows progress, budget / invoiced / committed (ordered, not yet invoiced), and the **feasibility
  decision** (proceed / extend / terminate, with a note), which is kept on the contract and logged on the site.
- **Claude** summarizes an uploaded report with citations to its pages (Citations API; focus per item, e.g. RECs and
  Phase II for a Phase I, Schedule B exceptions for a title commitment); the chips open the PDF at the cited page.
- **Closing checklist** on a PSA or option (contract page; `crm/contracts/closing.py`, model `ClosingItem`): 18
  standard items by side (us, seller, title / escrow, lender, after closing), due on the kept closing date (the
  clean-up 30 days after), done / not needed, your own items. **Mark closed** sets the closing date to the actual day,
  the contract to Closed, and through the timeline sync the site and every parcel's deal to Acquired (a whole-site
  contract now sets the parcel deals too, logged once on the site). The parcel card then says "Owned by <our entity>
  since <date>" (county records catch up later).
- **Deadlines:** the dashboard panel (now "Deadlines") and the contracts list mix contract dates, diligence items
  (tag DD) and closing items. Bell reminders: diligence items to whoever chases them (else the contract's owner) 7
  and 1 days ahead, on the day and once late (not once received); closing items to the contract's owner the same way.
  The campus summary adds "due diligence: N of M cleared, K issues" and flags for issues, late items and a
  feasibility crunch, so the weekly Claude draft picks them up.
- Checked in a browser on the scratch demo database (made-up data); Claude calls are tested with a mocked API.
- **Merged 2026-10-17:** main at `53afe42`, 276 tests (CI green on Postgres). On the real DB: backup
  `backups/db-2026-10-17-before-diligence.sqlite3`, migration 0036 applied (new tables and the decision fields).

## Built 2026-10-17: the entitlement tracker (next roadmap item; Kellen: "yes, start it")

- **Approvals** (`/entitlements/`, sidebar Development › Entitlements; `crm/entitlements/`, models `Approval`,
  `ApprovalStep`, `ApprovalCondition`, `ApprovalDocument`, migration 0037): one per rezoning, special use, variance
  (The Forge's sign), site plan, plat, grading / sediment control, stormwater, wetlands 404 / 401, floodplain, the
  DDESB explosives safety site plan, road access, utility agreements, environmental or Section 106 review, air / water
  permits, building permit, CO. Each belongs to a project, a land site or a campus (and can be tied to the PSA whose
  approvals contingency it serves): agency / board, case number, engineer or attorney, who runs it, ball in court and
  since when, next step, needed-by date, submitted / decision / expiration / appeal-end dates, budget and spent.
- **Permit matrix:** "Start a permit matrix" (list page, and the Entitlements panel on a project or site page) adds the
  usual approvals for a building (site plan, sediment control, stormwater, road access, utilities, building permit)
  plus whatever else is ticked (rezoning, variance, wetlands, the DDESB site plan, CO).
- **Steps and hearings:** pre-application meeting, submittal, comments, resubmittal, hearing, decision, appeal end.
  A hearing moves the status to Hearing scheduled, a submittal to Submitted, comments to In review (forward only).
  A step that passes without being marked done shows "record the outcome" in red.
- **Conditions of approval** with when they're due ("before plat", "before CO", a date) and met / not applicable.
  **Papers** (application, staff report, comment letter, resolution, plans): Claude summarizes one with page
  citations, and "Find conditions with Claude" pulls the conditions out of a resolution (structured JSON, each quote
  matched back to the text) as suggestions to keep or discard; it also fills in the expiration and appeal-end dates
  when the approval has none.
- **Hooks:** the approval list groups by campus and project with the next hearing and who has the ball; panels on
  the project, site and contract pages; the dashboard deadlines (tag Entitlement) and a "Coming up · 30 days" panel;
  bell reminders to whoever runs it (hearings and steps 7 and 1 days ahead, on the day, and once when they pass
  unrecorded; an approval lapsing 60, 30, 7 and 1 days ahead; conditions with a due date); the campus summary's
  projects and land lines (status and next step, flags for a hearing within two weeks, a missed step, a lapse within
  60 days, a denial, conditions due), which the weekly Claude draft picks up.
- **Also fixed:** the contract page's "Find dates with Claude" button did nothing in a browser (its action rode on the
  button, which the busy indicator disabled before the post); the action is now a hidden field. The site page looked
  up FEMA's flood layer id on every render and, when FEMA was down, waited up to about 70 s each time; a failed lookup
  is now remembered for 10 minutes and the fallback is the current id (35), not the old 28. Tests never call FEMA.
- **Merged 2026-10-17:** main at `e3ca2b0`, 285 tests (CI green on Postgres). On the real DB: backup
  `backups/db-2026-10-17-before-entitlements.sqlite3`, migration 0037 applied (new tables only).

## Built 2026-10-17: build records (next roadmap item; Kellen: "yes, start it")

Summaries of what the GC's own system (Procore etc.) holds in detail, one page per project:
`/projects/<id>/build/` ("Build" panel on the project page; `crm/build/`, models `BuildMilestone`, `TeamMember`,
`Bid`, `ProjectAnswer`, `BudgetLine`, `ChangeOrder`, `Draw`, migration 0038). The page prints with the logo for
lender and investor updates.

- **Schedule:** "Start the standard schedule" adds 20 milestones (design: test fit, SD, DD, CDs; procurement: GC
  RFP, proposals, interviews, GC selected, GMP; construction: NTP, site work, pad ready, foundations, vertical,
  dry-in, substantial completion, CO; closeout: move-in, rent start, final). Each has baseline, forecast and actual
  dates; slip = forecast (or actual) minus baseline. A forecast that passes without an actual date shows "record or
  re-forecast".
- **Team:** architect, civil, structural, MEP, geotech, surveyor, environmental, the QD / explosives safety
  consultant, GC, owner's rep, attorney, with contract amount and invoiced to date.
- **GC selection** (ARA's interviews and downselect): bidders with status (invited → received → interviewed →
  shortlisted → selected), price, fee %, general conditions, months, interview date, strengths and concerns, and the
  proposal file. "Select as GC" marks the others still in play Not selected, adds the GC to the team at the bid
  price, and dates the "GC selected" milestone. **Claude:** "Level the bids" reads every proposal side by side
  (price, fee, allowances, exclusions, schedule, scope gaps, questions per bidder) with each number cited to its
  proposal page; "Ask about the proposals" answers a question the same way; "Fill in from the proposal" reads one
  proposal's price, fee, general conditions and months (structured JSON) into the blank fields and notes the
  exclusions.
- **Budget:** "Start the standard budget" adds 16 lines (land, hard, soft, financing, contingency). Original,
  approved changes, current, committed, paid, forecast (blank = current) and variance, in whole dollars so the
  columns add up. **Change orders** (reason, amount, schedule days, line; Approve / Reject) add to their line when
  approved; ones on no line still count in the totals. **Draws:** requested, funded, retainage, status.
- **Hooks:** the campus summary's project line (next milestone and slip, GC or selection status, budget and
  forecast, pending change orders; flags for a slip of 14+ days, a forecast over budget, a milestone that passed
  without a date, a draw waiting 30+ days), and the dashboard deadlines (milestones forecast within two weeks and GC
  interviews, tag Build).
- Checked in a browser on the scratch demo database (made-up data, including approving a change order); Claude calls
  are tested with a mocked API.
- **Merged 2026-10-17:** main at `7378693`, 290 tests (CI green on Postgres). On the real DB: backup
  `backups/db-2026-10-17-before-build-records.sqlite3`, migration 0038 applied (new tables only).

## Built 2026-10-18: tenant site fit, lease terms and the rent roll (Kellen: "continue")

- **Site fit** (`/opportunities/<id>/fit/`, "Site fit" panel on the deal page; `crm/tenants/fit.py`, model
  `TenantNeeds`, migration 0039). A deal's acres and MW come from its details; the needs add the explosives cases
  (one row per building: hazard division, NEWQD, magazine or operating building, square feet) and rail / gas. Every
  saved site with land (the deal's campus only, or "All sites") is checked: net usable acres (from the parcels' live
  checks, scaled to trimmed portions), whether the IBD arcs close on the property (the QD planner places the
  buildings on the site's outline, same engine as the site planner), power (10 MW and up wants a transmission line
  within 3 mi; smaller loads a substation within 5 mi), rail and gas distances (the buy-box score's bands), and
  control (under contract or owned passes; anything else is neutral, not a failure). Sites sort by failed checks,
  then the deal's campus, then a 0-100 score weighting acres and arcs most. The arc check is a screen: confirm with a
  siting engineer.
- **Lease terms** (contract page, for leases; `crm/tenants/leases.py`, model `LeaseTerms`): premises sf and acres,
  commencement, rent start, expiration, first-year base rent, annual escalation, free rent, renewal options, notice
  months, security, TI allowance, guarantor. Typed in, or "Read the terms with Claude" (structured JSON over the lease
  as amended; fills only blank fields, each with the quote it came from, linked into the reader). Saving the terms
  puts the expiration, renewal notice deadline and rent commencement on the contract's critical dates (marked "From
  the lease terms" and kept in step; a date someone kept by hand of the same kind is left alone; past ones go in as
  done), so the usual reminders cover them.
- **Rent roll** (`/leases/`, sidebar Contracts › Rent roll): signed leases by campus with sf, rent start, expiration
  and years left, rent today (first-year rent stepped up each full lease year), $/sf, bumps, options and the renewal
  notice deadline (red within 18 months); totals with the rent-weighted WALT. Prints with the logo.
- **Deal page:** an Agreements panel lists the deal's LOIs, PDAs and leases with their next dates ("New LOI / lease").
- Checked in a browser on the scratch demo database (made-up sites, deal and lease); Claude is tested with a mocked
  API.
- **Merged 2026-10-18:** main at `71c1080`, 296 tests (CI green on Postgres). On the real DB: backup
  `backups/db-2026-10-18-before-tenant-fit.sqlite3`, migration 0039 applied (new tables only).

## Built 2026-10-18: flyer intake and landowner letters (roadmap item 7; Kellen: "yes, start it")

- **Flyer intake** (Listing details page, "Broker flyer" panel; `crm/land/flyer.py`, fields `LandSite.flyer` and
  `flyer_read_at`, migration 0040). Upload the broker's flyer (PDF or PNG/JPG/WebP, up to 25 MB); it stays on the
  site (`/land/sites/<id>/flyer/` opens it). With Claude on, Claude reads it (structured JSON) into the listing
  details: location, size, price, taxes, zoning per the flyer, parcel numbers, concept plan, access, highlights,
  market, nearby, documents, utilities, brokers and drive times. It also checks the flyer's claims (acres, zoning,
  floodplain, wetlands, utilities, distances) against the CRM's facts for the site's parcels, one row per claim with a
  status. It fills blank fields only, adds utilities by type that aren't there yet, and appends claims the table
  doesn't already have, so reading a flyer twice changes nothing. Without the API key the flyer is still kept.
- **Landowner letters** (`/land/sites/<id>/letters/`, "Landowner letters" panel on the site page;
  `crm/land/letters.py`, `crm/land/letter_views.py`, models `LetterTemplate` and `LandownerLetter`):
  - **Recipients:** one row per owner and mailing address from the appraisal roll (included parcels only), so an
    owner with three parcels gets one letter about all three. Owners with no mailing address are listed but can't be
    ticked.
  - **Names:** title-cased for the letters ("SMITH FAMILY TRUST LLC ETAL" becomes "Smith Family Trust LLC et al";
    names typed in mixed case stay as typed). Each owner has an editable "Dear" line, since rolls often list names
    last-first.
  - **The letter:** two built-in letters, a first letter and a follow-up, kept in step with the code. "Save as
    template" keeps a team copy. Merge fields are {owner}, {property}, {parcels}, {acres} (deeded acres where the
    roll has them), {county}, {site}, {date} and {sender}, {sender_title}, {sender_phone}, {sender_email}. A
    misspelled field, or a sender detail the letter uses but that is blank, stops the letters and says why.
  - **Outputs:** a letters PDF (one page per owner, logo letterhead, date and address block); a print view (also the
    fallback when Chrome is missing); Avery 5160 labels in capitals, which can start partway into a used sheet; and a
    mail-house CSV (with a byte-order mark for Excel, and formula-proofed cells).
  - **Logging what went out:** keeps each letter as merged, with method, date and notes. It dates "Owner contacted"
    on those owners' parcel deals and on the site, where the first contact counts (a letter logged late with an
    earlier date moves the date back). Statuses move forward to Owner outreach, never back. The same batch logged
    twice on one day is logged once. The history lists each letter with a reprint. The sender or a Manager can take
    one out of the log; dates stay.
- The sender details default to the outreach settings' title and phone for the outreach sender. Each person's last
  values are kept in their session.
- Checked in a browser on the scratch demo database (made-up owners): logging, the site page, a reprint, and the
  letters and labels PDFs. Claude is tested with a mocked API.
- **Merged 2026-10-18:** main at `71a9109`, 320 tests (CI green on Postgres). On the real DB: backup
  `backups/db-2026-10-18-before-flyers-letters.sqlite3`, migration 0040 applied (new tables and blank fields only). The
  built-in letters are created the first time someone opens a site's letters page.

## Built 2026-10-19: Sites as Trello-style cards, task times, live alerts, sidebar regrouped

Kellen asked for the Sites page to be reworked around a Trello-style card, with Trello's features brought into the
app, and for the sidebar to be organised properly with the map kept separate from sites.

- **Sites page** (`/land/sites/`): the deal-timeline Gantt at the top is gone. The page has two views, "By campus"
  (grouped cards) and "Board" (one column per stage; dragging a card to another column changes its stage), plus a
  "My sites" filter (owner or team). Each card shows open checklist items, comments, linked tenants, the deal clock
  and avatars for the owner and team.
- **Site card pop-up** (`/land/sites/<id>/card/`, `crm/land/site_card.py`, `card_views.py`,
  `partials/site_card.html`): it opens in place. It shows key facts, the AI summary (Claude, stored in
  `LandSite.ai_summary`, refreshed on demand and flagged stale once new activity arrives), the milestone timeline,
  the critical dates from contracts, contracts, tenants (with a picker to link one), diligence and approvals, a
  checklist (site tasks with a label, date and time) and comments. The sidebar has stage, owner, landowners, zoning
  and deal-killers. "Open site page" goes one level deeper. `?card=<id>` opens a card from a link.
- **Structure**: `LandSite.owner` / `team` (owner defaults to the creator, via migration).
  `Opportunity.land_sites` links tenant deals to sites directly. `Comment.site` replaces site notes: the migration
  copied the old note events into comments, and the change log no longer shows notes. `Task.site` and `Task.label`
  hold the Trello categories (deal info, legal, civil, architecture, program / procurement, land use, critical
  date). `LandSite.get_absolute_url` is now the site page; `site.map_url` is the map.
- **Milestones** were already filled in from contracts (signing, feasibility, closing: `crm/contracts`), so no change
  was needed there.
- **Task times**: `Task.due_time` (optional). The task forms, meeting action items and the site checklist take a
  time, and a task counts as overdue once its time passes. `notify.timed_sweep` sends "Due at …" 15 minutes ahead
  and "Due now" at the time.
- **Browser alerts**: `static/crm/alerts.js` on every page polls `/notifications/poll/` every 30 s. It updates the
  bell, plays a chime, shows an in-page toast or, when the tab is hidden, a desktop notification. Each person turns
  these on per browser on the Notifications page (desktop on/off, sound on/off, test).
- **Sidebar**: Dashboard, Notifications, Tasks and Meetings sit at the top. Then Land (Sites, Map, Client searches,
  Entitlements), Tenants (Tenant deals, Rent roll, Investor report), Campuses (Campus summary, Projects, Contracts,
  Abstract templates), Contacts (Companies, People, Lists, Possible duplicates) and Outreach.
- **Trello**: the team's MEIH board uses lists per deal (Land Sites, Critical Dates, Deal and Design, Contacts and
  Consultants) and the labels above; critical dates carry times. Not yet brought in: contacts and consultants per
  campus, and importing the existing Trello cards.

## Built 2026-10-19 (later): editable dropdown lists, outreach report scope

- **Settings → Dropdown lists** (`/settings/`, `crm/choices.py`, `crm/settings_views.py`, managers only). Every
  dropdown comes from a TextChoices class; the defaults stay in code and `ChoiceOption` rows hold the team's edits:
  label, position, hidden, and added options. `choices.install()` (from `CrmConfig.ready`) patches
  `ChoicesType.choices`, `Choices.label`, each model field's `choices` (its `deconstruct` still reports the code
  defaults, so migrations never change) and ModelForm fields (hidden options drop out unless a record already has
  one). Hand-built selects use `{% for v, l in statuses|offer:current %}`. System lists (activity kinds, sync
  states) are left out.
- **Adding options**: every list takes new options. On lists the code depends on (stages, statuses), a new option
  picks the built-in option it "works like" (`ChoiceOption.behaves_as`). The stage orders, groups and probabilities
  are `choices.Order` / `Group` / `Lookup` objects (`LandSite.STATUS_ORDER`, `OPEN_STAGES`, `STAGE_ORDER`,
  `STAGE_PROBABILITY`, `Contract.OPEN_STATUSES`, `queue.LATE_STAGES` and others), so an added option sorts, filters
  (`stage__in=OPEN_STAGES`) and scores like its look-alike. Code that compares against one specific value
  (`status == "acquired"`) still only fires on the built-in option.
- **Weekly outreach report** (`crm/outreach/report.py`) now covers campaign sends plus news outreach: approved
  news-review drafts, matched by company and subject. Companies already ACMI's are left out: tenants, a signed LOI,
  lease or DMA, or a signed or closed contract. A response only counts if it came back on the outreach email's own
  thread. For the week of Sep 21 this left out 7 of 937 emails and dropped 9 of the 10 "positive replies", which
  were deal conversations (ARA, Firehawk, Ondas, Supply Energetics, Elementum).
- **ETC PSA entered (2026-10-19, Kellen approved):** "4390 Indian Head Hwy PSA" (contract 1) on site 1. Signed,
  effective Jun 24, 2026, $150,000, 0.68 ac, Energetics Technology Center, Inc. It has 9 critical dates from the MEIH
  Trello board's "4390 Indian Head Hwy - Critical Dates" list; the Jul 1 deposit and the Jul 3 lease execution are
  marked done. The site's feasibility milestone is set to Oct 22. PSA signed stays Jun 22 (the executed file is dated
  2026.06.22; Trello gives the effective date as Jun 24). Backup: `backups/db-2026-10-19-before-etc-psa.sqlite3`.
  The site card's Critical dates now lists completed dates too, checked off.

## Built 2026-10-21: contract review (paper reader, real redlines, comments, closing timeline, critical dates calendar)

Kellen asked for the contracts pages to read like a PDF or Word document, an actual redline that says who made it,
comments in a sidebar with tagging and Claude's notes on what changed, everything in a summary clickable, a closing
timeline from an effective date compared across versions, the same timeline on signed contracts with a way to confirm
an extension and push the dates, and a critical dates calendar on the dashboard.

**Merged 2026-10-21** (`acf31a2`, CI green on Postgres; this push also carried 12 earlier commits that had never been
pushed). Migration `0051_contract_review` applied to the real DB (new fields only, plus `critical=False` on existing
payment / rent dates); backup `backups/db-2026-10-21-before-contract-review.sqlite3`.

- **Reader as paper** (`/contracts/<id>/read/<doc>/`): each page is a letter-size sheet on a grey desk, "Page n of N"
  at the foot. PDFs also have an "Original PDF" view (the file in a frame; `contract_file` now allows same-origin
  framing). Terms, sections and comments are sidebar tabs.
- **Redline** (`/contracts/<id>/compare/`): Word-style, each author's changes in their color (struck / double
  underline, change bar, numbered). "Redlined by" legend with counts, and a Changes sidebar: one card per change with
  who made it and the round it came from. Comparing rounds that aren't next to each other, `compare.blame()` credits
  each change to the round that made it. Who: `ContractDocument.author` ("Redlined by", new field on upload/edit), else
  our uploader for our drafts, else the counterparty for theirs (`ContractDocument.made_by`).
- **Word tracked changes:** a .docx's own `w:ins` / `w:del` are kept with author and date
  (`ContractDocument.tracked`, `text.extract_full`); the reader shows them in place and the redline credits them to the
  Word author. The text is the document with changes accepted.
- **Comments** (`Comment.contract / document / start / end / quote / parent / resolved / by_claude`): select words in
  the reader or redline → Comment; the words stay highlighted, the card sits in the sidebar, replies thread under it,
  Resolve hides the highlight. `@name` picks a teammate (`static/crm/mentions.js`) and notifies them; the contract's
  owner and people in the thread hear about replies. Offsets are counted from the page text in the browser
  (`static/crm/contract_review.js`; anything added on top of the document carries `data-x`) and checked against the
  quote on the server, which re-finds the quote if they drifted.
- **Claude's notes on a redline:** "Have Claude explain each change" (`ai.annotate_changes`, structured output, one
  note per numbered change, "Flag:" on ones to look at) saves them as Claude comments pinned to each change; redoing it
  replaces notes nobody replied to. The cited "summary of the changes" answer is still there.
- **Clickable summaries:** in answers and abstracts every paragraph or bullet that cites the agreement opens its
  source quote (pop-up, then the reader at the passage). The contract header's facts link too (type → list, counterparty
  → company, site, price / acres → ask Claude, effective date → timeline).
- **Closing timeline** (`/contracts/<id>/timeline/`, `crm/contracts/timeline.py`): a date is fixed or a rule (N days /
  business days / weeks / months after the Effective Date or another date's key). Pick any effective date ("What-if")
  and every date is worked out; tick versions to see them as lanes on one axis and side by side, with changed rows
  marked. Each version's dates come from "Read timeline" (`ai.read_timeline`, structured, keeps keys aligned with other
  versions); "Use these dates as critical dates" turns a version into suggestions on the contract. Typed rules are read
  without Claude (`parse_rule`, `parse_extension`: "ninety (90) days after the Effective Date", "extend the Feasibility
  Period by thirty (30) days"). `find_dates` now returns the rule parts too. The contract page has the timeline strip.
- **Signed contracts / extensions:** an extension option (kind Extension, `extends` = the key it pushes, `extend_by`)
  shows on the timeline with what it would move. "I've exercised it…" takes the notice date and the new date, moves the
  target and every date that counts from it (and, if ticked, other dates on the same day), keeps each old date in
  `ContractDate.history`, marks the option done, sets the feasibility decision to Extend, and updates the site's
  timeline. "Move a date by hand" does the same without an option.
- **Critical dates calendar** (dashboard, `dates.month_calendar`, `?crit=YYYY-MM`): month grid of the critical dates
  of **executed agreements only** (Kellen, 2026-10-21: "only things that are executed"): signed or closed contracts'
  dates marked critical (new `ContractDate.critical`, on by default; recurring payments off) and the closing checklist
  of a signed purchase. Tagged PSA / LOI / Option / Lease / PDA / Closing, done ones checked off, late ones outlined;
  tag chips filter it. Drafts, deals in negotiation, tasks, diligence and entitlement deadlines stay off it (they're
  still in the Deadlines panel).
- **Tests:** `crm/tests/test_contract_review.py` (17), 361 in all pass. Checked in the browser on a scratch database
  with made-up contracts (reader, select-to-comment with @mention, redline attribution and change threads, timeline
  lanes, exercising an extension, contract page, calendar, PDF frame headers). Claude calls are mocked in tests; no
  real agreement has gone through `read_timeline` or `annotate_changes` yet.
- **Not yet:** comment highlights in the redline view are by change, not exact words (the redline's text isn't the
  document's text); holidays aren't skipped in business days; "Original PDF" for Word files (download only).

## Built 2026-10-21 (later): drag-and-drop contract upload, Word redlines, extensions that work out their own dates

Kellen: the upload should be "drag to upload and AI to pull the info automatically"; the timeline UI cut things off;
the extension made him type the date; the executed agreement should be clearly marked; and "I still don't see
redlines between version 1 and version 2" on the Long Wall PDA.

- **Drop to upload** (`crm/contracts/intake.py`, `static/crm/dropzone.js`, `partials/contract_dropzone.html`): drop
  files anywhere on a contract page (or pick them). Each file is read, then Claude (structured output, effort low,
  first 60k characters) says what it is: kind (executed original, amendment, draft...), title, date, whose draft and
  who redlined it, a one-line summary, and fills the contract's blank fields. Dropping an agreement on the contracts
  list starts a new contract named and filled from it (title, kind, status, parties, price, acres, effective date);
  more files dropped with it go into the same contract. Without a key the file name is the guide
  (`intake.guess`: "Redline", "Amendment", a date in the name). The same file twice is refused. The old form is still
  there under "Or fill it in by hand".
- **Word redlines:** files uploaded before Word tracked changes were kept are read again with
  `manage.py reread_contract_files [--contract N] [--dry-run]` (comments and date sources are re-pinned by their
  quotes). The redline page has two views when the newer file is a Word redline: "Tracked in the Word file" (the
  markup exactly as its author left it, one card per change with who and when) and "Compared with <previous>". Every
  document in the file now has "compare with previous" (any kind, not only drafts), and Word redlines a bold
  "redline (N tracked)" link; the reader has the same buttons.
- **Extensions:** dates entered before rules existed (the ETC dates came from Trello) get their key and rule read on
  first use (`timeline.ensure_keys`; new phrasings: "on or before the end of the Feasibility Period", "before the
  Feasibility Period expires", "10 days' advance notice of the Closing Date", "extends it 120 days"). The extension card
  shows the deadline it pushes, its new date worked out (e.g. Oct 22 + 120 days = Feb 19, 2027), every date that
  counts from it with its new date, and the other later dates as tick boxes (same-day ones ticked). You enter only the
  notice date. The extension itself is never moved; a pushed deadline is reopened. If the agreement doesn't say what an
  extension pushes, the card asks which deadline (and remembers it).
- **Timeline layout:** an overview with a dot per date (names in their own column, no labels on the track, so nothing
  is cut off; hover for what it is), then the dates month by month, then the side-by-side table only when comparing.
  The contract page shows the overview plus the next four dates.
- **Executed agreement:** once a contract is signed, the latest signed original or restatement is marked "Final
  executed" in the file, with a green "Executed" banner on the contract, timeline and reader (drafts say "Draft: not
  signed").
- **Bug fixed (real data):** on Oct 21 the ETC "Extension Notice Deadline" was exercised through the first version,
  which pushed the extension date itself (and two same-day dates) to Feb 19 and left the Feasibility Period
  Expiration, closing and closing notice where they were. The fix script is in this session's scratchpad
  (`fix_etc.py`): it undoes the Oct 21 moves from each date's history, then exercises the extension properly (notice
  Oct 19, $10,000 deposit wired Oct 21): Feasibility Feb 19, 2027; title defects notice and lease supplement Feb 19;
  seller's title response Mar 1; closing (60 days after feasibility) Apr 20; closing notice Apr 10. Tested on a copy.
- **Merged 2026-10-21** (`b6c7e41`, CI green; 396 tests). No migration. On the real DB (backups
  `backups/db-2026-10-21-before-etc-fix.sqlite3` and `-before-etc-fix-2.sqlite3`): the ETC extension was redone with
  `fix_etc.py` (dates as listed above; decision Extend on Oct 19; site feasibility milestone Feb 19, 2027);
  `reread_contract_files` re-read the two Long Wall Word files (19 and 101 tracked changes); the duplicate copy of the
  executed ETC PSA (document 4, unused; Claude's answers cite document 3) was deleted with its file.
## Built 2026-10-21: the capitalization tracker

**Merged 2026-10-21** (`d1d1393`, CI green on Postgres). Migration `0052_capital` applied to the real DB (new tables
only); backup `backups/db-2026-10-21-before-capital.sqlite3`. No providers or sources entered yet.

Kellen: "We have equity (Fortress) but have to work through getting debt, so being able to track meetings and term
sheets etc. would be helpful." Sources, terms and conversations only; the pro forma stays in Excel.

- **Lenders and investors are not Companies** (Kellen, 2026-10-21: "I don't want to add debt brokers or banks to the
  CRM ... I don't want to mass send these companies my outreach emails"). They're their own list, `CapitalProvider`
  (`/capital/providers/`, sidebar Capital › Lenders and investors): name, type (bank, debt fund, broker / advisor,
  equity investor, agency), email domains, contacts (kept on the provider, not as People), notes. Nothing creates a
  Company or Person from it, so no provider can reach a send queue, list or outreach report. Kellen floated a separate
  directory for consultants too ("if anything it should be a separate CRM for consultants etc."); not built.
- **Their email:** the mail sync files mail from a provider's domain as `CapitalEmail` on the provider instead of the
  review queue (`mail/sync.py _park`), so a lender's domain is never offered as a new company. Adding a domain moves
  anything already waiting in the review queue onto the provider (`capital.logic.file_queued`). A domain belongs to one
  provider. If a lender's domain already belongs to a Company (from before), the sync still logs it on that Company.
- **Sources** (`crm/capital/`, models `CapitalProvider`, `CapitalSource`, `TermSheet`, `CapitalLog`, `CapitalEmail`,
  migration `0052_capital`): one per piece of a project's stack and who might provide it: equity (JV / LP), preferred
  equity, mezz, construction loan, senior, bridge, permanent, C-PACE, bonds / IRB, seller financing, grants /
  incentives, tax credits. Provider typed by name (the providers' names offered; a new name starts a provider), contact
  (one of the provider's contacts, as text), sought and committed amounts, stage (identified → reached out → in discussion → term sheet → committee / approved →
  docs / closing → closed, or we passed / they passed), when it got there, contact, who runs it, ball in court and since
  when, next step and its date, notes.
- **Pages:** `/capital/` (sidebar Campuses › Capital; also "Capital" on each campus card on Projects) lists every
  source by project with stage counts, latest terms, last touch, ball / next step, an "Expiring and due · 60 days" box,
  "Waiting on whom" and "Gone quiet" (in play, no touch in 21 days); filters for in play / closed / all, debt / equity,
  campus; prints with the logo. `/projects/<id>/capital/` (Capital panel on the project page) has the stack (equity,
  debt, grants; lenders quoting the same loan count once in the totals), the sources table with a quick-add row, the
  latest terms side by side and the last 60 days of touches. `/capital/<id>/` is one source.
- **Term sheets:** each version uploaded (PDF / Word, text read by `contracts/text.py`) with the key terms: amount,
  rate as quoted, spread, fixed rate, floor, term, extensions, amortization, LTC, LTV, min DSCR, debt yield, origination
  and exit fees, other fees, interest reserve, recourse, guaranties, prepayment, equity required, pref return,
  promote, covenants, deposit, conditions, offer expiry, close-by date. Status received / we marked it up / signed or
  accepted / declined / expired. Adding one moves the source forward to Term sheet. "Versions side by side" on the
  source and `/capital/compare/?ts=..` (printable, logo) mark the terms that differ.
- **Claude** (`capital/ai.py`, structured JSON at effort high, the `contracts/ai.py` pattern): "Have Claude fill in the
  key terms" on upload, or "Fill in with Claude" later. Fills blank terms only, each with the sentence it came from
  matched back to the text and its page ("source p. N" opens the PDF there). Typing over a term drops its quote.
- **Touches:** the provider's filed emails (all of them: a lender on two projects shows the same mail on both),
  Granola / calendar meetings (an attendee from its domains or one of its contacts, or the provider's short name in the
  title: "Fortress Investment Group LLC" matches "Fortress / ACMI weekly"), and the source's log (calls, notes, status
  changes, term sheets). Upcoming meetings show too. The provider page shows all of it across projects.
- **Signed agreement:** "Start the loan agreement" (or JV agreement for equity) creates a draft `Contract` (kind
  Loan / JV, provider as counterparty, the project's campus) and links it; or link an existing one. Its dates follow
  the usual contract rules once signed.
- **Hooks:** dashboard Deadlines (tag Capital): the newest open term sheet's expiry and close-by date and each source's
  next step. Critical dates calendar: only a **signed / accepted** term sheet or commitment's expiry and close-by date
  (tag Loan or Capital), keeping the "executed only" rule. Bell reminders to whoever runs the source: offer expiring 14,
  7, 1 days ahead, on the day, once lapsed; next step the day before, on the day, once missed. Campus summary project
  lines: each active source's stage, latest terms, ball and next step; flags for an offer expiring within 14 days, a
  next step due or missed, a source gone quiet. Settings › Dropdown lists has a Capital area.
- **Permissions:** everyone signed in adds and edits; Managers delete sources and term sheets; a log entry can be
  removed by whoever wrote it or a Manager.
- **Data to add once merged (waiting on Kellen):** Fortress as a provider (investor) with its email domain; 13 meetings
  already have "Fortress" in the title. Mail from before a domain is added stays wherever the sync put it (the review
  queue's is moved over; mail already dismissed is gone). The real DB has projects MEIH, The Forge, ARA Facility, NSIH,
  Greystar.
- **Tests:** `crm/tests/test_capital.py` (21: including that a new provider never creates a Company or Person, the mail
  sync filing lender mail on the provider, and queued mail moving over when a domain is added). Checked in a browser on a
  scratch database with made-up lenders (portfolio, project page, source page, logging a call, a stage change, dashboard
  Deadlines, the compare print; then the providers list and adding a domain). Claude is mocked in tests; no real term
  sheet has gone through it.
- **Not yet:** a Capital panel on the contract page (left alone while another session changes the contract upload
  form); OCR for scanned term sheets (Claude reads the PDF, but quotes can't be matched to text).

## Built 2026-10-21: the company page rework (Kellen: "really hard to navigate… meeting summaries should be more apparent")

**Merged 2026-10-21** (`befd879`, CI green, pushed; branch and worktree removed). No migration, no data change.
- **Layout:** tabs that stay at the top as you scroll: Overview, Meetings (n), Action items (n open), Activity, People
  (n), Deals (n). The record's Details, team connections, documents and lists moved to a right-hand column (below the
  page under 1100px wide). The overview runs: a strip of key facts (stage, owner, last touch, next meeting, who knows
  them, fit, campus, projects, latest news) → Who we know (top 5 people) and Open deals side by side → the latest
  meetings → open action items and tasks → folded panels for AI summary, Notes, Comments and Recent activity.
- **Meetings as cards** (`partials/company_meeting_card.html`): the next upcoming meeting plus the last three held,
  each with the Granola summary rendered in place (long ones start clipped, "Show full summary" opens the rest on the
  page) and its Next Steps listed beside it as action items. The summary drops its "Next Steps" section
  (`meetings.summary_without_actions`) so items aren't shown twice. The Meetings tab has every meeting, 10 a page.
- **Action items** (`partials/action_item.html`, now shared with the meeting pop-up): owner, a checkbox once a task
  exists (ticking it closes the task), and Make task (assignee, due date, time). On a card the form opens from a
  "+ Make task" link. `meeting_make_task` now honours `next`, so Make task returns to the company page; the pop-up
  passes the calendar URL, as before. The Action items tab lists open items across all the company's meetings, then
  the company's other tasks (tasks made from items aren't listed twice), with done ones folded.
- **Activity tab:** chips for All / Meetings / Email / Notes and tasks / Changes and news, with counts
  (`?kind=`, `crm/company_page.py ACTIVITY_FILTERS`).
- **Code:** the meeting and action-item logic is in `crm/company_page.py`, so `company_detail` in `views.py` only gained
  a few lines. CSS is one marked section at the end of `app.css` ("Company page … End company page").
- **Tests:** `crm/tests/test_company_page.py` (8). Checked in a browser on a scratch database with a made-up company,
  three meetings and tasks: desktop at 800 and 1440 wide and phone at 375 wide (no sideways scroll), Make task from a
  card and from the Action items tab, ticking an item done, the Activity filter, expanding a summary.
- **Not yet:** the person page still has the old layout; the same cards would fit there.

## Built 2026-10-21: the directory (consultants, vendors, lawyers, engineers, lenders)

**Merged 2026-10-21** (`69884a9`, CI green on Postgres with 412 tests, pushed; branch and worktree removed). Real DB migrated
through `0053_directory` (backup `backups/db-2026-10-21-before-directory.sqlite3`; the capital tables were still empty, so
nothing to carry over). **Cozen O'Connor moved** (Kellen's OK): now directory firm 1, Law firm, cozen.com, Jonathan
Ciriello as a contact; Company 1795 and its Person deleted; its 6 meetings show on the firm page. Backup
`backups/db-2026-10-21-before-cozen-move.sqlite3`.

Kellen, 2026-10-21, on keeping brokers and banks out of Companies: "If anything it should be a separate CRM for
consultants etc." He picked (2026-10-21): **one directory** shared with the lenders and investors rather than two lists;
a **Move to directory** button on a company, moving **only Cozen O'Connor** for now (the ~40 consulting-sector
Companies are often defense consultants that could be tenants, so they're moved one at a time by hand); and **project
team firms linked** to the directory.

- **One list, `Firm`** (`crm/directory/`, migration `0053_directory`, which renames `CapitalProvider` → `Firm` and
  `CapitalEmail` → `FirmEmail` in place, so the capital tables and rows carry over). Type is capital (bank, debt fund,
  debt broker / advisor, equity investor, agency) or services (law firm, architect, engineer, environmental, surveyor,
  contractor / builder, consultant / advisor, real estate broker, title / escrow, accountant / tax, insurance, vendor /
  supplier, other). `Firm.is_capital` says which side; an option added in Settings › Dropdown lists › Contacts ›
  Directory type picks the built-in type it works like. A capital source's provider field offers only the capital side.
  Never a Company or Person, so never in a send queue, list or outreach report.
- **Pages:** `/directory/` (sidebar Contacts › Directory; Capital › Lenders and investors opens it on the capital side;
  the old `/capital/providers/` redirects there): All / Lenders and investors / Consultants and vendors, a type filter,
  search by name, domain or contact; each firm's capital sources and project team roles and its last email.
  `/directory/<id>/` is one firm: project teams, capital sources, emails / meetings / capital log (120 days), contacts
  (kept on the firm, not as People), details.
- **Email:** the mail sync files mail from a firm's domains as `FirmEmail` (`mail/sync.py _park`, the hook the capital
  providers used) instead of the review queue. A firm's domain is never guessed onto a Company by name (the sync's
  name-spelling match skips it), can't be added to a Company (company form) or approved from the review queue, and a
  Company's domain can't be added to a firm (use Move to directory instead). Adding a domain to a firm moves what's
  already in the review queue onto it.
- **Mail review:** each queued domain has **File in directory** (name, type): the domain goes on a firm (an existing
  one of that name gets it added) and its queued mail moves there.
- **Move to directory** (company page, Managers only; `/companies/<id>/move-to-directory/` shows what happens first):
  domains and website to the firm; people become contacts and are deleted as People; emails become `FirmEmail`; notes,
  calls, comments (company's and its people's) are appended to the firm's notes; meetings stay on the calendar and show
  on the firm through its domains and contacts; it comes off any lists; the Company is deleted. Joins a firm of the same
  name if one exists. Blocked while the company has a tenant deal, document, contract or open task.
- **Project team:** a `TeamMember` links to its firm (`directory`, found by name or started on save, type from the role:
  civil → engineer, attorney → law firm, GC → contractor...). The Build page's firm field offers directory names and
  links each firm to its page. The real DB has no team members yet.
- **Tests:** `crm/tests/test_directory.py` (16), plus the capital tests updated for the rename; the whole suite (412
  after merging main) passes. Checked in a browser on a scratch database with made-up firms: the directory and its filters, moving a
  made-up law firm Company (its email, meeting, contact and notes arrived on the firm), File in directory from Mail
  review, the Build page's team links, and the capital pages after the rename.
- **Still open:** `terracon.com` and `greystar.com` have no Company yet: decide each in Mail review (Add as company,
  or File in directory). Fortress isn't in the directory yet (from the capital tracker's to-do).

## Built 2026-10-21: one meeting, not two (Kellen: "two meetings listed for Booz Allen on the same day?")

**Merged 2026-10-21** (`3d2a018`, CI green). No migration. **Applied on the real DB** (Kellen's OK): 11 calendar meetings folded into their Granola notes, 26 doubled timeline entries dropped; 403 → 392 meetings, Booz Allen now has one. Backup `backups/db-2026-10-21-before-meeting-dedupe.sqlite3`.
- **Cause:** a Google Calendar event only merged into a Granola note when the note was already there. The calendar
  sync runs every 15 minutes, so the event usually lands first; the Granola import then made a second meeting. Also,
  every calendar sync of an already-merged meeting re-wrote its title and link from the calendar and added a second
  "Meeting" timeline entry next to Granola's.
- **Fix (`crm/meetings.py`):** a Granola note now takes over a calendar meeting at the same time (within 15 minutes,
  similar title): same row, so its tasks, companies and team stay, with `calendar_id` set and the calendar timeline
  entries dropped. A calendar sync of a meeting Granola owns only fills the end time and adds no timeline entry.
- **Existing duplicates:** `manage.py merge_duplicate_meetings` (dry run; `--apply` to make the changes). Dry run on
  the real DB, 2026-10-21: 11 calendar meetings to fold into Granola notes (Booz Allen's Oct 20 call is one), 28
  doubled timeline entries to drop, no tasks on the calendar copies. (Applied; see above.) Three more same-time pairs have different titles and are left alone.
- **Tests:** `crm/tests/test_meeting_dedupe.py` (4).

## Built 2026-10-21: the sidebar hides until you hover (Kellen: "auto hide when I'm not hovering over the side")

**Merged 2026-10-21** (`99a9efd`, CI green). No migration.
- On a wide screen with a mouse (over 1000px, `hover: hover`), the sidebar is a 14px navy strip at the left edge with a
  "›". Hovering it (or tabbing into it) slides the full sidebar out over the page; it slides back about a third of a
  second after the mouse leaves. Pages get the full width.
- **Keep open** (above the user's name) pins it back as the fixed column; the button then reads **Auto-hide**. The
  choice is saved per browser (`localStorage nav:pinned`) and applied before the page draws.
- Phones, tablets and touch screens keep the existing layout (the menu across the top).
- Code: `base.html` (the pin button and two small scripts) and a marked section at the end of `app.css` ("Sidebar
  auto-hide … End sidebar auto-hide"). Checked in a browser on a scratch database at 1280 wide (hidden, hover, pin,
  pin kept across pages, unpin) and 375 wide.

## Built 2026-10-21: rename tasks and edit their details in place (Kellen: "I should be able to edit the name of tasks and the details")

**Merged 2026-10-21** (`a371a03`, CI green, pushed; branch and worktree removed). No migration, no data change.
- **Task page:** click the name (or the ✎ next to it) to rename; Enter or Save keeps it, Esc puts it back. The
  Description panel is always there: Edit (or "Add details" when empty) opens a textarea; Ctrl+Enter saves, Esc
  cancels. Links in the description are clickable. On a phone the description and comments come before the side form.
- **One source of truth:** both post to `task_text` (`/tasks/<pk>/text/`), which changes only the field it's sent and
  refuses a blank name. The side Card form no longer shows Title and Notes; `task_detail` fills them from the task, so
  saving the side form can't blank them.
- **From lists:** task rows (Tasks page, company action items) show a ✎ on hover (always on touch screens) that opens
  the task with the rename box open (`#edit`).
- Renaming a task made from a meeting action item doesn't change the item's text in the meeting notes; the item
  still links to the task.
- **Tests:** `crm/tests/test_task_edit.py` (6). Checked in a browser on a scratch database: rename from the list's ✎,
  the description with Ctrl+Enter, Esc, a side-form save keeping name and description, desktop and 375 wide.
- `.claude/launch.json`: removed a temporary `task-edit-scratch` entry that the contract-upload merge (`1c8233d`)
  committed by accident.

## Built 2026-10-21: Claude sorts email domains into the directory

**Merged 2026-10-21** (`e1a8b55`, CI green with 430 tests, pushed; branch and worktree removed). Real DB migrated through
`0054_domain_sort` (backup `backups/db-2026-10-21-before-domain-sort.sqlite3`). **First run** (Kellen's OK): 60 domains
sorted, **20 filed in the Directory** (checked by hand, all right: 7 contractors / GC bidders, 4 architects, 3 law firms
incl. Holland & Knight, Structurepoint, a surveyor, a NEPA consultant, a land broker, a PR firm, BGO as an investor);
the other 40 are suggestions on Mail review (13 directory at medium, 13 prospects, 8 not sure...). The scheduled sync
keeps going at 60 a run through the rest of the queue (about 410 domains left). **Companies check**, all 236 candidates:
**none flagged**; Claude read every one as a prospect, with reasons. The candidate filter is wide (an Exclude flag
mostly means a prospect opted out, and "engineering services" matches prospects), so it's worth narrowing before
anyone runs it again. The first try at the domains hit a dropped connection to Anthropic and simply stopped, as designed.

Kellen, on the directory: "this should be automated from email traffic." He picked (2026-10-21): **auto-file the sure
vendors, suggest the rest**, and **flag existing Companies** that look like vendors, without moving them.

- **After each mail sync** (`manage.py sort_domains`, added to `scripts/sync_mail.cmd`, `sync_all` and the cron
  endpoint; `crm/directory/sort.py`): Claude reads each review-queue domain it hasn't seen (busiest first, up to 60 a
  run, 20 per call, effort low; up to 5 emails each with the sender, subject and 500 characters of the body, where the
  signatures are). For each, a `DomainSort` (migration `0054_domain_sort`): prospect (Company), directory firm (with
  type), neither (newsletter, personal, one-off) or not sure (government bodies go here), confidence, a one-line
  reason and the signature contacts (only people at that domain).
- **Directory + high confidence:** filed automatically (`logic.file_domain`: an existing firm of that name gets the
  domain, else a new one) with the contacts and the waiting mail, and every Manager gets a bell note ("Claude filed
  x.com in the Directory as ...", one note per sync run). **Undo** (Directory › Sorted from email, the firm page, or the link on Mail review)
  puts the mail back in the queue, takes the domain off the firm, deletes a firm the filing started if nothing else is
  on it, and that domain is never filed automatically again.
- **Everything else is a suggestion** on its Mail review row ("Claude: Directory, law firm (medium)", the reason, and
  File as Claude says; for a prospect, Claude's name for it fills the Add as company box). **Claude never creates a
  Company**, so nothing reaches outreach without someone deciding.
- **Existing Companies** (Directory › Sorted from email › Check N more Companies with Claude, Managers, 20 a click; or
  `sort_domains --companies`): candidates are Companies with no tenant deal and a service-like sector or name, or
  flagged Exclude. Claude reads the record and its recent mail; the ones it reads as vendors are listed with the reason,
  Move to directory (the type preselected) and Keep as company. Nothing moves on its own.
- Claude off (no key) or failing never fails the sync: sorting just stops for that run and tries again next time.
- **Tests:** `crm/tests/test_domain_sort.py` (8, Claude mocked; 424 in all pass). Checked in a browser on a scratch database with
  made-up domains: the Mail review suggestions, File as Claude says, the Sorted from email page, Undo.

## Designed 2026-10-21: Trello boards on top of the CRM's records (step 1 built the same day; see the section after this one)

Kellen asked how to layer Trello's functionality in, exactly. The team ran two Trello boards: **Properties-Prospecting**
(lists per stage; already imported as tenant deals, `pipeline_import.py`) and the **MEIH board** (lists per deal:
Land Sites, Critical Dates, Deal and Design, Contacts and Consultants; labels deal info / legal / civil / architecture /
program / land use / critical date; critical dates with times). Pieces of Trello are already in the CRM but each was
built on its own: the Tenant pipeline Board tab (`opportunity_board`, `partials/board_js.html`), the Sites Board tab
(`land_sites.html`), Lists with per-entry stages (`List`, `ListEntry`, `list_detail.html`), the task card
(`task_detail.html`: rename in place, description, label, repeat, due time, comments) and comments with @mentions
(`Comment`, `notify.py`). None of them has card ordering, checklists, attachments, members, an activity log,
archiving or a keyboard path, and a record can't sit on more than one board.

**The one rule that makes this a layer and not a second Trello:** a card is a view of a CRM record wherever one exists.
Moving a card changes the record; changing the record moves the card. Cards with no record are allowed (a reminder,
an idea), and can be linked later.

### Model (`crm/boards/`, migration `0056_boards`)

- `Board`: name, kind (`project`, `campus`, `team`, `custom`, `bound`), campus and project (optional), description,
  colour, position, archived, members (M2M users, for "My boards"), created_by / at. Everyone signed in can open and
  edit every board (same as records); Managers archive, delete and import.
- `BoardList` (a Trello list, i.e. a column): board, name, position, archived, and two optional bindings:
  - `maps_to` = a status value of the board's record type (`Opportunity.stage`, `LandSite.status`,
    `CapitalSource.status`, `Approval.status`, `Contract.status`). A **bound list** is where every record with that
    status sits; dropping a card there calls the existing stage path (`opportunity_set_stage` →
    `notify.deal_stage_changed`, `LandSite` status + `LandSiteEvent`, and so on), and a stage change made anywhere
    else moves the card (TrackedModel.save already notices the change; a hook in `signals.py` moves the card).
  - `feed` = `critical_dates`, `open_tasks`, `upcoming_meetings`, `needs_filing`, `contracts`, `sites`: a **feed
    list** is read-only and filled from the records of the board's campus or project, so the MEIH board's "Critical
    Dates" list is the contract dates themselves and never goes stale. Cards in a feed list open the record.
- `Card`: list, position (integers 1,000 apart, reindexed when a gap closes), title, description, start, due, due_time,
  done (Trello's dueComplete), cover colour, archived, created_by / at / updated_at, `source_key` (unique; the import
  uses `trello:<cardId>`), and one optional record link: company, opportunity, site, contract, firm, approval,
  capital_source, meeting, task (explicit nullable FKs, as `Comment` does; `card.record()` returns the one that is
  set). Labels M2M → `BoardLabel`; members M2M → users.
- `BoardLabel`: board, name, colour (Trello's palette; the text is always shown next to the colour), position. New
  boards start with the team's seven categories (`Task.Label`).
- `Checklist` (card, name, position) and `ChecklistItem` (checklist, text, done, due, assignee, position, `task`
  optional). "Make it a task" turns an item into a real `Task` (reminders, My tasks) and keeps both in step; a `Task`
  with `card` set appears as an item.
- `CardAttachment`: card, name, url or file (`upload_to="cards/%Y/%m/"`, the bucket in production), added_by / at.
  A card linked to a company or deal also lists the record's `Document`s.
- `Comment.card` (nullable FK): comments, @mentions, watchers and notifications work unchanged; watchers add the
  card's members and creator.
- `CardEvent`: card, when, user, kind (`created`, `moved`, `renamed`, `label`, `member`, `due`, `done`, `archived`,
  `linked`), text. The card's activity log; moves of a bound card also land on the record's timeline as today.
- Saved filters per board through `SavedView` with `object_type="board:<id>"`, so the view bar works as on every list.

### Pages and behaviour

- `/boards/`: My boards (tiles with colour, campus, card counts, "N due this week"), every board, archived boards.
  "+ New board" offers blank, **Project board** (the MEIH layout: Land Sites feed, Critical Dates feed, Deal and
  Design, Contacts and Consultants, Open tasks feed), **Deal board** (one list per step), **Record board** (pick the
  record type; lists are its statuses, bound).
- `/boards/<id>/`: lists across the page, cards with a cover stripe, labels (colour + text), the due chip (red
  "Overdue", amber "Today", with the word), member avatars with names on hover and in the accessible label,
  checklist progress ("3/7"), attachment and comment counts, and a record chip (type and name, linking to the
  record). Add a card at the foot of a list (title, Enter adds the next). List menu: rename, add card, sort by due /
  label / name, move all cards, archive. Board menu: members, labels, colour, archived items, export CSV, delete
  (Managers). Filter bar: Mine, member, label, due (overdue, this week, none), text; saved as views.
- **Moving cards**: pointer drag within and across lists (position saved), and a keyboard path: a card is focusable;
  Space picks it up, arrows move it, Space drops, Esc cancels; every card also has a "Move…" menu (list and
  position) so no one needs to drag. Moves are announced in a live region ("Moved Apex Optics to LOI / PDA
  negotiation, position 2"). Lists reorder the same way. One component (`static/crm/board.js`) replaces
  `partials/board_js.html` and the Sites board script.
- **Card dialog** (`/boards/<id>/cards/<card>/`, opens in place, `?card=` deep link): title in place; "in list …"
  with Move and Copy; members, labels, dates (start, due, time, reminder on/off), done; the linked record with a
  search picker and the record's facts (stage, owner, last touch, next meeting); description (links, bold, bullets);
  checklists; attachments (upload, drop, or a Drive link); comments; the activity log; Watch; Archive; "Make a task
  from this card". Keyboard shortcuts as in Trello (`l` labels, `m` members, `d` due, Space assign me, `c` archive,
  `e` edit description), listed on the Help page.
- **Records**: company, deal, site, contract, firm and entitlement pages get an "On boards" row (board › list) and
  "Add to board". Tasks get "Add to board". The meeting pop-up's action items get "Add to board" next to "Make task".
- **Dashboard**: a "My cards" widget (due soon, assigned to me, across boards) in `prefs.WIDGETS`. **+ New** gets
  "Card" (pick a board). **Search** covers cards by title and shows the board and list.
- **Notifications**: added to a card, card due (the existing due sweep also covers cards with a due date and
  members), mentioned (exists), a card moved into a list you watch (optional per list).

### What the existing boards become

- The Tenant pipeline Board tab and the Sites Board tab are rendered by the boards component as **bound boards**
  ("Tenant pipeline", "Sites"; same tabs, same URLs, same colours), with a card auto-made for every open deal and
  site (data migration) and kept in step by the stage hook. Capital, Entitlements and Contracts get the same board
  view as a tab. Three hand-built boards become one engine with ordering and a keyboard path.
- **Lists** (`/lists/`) become boards: each `List` → a `Board(kind="custom")`, its stages → `BoardList`s, each
  `ListEntry` → a `Card` linked to the record. `/lists/…` redirects to `/boards/…`; the Lists pages and the
  "Directory › Lists" menu item go away (one word for one thing: Boards).

### Importing the real Trello boards (`manage.py import_trello <export.json> [--campus MEIH] [--project "4390 Indian Head Hwy"]`)

Reads Trello's standard JSON export: open lists → `BoardList` (position from `pos`); cards → `Card` (name, desc, due,
dueComplete, pos, closed → archived, idMembers → users through `pipeline_import.TEAM`, labels → `BoardLabel` with
Trello's colours); checklists and checkItems → `Checklist` / `ChecklistItem` (state); attachments → `CardAttachment`
(link to the Trello-hosted file first; `--download` fetches them into media); `commentCard` actions → `Comment` with
the author mapped and the original date; `source_key="trello:<id>"` so a re-run updates instead of duplicating.
Cards are linked to records by name (`merge.core_name` for companies and deals, site name, contract title); the
**dry run** (default) prints a report of what will link where and what won't, for a human to settle before
`--apply`. Back up first, and get Kellen's OK per board, as with every import.

### Accessibility and speed

Cards and lists are real buttons and regions with names; colour never travels alone (label text, due words, the
record type on the chip); contrast checked for every label colour on the card background; the keyboard path above.
One board page is one query per model (lists; cards with their record FKs selected; labels, members, checklist
progress and counts annotated), ordered by position, archived excluded; 500 cards render in well under 200 ms. A
query-count test guards it.

### Order of work (one branch each, CI, fast-forward merge; backup before migrating the real database)

1. `boards-core`: models and migration, board page, pointer and keyboard moves, card dialog (title, description,
   labels, members, dates, record link, comments, activity), My boards, + New › Card, search, tests. 3–4 days.
2. `boards-bound`: bound and feed lists; Tenant pipeline and Sites Board tabs on the component; Lists → Boards
   migration and redirects; the My cards widget. 2–3 days.
3. `boards-import`: `import_trello` with checklists, attachments and comments; dry run on Kellen's exports; apply
   board by board. 1–2 days.
4. `boards-polish`: checklist ↔ task sync, uploads to the bucket, templates, archived view, CSV export, Help page
   section, shortcuts. 2 days.

### Decided by Kellen before building

- Replace Lists with Boards (recommended: yes).
- Tenant pipeline and Sites boards move onto the boards component (recommended: yes; same look, one engine).
- Which Trello boards to import (the MEIH board; Properties-Prospecting is already in as deals; NSIH if there is
  one), and whether to download attachments or link to Trello (recommended: link first).
- Cards without a task still get due reminders (recommended: yes, through the existing sweep).
- Boards open to everyone signed in, with members only for "My boards" (recommended), or members-only visibility.

## Built 2026-10-21: boards-core (Kellen: "go with the defaults and start boards-core")

**Merged 2026-10-21** (branch `boards-core`, CI green on Postgres, 470 tests). Migration `0056_boards`: Board, BoardList,
BoardLabel, Card, Checklist, ChecklistItem, CardAttachment, CardEvent, `Comment.card`, and a `card` notification kind.
**Migrated on the real database 2026-10-21** (backup `backups/db-2026-10-21-before-boards.sqlite3`, taken right after a
mail sync; the eight new tables start empty); nothing else changes for existing data. The `boards-core` and
`docs-trello-boards` branches are deleted on GitHub. Decisions taken as recommended: Lists become Boards (next step),
the pipeline and Sites boards move onto the component (next step), Trello attachments link rather than download, cards
get due reminders through the daily sweep, every board is open to everyone signed in with members only deciding "My
boards".

- **Code:** `crm/boards/` (`logic.py`: positions 1,000 apart with renumbering, moves, templates, record links and facts,
  the activity log; `views.py`; `forms.py`), templates `boards.html`, `board.html`, `partials/board_list.html`,
  `board_card.html`, `card_dialog.html`, `board_tile.html`, `boards_row.html`; `static/crm/board.js`; styles at the end of
  `app.css` (`.bl-*`, `.cd-*`, label and cover colours). URLs under `/boards/` (`crm:boards`, `board`, `card`, `card_edit`,
  `card_move`, `card_checklist`, `card_attach`, `board_edit`, `board_list_edit`, `quick_card`, `record_add_to_board`).
- **Pages:** `/boards/` (My boards, other boards, archived boards; + New board with the Blank, Project board (the MEIH
  Trello layout) and Deal steps templates; + New card picks a board and list). `/boards/<id>/`: lists across the page,
  cards with labels (colour + text), a record chip (type and name), the due chip with the word (Overdue, Today, Done),
  checklist progress, attachment and comment counts, member initials; add a card at the foot of a list (Enter adds the
  next), add a list; list menu (rename, move left/right, sort by due / name / label, archive); Members, Labels and Board
  menu (name, description, colour, campus, project; archive and delete for Managers); the filter bar (Mine, member,
  label, due, text) saved as views through the view bar (`SavedView.object_type = "board:<id>"`); Archived items with
  "Send to board" and, for Managers, delete.
- **Moving cards:** pointer drag within and across lists (the drop index is the position among the list's cards), and the
  keyboard: Space picks a focused card up, arrows move it (up / down within the list, left / right across lists at the same
  index), Space drops, Escape puts it back; every step is announced in a live region. One POST (`card_move`) saves a
  move; a refused move snaps the card back. Positions: integers 1,000 apart; when two neighbours touch, the list is
  renumbered (`logic.place`).
- **Card dialog** (fetched into a `<dialog>`, `?card=<id>` deep link; opened directly, the URL redirects to the board with
  the card open): rename in place, "in list … on board", description (Ctrl+Enter saves), labels, members (newly added
  people get a bell note), start / due / time with a Done tick, a linked record with a few facts (stage, owner, last
  touch, next date…) and "Open <record>", checklists (items with a date; ticks save without a reload; an item linked to a
  Task keeps the task in step), attachments (a Drive link or an upload, served through `card_attachment_file`),
  comments (the shared partial, mentions and watchers include the card's members), the activity log, Move (list and
  position), Copy, Join / Leave, Cover colour, Archive; Delete only on an archived card and only for Managers.
- **Records:** the Record tool in the dialog searches through `/search/` and links a company, deal, site, contract, firm,
  entitlement, capital source, meeting or task (`logic.link_from_url`); "Rename the card after it" is optional. Company
  pages get a Boards panel (on which boards, with "Add to board"); other record pages get it in `boards-bound`.
- **Elsewhere:** Boards in the top bar after Calendar; search finds cards ("board › list"); + New › Card; recently opened
  records include boards; cards due today or overdue reach their members in the daily due sweep (`notify.due_sweep`).
- **Accessibility:** cards are focusable list items with a full label (title, due state, record), the keyboard path above,
  a live region, named lists and controls, labels always carry text, due states carry words; axe-core reports no
  violations on the board page or the card dialog. The board page runs a flat number of queries however many cards it
  holds (a test guards it).
- **Tests:** `crm/tests/test_boards.py` (21): positions and renumbering, moves and events, record links and facts, the
  board page and filters, add by fetch, the move endpoint and its board boundary, every card action, add to board from a
  company page, checklists and attachments, comments and notifications, list actions, board settings and Manager-only
  archive / delete, saved views per board, search and + New, the due sweep, the query count.
- **Checked in a browser** on a scratch database with made-up boards: keyboard pick-up / move / drop / cancel with the
  announcements, pointer drag, inline add, the dialog, record linking through the picker, checklist ticks, 1440 and 390
  px (the lists scroll sideways at 84vw each on a phone).
- **Not in this step** (next branches): bound lists (a list per status, the pipeline and Sites boards on this engine),
  feed lists, the Lists → Boards migration, the My cards widget, the Trello import, checklist → task conversion,
  CSV export, a Help page section.

## Built 2026-10-22: boards-bound, part 1 (bound and feed lists)

Branch `boards-bound` (not merged). Migration `0057_board_record_type`: `Board.record_type`. 487 tests pass locally
on SQLite (17 new in `crm/tests/test_boards_bound.py`); CI on Postgres not run yet.

- **Record boards** (`crm/boards/bound.py`): `create_record_board(type)` makes one list per status
  (`BoardList.maps_to`) of a deal, site, capital source, entitlement or contract, and a card for every record in the
  board's campus or project. Dropping a card on another bound list changes the record through the record page's own
  path (a deal's stage notification, a site's status log). A status changed anywhere else moves the card (a
  `post_save` hook in `signals.py`; archived cards stay archived; a drop doesn't log the move twice). Cards can't be
  typed into a bound list, and an unlinked card can't be dropped into one. "+ New board" offers "Record board: …" for
  each type.
- **Feed lists** (`BoardList.feed`: `sites`, `critical_dates`, `open_tasks`, `contracts`): read-only, filled from the
  board's campus or project each time the board opens (sites not Dropped, furthest along first; contract dates not
  done, by date; open tasks on the campus's sites and deals or linked from the board's cards). A card stored in a feed
  list for one of its records (what `import_trello` will make, so Trello comments stay reachable) shows in the
  record's place. Nothing can be dropped or typed into a feed. The Project board template's Land sites, Critical
  dates and Open tasks are feeds now.
- **Card facts:** contracts, entitlements and capital sources show "Waiting on … since …".
- **`manage.py boards_setup [--apply]`** (dry run by default; leaves boards that exist alone): the Tenant pipeline
  record board (every deal, all campuses) and the MEIH board as decided in `docs/TRELLO_MAPPING.md` (Land sites,
  Critical dates, The Forge – 4390 / ETC, ARA Facility – deal, ARA Facility – sites and design, Consultants, Open
  tasks; the seven category labels plus Ongoing, To do, Contact information; Kellen, Simon, David and Dave as
  members).
- **Checked in a browser** on a scratch database: the MEIH board's feeds (an overdue and a today date chip, the dropped
  site hidden, a stored card shown in its site's place) and a keyboard move on the pipeline board that changed the
  deal's stage and notified its owner; no console errors.
- **On the real database, after merging:** back up, `migrate`, then `boards_setup` (dry run, then `--apply`).
- **Not in this part:** the existing Tenant pipeline and Sites Board tabs still use their own boards (moving them onto
  this component is next), Lists → Boards, the My cards widget.

## Built 2026-10-22: data room (capital partners)

Branch `data-room`, stacked on `boards-bound` (not merged). Migration `0058_data_room`: DataRoom, DataRoomMember,
DataRoomDeal, DataRoomFile, DataRoomView, and `Opportunity.partner_note` ("Note for capital partners", on the deal's
Edit form). Code in `crm/dataroom/` (`logic.py`, `views.py`), templates in `crm/templates/crm/room/`, tests in
`crm/tests/test_data_room.py` (16).

- **Partner accounts:** a Manager invites a person (Settings › Capital partner data room); the hub shows a one-time
  sign-in link (14 days; only its hash is stored) to send them. They choose a password (12+ characters, not common,
  not like their name), which makes a user in the **Partners** group with username = their email. "New password
  link" resets it; "Switch off" ends access at once (sign-in and any open session). They sign in at `/room/login/`
  through their own backend (`crm.auth.PartnerBackend`), which works with the team's password sign-in off; the
  team's sign-in refuses partner accounts and the partner sign-in refuses the team. Five wrong passwords lock an
  account or address for 15 minutes (cache-based: per server instance on Cloud Run).
- **Confinement:** `crm.middleware.PartnerOnlyMiddleware` sends a partner account back to `/room/` from every other
  page (records, search, boards, admin, document downloads, the Manager page). Partners never count as the team:
  `permissions.team_users()` now feeds the assignee and owner lists, @mentions, meeting attendee matching and mail
  sync's teammates (so Fortress mail isn't mistaken for internal mail).
- **What a partner sees** (built from an allow-list in `crm/dataroom/logic.py`, never from internal pages): the
  pipeline as a table or a board by stage: tenant, campus / site, deal and asset type, stage, acres, size, project
  cost and $ PSF (room setting), target close, est. delivery; win % and weighted only if the room turns them on.
  Deals on the investor report, minus those switched off, never On hold or Cold / lost. A company card per deal:
  company, website, sector, those facts, the deal's partner note and its approved documents. Never notes, next steps,
  lead source, owners, contacts, emails, meetings, comments or Trello history.
- **Files:** a Manager approves a deal's document or uploads a file (for a deal, or the top of the room). Uploads are
  served through the room; a Drive link opens only if the partner has Drive access. Files of a switched-off deal
  stop downloading. Every page view and download is in the room's log ("What they looked at").
- **Manager page** (`/data-rooms/`): deal switches (with a "no partner note" hint), shared files, people and their
  state, room settings (partner firm, intro, campuses, show costs, show win %, on / off), the log, and "See what
  Fortress sees" (a preview that isn't logged).
- **Checked in a browser** on a scratch database: the Manager page, making a partner account from the link, the
  pipeline and company card as the partner (no internal fields), internal URLs bouncing to `/room/`, phone width.
- **On the real database, after merging `boards-bound` then this:** back up, `migrate`, then Settings › Capital
  partner data room › "Start a data room" (Fortress), write partner notes on the investor-report deals, invite the
  Fortress people. Partners can only reach it once the hub is hosted (hub.acmigroup.com).

## Built 2026-10-25: Drive folders for companies, projects and the data room

Branch `data-room` (on `boards-bound`; not merged). Migration `0059_drive_folders`: `Company.drive_folder`,
`Project.drive_folder`, and `DataRoomFile.drive_id / drive_name / drive_folder / drive_mime`. `crm/drive.py` (read-only
Drive API through a service account; version grouping by dated names; `current_documents`, `newer_version`,
`suggest_folder`), `crm/dataroom/drive_views.py` (the panel, mapping, share, swap), `partials/drive_panel.html` and
`drive_panel_slot.html` (fetched after the page loads). The room's file view streams Drive files
(`dataroom.views._from_drive`); Drive errors show a plain message, never Google's. Google's auth library is imported
only when Drive is used. 12 tests in `crm/tests/test_drive_files.py` against a fake Drive. Checked in a browser on a
scratch database without Drive connected (panel, mapping); listing real folders waits on the service account. Design
and setup: `docs/DATA_ROOM.md`, "Drive folders and files".


## Built 2026-10-25: mail-connect (email client step 1; Kellen: "go with the recommended answers… start step 1")

Branch `mail-connect`, migration `0061_mail_connect` (after web-push's 0060). Design in `docs/EMAIL_CLIENT.md`. No mail on screen yet: this
step is the switch, the permission and the safety around them.

- **Each person's switch:** avatar menu › **Mail in the hub** (`/mail-setup/`, `crm/mail/connect.py`, `MailConnection`).
  Off by default. **Turn on Mail** goes through allauth's Google connect asking for `gmail.modify` on top of the
  existing grant (`include_granted_scopes=true`, `prompt=consent`); coming back, the hub checks with Google's tokeninfo
  that Gmail really was granted and that the Google account is the person's own address, then switches it on.
  **Turn off Mail** revokes the permission at Google and deletes the token. Google keeps one permission per person for
  the hub, so this disconnects the calendar too; the page says so and shows Connect Google Calendar.
- **Site-wide:** `ACMI_MAIL_CLIENT=false` hides it for everyone. Mail can't be turned on without `ACMI_TOKEN_KEY`.
- **Google tokens encrypted at rest** (`crm/tokens.py`): with `ACMI_TOKEN_KEY` set, allauth's `SocialToken` (calendar
  and Gmail) is stored as `enc1:` + Fernet and decrypted on load, so `crm/gcal.py` and allauth read plain values.
  Comma-separated keys rotate (first encrypts, any decrypts). Migration 0061 encrypts existing tokens if the key is
  set when it runs; otherwise `manage.py encrypt_tokens` does it later. A lost or wrong key reads as an empty token:
  people reconnect, nothing crashes.
- **Every sign-in keeps earlier permissions:** `include_granted_scopes=true` is now in the Google `AUTH_PARAMS`, so
  reconnecting the calendar doesn't drop Gmail.
- **Partners kept out:** `PartnerOnlyMiddleware` (already), the Mail views 404 for a partner themselves, Turn on Mail
  and `finish` refuse partners, the sign-in adapter refuses a partner connecting Google and anyone connecting a
  different ACMI address, moving someone into Partners (either side of the group) or deactivating an account turns
  their Mail off and revokes the token, and the mail and calendar syncs only read `team_users()`.
- `Mailbox.backend` (`imap` / `gmail_api`) and `Mailbox.history_id` are added for step 2; everything stays on IMAP.
- **Tests:** `crm/tests/test_mail_connect.py` (29; Google faked, nothing reaches Google). 545 pass in all. Checked in
  a browser on a scratch database (off and on states, phone width); the Google screen itself wasn't opened.
- **Before Mail can be turned on for real (Kellen):** in Google Cloud project `sincere-passage-511621-j3`, enable the
  Gmail API, confirm the OAuth consent screen is **Internal** and add the `gmail.modify` scope; a Workspace super
  admin confirms acmiproperties.com is in the same Workspace and the hub's client is allowed under Security › API
  controls. Then put `ACMI_TOKEN_KEY` in `.env` (Claude can generate it without printing it; keep a copy in the
  password manager).
- **Merged and migrated 2026-10-25** (PR #4, `eb7ca51`): key set in `.env` first, then backup
  `backups/db-2026-10-25-before-mail-connect.sqlite3`, fast-forward, `migrate crm`. The one stored Google token
  (Kellen's calendar) is encrypted (`enc1:`) and decrypts; mailboxes stay on IMAP; no one has Mail on yet.
  **If the key is ever lost,** tokens read as empty and people reconnect Google; nothing else breaks.
  The 22:47 scheduled sync ran clean on the encrypted token (mail caught up, calendar 366 meetings).


## Built 2026-10-25: mail-api-reader (email client step 2; Kellen: "Yes, start step 2")

Branch `mail-api-reader`, migration `0062_mailbox_pending_ids`. Still nothing new on screen apart from the Mail page's
"read through Mail in the hub": this step changes how the timeline sync reads Gmail.

- **Who switches:** each sync calls `sync.choose_backend`. A mailbox whose owner has Mail in the hub on (and whose
  address is their own) is read through the Gmail API; Mail off, or the key gone, puts it back on IMAP. A switch
  clears the marker. Kellen's mailbox switches on the first sync after this merges (his Mail is on).
- **The reader** (`crm/mail/gmail_api.py`) produces the same `ParsedEmail` as IMAP and hands it to the same
  `process_messages`, so classification, matching, Mail review, the directory and stages are untouched.
  - Headers go through `parse_message` (it now takes text as well as bytes); bodies come from `format=full`, which
    leaves attachment bytes in Gmail (names and sizes only, as before). Gmail's system labels are mapped to IMAP's
    names (`INBOX` → `\Inbox`, `DRAFT` → `\Draft`...); Chat is skipped.
  - **Thread ids are stored in decimal**, as IMAP's X-GM-THRID: the API's hex id is the same number.
  - **First read / switch-over:** note Gmail's `historyId`, then queue everything `after:` a day before the last
    IMAP sync (or the backfill window for a new mailbox), oldest first. **Later reads:** `history.list` from the
    marker. A marker too old for Gmail (about a week) falls back to reading by date. Message-ID keys make every
    overlap harmless (`activities already logged`).
  - **The queue** (`Mailbox.pending_ids`) holds what's waiting; up to 500 a run (8 requests at a time), and it only
    shrinks after the batch is logged, so a dropped connection loses nothing.
  - Errors in plain words on the Mail page: Google refused access / another account → "Turn on Mail again".
- **New-mail alerts** (`crm/mail/client.py on_new_mail`, the hook the web-push session built `notify.new_mail` for):
  only messages that arrived since the last run (never a back-read), in the Inbox, Primary tab (Promotions,
  Social, Updates, Forums stay quiet), not sent by you, not older than two days, and only while Mail is on. The
  alert opens the thread in Gmail on the web until the hub has its own inbox; `notification_open` now allows
  `https://mail.google.com/` and still refuses every other outside address.
- **Tests:** `crm/tests/test_gmail_api.py` (17, Gmail faked), including the same two emails read by IMAP and then
  the API giving the same two timeline rows and thread id.
- **Merged and migrated 2026-10-26** (PR #5, `3b32a33`). The 9:17 sync switched Kellen's mailbox to `gmail_api`:
  it read the 10 messages since Oct 25 (3 newsletters and 5 unknown or ignored senders skipped, 2 with no outside
  address), took Gmail's marker, empty queue, no error, Gmail timeline rows unchanged at 27,413, no alerts (a back-read
  never alerts). To go back: turn Mail off (IMAP resumes from its last UID).


## Built 2026-10-26: mail-inbox (email client step 3; Kellen: "Yes, start step 3 … merge when green")

Branch `mail-inbox`, migration `0063_mail_index`. **Mail** in the top bar (only for people with Mail in the hub on)
opens `/inbox/`: folders, the conversation list, the reading pane. Read only: archive, labels and the rest are step 4,
compose and reply step 5.

- **Folders:** Gmail's system folders in Gmail's order, All mail, then your own labels with their colours
  (`MailLabel`, re-read every 10 minutes; Inbox unread from Gmail). On narrower screens a Folders button.
- **The list** comes from Gmail's own order each time (`threads.list`, 50 a page, Older for more), drawn from the
  private index (`MailThread`, `crm/mail/index.py`) and re-reading only threads whose `historyId` changed. Unread
  bold with a bar, paperclip, star, message count, "me" for you.
- **CRM tags on every thread** (`index.chips`): projects and deals and companies from what the timeline logged for
  that thread, directory firms from `FirmEmail`, then the senders' domains; **New sender** (red) when the domain
  waits in Mail review. Chips in the reading pane link to the record.
- **Split inbox** (Inbox only): All, Tenants, Projects, Capital & directory, Everything else, from flags stored on
  the index. The sync fills the inbox's newest 300 threads once (`warm_inbox`, `Mailbox.inbox_indexed_at`) and
  re-reads every thread Gmail's history says changed (new mail, label changes; up to 300 a sync). Rows untouched for
  90 days are dropped.
- **Search** is Gmail's own search across all mail (`q=`), so `from:`, `has:attachment` and plain words work.
- **Reading pane:** newest message first and open (and any unread), older ones folded with a one-line preview; To
  and Cc; attachments listed under each message; Open in Gmail. Bodies load in a sandboxed frame from
  `/inbox/m/<id>/body/` (allow-list sanitizer from news review, no scripts, `<base target=_blank>`, its own CSP with
  `img-src 'self' data:`). **Remote images are held back** (`data-blocked`) until **Show images**, which reloads the
  frame with `?images=1`. Inline `cid:` pictures come through the hub. Messages are cached 5 minutes in memory only.
- **Attachments** stream from Gmail through `/inbox/m/<id>/part/<partId>/` (part ids, because Gmail's attachment
  ids change between reads), always as downloads (`application/octet-stream`, sandbox CSP), except inline png, jpeg,
  gif and webp that a body shows.
- **Privacy:** every view goes through the signed-in person's own mailbox and token (a thread that isn't theirs is
  404 from Gmail); partners are redirected by the middleware and refused by the views; `MailThread` and `MailLabel`
  are not in the admin. Someone with Mail on and no timeline mailbox gets an inbox-only one (`enabled=False`, so
  the timeline sync stays off for them).
- New-mail alerts now open the thread in the hub's inbox.
- **Tests:** `crm/tests/test_inbox.py` (19). Checked in a browser on a scratch database with made-up mail: three
  columns on a wide screen, two at tablet width with the Folders button, one at phone width with Back, blocked
  images and Show images, the tags and tabs, no console errors, no page wider than the phone.
- **Not yet:** marking read when opened (step 4 adds read / unread), the long-thread tools (step 3b).


## Built 2026-10-26: mail-long-threads (email client step 3b; Kellen: "threads that have over 80 messages… hard to find attachments or the most recent message")

Branch `mail-long-threads`, no migration. All in the reading pane of `/inbox/` plus a Files page.

- **Newest first** (as before), with **Oldest first** next to it, remembered per person (`UserPrefs` "mail_order").
  In oldest-first order a "New since you last read" line sits above the first unread message and the pane opens
  there; newest first, unread messages are already on top (open, tagged New).
- **Attachments panel** at the top of every thread with files (`crm/mail/threads.py`): newest first, a filter box and
  type buttons (PDF, Word, Excel, Images...), **Go to message**, **Download all** (a zip of the newest version of each
  document, built in memory, up to 100 MB). **Versions grouped** by `doc_key`: dates, v2 / rev 3, "redline", "clean",
  "final", "(1)" and the like don't make a new document; a PDF stays separate from the Word file. **Hidden**: logos and
  signature pictures (small images, `image001.png`), `.ics` files, and the same file re-attached unchanged; each
  message's own file list leaves logos out too.
- **Outline** ("All 85 messages", for threads over three): one line per message, oldest first and numbered (drafts
  unnumbered), filter by person or "only with attachments", click to jump.
- **Only the new words:** each body's quoted history (Gmail, Outlook, Apple Mail, "Original Message", "On … wrote:")
  is folded behind **Show quoted text**, inside the sandboxed frame (`render.fold_quotes`); a bare forward stays open.
- **Find in this thread:** matches counted from each message's text (sent once with the page), Enter / arrows step
  through the messages, matches highlighted inside the frames (quoted text opens when it has one).
- **Latest message** button whenever the newest message is off screen.
- **Other parts of this conversation:** for threads of 50+ messages, Gmail search on the subject finds the other
  threads with the same subject and some of the same people (Gmail starts a new thread at about 100).
- **Summarize** (only with `ANTHROPIC_API_KEY`, only on click; `crm/mail/summary.py`): the thread's new words,
  numbered, go to Claude (`claude-opus-5-5`, effort medium, structured output, refusal fallback); back come where it
  stands, what's open and who owes it, the latest documents and the next step, each linking to its message. Cached in
  memory an hour per thread version, never stored.
- **Files page** (`/inbox/files/`, "Files" in the folder pane): every attachment in the mailbox through Gmail search
  (`has:attachment`), by type, words, project (its name and keywords) or company (its domains), versions grouped,
  25 messages a page.
- **Fix for step 3:** Gmail answers "slow down" with a 403 as well; that was read as a lost permission and stopped the
  first inbox fill on the laptop at 299 of 300 threads (10:02). `GmailClient.get` now reads the reason and waits and
  retries; the index reads three threads at a time. (The fill had in fact reached 303 threads; the 10:17 sync marked
  it done with no error.)
- **Tests:** `crm/tests/test_long_threads.py` (16) plus rate-limit tests in `test_gmail_api.py`; 621 pass. Checked in a
  browser on a scratch database with a made-up 85-message thread (desktop and phone width).


## Built 2026-10-26: mail-actions (email client step 4; Kellen: "Yes, start step 4 … merge when green")

Branch `mail-actions`, migration `0064_mail_snooze`. Every change is a label change on the person's own Gmail
(`crm/mail/actions.py`, `POST /inbox/act/`), so it shows in Gmail on the phone a moment later.

- **In the reading pane:** Archive (or Move to inbox, or Restore in Trash), Mark unread, Star, Trash, **Snooze**
  (later today, tomorrow 8 AM, this weekend, next week, or a picked date and time), **Label** (tick your own labels on
  and off) and **Move to** (label and archive). Keys: `e` archive, `s` star, `#` Trash, `Shift+U` unread, `z` undo,
  plus `j` / `k` and `/` from before.
- **Several at once:** a tick box on each row; the bar that appears has Archive, Read, Unread, Star, Trash, Snooze and
  Add label / Move.
- **Undo** after every change: a dark bar above the list with Undo (kept in the session for the next page only).
- **Opening a thread marks it read**, as in Gmail; the messages that were unread still show "New" that once.
- **Trash** is Gmail's Trash (Gmail empties it after 30 days). Nothing in the hub deletes permanently; unknown
  actions are refused. Labels and Move only take the person's own labels (never Spam or other system labels).
- **Snooze** is the hub's own (Gmail's isn't open to apps): the thread leaves the inbox under a Gmail label
  `ACMI/Snoozed` (made the first time) and `MailSnooze` keeps the time; each 15-minute sync (`actions.wake_due`, also
  for inbox-only mailboxes) puts due threads back in the inbox, unread. A snoozed row shows ⏰ and its time.
- `next` after an action must be a hub path; archive, move, Trash and snooze close the reading pane, as in Gmail.
- **Fix from 3b:** the Latest message button showed even when it shouldn't (`.mail-latest[hidden]`).
- **Tests:** `crm/tests/test_mail_actions.py` (15); 637 pass. Checked in a browser on a scratch database: the toolbar
  and menus at desktop and phone width, bulk select, `e` to archive and `z` to undo.


## Built 2026-10-26: mail-compose (email client step 5; Kellen: "Yes, start step 5 … merge when green")

Branch `mail-compose`, no migration. `crm/mail/compose.py`, `compose_views.py`, `/inbox/compose/`.

- **Where:** ✎ New message at the top of the folder pane (`c`); **Reply / Reply all / Forward** above each thread
  (`r` / `a` / `f`, on the newest message that isn't a draft) and under every message; **✉ Email** on a person's page
  and each person's address on the company page's People tab (only for people with Mail on).
- **Addressing:** Reply goes to Reply-To or From (or, for your own message, the people you wrote to); Reply all adds
  everyone else on To and Cc except you; Forward starts empty with `Fwd:` and the original's attachments ticked.
  Replies stay in the Gmail thread (`threadId`, `In-Reply-To`, `References`) and quote the original the way Gmail
  does (plain `>` lines, HTML `gmail_quote`; pictures left out because the hub's links wouldn't work outside).
- **The page:** To, Cc / Bcc, Subject, **Insert a template** (the outreach templates' text, `{first_name}` filled
  when the hub knows the person), the message, your **signature** (set on Mail in the hub; default your name), files
  (25 MB in all, Gmail's limit) and, on a forward, which original files to keep. Bad addresses, no recipient or too
  much attached come back with the text kept.
- **Undo send:** Send counts down 10 seconds with Undo; leaving the page stops it.
- **Drafts:** saved to Gmail 3 seconds after you stop typing (text only; files go on at Send), updated in place, and
  dropped once the email is sent or you click Discard.
- **Sending:** Gmail API multipart upload (`GmailClient.send`), never retried after a Gmail server error (the email
  may have gone; the message says to check Sent). `ACMI_MAIL_SEND=false` blocks Send; it is always off while the
  tests run. Logged on the timeline at once through `process_messages` (same key as the sync, so the next sync says
  "already logged"); not for inbox-only mailboxes, whose mail isn't logged anyway.
- **Tests:** `crm/tests/test_compose.py` (17, fake Gmail records what would be sent); 654 pass. Checked in a browser
  on a scratch database through the fake Gmail (nothing really sent): reply from `r`, the countdown and Undo, a draft
  saved, Send and back to the thread, forward at phone width.
- **Not yet:** recipient autocomplete, rich-text formatting (bold, links) in the editor, "remind me if no reply".


## Built 2026-10-26: inline reply and the Gmail signature (Kellen: "I would rather keep the reply function inline with the email message… my signature isn't showing up")

Branch `mail-inline-reply`, no migration.

- **Inline reply:** Reply / Reply all / Forward (buttons, `r` / `a` / `f`, the links under each message) open a reply
  box right under that message in the reading pane, filled from `/inbox/compose/fields/` (to, Cc, subject,
  threading, the original's files on a forward, the signature). It has the same Cc / Bcc, files, draft autosave and
  10-second Undo as the full page, sends in the background (`inline=1` answers JSON) and reloads the thread with the
  reply in it; mistakes show in the box with the text kept. **Full page** opens the old page; New message still uses it.
  The page script is now `crm/static/crm/compose.js`, shared by both.
- **Signature:** the hub only added the person's name. It now uses **the signature set in Gmail** (read from Gmail's
  send-as settings with the existing `gmail.modify` permission, cleaned, cached an hour), shown under the reply box
  and the compose page. Mail in the hub › Your signature: the one in Gmail (default), a different one for the hub, or
  none (`UserPrefs` "mail_signature_mode" / "mail_signature").
- **Tests:** 5 more in `crm/tests/test_compose.py`; 659 pass. Checked in a browser through the fake Gmail: `r` opens
  the box under the newest message, Send counts down and returns to the thread with the reply, Reply all at phone
  width with the Gmail signature showing.
- **Merged 2026-10-26** (PR #10, `29adfa6`).


## Built 2026-10-26: mail-filing (email client step 6; Kellen: "Yes, start step 6 … merge when green")

Branch `mail-filing`, migration `0065_mail_filing`.

- **File to…** in the reading pane's toolbar: type two letters, pick a company, deal, project or directory firm
  (`crm/mail/filing.py`, `MailFiling`). The thread's emails go on that record's timeline with the sync's own keys
  (`gmail:<Message-ID>|c<company>`, or `|j<project>` for a project-only email), so nothing is logged twice; a deal
  also gets `Activity.opportunity`, a project `Activity.projects` and context Project; a firm gets `FirmEmail` rows,
  never a Company. A sender whose domain waits in Mail review can come along ("Also attach newco.io…", Mail
  review's own `approve_domain`). **Filed to** chips with × undo exactly what the filing added.
- **The projects tagger keeps hand-filed projects** (`projects.Tagger.filed`), whatever the keywords say.
- **Open in Mail** on every Gmail entry of a timeline (for people with Mail on): `/inbox/open/?m=<Message-ID>` finds
  that email in your own mailbox (`rfc822msgid:`) and opens its thread; "That email isn't in your mailbox" if not.
- **CRM labels in Gmail** (`crm/mail/crm_labels.py`, `MailCrmLabel`, `MailCrmLabelOnThread`): `ACMI/Projects/<project>`
  for every project a thread is tagged with, `ACMI/Tenants/<company>` for companies with an open deal or filed,
  `ACMI/Capital/<firm>` for lenders and investors, `ACMI/Directory/<firm>` when filed; parents made as needed. Each
  sync labels the threads that changed (and once, the last 90 days of the index, `Mailbox.crm_labels_done_at`) and
  renames labels when a record is renamed. A label taken off in Gmail stays off; one added in Gmail files the thread
  (`MailFiling.Source.GMAIL`). Switch on Mail in the hub ("CRM labels in Gmail", `UserPrefs` "mail_crm_labels",
  default on).
- **Team notes** on a thread (`crm/mail/comments.py`, `MailComment`, `MailCommentKey`): "Add a note for the team",
  @mentions by the hub's usual rules, a bell / browser alert that opens the email in the mentioned person's own
  mailbox. Only the author and the people mentioned see a note; it's matched by the thread's Message-IDs, because the
  same conversation has a different Gmail id in each mailbox.
- **Tests:** `crm/tests/test_mail_filing.py` (17); 676 pass. Checked in a browser on a scratch database: File to…
  finds the deal and the company, filing a gmail.com sender's thread puts the deal and company tags on it.


## Built 2026-10-26: mail-shared (email client step 7; Kellen: "Start step 7 (shared mailboxes), merge when green")

Branch `mail-shared`, migration `0066_shared_mailboxes`. `crm/mail/shared.py`.

- **A shared mailbox is a Mailbox with no user** (`Mailbox.shared`, `name`; `user` is now nullable) and its own
  Google permission (`SharedMailboxToken`, encrypted like every token). Its index, labels, snoozes, filings and CRM
  labels work as for a person's mailbox; `gmail_api.client_for(mailbox)` picks the right permission.
- **Connecting one (Managers):** Mail in the hub › Shared mailboxes › Connect a shared mailbox: the hub sends the
  Manager to Google **signed in as that address** (`login_hint`, `gmail.modify`, offline), checks on the way back that
  it really is that address and Gmail was granted, and keeps the permission. Refused: non-ACMI addresses, a teammate's
  own address (they turn Mail on themselves), no `ACMI_TOKEN_KEY`. Not domain-wide delegation.
- **Access:** Managers give teammates (only `team_users()`) **read** or **read and send** (`MailboxAccess`); read and
  send also allows archive, labels, Trash and snooze, and is the only way to send. With read only, the toolbar,
  reply box and tick boxes don't show, the views refuse changes, and opening a thread doesn't mark it read for
  everyone. Partners are refused at the grant and again at every check (`shared.role`).
- **In Mail:** a switcher at the top of the folder pane (Your mail / 👥 Deals; `?mb=<id>`, `?mb=own`, kept in the
  session). Someone with only a shared mailbox (no Mail of their own) still gets the Mail link and lands in it.
- **Sending as it:** From is "Deals <deals@…>", with the shared account's own Gmail signature; the timeline entry's
  actor is the teammate who sent it.
- **The sync** reads connected shared mailboxes with their own permission (only the last 30 days on the first read);
  their mail reaches timelines unless "Log its mail on CRM timelines" is unticked; no one person's connection is
  recorded on those entries. New-mail alerts go to everyone given the mailbox ("[Deals] …").
- **Access log** (`MailboxAccessLog`, Mail in the hub › Access log): every open, send, change, filing and download
  in a shared mailbox, with who did it. **Disconnect** revokes at Google and drops the hub's index, labels, snoozes and
  grants (timeline entries stay).
- **Tests:** `crm/tests/test_shared_mailboxes.py` (16); 692 pass. Checked in a browser on a scratch database: the
  Managers' panel and the switcher.
- **Before the first one (Kellen):** in Google Cloud › APIs & Services › Credentials › the hub's OAuth client, add
  `http://localhost:8000/mail-setup/shared/connected/` to **Authorized redirect URIs** (and the hub.acmigroup.com one
  when hosted). The shared address must be a real Workspace account (a Google Group isn't a mailbox), and whoever
  connects it needs its password.


## Built 2026-10-26: mail-outreach-api (email client step 8; Kellen: "Yes, start step 8 … merge when green")

Branch `mail-outreach-api`, no migration. `crm/mail/outbound.py`.

- **One way out:** `outbound(mailbox)` is the Gmail API (`GmailOutbound`) when the sender's Mail in the hub is on for
  their own address (or it's a shared mailbox), and the old `Smtp` with the app password otherwise. Outreach batches
  (`run_batch`), test sends, the weekly outreach report and campus updates all use it.
- **Gmail's own Message-ID:** after each API send the hub reads back the Message-ID Gmail really sent with
  (`GmailClient.sent_message_id`) and logs that, so the timeline key matches what the sync reads later (also for
  compose). Outreach entries now carry the Gmail thread id too.
- **Never twice:** a Gmail server error while sending isn't retried (`UnconfirmedSend`); in a batch that email stays
  "sending (unconfirmed)" and the batch stops, so nobody gets it twice. A lost permission fails the batch with "Turn
  on Mail again".
- **News review drafts** (`crm/news/drafts.py ApiDrafts`, `open_drafts`): list, edit in place and move to Trash
  through the Gmail API for the person's own mailbox with Mail on; IMAP otherwise (including the old fallback to
  another person's mailbox, which never goes through anyone else's Google permission).
- **Tests:** `crm/tests/test_outreach_api.py` (8, SMTP patched to fail if it's touched); 700 pass.
- **App password retired 2026-10-26** (Kellen: "remove the ACMI_IMAP_PASSWORD line"): the
  `ACMI_IMAP_PASSWORD_KELLEN` line is out of `.env` (backup `backups/env-2026-10-26-before-app-password-removal.bak`,
  which still holds it: delete that backup once the password is revoked in Google). The 14:47 sync ran clean through
  the Gmail API. Kellen deletes the app password itself in his Google account. The old scripts in
  `Desktop/Prospecting` (retired) carried it in plain text too and stop working once it's revoked.
- **Retiring the app password, as it was planned:** everyone who sends outreach or reports turns Mail on
  first (today that's Kellen, already on). Then: Google Account › Security › App passwords › remove the hub's; and
  delete the `ACMI_IMAP_PASSWORD_KELLEN` line from `.env` (Claude can do that part). Anyone without Mail on would
  then fall back to IMAP and fail, with a message saying so.


## Built 2026-10-26: ux-capture (UX second pass area 1)

Branch `ux-capture`, migration `0068_ux_capture` (may be renumbered at merge). Findings C1–C5 and the code part of G1
in `docs/UX_REVIEW.md` "Second pass", with decisions 2 and 3 as recommended. `crm/needs_filing.py`, `crm/invites.py`.

- **Needs filing (C1)** at `/needs-filing/` replaces Mail review (`/mail/` redirects there; the menu item, search
  page link and badge are renamed). One card per unknown domain joining its queued emails, its meetings (by attendee
  domain, 90 days back to 60 ahead) and its emailed invites. Claude's domain verdict is the headline with one primary
  action: "Looks like a bank. File Harbor Point Bank as a lender on Project Anvil?". **Accept** files the firm (or
  adds the company, or ignores the domain), links its meetings, and for a bank, debt fund or broker adds an
  Identified capital source on the project. **Change** opens the same choices by hand (directory type, project,
  company or existing company, ignore, name). **Not now** hides the card 14 days (`FilingSnooze`); **Never** is the
  Ignore filter. The project guess: project names and keywords in subjects and meeting titles, then the projects
  the same threads are tagged or filed with, then the projects the other firms and companies at its meetings work
  on. Cards with a meeting come first, then Claude's surest verdicts, then the latest. 25 per page; `?domain=` shows
  one card (the calendar's File link). Mailbox setup, team mailboxes and filters moved to the bottom of the page.
  Undo works as before for what Claude filed on its own, and now also takes the firm off its meetings.
- **Claude files more on its own (decision 2):** a directory verdict at high confidence, or at medium once the team
  has met someone at the domain (a medium verdict from an earlier run is filed when a meeting turns up). Domains seen
  only as meeting attendees are sorted too, with the meetings in the evidence. Still never a Company.
- **Firms on meetings (C2):** `Meeting.firms`, filled at import, from invites and after any filing by attendee domain
  or firm contact email (`needs_filing.link`; companies are linked the same way). Firm chips with the type on
  calendar rows, the pop-up and the week widget; "Coming up" meetings on the firm page, a project's Capital page (its
  providers) and Build page (its team). The firm page's meeting list also reads the links.
- **"No record" marker (C3):** `Meeting.unfiled` / `unfiled_domains`: outside attendees no person, company or firm
  knows (free mail and ignored domains don't count). "2 outside attendees, no record · File" on the calendar, the week
  widget and the pop-up (attendees tagged No record); a "No record" filter on the calendar. Counted in the Outreach
  badge (the Needs filing count) and a once-a-day bell note to Managers from the due sweep ("Needs filing: 3 meetings
  this week with outside people and no record; 12 organizations waiting"). The links are refreshed for the window
  at the start of every `sort_domains` run (every 15 minutes), with or without Claude.
- **Emailed invites (C4):** the mail sync keeps calendar parts (`ParsedEmail.calendar`, both readers; an attached
  .ics with no data inline is skipped) and reads them to RFC 5545 (folding, quoted CN, TZID including Outlook's
  Windows zone names, alarms), or Google's "Invitation: Title @ Thu Oct 29, 2026 2pm - 3pm (EDT)" subject when there's
  no calendar part. Forwarded ones too. The meeting is source "invite" (`invite:<UID>`); a meeting already within 15
  minutes with a similar title or a shared outside attendee just gains the guests; a later calendar event or Granola
  note takes over an invite row the same way. Cancellations remove an unclaimed invite row; replies never add one;
  ACMI-only invites are left to the calendar sync. An odd invite never fails the sync.
- **Same person (C5):** a free-mail sender whose display name (two words or more) is exactly one Person's name is
  held as `AddressSuggestion`, shown on Needs filing: "Same person, add this address" puts the address in
  `other_emails` and the waiting email on their timeline; "Not them" stops it for that address. **Search** now finds
  queue senders and unfiled meeting attendees (group "Not filed yet", badge "Not filed yet") and firm contacts (group
  "Directory contacts": they are filed, on a firm, so they aren't called unfiled).
- **Action items (G1, code only):** `Task.meeting` became `Task.meetings` (the migration copies each link over and
  back; `task.meeting` still reads the first one). The owner comes from the name in brackets, `(Simon)` or `[Simon]`;
  "by Friday", "tomorrow", "end of week", "next week", "end of month", "by Oct 30", "by 10/30" are read as the due
  date from the meeting's day (prefilled in the form). The same title and owner within 7 days is one task (the other
  meeting joins it; the item shows "also from …"). "Make tasks for all N items" on the pop-up. `import_meetings` was
  already idempotent by Granola id (tested now); it prints and posts one bell note per run to Managers: "Granola
  import: 5 meetings (3 new, 2 updated), 4 transcripts; 2 meetings need filing" (`--quiet` for none). No scheduled
  task or connector was set up: that goes on the acmiproperties Claude account.
- **Backfill:** `manage.py link_meetings` (dry run; `--apply` writes) links every existing meeting to its firms and
  companies and marks who has no record.
- **Files:** `crm/needs_filing.py`, `crm/invites.py`, `crm/management/commands/link_meetings.py`,
  `crm/templates/crm/needs_filing.html` (replaces `mail.html`), partials `mail_setup.html`, `firm_chip.html`,
  `unfiled_marker.html`, `upcoming_meetings.html`; changes in `crm/meetings.py`, `crm/directory/sort.py` and
  `logic.py`, `crm/mail/sync.py`, `parse.py`, `gmail_api.py`, `views.py`, `crm/search.py`, `crm/notify.py`,
  `crm/views.py`, the calendar, week widget, pop-up, firm, Capital and Build templates.
- **Tests:** `crm/tests/test_capture.py` (27, the invite is made up to the standard); 737 pass. Checked in a
  browser on a scratch database: the cards and Accept, firm chips and the marker on the calendar and week widget,
  the pop-up's "Make tasks for all", the page at phone width. The migration was run forward and back on a scratch
  copy with a task linked to a meeting.
- **On the laptop, after a backup:** `manage.py migrate crm`, then `manage.py link_meetings` (read the counts) and
  `manage.py link_meetings --apply`. Then look at Needs filing (expect more cards than Mail review had: meeting-only
  domains are new), a lender meeting on the calendar, and the next `sort_domains` log line for what Claude filed at
  medium with a meeting.
- **Left out:** the attendee-based merge of calendar and Granola duplicates (D1, area 2: invites use it, calendar ↔
  Granola still compares titles); reading an .ics that is only an attachment with no inline copy (needs one more
  Gmail call per message); undo for a card someone accepted by hand (as before, only Claude's own filings undo); the
  scheduled Granola task itself.

## Built 2026-10-26: land-sales (sale comps, off-market signals, Charles and Hays owner data)

Branch `land-sales`, migration `0069_land_sales` (depends on `0067_assignment_company`; another branch takes `0068_`,
so re-chain it at merge). Kellen asked for it the same day; the owner groups and the value range were added mid-build
(Kellen approved both). Code: `crm/land/sales.py`, `signals.py`, `portfolio.py`, `comps.py`, `comp_views.py`;
`manage.py refresh_sales`.

- **Last transfer on every parcel** (`Parcel.last_sale_date / _price / _type / _arms_length / _deed / _source`), from
  each county's own records (table below). Never a derived price: Texas never has one. Blanks never overwrite; a
  different transfer replaces all of the old one (its price doesn't carry over). `load_parcels` now runs it after a
  load (`parcels.after_load`; a sale source that's down leaves the load as it is), and
  `manage.py refresh_sales <county ...> | --all [--commit]` re-reads it without geometry (dry run by default).
- **Charles mailing and values:** the statewide MD iMAP layer (`mdgeodata.md.gov/imap/.../MD_ParcelBoundaries/
  MapServer/0`, SDAT data, joined on ACCTID, `JURSCODE = 'CHAR'`; geodata.md.gov shows a maintenance page) gives the
  owner mailing address (`OWNADD1/2`, `OWNCITY`, `OWNSTATE`, `OWNERZIP`) and values (`NFMLNDVL`, `NFMIMPVL`,
  `NFMTTLVL`). It has no owner names: those stay the county layer's. `KEEP_IF_BLANK` keeps the filled fields through a
  Charles reload.
- **Hays:** Hays County's own layer (`services5.arcgis.com/bVphnK8rPe5MHUSr/.../Hays_County_Parcels/FeatureServer/0`,
  Hays CAD data, keyed `prop_id`) gives the deed date and volume / page or document number. It has several rows per
  parcel (deed history): the latest is kept. Its `market` is blank on the same parcels as StratMap and isn't land +
  improvements, so market value isn't filled or derived. hayscad.com refuses scripts (403); not worked around.
- **Sale comps** (`SaleComp`): county comps come from parcels whose last transfer has a price of $1,000+ and looks
  arms-length (the county marks it so; Maricopa, which never says, counts unless marked otherwise). SDAT
  multiple-account sales (code 3) are left out (the price covers accounts we may not store). Parcels on one sale (same
  date and deed, or same date and price) make one comp with their acres added. `improved` = buildings worth $25k+ and
  10%+ of the value. They're re-made on every refresh; ones that no longer qualify go, unless the team put notes, a
  site or a flyer on them; their figures can't be edited. **Team comps** (/land/comps/new/, "+ Add a comp" on the card
  and site page): date, price, acres ($/acre worked out), buyer, seller, arms-length, deed, county or location,
  parcel / site link, source note, team notes, broker flyer, "keep off outside reports".
- **Comps page** /land/comps/: a county, a site or a parcel (?parcel=) with a radius (15 mi default), acreage band,
  date range, arms-length (default leaves out non-arms-length; "confirmed" only; all), land only. Table, map pins,
  count, median and quartile $/acre; `?format=csv`. Texas pages say why there are only team comps.
- **Panels:** "Sale comps nearby" on the parcel card, the site page (`#comps`) and the site brief (page 2, section
  `comps`, which the customize page can switch off). The brief shows no buyer, seller or notes and no comp marked
  internal, and gives "County record" or "ACMI research" as the source. It keeps the ACMI logo.
- **Value range (team only):** low / base / high $/acre = lower quartile / median / upper quartile of nearby land comps
  (within 15 mi, or comps without a location in the same county; a third to three times the acres; last 5 years, 10
  when that gives under 3; no buildings; not marked non-arms-length), with the count, the date span and a link to
  exactly those comps. Under 3 comps it says "Not enough comps". It's on the parcel card and the site page only: never
  the brief, the PDF, guest views, the data room or letters. Never from asking prices or assessed values.
- **Off-market signals** (`Parcel.signals`, `motivation`; worked out in `score.apply`, so every load, owner-file join
  and `score_parcels` updates them; the buy-box score is unchanged): long hold (last transfer 20+ years ago: "Owned
  since YYYY"), estate / heirs / life estate / deceased, family (ET AL / ET UX), trust, out-of-state owner, absentee
  (mails from outside the 3-digit ZIP areas of the county's own situs addresses), inherited / non-arms-length last
  transfer (where the county marks it: SDAT code 4, Madison's will / estate / gift / quitclaim deeds). Public owners get
  none. Chips on the card and the land-search list; a Signals filter and "Sort list by Motivation" on land search. In
  the brief they show only on the app's own page (class `no-print`, never in the PDF or a printout). `motivation`
  weights: estate 3; long hold, non-arms-length, out-of-state 2; absentee, family, trust 1. The old "Out-of-state
  owner" checkbox compared every county to TX; it now compares to the land's own state.
- **Tax-delinquent: none.** No loaded county publishes delinquency in its parcel service (Pinal's TaxParcels has
  tax-due fields, all empty for 20+ acre parcels, and they're bills, not arrears). Tax-sale lists exist as PDFs or
  newspaper notices (Charles County's annual tax sale, Maricopa's lien sale) and aren't loaded. So there's no signal
  and no filter for it.
- **Owner groups** (`Parcel.portfolio_key / _parcels / _acres`; `portfolio.rebuild()` runs after every load, owner-file
  join and refresh). The rule: two stored parcels are one group when the owner names match after normalising and
  they're in the same county, or when the owners mail to the same address anywhere (street + 5-digit ZIP; case,
  punctuation, suite / unit numbers and street-word spellings ignored). An address doesn't link when its c/o line is a
  tax service, bank, law firm or accountant, when more than 5 different owner names use it (a registered agent, a
  manager), or, for a PO box, more than 3. Links chain. The card shows "Same owner group: N parcels, X ac across
  <counties>" with the others; land search has an owner-group filter and a sort by group acres. **Landowner letters**
  have "One letter per owner group instead" (`?group=1`): one letter to the owner and address of the group's largest
  parcel, with "Some of this land is held as A and B." after the paragraph about the land (or wherever
  `{other_owners}` is in the letter). No LLC / Secretary of State tracing (not now).
- **Tests:** `crm/tests/test_land_sales.py` (21; every county service mocked); 731 pass. A live read-only check of each
  sale source's query on 2026-10-26 (no database) found these records with a transfer date at 16+ ac: Charles 2,233 of
  2,465 (1,186 with a price), Hays 3,434 of 3,699, Madison 9,379, Morgan 4,729 (1,487 with a price), Harnett 4,956,
  Hoke 18,124, Pinal 57,524 (32,620 with a price; the full-window area adds small parcels). Not checked in a browser.

**Sale and deed fields by county (checked 2026-10-26 with `?f=json` and sample queries)**

| County | Layer | Transfer date | Price | Arms-length / type | Deed |
|---|---|---|---|---|---|
| Charles MD | MD iMAP statewide (SDAT) | `TRADATE` | `CONSIDR1` | `CONVEY1`: 1 arms-length improved, 2 vacant, 3 multiple accounts, 4 not arms-length (MD Assessment Procedure Manual) | `GR1LIBR1` / `GR1FOLO1` liber / folio |
| Hays TX | Hays County parcels (Hays CAD) | `Deed_Date` | none (Texas) | none | `Volume` / `Page`, else `Number` |
| Bastrop, Blanco, Caldwell, Comal, Guadalupe TX | TxGIO StratMap | none (`DATE_ACQ` is when TxGIO got the file, the same on every record) | none | none | none |
| Limestone AL | Huntsville combined parcels | none | none | none | `Book1` / `Page1` exist but aren't labelled as the deed: not used |
| Madison AL | City of Madison copy | `DeedDate` | none | `DeedType` (WARR, SURV, DEED, QTCL, LIFE, PREP, WILL, GIFT, EXEC, DIST, AFFD, FORC...) | `DeedBook` / `DeedPage` |
| Morgan AL | Huntsville combined parcels | `LastSalesDate` | `SoldTotalPrice` | `UseOnSales` (1 = used in the assessor's sales study) | `LastDeed` |
| Cumberland NC | NC OneMap | none | none | none | `sourceref` "Deed Book/Page" |
| Harnett NC | NC OneMap | `saledatetx` (month and year) | none | none | `sourceref` |
| Moore NC | NC OneMap | `saledate` (602 of 5,095 big parcels; the rest a 1900 fill value) | none | none | `sourceref` |
| Hoke NC | Hoke County GIS | `DEED_DATE` | none | none | `DEED_BOOK` / `DEED_PAGE` |
| Maricopa AZ | County Assessor | `DEED_DATE`, `SALE_DATE` | `SALE_PRICE` (only when the sale is the latest deed, within 90 days) | not given | `DEED_NUMBER` |
| Pinal AZ | County TaxParcels | `SALEDATE` | `SALEPRICE` | `REASONTOEXCLUDE` (blank = used in the assessor's study; the codes aren't published, so excluded = not known) | `RECEPTIONNO` (mostly blank) |
| Greene IN | IndianaMap + WTHGIS table | none | none | none | none |

NC OneMap's `transfdate` is the date NC OneMap got the county's file, not a sale. No NC layer has a price field (no
excise-stamp figure in any loaded layer).

**To run on the real database (Kellen's OK first; back up as "How to work in this repo" says):**
1. Re-chain `0069_land_sales` onto whatever `0068_` lands, then `manage.py migrate crm`.
2. `manage.py refresh_sales Charles Hays` (dry run), then the same with `--commit`.
3. `manage.py refresh_sales --all` (dry run), then with `--commit`: the other counties' transfers, signals on every
   county (including the ones with no sale source), county comps and owner groups.

## Built 2026-10-26: deals live in the company card, and a Back button (Kellen: "Everything should live in the company card. Also make it easier to go back")

Branch `company-card`, no migration.

- **Tenant pipeline** cards and table rows lead with the company name; the deal name sits underneath (hidden when it's the same).
- **A deal opens inside its company card:** the deal page now carries the company's header, breadcrumb (Companies › company › Deals › deal)
  and tab bar with Deals active, plus the company's other deals (`partials/company_card_head.html`). Same URL
  (`/opportunities/<pk>/`), so every existing link (board, report, search, alerts) lands there. The old "Back to board" link is gone.
- **← Back to <page>** above every page (`static/crm/back.js`): the hub page you came from, per browser tab (sessionStorage).
  Tab and filter changes on the same page update its entry, so Back goes to the previous page, not the previous tab; it
  uses the browser's own back when that's where it goes, so the scroll position comes back too.
- Tests: `crm/tests/test_company_card.py` (3); 716 pass. Checked in the browser on a scratch database with made-up deals.

## Built 2026-10-27: sheet-import (Kellen: "I want the ability to upload an excel sheet to the CRM - it should be able to create companies and add contacts")

Branch `sheet-import`, migration `0071_import_batch` (the `ImportBatch` table). Code in `crm/sheet_import/`
(`logic.py` reads, guesses, plans, runs and undoes; `views.py` is the pages).

- **Where:** an Import button on Companies and on People, "Import from a spreadsheet" in the + New menu and in the
  Ctrl K page list. `/import/` → match the columns → preview → import → results. `/import/` also lists recent imports.
- **Files:** .xlsx (with several sheets, chips pick one), .csv (comma, semicolon or tab), and the HTML-table ".xls"
  that old web viewers export. A real binary .xls is refused with "save it as .xlsx". Limits: 5 MB and 5,000 rows a
  sheet. The file is never saved: its cells sit in the session between the steps and are dropped once it's imported.
- **Columns** are guessed from the header (aliases, as in `crm/land/owners.py`): company name, website / domain,
  sector, city, state, HQ location, priority, stage, owner, company LinkedIn, company notes; first / last / full name,
  email, title, phone, LinkedIn, contact notes. Each can be changed or set to Ignore. A row can be a company, a contact
  or both.
- **Matching** (as `import_workbook` does): contacts on email (then their other emails; with no email, by name at a
  matched company); companies on `normalize_key(name)`, then on a domain a company already has (from the website
  column, else the contact's email unless it's free mail), and for a row with no company name, a domain that spells a
  company's name. A contact with no company name and an unknown email domain is added without a company (no company
  is guessed from an email domain); a website with no name makes a company named from the domain.
- **Preview** writes nothing: counts at the top, then each row as New company / Matches X (by name, by website, by
  email domain) / New contact / Matches X, with the fields it would fill, or Skipped with the reason. Skipped: a
  directory firm's domain (never a company, and the contact isn't put on the firm either), a protected address (Mail
  filters), an ACMI address. Flagged: an ignored domain (not added), an invalid email, the same email twice in the
  file, a stage / priority / owner the CRM doesn't have, a domain already on another company, the sheet's name
  differing from the company its domain found, a name close to another new company in the file. New companies whose
  names look like existing ones (`merge.core_name`, the Possible duplicates check) get a dropdown to put the rows on
  the existing company instead.
- **Options:** Fill blanks only (the default; never changes a value the CRM has, and an existing person's name is left
  alone if they have one) or Update with the sheet's values (notes are added to, never replaced; a contact at another
  company moves). Owner, stage and priority for new companies when the row has none. Add the companies and / or the
  contacts to an existing list or a new one.
- **Import:** one transaction, history suppressed. Each new record gets a background "Imported from <file>" (Record
  created) and each updated one "Updated from <file>: sector, title" (Field change), by the importer (the Activity
  tab's "Show background" shows them, as it does "Company created").
- **Undo** (Managers only, like every delete; for 7 days): deletes the companies and contacts the import created (and
  anything logged on them since; the page says how many have some), its list entries, the lists it made if now empty,
  the domains and phones it added, and puts back each field it filled unless someone has changed it since.
- Partners never reach it (PartnerOnlyMiddleware sends them to the room; tested).
- Tests: `crm/tests/test_import_sheet.py` (16); 805 pass. Checked in the browser on a scratch database with a made-up CSV:
  upload, mapping, preview flags, the close-name pick, import, the timeline line, Undo.
- **To run on the real database (Kellen's OK first; back up as "How to work in this repo" says):** `migrate crm`.
