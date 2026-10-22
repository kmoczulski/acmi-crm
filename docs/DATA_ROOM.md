# Capital partner data room: proposal (2026-10-22)

Kellen asked for a data room for capital partners: they see the pipeline, open a company's card, and don't see the
internal details. A proposal for Kellen to settle before anything is built.

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
