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
Park, Properties-Project Volare, Federal-Airport Commerce Prospects, Federal-General, Group - Indiana. **To import:**
Properties-Prospecting, Maryland / MEIH, the second MEIH board, Properties-NSIH, Properties-General, Properties-Campus Pursuits and Properties-Capitalization. 

**Imported 2026-10-26: Properties-Prospecting** (`manage.py import_trello`, see HANDOFF "Trello history, Prospecting"): 1,661 comments, 200 checklists, 175 attachments, 807 history entries onto the Tenant pipeline board; decision 6 below taken as recommended (New Leads and Cold as unbound lists, cards linked to their company). The other boards still need their mapping.

## The two MEIH boards: import plan (2026-10-27)

**Imported 2026-10-27** (Maryland / MEIH only; see HANDOFF "Trello history, Maryland / MEIH"). **Kellen's answers:** 1 and 2 yes; Lewis Place is **Bryans Road Assemblage**; 4390 is The Forge; NSWC - IHD is managing the relationship with the base, so it's a plain card on Consultants; the ARA date cards are skipped until Kellen uploads the final PDA (their history went onto Contact List & Agreement); 5 yes (the three firms added); 6 no: **the second MEIH board is skipped**. The plan as proposed follows.

### Maryland / MEIH → the hub's MEIH board (board 2, from `boards_setup`; its 7 lists are empty today)

73 cards, 118 comments (Kellen 109, Simon 6, Dave 1, David 1, Joe Don Bowling 1), 55 checklists, 58 attachments.
Every comment comes over with its author and date. A card linked to a site, contract or deal also puts its comments on
that record's page. The CRM wins on names, statuses and dates, as on Prospecting.

| Hub list | Trello cards | Linked to |
|---|---|---|
| **Land sites** (feed) | Lake Acton Business Park; County Land; Rosewick Development Site; 4390 Indian Head and ETC Building (archived); Billingsley Assemblage; NSWC - IHD; Lewis Place | Lake Acton (site 2); **County Land → Airport Land – 4075 Livingston Rd (County)**; **Rosewick → Rosewick Road** (the site with the Rosewick Road PSA, not Bardon – 9622 Rosewick Rd); 4390 Indian Head Hwy (ETC); Billingsley WCD Assemblage. NSWC - IHD and Lewis Place have no site: plain cards at the end of the list |
| Land sites, **archived** | COLD - Land Sites: CA Billingsley, Berry Pointe, Berry Rd Assemblage, Copart, Indian Head / Maryland (8 comments); Garner Land | Their sites (all Dropped, except Copart, which is Outreach). Indian Head / Maryland has none. The comments show on each site's page |
| **Critical dates** (feed) | "4390 Indian Head Highway PSA" (1 attachment) and the 1 comment on "Feasibility Period Expiration" become one card for the 4390 PSA | Contract: 4390 Indian Head Hwy PSA. **The other 10 date cards aren't imported:** Trello still has the pre-extension dates (Oct 22 / Nov 1 / Dec 11 / Dec 21); the hub has the extended ones (Feb 19 to Apr 20, 2027) |
| **The Forge – 4390 / ETC** | 4390 / ETC - Deal and Design (7): PSA & ETC Lease, Design Meetings / Plans, GC RFP (30 items), Powers Brown B101, Survey Completion Date, "4390 Indian Head Hwy", Survey (archived); plus "4390 IHH - Design Meetings / Plans", which sat in the LABP list | PSA & ETC Lease → the 4390 PSA; "4390 Indian Head Hwy" → its site; the rest plain cards |
| **ARA Facility – deal** | ARA (16): Contact List & Agreement, Action Items (17 items), certificates of insurance, invoices Month 1-5, budget updates, lease negotiation start / finish, non-renewal notice, PDA initial term expiry | The ARA deal. Dates stay as card due dates, done where Trello ticked them. **Not ticked in Trello, so they'll show as late:** Invoice Month 3 (Aug 17), Complete lease negotiations (Oct 12), Invoice Month 4 (Oct 14), Non-renewal notice (Oct 16). Still ahead: Invoice Month 5 (Nov 16), PDA initial term expires (Nov 15) |
| **ARA Facility – sites and design** | County Land - Deal and Design (3), LABP - Deal and Design (PSA Negotiation, LABP design archived), ARA Building (4: Design, Design Meetings, GC RFP, MBI B101), Land Use (PGM / Permitting) | County Land PSA and County Land → Airport Land site; PSA Negotiation → Lake Acton site |
| **Consultants** | Contacts and Consultants (7): Cozen O'Connor (Engaged), Kimley Horn, Maryland Department of Commerce, Transaction Attorney, Land Use Attorney, Contractors, Construction Attorney - Firm Options; archived: Land Use Attorney (6 comments, 12 items), "Respond to counsel on County land PSA", ARA - Architect RFP | Cozen → the firm. Kimley Horn and Maryland Department of Commerce link once they're in the directory (below) |
| Archived on The Forge | Land Sites' old task cards: Groundbreaking, Civl Engineering (6 comments), Architectural RFP (6 comments), Prometheus extension notice | none |

