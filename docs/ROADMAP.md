# Improvements review (2026-10-16)

**Status (2026-10-16, night):** Kellen took the recommendations. Done: the audit fixes and roles; the repo on GitHub
with CI; the transcript archive in the CRM (waiting on the Granola connector to pull from Granola); the campus Summary
tab v1 and the weekly update drafted by Claude; Cloud Run jobs and Cloud Storage support in code; the contract &
critical-dates module, modeled on Notessa (cited answers, abstracts, defined terms and cross-references, redlines,
reminders). Next: connect Granola and start the daily archive, then the due-diligence tracker and closing checklist.
Details in `docs/HANDOFF.md` ("roadmap phase 1" and "contracts and critical dates").

What to build next, after the audit fixes. Four parts: what blocks going live, the campus summary (Kellen's proposal,
worked through), the platform seen by a developer who has taken land from sourcing through construction, and the
smaller items the audit turned up. Section 5 is the suggested order.

## 1. Decide before going live

1. **Roles (built; confirm membership).** The *Managers* group sends batches, edits outreach settings and templates,
   deletes, merges, deletes sites and turns client links on. Everyone with an account today is in it, so nothing changed
   yet; new Google sign-ins start without it. Decide who should stay a Manager and remove the rest in Admin › Groups.
2. **Background work on Cloud Run.** Outreach batches run in a background process after the page returns, the mail and
   calendar sync runs inside one web request, and a cold PDF package takes minutes. Cloud Run slows or stops all of these.
   Move them to **Cloud Run jobs** started by **Cloud Scheduler** (mail/calendar every 15 min, send batches on demand).
   The alternative is CPU always allocated, which is simpler but costs more every month.
3. **Files on Cloud Run.** Uploaded documents and the outreach attachments (the ~16 MB brochure pack) live in `media/`,
   which Cloud Run wipes on restart. Sends now stop instead of going out without the brochure, and uploads are served
   through the app, but the files still need a home that lasts: a Cloud Storage bucket (`django-storages`), or the
   Drive upload HANDOFF already plans.
4. **After merging the fixes:** run `manage.py score_parcels` so stored scores pick up the public-owner rule. Then push
   the repo to GitHub and add CI that runs the tests on Postgres, so every change is checked the way production runs.

## 2. Campus summary (your proposal, worked through)

### What Simon's email shows
The weekly update is one list per campus, grouped by **workstream**: tenants (ARA, Shine, Thales, ERG, Supply Energetics),
land (Outside the Gate owner by owner, county land, Lake Acton), projects (The Forge: sign, geotech, variance). Each
line carries four things: the latest status, **who the ball is with** ("still waiting on redlines"), **how long it has
been** ("promised first five weeks ago", "sent Sept 24, haven't heard back"), and "no updates" lines for the quiet ones.
Today it's written by hand from memory, email and meetings.

### Proposed design
**A Summary tab on each campus**, rebuilt from data the CRM already has, with Claude writing the weekly draft.

- **Workstreams:** *Tenants*: the campus's opportunities (stage, next step, last contact). *Land*: the campus's sites,
  owner by owner from the per-parcel deals. *Projects*: each building or program (The Forge) with its design,
  entitlement and construction milestones. *Other*: financing, government, incentives.
- **Each line is assembled, not typed:** last email in or out (the company timeline), meetings (Calendar and Granola),
  deal stage moves, land milestones (LOI sent, PSA signed), tasks done, comments, plus a one-line **"Add update"** box for
  what happened off-system (a phone call, a site visit).
- **Ball in court and aging, from dates:** each item carries *waiting on us / them / third party* and an optional
  *promised by* date. The page computes "promised 5 weeks ago" and "no reply in 22 days". Items with nothing new this
  week say so ("Supply Energetics: no activity in 30 days") without anyone writing it.
- **Daily feed:** what changed today on the campus, straight from the events, no AI needed. Shown on the tab and
  optionally a short morning email or notification.
- **Weekly draft by Claude:** every Monday (driven by the same calendar reminder), Claude turns the week's events into
  the email's format, one line per item, in Simon's style. Guardrails: it may only use facts on the page, each line
  links to its sources, and anything uncertain is marked. Simon edits, then **Approve & send**. The report goes out as
  an email in the familiar bullet format plus a PDF with the logo, and is archived on the tab.
- **History:** archived weeks give trends for free: how long ARA's redlines have been outstanding, how each owner moved
  week to week. That's the raw material for investor updates.

How the weekly draft would read (illustration from the email):

> **Week of Oct 12**
> *Tenants.* ARA: GC interviews done, downselect under way; **waiting on ARA's redlines, promised 5 weeks ago**.
> Shine: no change; IHD is supporting the business case. Thales: Dave met Thales (link to the meeting notes).
> ERG: updated LOI sent Sep 24, **no reply in 22 days**. Supply Energetics: no activity this week.
> *Land.* Outside the Gate: Wamsley has our draft (spoke Tuesday); Kroc, attorneys connecting on the last points;
> Infinite Designs, no reply to last week's outreach; Focus On, no longer pursued. County land: attorney on the last
> items. Lake Acton: PSA execution under way.
> *Projects.* The Forge: sign installed, geotech done, variance submitted to the town.

### Granola transcripts: never lose one
The free Granola plan shows only the last 30 days, so every day a meeting's transcript is lost for good. The fix is
an **archive that runs daily**: each meeting's transcript and notes saved once, as soon as they exist.

