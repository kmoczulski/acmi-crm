# Trello boards → hub boards: per-board mapping (2026-10-22)

Read-only review. Nothing was written to the real database or to Trello. Database figures are read-only counts
(`db.sqlite3` opened with `mode=ro`).

## Source

- **Kellen's Trello JSON exports**, downloaded 2026-10-22 (Board menu › Export as JSON): Maryland / MEIH (`1Ya0vrCo`),
  Properties-Prospecting (`ucXAoRb7`) and Properties-General (`XIDdu6tv`). The Trello connector wasn't available in
  this session, and `trello_fetch.py` wasn't run.
- Converted to the layout `scripts/trello_fetch.py` writes, in `data/imports/trello/2026-10-22/` (gitignored): one
  `{board, cards, checklists, actions}` file per board, named `<shortLink>-<slug>.json`, plus `index.json`. Each card
  carries its `checklists` and `members`, as the API call returns them. The board has `organization: {id}` with no
  display name. The original downloads are in `raw/` beside them. `Downloads\XIDdu6tv - properties-general (1).json`
  holds the same data as the original in a different order, so it was ignored.
- **The export is not the full history.** Trello's own export keeps only the newest 1,000 actions per board:

  | Board | Actions in export | Covers | Comments in export / on cards |
  |---|---|---|---|
  | Maryland / MEIH | 921 | 2025-04-18 to 2026-10-15 | **118 / 118 (complete)** |
  | Properties-Prospecting | 1,000 (capped) | 2026-06-26 onward | **186 / 1,660 (11%)** |
  | Properties-General | 1,000 (capped) | 2025-10-15 onward | **55 / 697 (8%)** |

  Cards, lists, labels, checklists and attachments are complete in all three. Only the action history (comments,
  moves, creation dates of older events) is cut off.
- **There are more boards.** Nine mirror cards point at cards that aren't in these three exports: five on Prospecting's
  "Partners - Business Development" list and four on Properties-General (one on "Critical Dates - NSIH", three in
  archived lists). So at least one more board exists that Kellen can see but didn't export.

## Board 1: Maryland / MEIH → one MEIH project board (Project board template)

73 cards (60 open, 13 archived), 15 lists (10 open, 5 archived), 12 labels, 4 members. 118 comments, 55 checklists
with 211 items (176 done), 58 attachments (all uploaded to Trello), 21 cards with due dates (15 open and not done),
921 actions.