Labels: Critical Date, Deal Info, Legal, Program/Procurement, Civil and Architecture go onto the board's own categories;
Ongoing, To Do and Contact Information onto the three labels `boards_setup` added. Members: Kellen, Simon, Dave, David.

### The second "MEIH" board → its own board, "MEIH program"

It isn't a property board. It tracks the MEIH program's milestone deliverables to the funder: MS#1 kickoff to MS#11 Year 1
final report, quarterly technical and business status reports, quick-win plans. Its members are Paul Kadzielski, John
Burer, Melissa Guerrero, Ken Rothaermel and Todd Catlin; Kellen isn't one. 21 cards, 34 comments (Joe Don Bowling 31,
Todd Catlin 2, Ken Rothaermel 1), 11 checklists of 6 items, 23 attachments.

- **Recommended: a separate board, "MEIH program"** (campus MEIH), with Trello's lists kept: Project Background,
  Upcoming Deliverables, Action Items (RAIL), Blockers, Management Concerns, Campus Engagement, Completed Deliverables
  (Graveyard is empty and is dropped). Milestones keep their due dates and done ticks (MS#6, due Sep 7 and not ticked,
  will show late; MS#9-11 are due Dec 18, Mar 19 and Mar 30). Paul is the board's member; the other four aren't in
  the hub, so their names stay in the comments. Putting it on the MEIH board instead would mix funder reporting into
  the deal and design lists.

### Kellen's decisions for these two boards

1. County Land = **Airport Land – 4075 Livingston Rd (County)**? (recommended: yes)
2. Rosewick Development Site = **Rosewick Road** (the one with the PSA)? (recommended: yes)
3. NSWC - IHD, Lewis Place and Indian Head / Maryland: plain cards (recommended), or make them sites?
4. ARA dates: card due dates (recommended for now), or make the ARA PDA a contract first so they reach the critical
   dates calendar and alerts?
5. Add **Kimley Horn**, **Maryland Department of Commerce** and **Powers Brown** to the directory so the cards link?
   (recommended: yes)
6. The second board: its own "MEIH program" board (recommended), lists on the MEIH board, or skip?

## Properties-NSIH: import plan (2026-10-27)

**Imported 2026-10-27**: Kellen took all six recommended answers below (see HANDOFF "Trello history, Properties-NSIH").

David DeNardo's working board for the Bloomfield, Indiana campus. 121 cards on 10 open lists and 6 archived
ones, **521 comments** (David DeNardo 455, Simon 43, Patrick Dillmann 18, Kellen 2, Olivia Jaskula-Dybka 2, Eli
Miller 1), 42 checklists, 61 attachments. Comments by list: Design & Construction 218 (Haskell alone 57, Pepper /
Weddle 31, Prometheus change orders 29, Structurepoint 27), Administration 99, Infrastructure / Utilities 52, Due
Diligence 41, Land Contracts 26, Parcel Owners 25, Tenancy / Contracts 24, Prospecting (archived) 18, Capitalization 15.

**What the hub has for NSIH:** the campus and one project (NSIH), 38 tenant deals, no land sites, no contracts, no
entitlements, no board. So, unlike MEIH, there's little for feeds to show, and the board keeps Trello's workstream
lists.

### A new NSIH board (campus NSIH, project NSIH)

| # | Hub list | From Trello | Links |
|---|---|---|---|
| 1 | Administration | Administration (19; 9 archived: Deal Summary 28 comments, Financial Model 11, Investor Deck 9, PSA, Schedule, FAQ…) | Insurance - Marsh → Marsh McLennan Agency. Tax Incentives - Ryan LLC, Legal - Drewry Simmons Vornehm: not in the directory |
| 2 | Design & construction | Design & Construction (20; 11 archived) | Architect - Haskell → Haskell; CM - Pepper / Weddle → Pepper Construction; Civil - Structurepoint → Structurepoint; Schreifer Group → The Schreifer Group; CMc Contract - Shiel Sexton → Shiel Sexton Company. Architect - Haskell keeps its Nov 16 due date |
| 3 | Due diligence | Due Diligence (9; Survey and Wetlands archived) | Geotech, gas wells, loggers, Earthsource, SMS Energetics, TRC, Johnny's Signs: none are in the directory |
| 4 | Infrastructure / utilities | Infrastructure / Utilities (12) | Fiber - Smithville → Smithville; ROW Acquisition → First Group Engineering. The utilities (UDWI & Hoosier, Eastern Heights, Greene County, Midwest, INRD, INDOT, APT Solar) aren't in the directory |
| 5 | Land contracts | Land Contracts (5): Land PSA - NFP, Land PSA - Hostetler, offers to Sullivan & Myers (archived) and Prometheus, Kelly - neighbor | none: the hub has no NSIH contracts |
| 6 | Parcel owners | Parcel Owners (25 landowner cards, Royal Family 16 comments) | Plain cards. Landowners never become companies (Kellen, 2026-10-19) |
| 7 | Tenants and leases | Tenancy / Contracts (7 + 1 mirror skipped) | Prometheus Ground Lease, DMA, Points of Contact, Export Control (archived) → the Prometheus (Kratos / Rafael) deal; Ground Lease - iRocket → iRocket; Tenant - Spinner → Spinner; CFIUS Review → Holland & Knight |
| 8 | Capitalization | Capitalization (10): CTL Loan, Scaling Space HNW, Prospective LPs, Strongbox, Browning, OneIM, Bain (×2), Fortress, Cerberus | Bain → Bain Capital. The rest aren't in the directory |
| 9 | Models & decks | Models & Decks (2) | none |
| 10 | Open tasks (feed) | (new) | NSIH's open tasks |
| — | Archived on Tenants and leases | Prospecting, an archived list: Lease - Prometheus, Kratos, LOI - Kratos → the Prometheus deal; VTS, Lauth Partnership plain (6 mirror cards skipped) | |

Labels: the board uses none. Members: David, Simon, Kellen, Dave, Paul, John (Melissa Guerrero and Patrick Dillmann
aren't in the hub). Properties-General's "Critical Dates - NSIH" and "Critical Dates - Hostetler / NSIH" lists will
join this board when General is imported.

### Kellen's decisions for NSIH

1. **Board layout:** keep Trello's workstream lists as above (recommended), or the MEIH layout (sites, dates, project
   lists, consultants)? The MEIH layout needs NSIH sites and contracts that don't exist yet.
2. **Land PSAs (NFP, Hostetler) and the Prometheus ground lease / DMA:** plain cards now (recommended), linked once
   the documents are uploaded as contracts, or upload them first?
3. **Directory:** add the firms the cards name that aren't there yet, so the cards link? Services: Ryan LLC, Drewry
   Simmons Vornehm, Earthsource, SMS Energetics, TRC Companies, Johnny's Signs, APT Solar Solutions, Weddle. Capital:
   Fortress, Strongbox, Browning, OneIM, Cerberus, CTL Capital. Utilities (UDWI, Hoosier, Eastern Heights, Midwest
   Gas, INRD, INDOT) stay plain cards. (Recommended: yes for services and capital.)
4. **Kratos / LOI - Kratos** (archived, 2025): onto the Prometheus (Kratos / Rafael) deal (recommended), or the
   separate "Kratos · NSIH" deal?
5. **The Prometheus deal has no campus.** Set it to NSIH? (Recommended: yes; it's a one-field change.)
6. **Patrick Dillmann** (18 comments, a board member): outside the team, name kept in his comments (recommended), or
   does he need a hub account?

## Properties-General: import plan (2026-10-27)

**Imported 2026-10-27** (see HANDOFF "Trello history, Properties-General"). Kellen's answers: 1 changed: the NSIH and Hostetler date cards wait for the PSA and related documents ("I can upload the PSA and related docs"); their comments and attachments went onto the NSIH board's agreement cards. 2, 3 and 4 as recommended.

334 cards on 31 lists (9 open, 22 archived), **688 comments** (Kellen 326, David DeNardo 245, Simon 101, Olivia
Jaskula-Dybka 7, a few from others). It's three things at once: the team's live general lists, the NSIH critical
dates, and the history of old campuses and projects. Comments by group:

| Group | Trello lists | Comments | Goes to |
|---|---|---|---|
| Team lists (open) | Press / Postings (12), Administrative (8), Other Markets (23), Airport Commerce (11) | 146 | A new team board, **"ACMI general"** |
| NSIH dates | Critical Dates - NSIH (19 + 1 mirror), Critical Dates - Hostetler / NSIH (20), and the archived "NSIH (Bloomfield, IN)" (11) and its Critical Dates [David] (4) | 28 | The **NSIH** board |
| Deals | Project North Star - Niron Magnetics (5, archived list), MEIH - ARA's one real card (ARA - Lease Negotiation) | 5 | Niron's cards go on "ACMI general" in an archived list, linked to the Niron deal; the ARA card goes on the MEIH board, archived, linked to the ARA deal |
| Old campuses and projects (archived) | Avio (15) and Critical Dates - Avio (3), Strategic (34), RRAD (5), Propulsion Park (Tasks 7, Critical Dates 7), Precision Park (9, 5), King Ranch - Aurora (7), TexAmericas (2), U.S. Army Enterprise Strategic Capital Partnership (2 + 1 mirror), Prospecting (7), Tenant Followups (1) | 276 (Avio 158, Strategic 48, Propulsion Park 34) | An archived board, **"Properties-General archive"** |
| Exploration Park and Volare (archived) | Exploration Park (30), its Critical Dates (19), Exploration Park - Archive (37), Project Volare (4) | 233 | **Skipped**, as the Exploration Park and Volare boards were |
| Mirrors and empty lists | 4390 Indian Head Hwy - Critcal Dates (11 mirrors of MEIH cards), MEIH - ARA's 13 mirrors, Project Dragon - Elbit, Volare (Hurt, VA), Press Releases (empty) | 0 | Skipped |

### "ACMI general" (team board)

Lists kept as in Trello: **Press / postings**, **Administrative**, **Other markets**, **Airport Commerce**, and the
archived **Project North Star – Niron**. Links: Spinner Modifications (22 comments) and Airport Commerce: Spinner
Questions → the Spinner deal; Supernova Modifications → the Supernova deal; Vendor: The Schreifer Group → that firm;
Niron's five cards → the Niron Magnetics deal. Everything else is a plain card (Denver MSA, iMarket - Taylor TX,
Berkeley Space Center, Prince 10 - Tucson…). Press cards keep their due dates (Prometheus permits and grading, the NYC
office, the sign at Harvest Church…). Labels: Ongoing and On-Going merge, To Do and To-Do merge; Tentative, Follow Up,
PR/Postings, Information and the campus labels (NSIH, Exploration Park, Precision Park, Project Volare, BAHAP,
Houston Space Center, Ground Lease) are kept as labels.

### NSIH dates on the NSIH board

Two new lists: **Critical dates – Prometheus / NFP** (Critical Dates - NSIH: the Phase 2 feasibility and extension
dates out to 2030, the monthly project-control report and invoice to Prometheus, the ground-lease extensions, the
NFP Phase 1 dates) and **Critical dates – Hostetler PSA** (20 cards: feasibility and its two extensions, deposits,
title objection, closing with 0 / 1 / 2 extensions on Dec 21, Mar 22 and Jun 21, farming, timber, survival to Dec
2028). Cards keep their due dates and done ticks; Trello's archived ones come over archived. The old "NSIH (Bloomfield,
IN)" lists (2025, Phase 1) come over as archived cards on Administration. **Not ticked in Trello, so they'll show as
late:** Title Objection Deadline (Sep 22) and Send ACMI Invoice to Prometheus (Sep 1).

