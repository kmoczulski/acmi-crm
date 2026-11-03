# Capital partner data room: proposal (2026-10-22)

Kellen asked for a data room for capital partners: they see the pipeline, open a company's card, and don't see the
internal details. A proposal for Kellen to settle before anything is built.

## Decided by Kellen, 2026-10-22 (built the same day on branch `data-room`)

- **Fortress only** for now (the model allows a room per partner).
- **Partners make an account and sign in**: a Manager invites a person by email, the hub makes a one-time sign-in
  link (14 days) for the Manager to send, and the person picks their own password. No shared links.
- **Every deal on the investor report**, each with a switch to take it out of the room.
- **Win % off** (a room setting, off by default; costs on).
- **Files only when approved**: a deal's document approved for the room, or a file uploaded for it.

## What the hub already has to build on

- **Private share links** for land-search clients (`Assignment`, `/g/<token>/`): a long random token, on/off by a
  Manager, an expiry date, a view count with the last view, and a per-row "show to guest" tick. Guests need no
  account. The data room reuses the same pattern.
- **The pipeline report** (`crm/reports.py`): one column list for the page, its PDF and the Fortress-style Excel, with
  the cost and win % groups that people hide before sending it out, and an "On investor report" flag on every deal.
  That flag is the natural "this deal is in the data room" switch.

## Proposed design

- **A room per partner** (`DataRoom`): the partner (a directory firm, e.g. Fortress), a cover note, the columns it
  shows (chosen from the report's list, costs and win % off by default), which campuses it covers, its share link
  (token, on/off, expiry), and a view log (every open, with the time and page, so you can see what Fortress looked at).
- **What a partner sees:**
  - **Pipeline:** Board view (stage columns, read-only) and Table view. Only deals flagged "On investor report" (or
    ticked for that room), with the room's columns.
  - **Company card:** name, website, sector, a short description, campus, stage, space / acreage need, timeline,
    target close, est. delivery, and a **partner note**: a new field, written for outsiders, never the internal notes.
    Optionally files you pick for that deal (an LOI summary, a site plan), served through the link and logged.
  - **Sites and projects**, optionally: a campus page with its projects and the site status.
- **What a partner never sees:** emails, meetings and transcripts, notes, comments and Trello history, contacts and
  people, owners, priority and fit score, tasks, costs and win % unless the room turns them on, other partners'
  rooms, and anything in the directory.
- **Built as its own small set of guest pages** with an allow-list of fields per object. The internal pages are never
  reused with things hidden, so a field added to an internal page later can't leak into the room.

## Questions for Kellen

1. **Who:** Fortress only for now, or several partners, each with its own room and deals?
2. **Access:** a private link like the land-search client pages (no account, expiry date, view log; recommended to
   start), or a sign-in for each partner contact (email code or Google), which takes longer but shows who each viewer
   was?
3. **Which deals:** every deal flagged "On investor report" (recommended), or picked per room?
4. **Columns:** the investor report's defaults with costs and win % hidden (recommended), or including them?
5. **Files:** v1 with no files, or a per-deal list of documents you choose (each one logged when opened)?
6. **Hosting:** a link only works once the hub is on the internet (hub.acmigroup.com, waiting on billing; `docs/ROADMAP.md`
   section 1). Until then it can be built and tested on the laptop. Go-live has to come first.

## Drive folders and files (Kellen, 2026-10-25; built on branch `data-room`)

Kellen: map Drive locations per tenancy folder or project folder; the team sees everything in them, and he approves
from there what goes to the data room. Decided: keep serving the approved version and flag newer ones (no automatic
swap); read Drive through a service account on the "1 ACMI Properties" shared drive only.

- **Mapping:** each company and project page has a **Drive files** panel. Paste the folder's Drive link, or, for a
  company, take the suggested folder: `3 Tenancy/<name>` matched by name ("Castelion (Ranger)" → Castelion).
- **What the panel shows (team only):** every file under the folder, three levels deep, grouped by sub-folder (LOI,
  PDA, Design…), one line per document at its current version: the newest date in the name (2026.03.23, 20260910,
  5_15_26…, else Drive's modified time), the PDF when a .docx of the same version sits beside it. Older versions fold
  under "N older". Folders with "archive" in the name, `~$` lock files, desktop.ini and shortcuts are skipped. Names
  with internal, redline or draft are tagged Internal.
- **Sharing (Managers):** "Share in <room> room" on a document. A company's file goes on its investor-report deal (pick
  one if there are several); a project's at the top of the room. An Internal-tagged file needs a ticked confirmation.
  Only a file that is the current version in that mapped folder can be shared.
- **Newer versions:** when a newer dated version appears (beside the old one, or with the old one moved to Archive),
  the panel and the room's Manager page say "Newer version" with **Show the newer one**. Until a Manager clicks it,
  the room keeps serving the approved file.
- **What Fortress gets:** the file's name and, on click, its bytes read from Drive at that moment through the hub
  (Google Docs / Sheets / Slides exported as PDF / Excel / PDF). No copy is kept, no Drive address or folder reaches
  them, and the download is logged. A deal's document whose link is a Drive file is served the same way.

### Setting up Drive access (Kellen, once)

1. In the Google Cloud project the hub's Google sign-in uses: **APIs & Services › Enable APIs** → enable **Google
   Drive API**.
2. **IAM & Admin › Service accounts › Create**: name it `acmi-hub-drive-reader`. Give it no project roles.
3. In Google Drive: **Shared drives › 1 ACMI Properties › Manage members** → add the service account's email
   (`acmi-hub-drive-reader@<project>.iam.gserviceaccount.com`) as **Viewer**. If Drive refuses an outside address,
   a Workspace admin allows it for that shared drive. Add it nowhere else.
4. On the laptop: the service account › **Keys › Add key › JSON**. Save it outside the project folder (e.g.
   `C:\Users\Kemoc\.acmi\drive-reader.json`) and add `ACMI_DRIVE_SA_FILE=C:\Users\Kemoc\.acmi\drive-reader.json`
   to `.env`. (If the organisation blocks key creation, skip this; the hosted hub doesn't need a key.)
5. Add `ACMI_DRIVE_TENANCY_FOLDER=<the ID at the end of the 3 Tenancy folder's link>` to `.env` for the name
   suggestions.
6. `.venv/Scripts/pip install -r requirements.txt` (Google's auth library comes with `google-cloud-storage`).
7. Hosted (Cloud Run): add the Cloud Run service's own service account as Viewer on the shared drive instead, and set
   `ACMI_DRIVE_ADC=true`; no key file.