- **Where it's kept:** in the CRM (a transcript on the Meeting, full-text searchable and linked to companies and campus
  like the notes) **and** as a `.txt` file per meeting in a shared folder: `Meeting transcripts/<campus or "General">/
  2026-10-14 Thales intro.txt`, with title, date, attendees and Granola link at the top. Saving is idempotent (keyed by
  Granola's id), so re-runs never duplicate.
- **How to get them** (needs your pick):
  1. *A daily scheduled Claude task with the Granola connector* (how the first 65 notes came in) that writes the `.txt`
     files and posts them to the CRM. No new subscription, but it relies on that connector exposing transcripts.
  2. *Granola's local app data on the laptop*, read by the existing 15-minute task. Works offline, but the file format
     is undocumented and only covers your own meetings.
  3. *A paid Granola plan* with full history and an API. The sturdiest, and it covers the whole team, at a monthly cost.
  Whichever route: whose meetings? Dave met Thales, so Dave's and Simon's Granola matter too.
- **Then it pays off twice:** Claude can answer questions across the archive ("what did Thales say about power?"),
  and the weekly draft can quote what was actually said.
- **Consent and care:** Maryland requires every party's consent to record a call. Transcripts of tenant calls under NDA
  are sensitive. Keep them internal (Managers role, if adopted), and decide on a retention period.

### Build plan
1. **Archive first** (time-sensitive): transcript field and import, `.txt` export, daily job.
2. **Summary tab v1:** workstreams from existing data, "Add update", ball in court, promised dates, aging, daily feed.
3. **Weekly report v2:** Claude draft, Approve & send, PDF with logo, archive.
4. **Capture from anywhere:** forward an email or reply to the daily digest to add an update.

## 3. The developer's lifecycle view

What someone who has sourced, contracted, closed, entitled and built would look for, against what exists today.

- **Source.** *Have:* parcel search across 17 counties, buy-box score, overlays, live checks, assemblages, owners,
  Pursue → company and task, site briefs. *Add:* **flyer intake** (drop a broker PDF, Claude fills the listing details
  and the claims table, which are typed by hand today), **landowner letters** (mail merge to the mailing addresses, with
  logo), off-market signals (long holds, estates, tax-delinquent where counties publish it), **sale comps** per area.
- **Pursue → contract.** *Have:* site milestones, per-owner deals, documents. *Add:* the deferred **Contract
  negotiations** module: LOI / PSA terms (price, deposits, feasibility period, closing date, extensions, contingencies),
  redline rounds with ball in court, and a **critical-dates engine**: deposits going hard, feasibility end, extension
  notices, with reminders and calendar entries. Missing one of these dates is the costliest mistake in the business.
- **Due diligence → close.** *Have:* the 44-item DD checklist, but only inside client assignments. *Add:* the same
  checklist on ACMI's own deals (title, ALTA, Phase I, geotech, wetlands delineation, utility will-serves, zoning letter,
  traffic), each with consultant, cost, ordered / received dates and findings; a **DD budget**; a closing checklist; on
  closing, the site flips to Acquired and the parcels to ACMI's entity.
- **Entitle.** *Have:* zoning reference, quick site planner, QD planner, the permit matrix (in client reports). *Add:*
  an **entitlement tracker** per project: each approval (rezoning, variance (The Forge's), site plan, subdivision,
  grading / stormwater, wetlands 404, the DDESB explosives safety site plan) with jurisdiction, submitted date, hearing
  dates, conditions and status.
- **Design → build.** *Have:* nothing yet. *Add:* **project records**: design phases, consultants and deliverables, GC
  selection (ARA's interviews, downselect, award), schedule milestones (NTP, pad ready, vertical, dry-in, CO), budget vs
  actual by cost code, change orders and draws. Keep the detail in the GC's system (Procore etc.) and bring summaries
  in rather than rebuilding it.
- **Lease and deliver.** *Have:* opportunities with stages, stakeholders, LOI / PDA documents. *Add:* tenant
  **requirements matching** (acres, MW, explosives class and quantity → the QD planner → which site fits), lease
  abstracts and their critical dates (rent start, options, TI).
- **Portfolio and investors.** *Have:* the investor pipeline report. *Add:* a **pro forma** per project (land, hard and
  soft cost, yield, IRR), and investor updates rolled up from the campus summaries.

Across every stage: **one timeline per project**, ball in court everywhere, **critical dates in one calendar**,
consultants as companies with roles and costs, and quick capture from the phone.

## 4. Smaller items from the audit
- **Done:** duplicate outlines are stored once, slivers under 0.01 ac are skipped, and object-id keys follow the land
  across reloads; `clean_parcels` tidied the stored data.
- **Gaps in owner data:** Hays values 42%, Charles mailing addresses 66%. Fill them from assessor files with `load_owners`.
- **Security housekeeping:** change the Gmail app password (still open in HANDOFF), encrypt the Google tokens at rest,
  set a retention period for `backups/` (7 full copies), turn on device encryption on the laptop.
- **Verification still owed:** a Docker build (Chromium layer) and a full test run on Postgres, once Docker is
  installed or in CI.

## 5. Suggested order
1. Merge the audit fixes, run `score_parcels`, push to GitHub with CI.
2. **Start the transcript archive now:** the 30-day window keeps closing.
3. Go-live blockers: roles, Cloud Run jobs, file storage. Then deploy to hub.acmigroup.com.
4. Campus summary v1, then the weekly report v2.
5. Contract and critical dates (the deferred Contract negotiations module). **Built 2026-10-16**, modeled on Notessa.
6. Entitlement tracker, then project and build records.
7. Flyer intake, landowner letters, comps, and the data clean-ups above.