### Kellen's decisions for Properties-General

1. **NSIH and Hostetler dates:** import them as dated cards on the NSIH board now (recommended: the hub has no
   Hostetler or Prometheus contract, and the Hostetler closing is Dec 21), or skip them until the PSA and agreements
   are uploaded as contracts, as with ARA?
2. **The team lists:** a new "ACMI general" team board (recommended), or onto an existing board?
3. **Old campuses and projects:** one archived "Properties-General archive" board with Avio, Strategic, RRAD,
   Propulsion Park, Precision Park, King Ranch, TexAmericas, the Army partnership, Prospecting and Tenant Followups (276
   comments; recommended, so the history is searchable), or skip them?
4. **Exploration Park and Volare lists** (233 comments): skip, as their boards were (recommended), or put them in the
   archive board too?

## Properties-Campus Pursuits and Properties-Capitalization: import plan (proposed 2026-10-27, waiting on Kellen)

### Properties-Campus Pursuits → a new team board, "Campus pursuits"

The ranked list of places ACMI might build next. 57 cards (36 live), **185 comments** (Simon 80, John Burer 45, Dave
29, Paul 28, Kellen 2, Olivia Jaskula-Dybka 1), 9 checklists, 27 attachments. Lists kept: **High priority**, **Medium priority**,
**Low priority**, **Holding / overflow**; the archived lists' cards (Strategic Initiatives, 2025 ranking) come over
archived. 2 mirror cards skipped.