| Trello list (open / archived cards) | Becomes | Records |
|---|---|---|
| Land Sites (7 / 6) | **Land sites feed** (MEIH sites) | Lake Acton Business Park matches by name. About 6 more match by reading: County Land, Garner Land, Billingsley Assemblage, 4390 Indian Head and ETC Building (both the 4390 site), Rosewick Development Site (**ambiguous**: "Rosewick Road" or "Bardon – 9622 Rosewick Rd"). No record: NSWC - IHD, Lewis Place. The four archived task cards (Civl Engineering, Architectural RFP, Groundbreaking, Prometheus extension notice) go to Deal and design as archived cards. |
| COLD - Land Sites (5 / 0) | Same feed. These sites are Dropped or Outreach in the CRM, so they leave the feed; their cards' comments land on the site | CA Billingsley, Berry Pointe, Berry Rd Assemblage and Copart each match a site by reading. Indian Head / Maryland matches no record (8 comments). |
| 4390 Indian Head Hwy - Critical Dates (11 / 0) | **Critical dates feed** (the 4390 PSA's contract dates) | 9 cards are the PSA's 9 contract dates. The CRM has the extension exercised on Oct 22; **Trello still shows the pre-extension dates** (Oct 22 / Nov 1 / Dec 11 / Dec 21) and has none ticked done. Importing them as cards would bring back stale dates. "4390 Indian Head Highway PSA" links to the contract; "Effective Date" is a field on the contract. |
| County Land - Deal and Design (3 / 0) | Kept, as "Deal and design – County land" | Cards link to the County site and, once a contract exists, the County PSA (no contract in the CRM yet) |
| LABP - Deal and Design (2 / 1) | Kept, "Deal and design – LABP" | Lake Acton site |
| 4390 / ETC - Deal and Design (6 / 1) | Kept, "Deal and design – 4390 / ETC" | "PSA & ETC Lease" → the 4390 PSA contract. Powers Brown B101 is not in the directory |
| ARA (14 / 2) and ARA Building (4 / 0) | Kept as two lists | Cards link to the ARA deal (Applied Research Associates, LOI signed). Invoices and budget updates stay cards with due dates. The ARA PDA and lease aren't contracts in the CRM yet |
| Contacts and Consultants (7 / 0) | Kept as is | "Construction Attorney – Cozen O'Connor" → firm Cozen O'Connor. Kimley Horn, Maryland Department of Commerce and Powers Brown aren't in the directory. Placeholder cards (Transaction Attorney, Land Use Attorney, Contractors, Firm Options) have no record |
| Land Use (1 / 0) | Kept (or folded into Deal and design) | PGM / Permitting; the 2 entitlements in the CRM don't match by name |
| (none) | **Open tasks feed** (new) | MEIH's tasks |
| 5 archived lists (3 cards) | Archived cards on the board | none |

- **Name matching:** `merge.core_name` matches only 1 of the 73 cards (Lake Acton), because MEIH cards are named after
  addresses and tasks, not companies. With reading, about 15 cards link: roughly 11 to sites, 2 to the 4390 contract,
  1 to Cozen and the ARA lists to the ARA deal. The import's dry run needs a hand-made link table for this board, not
  name matching alone.
- **Labels:** six map onto the hub's categories (`Task.Label`): Critical Date (25), Deal Info (10), Legal (9),
  Program/Procurement (7), Civil (6), Architecture (5). Ongoing (19), To Do (6) and Contact Information (2) are kept
  as board labels. "4390 Indian Head" and the two unnamed labels are on no card, so they're dropped.
- **People:** Kellen Moczulski → `Kellen` (109 comments, 28 cards), Simon Shewmaker → `Simon` (6 comments, 9 cards),
  David Dowell → `Dave` (1 comment, 3 cards), David DeNardo → `David` (1 comment). **Outside the team:** Joe Don
  Bowling (1 comment; keep his name in the text).

## Board 2: Properties-Prospecting → the bound "Tenant pipeline" board (overlaps the Oct 14 deal import)

376 cards (305 not archived, but 54 of them sit in archived lists, which leaves 251 live cards, the same number the
Oct 14 import saw). 20 lists (11 open, 9 archived), 18 labels, 9 members. Comments 186 of 1,660 in the export, 200
checklists with 564 items (428 done), 174 attachments (168 uploads, 6 links), 1,000 actions (capped).

**Overlap with the tenant deals:** every one of the 102 open cards in the six stage lists is already a deal. 83 are
matched by `source_key = trello:<cardId>` and 19 are tracker deals carrying the card's Trello URL. Castelion's deal
points at a different Castelion card. No open card sits in a list that disagrees with its deal's stage. The CRM has
110 deals: 83 from Trello, 23 from the tracker and 4 others. What the CRM lacks is the cards' **history**: comments,
checklists and attachments. The deals carry only the card description (as "From Trello" notes).

| Trello list (cards / archived) | Becomes | Records |
|---|---|---|
| EARLY ENGAGEMENT (29 / 9) | **Bound** to stage Prospect | 19 by source key, the rest are tracker deals or companies |
| ACTIVE DISCUSSIONS (59 / 5) | **Bound** to Qualified | 49 by source key |
| LOI / PDA NEGOTIATIONS (23 / 11) | **Bound** to LOI negotiation | 4 by source key, 8 by URL; 11 archived cards, 6 of them mirrors of cards on this board |
| LOIs Executed (13 / 4) | **Bound** to LOI signed | 8 by source key |
| LEASE NEOGTIATIONS (4 / 0) | **Bound** to Lease negotiation | 2 by source key, 2 by URL |
| LEASE / PDA SIGNED (6 / 2) | **Bound** to Signed (a "PDA Executed" label → LOI signed, as `pipeline_import` does) | ARA, iRocket and Spinner by URL |
| New Leads (87 / 0) | **Decision** | 86 match a company (38 Prospect, 45 Contacted, 3 Engaged); 1 has a deal. 8 comments |
| Cold (59 / 9) | **Decision** | 40 match a company, 19 match nothing; 1 has a deal. **419 comments on the cards, 15 in the export** |
| Partners - Business Development (6 / 0) | **Decision** | 5 are mirrors of cards on a board not in the exports; 1 company |
| Outreach Summary / Admin (14 / 9) | A team board (see board 3) | none |
| 9 archived lists (Targets 53 not-archived cards, Contacted Prospect 14, Leasing / Tenancy 5, Additional Intel 2, Early Discussions 1, Deal Failed 1, 3 empty) | Archived cards, or skip | Targets: 35 match a company, none has a deal |

- **Name matching (all cards):** 103 by source key, 175 more match a company by `core_name`, 98 match nothing.
- **Labels:** High / Medium / Low Value are already deal priority and NSIH / MEIH / Exploration Park are already the
  deal's campus, so neither needs a label. Kept as labels: Build to Suit Opp (16), To Do (11), Non-actionable (9),
  Actionable (7), Austin interest (6), Comeback Sauce (3), Virginia (3), Indian Head / Maryland (3), Research (3),
  Airport Commerce (2), Federal Licensee (1). PDA Executed (1) is a stage rule.
- **People:** Kellen (113 comments), Simon (32), Dave = David Dowell (18), David = David DeNardo (13), Paul (5). John
  Burer is on 8 cards with no comments in the export. **Outside the team:** Todd Catlin (3 comments, a board member),
  Joe Don Bowling (2 comments), Melissa Guerrero and Nick Bass (board members, 1 action between them). None of the
  four is a Person in the CRM.
- **Duplicate company:** "Applied Research Associates" (Opportunity) and "ARA (Applied Research Associates)" (Prospect)
  were two companies. Merged 2026-10-22 (see "Decided" below).

## Board 3: Properties-General → a team board, plus an NSIH project board

334 cards (165 not archived, 169 archived; most of the "not archived" cards sit in its 22 archived lists). 31 lists
(9 open, 22 archived), 17 labels, 7 members. Comments 55 of 697 in the export, 68 checklists with 274 items, 176
attachments (all Trello uploads), 151 cards with due dates (55 open and not done), 1,000 actions (capped).

| Trello list (open / archived cards) | Becomes | Records |
|---|---|---|
| 4390 Indian Head Hwy - Critcal Dates (11 / 0) | **Skip**: all 11 are mirrors of the MEIH board's cards | — |
| MEIH - ARA (12 / 2) | **Skip**: 13 of 14 are mirrors of the MEIH board's ARA cards | — |
| Critical Dates - NSIH (8 / 12) | NSIH project board, **Critical dates feed** if the agreements become contracts; otherwise a kept list | Prometheus Phase 2 feasibility and extension dates, monthly reports and invoices. **The CRM has no NSIH contract.** 1 mirror from a board that isn't in the exports |
| Critical Dates - Hostetler / NSIH (14 / 6) | Same NSIH board | The Hostetler PSA dates (feasibility, extensions, title, closing, farming, survival); no contract in the CRM |
| Press / Postings (10 / 2) | Team board, kept as is | 4 cards name NSIH or MEIH (project name match only) |
| Administrative (4 / 4) | Team board, kept as is | "Vendor: The Schreifer Group" isn't in the directory |
| Other Markets (6 / 17) | Team board, kept, or a "Markets" board | none (Denver, Taylor TX, Berkeley, NC…) |
| Airport Commerce (2 / 9) | Team board, kept | none |
| Project Dragon - Elbit (0 / 0) | Drop | The Elbit deal exists |
| 22 archived lists (Exploration Park and its Archive, Propulsion Park, Precision Park, NSIH Bloomfield, King Ranch, Volare, TexAmericas, RRAD, Avio, Niron, Strategic, Prospecting, Tenant Followups…) | **Decision**: an archived "Properties-General archive" board, or skip | Old campuses and projects that aren't in the CRM (it has campuses NSIH, MEIH and Greystar only) |

- **Name matching:** only 7 of 334 cards match anything by `core_name`: 4 project names in Press cards, 1 firm and 2
  people, both likely false hits. Nothing here maps to companies or deals.
- **Labels (17; normalize):** Ongoing and On-Going, and To Do and To-Do, are duplicates to merge. Critical Date (82)
  and Deal Info (11) are categories. NSIH (30), Exploration Park (19), Project Volare (9), Precision Park (5), BAHAP,
  Houston Space Center and Ground Lease are project tags, kept as labels. Follow Up (15), PR/Postings (9), Tentative
  (7) and Information (1) are kept.
- **People:** Kellen (50 comments, 108 cards), Simon (3, 69), David DeNardo → `David` (2 comments, 72 cards; 140
  actions, the most of anyone after Kellen), David Dowell → `Dave` (45 cards), Paul (3 cards), John (1). **Outside
  the team:** Melissa Guerrero (1 card).

## Totals

| | MEIH | Prospecting | General | All |
|---|---|---|---|---|
| Cards (open / archived) | 60 / 13 | 305 / 71 | 165 / 169 | 530 / 253 |
| Live cards (open, in open lists, not mirrors) | 60 | 251 | 44 | 355 |
| Comments in export / on cards | 118 / 118 | 186 / 1,660 | 55 / 697 | 359 / 2,475 |
| Checklists (items) | 55 (211) | 200 (564) | 68 (274) | 323 (1,049) |
| Attachments (linked, not downloaded) | 58 | 174 | 176 | 408 |
| Linked to a CRM record | ~15 by hand (1 by name) | 103 by key + 19 by URL; 175 to companies | ~4 | |

Attachments are Trello-hosted uploads. A link opens only for someone signed in to Trello and dies if the workspace
closes.

## Decided by Kellen, 2026-10-22

- **The CRM takes priority; the comment history still comes over.** Where Trello and the CRM disagree (dates, stages,
  names, statuses, links), the CRM wins and the import overwrites nothing on a record. Every Trello comment is imported
  with its author and original date, so the hub keeps the record of the conversation. That includes the comments on
  cards that become feed or bound cards: they land on the record's card. Getting every comment needs the full-history
  fetch (decision 1 below), not just the export.
- **ARA merged** (done 2026-10-22): "ARA (Applied Research Associates)" (a Prospect with no people, deals or mail and
  one timeline entry) folded into "Applied Research Associates" with `merge.merge`, as Kellen. The surviving company
  keeps the ARA deal, 19 people, 23 meetings and its timeline. Backup `backups/db-2026-10-22-before-ara-merge.sqlite3`.
  1,769 companies now.
- **One MEIH board** for the campus. The Forge and ARA Facility are MEIH's projects, so they're lists on it, not boards
  of their own. Kellen left the layout for attorney, designer and consultant work to Claude; it follows.

### The MEIH board (campus MEIH, Project board template)

| # | List | Kind | Holds | From Trello |
|---|---|---|---|---|
| 1 | Land sites | Feed: MEIH sites, Dropped hidden | One card per site, open on the site | Land Sites, COLD - Land Sites (their comments go onto the site's card) |
| 2 | Critical dates | Feed: dates on MEIH contracts | The 4390 PSA today; the County / LABP PSA and the ARA PDA and lease once they're contracts | 4390 Indian Head Hwy - Critical Dates (cards not imported; their 1 comment goes onto the contract's card) |
| 3 | The Forge – 4390 / ETC | Kept | One card per workstream: PSA & ETC lease (→ contract), design meetings / plans, GC RFP, survey, Powers Brown B101 | 4390 / ETC - Deal and Design |
| 4 | ARA Facility – deal | Kept | The ARA PDA and lease, monthly invoices, budget updates; cards linked to the ARA deal | ARA |
| 5 | ARA Facility – sites and design | Kept | County land PSA, LABP PSA, design, GC RFP, MBI B101, permitting | County Land - D&D, LABP - D&D, ARA Building, Land Use |
| 6 | Consultants | Kept | One card per firm, linked to it in the directory, with its role in the description; roles not yet filled (transaction attorney, land use attorney, contractors) stay cards whose checklist is the shortlist | Contacts and Consultants |
| 7 | Open tasks | Feed: MEIH's open tasks | Every real Task for MEIH, with its assignee and due date | (new) |

**How attorney, designer and consultant work is tracked** (unlike Trello, where the deal list, the contacts list and
loose to-do cards each held part of it):
- **Lists are projects, labels are disciplines.** A workstream is one card in its project's list, labelled Legal,
  Architecture, Civil, Land use or Program / procurement. To see everything with the attorneys across both projects,
  filter the board by the Legal label (and save it as a view, "With counsel"); the same goes for designers
  (Architecture) and civil engineers (Civil).
- **The card's members are the ACMI people who own it.** The outside party is the firm on the Consultants list,
  @-named in the description ("Cozen: drafting the ETC lease"). The card's record link stays on what the work is
  about: the contract, the site or the deal.
- **To-dos are checklist items with a date and an assignee.** One with a hard date or someone outside waiting on it
  becomes a real Task ("Make it a task"). It then shows in Open tasks and My tasks and gets the daily due sweep.
- **Who has the ball** on a contract or an entitlement is the record's own "Waiting on" field, which the hub already
  has. `boards-bound` adds "Waiting on" (and since when) to the contract and entitlement facts on the card (today
  they show status, counterparty and the next date or step), so nobody keeps a second copy in a comment.
- **Dates that come from a document** (feasibility, deposits, closing, notices) are contract dates, never card due
  dates. That way they appear in the Critical dates feed, the calendar and the alerts, and an extension moves them
  everywhere at once.

## Full-history fetch, 2026-10-26

Kellen ran `trello_fetch.py` at the laptop: 15 boards into `data/imports/trello/2026-10-26/` (gitignored), every
action back to each board's start. Comments per board: Properties-Prospecting 1,661 (the export had 186),
Properties-General 688, Federal-Pursuits 581, Properties-NSIH 521, Group-Communications Calendar 490, Federal-General
198, Properties-Campus Pursuits 185, AMP 128, Maryland / MEIH 118 (same as the export), Exploration Park 97 (closed),
Group - Indiana 74, Capitalization 41, MEIH (a second board, 21 cards) 34, Project Volare 22 (closed), Federal-Airport
Commerce Prospects 1 (closed).

**Kellen, 2026-10-26: not imported:** AMP, Group-Communications Calendar, Federal-Pursuits, Properties-Exploration
Park, Properties-Project Volare, Federal-Airport Commerce Prospects. Not yet decided: Federal-General, Group - Indiana,
Properties-Capitalization, Properties-Campus Pursuits, Properties-NSIH and the second MEIH board. No import command
exists yet (`boards-import` isn't built); nothing has been written to the database.

## Needs Kellen's decision

1. **Full history.** The exports lack 89–92% of comments on Prospecting and General. Options: run
   `trello_fetch.py` at the laptop (the API pages through every action), or open a new session where the Trello
   connector loads. Recommended: fetch before importing those two boards. MEIH is complete and can go from this export.
2. **Other boards.** Nine mirror cards point at a board that isn't here (an NSIH board, or a "Partners" board?). Export
   it too, or say it's out of scope.
3. ~~MEIH: one board or two?~~ Decided: one MEIH board (above).
4. **MEIH site links to confirm:** County Land → "Airport Land – 4075 Livingston Rd (County)"? Rosewick Development
   Site → "Rosewick Road" or "Bardon – 9622 Rosewick Rd"? Make sites for Lewis Place and NSWC - IHD, or leave them
   plain cards?
5. **4390 critical dates.** Recommended: the feed shows the contract's dates (already extended). Don't import the 11
   Trello date cards; put their 1 comment on the contract.
6. **Prospecting's non-stage lists:** New Leads (87 companies, none with a deal), Cold (59; 419 comments), Partners
   (6, mostly mirrors). Recommended: New Leads and Cold become unbound lists on the Tenant pipeline board, each card
   linked to its company, so the Cold history comes over without inventing "lost" deals. The 9 archived lists come over
   as archived cards.
7. **NSIH critical dates:** add the Hostetler PSA and the Prometheus agreement as contracts first (dates then live in
   the Critical dates feed and the due sweep), or import the 22 date cards onto a kept list.
8. **Properties-General's 22 archived lists** (old campuses and projects): import as one archived board, or skip.
9. **Directory gaps** found in passing: Kimley Horn, Maryland Department of Commerce, Powers Brown and The Schreifer
   Group aren't firms. Add them so cards can link?
10. ~~Merge the two Applied Research Associates companies.~~ Done 2026-10-22 (above).
11. **Outside people:** Joe Don Bowling, Todd Catlin, Melissa Guerrero and Nick Bass. Their comments keep their names
    in the text, as specified, and none of them gets an account.

## boards-bound before boards-import? Yes

- **Prospecting is the bound board.** All 102 live stage cards are deals already. Imported before bound lists exist,
  they'd become 102 loose cards duplicating the pipeline, and `boards-bound`'s data migration would then have to merge
  them. With bound lists first, the migration makes one card per deal, and the import only attaches history
  (comments, checklists, attachments) to those cards by `source_key` / Trello URL.
- **MEIH's Critical Dates and Land Sites are feeds.** Its 11 date cards are the contract's dates, and Trello's are
  stale (pre-extension). 11 of its 18 site cards are existing sites. Importing them as plain cards would make a
  second, wrong copy that `boards-bound` then has to remove.
- **Properties-General barely depends on it** (no records to bind). Its team-board lists could be imported any time,
  but they wait on the full-history fetch anyway.
- Order: `boards-bound` → full-history fetch → `boards-import` dry run for MEIH (with a hand-made link table), then
  Prospecting (history onto deal cards), then General → `boards-polish`.