| Card | Linked to |
|---|---|
| TBD - Elbit America ("Project Dragon") | Elbit America / 26Defense deal |
| Niron RFP | Niron Magnetics deal |
| Thales - TBD location | Thales deal |
| SAIC - South Carolina | SAIC deal |
| MD - Johns Hopkins / APL | Johns Hopkins University deal |
| Long Wall (Indiana Munitions Campus) | Long Wall · NSIH deal |
| Minnesota / North Wind, Minnesota - Northwind, North Wind | North Wind deal |
| Project: Saab (supplier campus in Michigan) | Saab deal |
| Vulcan Forms & Specter Aerospace | Specter Aerospace deal |
| Castelion | Castelion deal |
| Everything else (IN - NSIH Indiana 17 comments, MD - Indian Head 23, Investor - Scaling Space 17, NC - Cumberland County 10, CA - California Forever 12, TX - Red River 7, Bee Cave, Metis Endeavor, Karman, UMKC, ORNL…) | plain cards |

**Two cards are Exploration Park and Volare:** "TX - Exploration Park / Nassau Bay" (42 comments, 31 checklist items) and
"VA - Project Volare (Avio)" (15 comments), plus the archived "Exploration Park" (1). They're the go / no-go history
of those pursuits rather than the project boards that were skipped.

### Properties-Capitalization → a new team board, "Capital partners"

The equity and debt partner pipeline: 16 cards (15 live), **41 comments** (Kellen 36, Dave 5), 29 attachments
(decks and term sheets). Lists kept as the pipeline: **Intro conversations**, **Active discussions**, **Term sheet /
negotiation**, **Committed / closed**, **Passed / not a fit**. Labels kept: GP Capital, LP Capital, Programmatic JV,
Lender, NSIH, MEIH, Virginia. Each card links to its directory firm:

- Already in the directory: BGO, Montgomery Street Partners, Koch Real Estate, Bain Capital, OneIM, CTL Capital,
  Cerberus, Fortress.
- Not yet: Wellington, Temerity, Silver Peak, Davis Companies, L&B Capital, Blue Owl, JRE (added as investors).

The hub's capital tracker (`CapitalSource`) is per project and per loan or raise (today: the two Forge construction
loans), so these partner relationships are cards on firms rather than capital sources. A partner that makes an offer
on a project becomes a capital source then. The NSIH board's Capitalization list (Fortress, Bain, OneIM, Cerberus,
CTL…) stays as it is.

### Kellen's decisions

1. **Campus pursuits:** its own team board (recommended), or lists on "ACMI general"?
2. **The Exploration Park / Nassau Bay and Project Volare (Avio) pursuit cards** (58 comments): bring them over
   (recommended: they're the record of why those pursuits went the way they did, and sit on a live list), or skip
   them like the Exploration Park and Volare boards?
3. **Capital partners:** its own team board linked to directory firms (recommended), or capital sources?
4. **Add the 7 missing investors** to the directory (recommended: yes).
5. **Fortress:** rename the directory firm "Fortress" to **Fortress Investment Group** and make it the Fortress data
   room's partner firm (the room has none today)? (Recommended: yes.)

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
