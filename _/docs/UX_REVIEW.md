# UX review: the CRM through a new team member's eyes (2026-10-21)

Kellen asked for a fresh look at the whole web app as a brand-new ACMI Properties team member: how pages are laid
out, how actions work, and how hard it is to move around. Walked on the scratch demo database (made-up data).

## What a new person runs into

| # | Problem | Where | Why it hurts |
|---|---|---|---|
| 1 | **No search anywhere.** To find a company you have to know which page it lives on. | every page | The fastest way to find a company we're talking to today is the Investor report, because Companies lists every outreach prospect (about 1,800) A-Z. |
| 2 | **The sidebar hides itself.** It collapses to a thin strip until you hover; nothing says it is there. | `base.html` | A first-time user sees a page with no navigation. |
| 3 | **30 links in 6 sections, sorted by module, not by job.** Sub-links ("Lenders and investors", "Abstract templates", "Possible duplicates") hang under unrelated items. | sidebar | You have to learn the app's internal structure before you can use it. |
| 4 | **One thing, three names.** Sidebar "Tenant deals" opens a page titled "Opportunities"; "Investor report" opens "ACMI Properties pipeline"; the dashboard button says "Investor pipeline report". | nav, page titles | People can't tell whether these are the same page. |
| 5 | **No "+ New" or recent items.** Creating a deal, task or note means finding the right list page first. | global | Common actions are 2-3 clicks deeper than they need to be. |
| 6 | **Colour carries no meaning.** Every stage pill is the same pale blue; overdue is only red text; primary and secondary buttons look alike. | app-wide | Nothing on a page tells you what matters most. |
| 7 | **The dashboard is one fixed, long page.** An empty month-grid of critical dates takes a full screen; the calendar can't be resized; sections can't be hidden, moved or added. | `home.html` | Everyone gets the same page whether they work land, tenants or capital. |
| 8 | **Saved views exist only on Companies and People.** Boards, tasks, meetings, sites, contracts and the investor report can't be filtered and saved. | list pages | People rebuild the same filters every visit. |
| 9 | **Investor report tables overflow.** Each table scrolls sideways; stage pills overlap tenant names; "Weighted" wraps one digit per line. | `report_pipeline.html` | The page people actually use to find tenants is the hardest to read. |
| 10 | **Notification toasts can repeat** (the same "Task due" shown twice). | `alerts.js` | Noise; needs a check. |

## Proposed redesign

Built in phases, each its own branch, CI run and merge, so the app stays usable throughout.

**Phase 1: Find anything, go anywhere**
- A top bar on every page: search box (also `Ctrl+K` / `/`), a **+ New** menu (company, person, tenant deal, task,
  note, site), notifications and your name.
- Search covers companies, people, tenant deals, sites, projects, contracts and meetings, ranks active deals and
  recently touched records first, and opens with recently viewed records before you type.
- Sidebar open by default (collapse button, remembered), regrouped by job:
  - **Home:** Dashboard, My tasks, Calendar, Notifications
  - **Tenants:** Tenant pipeline, Companies, People, Rent roll
  - **Land:** Sites, Map, Client searches, Entitlements
  - **Campuses:** Campus summary, Projects, Contracts, Capital
  - **Outreach:** Send queue, Mail review, News review, Templates, Weekly report
  - **Directory and lists**, then Settings
- **Tenant pipeline** (renamed from Investor report) becomes the one tenant page, with Board / Table / Report tabs;
  "Tenant deals" and "Opportunities" go away as names. The printed PDF keeps its outside-facing title and the logo.
- Companies opens on "Active: in a deal or talked to in 30 days" with a one-click "All companies".
- Breadcrumbs and a back-to-list link on every record page.

**Phase 2: Colour that means something**
- One token set in `app.css`: navy stays the brand; colour is reserved for meaning.
  - Stages: grey (prospect) → blue (qualified) → indigo (LOI) → teal (lease) → green (signed); amber on hold; muted red lost.
  - Dates: red overdue, amber due today/this week, neutral later.
  - Priority: red high, amber medium, grey low.
  - Buttons: solid navy primary, outline secondary, red only for delete.
- A small legend wherever colour is used, and text labels kept so it reads in print and for colour-blind users.

**Phase 3: A dashboard you build yourself**
- The dashboard becomes a grid of widgets: KPI cards, This week calendar, Critical dates, My tasks, Replies waiting,
  Deals needing attention, Pipeline by stage, Pipeline changes, Recent activity, Deadlines, Deal suggestions,
  Outreach funnel, plus new ones (My sites, Campus updates, Rent roll summary).
- **Customise** mode: drag to reorder, drag the corner to resize (the calendar can be made taller or full width),
  hide a widget, or add one from a gallery. Reset to default. Saved per person.

**Phase 4: Customisable views everywhere**
- One shared "view bar" on every list and board: filters, sort, columns (show/hide/reorder), density, saved views
  (personal or shared). Extends the existing `SavedView` to tasks, meetings, the tenant pipeline (all three tabs),
  sites, contracts, client searches, rent roll and the directory.

**Phase 5: Polish**
- Investor report layout (no sideways scroll), the duplicate toasts, helpful empty states ("No sites yet: open the
  Map to build one"), and a short first-visit tour.

## Decided with Kellen (2026-10-21)

- "Rename that to Tenant pipeline": the Investor report and the Tenant deals board are one **Tenant pipeline** page
  with Board / Table / Report tabs. The printed report keeps its "ACMI Properties pipeline" title and the logo.
- Dashboard layouts, hidden columns and compact rows are saved **per person on the server** (`UserPrefs`, migration 0055).
- Layout: **CoStar style**, a navy bar across the top with drop-down menus (no left sidebar), and a large search box
  at the top of the dashboard that searches everything.
- Built as **one branch** (`ux-overhaul`), all phases together.

## What was built (branch `ux-overhaul`)

- **Top bar on every page** (`base.html`, `nav.js`): Home, My tasks, Calendar, then Tenants / Land / Campuses /
  Directory / Outreach / Settings menus that open on hover with a one-line description per page; Search (`Ctrl K`
  or `/`), an amber **+ New** menu, the bell and an account menu. Narrow screens get a ☰ menu.
- **Search** (`crm/search.py`, `/search/?q=`): companies (in a deal first, then most recently touched), tenant deals,
  people, sites, projects and campuses, contracts, directory firms, meetings, and pages by name. Before you type it
  shows recently opened records (per browser).
- **Dashboard** (`home.html`, `widgets/*.html`, `dashboard.js`, `crm/prefs.py`): the search hero, then widgets.
  Customize mode drags to reorder, drags the corner to resize (width snaps to ¼ ⅓ ½ ⅔ full, height is free;
  double-click the corner to fit content), hides with ×, adds from a gallery, resets to default. Only shown widgets
  are computed. New widget: My sites.
- **View bar on every list** (`partials/view_bar.html`, `views.js`, `{% view_bar "page" %}`): saved views (personal
  or shared), "open this view when I come here" (★), show/hide columns, compact rows. On Tenant pipeline, its report,
  Tasks, Calendar, Sites, Contracts, Rent roll, Directory, Entitlements, Capital, Projects, Companies and People.
- **Companies** opens on **Active** (in a deal, engaged or later, or touched in 30 days), with In a tenant deal and
  All companies one click away; a person's default view wins.
- **Colour**: each deal stage has its colour (grey prospect → blue → violet LOI → teal lease → green signed, amber
  on hold, rose lost) on pills, board columns and search results; site statuses likewise; red overdue and amber
  due-today on tasks; amber is the "create" colour.
- Breadcrumbs on company, person, deal, task and project pages; the report no longer scrolls sideways.
- **Bug fix:** the daily due-task alert could be stored twice when two pages loaded at once, giving duplicate toasts.
  Keyed notifications are now unique per person (migration 0055 first removes existing duplicates).

## Second pass (2026-10-22): findings only, no code changed

Walked the whole app again as a first-week team member, on a scratch database of made-up data sized like the real one
(2,000 companies, 2,855 people, 18,600 emails, 88 meetings through the real import path, 952 queued emails over 396
domains, 157 firms, 12 sites on 400 parcels). 59 URLs at 1440 / 1024 / 390 px: 102 screenshots, axe-core on every
desktop page, keyboard probes, server time and query counts per request, signed in as a Manager and as a non-Manager.
449 tests pass on main. Screenshots: `docs/ux-review-2/` (desktop-* and phone-*, cropped and downsized). Web page of this report: https://claude.ai/artifact/7MeJEVyTfnFBXXDgVefAtj

Live counts (read-only, from the desktop session): 126 of 189 outside meetings in 60 days reach no record; 2,470 emails
over 406 domains wait in Mail review (oldest Dec 2024); 7 same-minute meeting pairs; 0 saved views made by anyone;
6 users, all Managers.

### Priority 1

| # | Finding | Proposed fix |
|---|---|---|
| C1 | An unknown organization has no single place and no primary action: email in Mail review (sorted by count, a newsletter above a bank), its meetings grey on the calendar, a forwarded invite one email among ten; "Add as company" is the primary button on every row; Claude's "medium" verdict (the usual one for a bank) never auto-files. | A **Needs filing** inbox: one card per domain joining emails, meetings (by attendee domain) and invites; Claude's verdict as the headline and one primary action ("Looks like a bank. File as a lender on Project Anvil?") with Accept / Change / Not now / Never; project guessed from title keywords, thread people and the other attendees' firms; Accept files the firm, links the meetings and adds an Identified capital source for a bank, debt fund or broker. Undo as today. |
| C2 | Meetings link only to Companies; firms are matched at render time on the firm page, so a lender call never shows "with Harbor Point Bank" on the calendar, the week widget or the project's Capital page. | A firms link on Meeting filled at import by attendee domain; firm chips with type on calendar rows and pop-ups; upcoming meetings on firm, Capital and Build pages. |
| C3 | No nudge when a meeting has outside attendees and no record; the legend says only "blue = with an outside company". | "2 outside attendees, no record" marker with a File button on the calendar and week widget; counted in the Outreach badge and Managers' daily bell note. |
| C4 | A forwarded invite never reaches the calendar: the sync keeps text only. | Read .ics parts and "Invitation:" bodies in the mail sync into a Meeting (source "invite") that merges with Granola by time and attendee. Built from the standard and a made-up invite. |
| C5 | Free-mail senders are dropped unless the exact address is on a Person; search can't see the review queue, firm contacts or meeting attendees ("Harbor Point", "Priya Raman" find nothing until filed). | "Same person, add this address" suggestions by display name; queue, contacts and attendees in search, labelled "Not filed yet". |
| G1 | No scheduled Granola import; each action item becomes a task one click at a time; the same item on two meetings becomes two tasks; a task points at one meeting. | Scheduled task in the Claude desktop app on the laptop, weekday mornings: `list_meetings` (3 days) → `get_meetings` + `get_meeting_transcript` → `data/imports/granola/<date>.json` → `import_meetings` → `export_transcripts` → one bell note with counts and "N meetings need filing". Idempotent by Granola id; first run 30 days. Tasks get a many-to-many to meetings; same title + owner within 7 days = one task; "Make tasks for all N items" with owner from the bracketed name and "by Friday" parsed to a date. |
| D1 | Same call twice when the other side's invite has a different title: the merge compares titles only. | Match start within 15 min and (similar title OR a shared outside attendee); Granola wins; other title kept as "also invited as"; nightly dry run listed in Needs filing; live pairs merged one by one with Kellen's OK. |
| D2 | A Company and a Firm can share a name unflagged; people with two emails are joined only by hand. | Two new sections on Possible duplicates: "Also in the directory" and "Same person?", run nightly, counted in the menu badge. |
| D3 | Nothing can be undone: deletes and merges are a browser `confirm()` then final. | 30-day Trash (soft delete + Restore) for companies, people, deals, sites, contracts; merges store what came from where so Unmerge works; in-page confirmation dialogs that list what moves. |

### Priority 2

**Accessibility** (axe-core, 58 desktop pages): 1,607 fields without a label (Mail review 796, Possible duplicates
547, Campus summary 57, every New task form); 38 unnamed filter selects on 20 pages; 324 contrast failures (muted
#6b7280 on tinted pills and cards at 11–12 px is 4.0–4.4:1; calendar other-month dates 1.9:1; week times 3:1); 226
links told apart by colour only; 450 tooltip-only labels; heading levels skip on 35 pages.
- A1 Drag-and-drop has no keyboard path: dashboard drag handle tabindex −1, resize corner is a span, width buttons read "¼" with no pressed state; board cards drag only. Fix: a Move menu per widget, focusable handle with arrow keys, labelled width buttons, a stage menu on cards, a live region.
- A2 Menus and the palette work from the keyboard (Enter / Escape / "/" / arrows) but the search inputs have no combobox semantics and `outline: none`; no skip link (14 tab stops before content); no `aria-sort`.
- A3 Colour-only signals: calendar blue = with a company, pencil = notes (legend renders broken); overdue vs today is red vs amber text with no word. Fix: the word next to the colour, record chips on calendar rows, fix the legend, an "Unfiled" marker.

**Speed** (scratch data, warm, median): Site briefs 59 s (zoning/overlay checks on 22 parcels during the page load,
each an outside map call); one site brief 13.6 s; aerial image 3–6 s per request, uncached; Campus summary 178 queries
(139 repeats: five per tenant, site and project); Dashboard 108 (77 repeats: "Replies waiting on us" checks each reply
thread, `pipeline.py` my_day); Sites 88 (67); Sites JSON 54 (45); Company page 43 (17: the user list per form);
Person page 32 (12); Mail review one 1.4 MB page. Fine: Companies 56 ms / 16 queries, board 55 / 14, calendar 25 / 12.
- S1 Move parcel checks and aerials to a background job with cached results and a "checked N days ago · Refresh" link.
- S2 Prefetch / annotate the five N+1 pages; a query-count test on each.
- S3 Paginate and filter Mail review, Directory, Possible duplicates, Contracts, Entitlements, Capital.
- S4 Search ranks by activity, not match: score exact > starts-with > word-start > contains; people by company; firms by contact; the unfiled items; Postgres full-text on Cloud SQL.

**Phone and tablet**: 11 of 32 phone pages render wider than the screen (Directory 844 px, Entitlements 762, Capital
734, Site 640, Client search 513, Firm 478, Mail 454, Home 438, Projects 427, Project 417) so the phone zooms out;
the board scrolls sideways through 8 columns; the map opens on a full screen of filters; the company page's tab row
clips with no cue and has 25 tap targets under 32 px; no manifest, icons or favicon; no quick note or quick task in
+ New; nothing uses location. Sites works well on a phone.
- M1 Scroll containers for tables, tables to cards under 700 px, bigger targets, a fade on scrolling tabs.
- M2 Manifest and icons; "Quick note" in + New (text, photo, record from search, pre-filled from the next meeting); map-first layout with a filter sheet and "Sites near me"; "Log a visit" on the site card.

**Forms and editing**: Company, Person, Deal edit in a dialog; Contract, Site, Firm, Project, Entitlement, Capital
source in an always-open side form; stock `as_p` forms (colon labels, no required marks, errors after submit); only
News review warns before leaving with unsaved edits. Outbound is well guarded (typed word to send a batch, recipient
confirms on the weekly emails, Manager-only share links with expiry); deletes and merges are `confirm()` with no undo;
bulk "Add selected as companies" has one generic confirm.
- F1 One form partial (required mark, help under the field, inline validation, sticky Save, beforeunload on dirty); read view + Edit dialog on every record page.
- F2 In-page confirmation dialogs listing what moves or is lost, plus the Trash (D3).

**Empty, loading, error, help**: no 404/500 templates and DEBUG defaults on, so a wrong link shows Django's debug page;
GET /entitlements/matrix/ is a blank 405; /capital/compare/ without ids is a 404; Mail, Calendar, Person, Company,
Contract and Campus pages tell every user about `.env`, `GOOGLE_CLIENT_ID`, `ANTHROPIC_API_KEY` and restarting the
app; a dozen panels end at "None yet."; fetched pop-ups say "Loading…" with no busy state; the tour is one tip in one
browser; no Help page.
- E1 Branded 404/500 with search; setup notices only for staff, phrased "Ask an admin…"; DEBUG off by default with a local override.
- E2 Every empty panel names and links the next action; skeleton rows with `aria-busy`; a Help page and a five-step tour saved per person (search, + New, menus, customize, save a view).

### Priority 3

**Wording** (menu → page → elsewhere → proposed): Calendar → Meetings → Calendar; My tasks → Tasks → Tasks; Mail review
→ Mail → Needs filing; Send queue → Outreach → Send queue; Firms and contacts → Directory → Directory; Campus summary →
MEIH summary (no index) → Campus summary with a switcher; Projects → Campuses & projects → Projects; Possible
duplicates → Possible duplicate companies; "opportunity" survives in the company Deals tab, delete messages and the
deal page; "Investor report" tag vs "Tenant pipeline report"; buttons say New company / + New tenant deal / New source /
Add one / Add project; seven titles lack " · ACMI CRM"; the dashboard tab reads "ACMI CRM".

**Roles** (R1): Manager-only today is sending, outreach settings and templates, delete and merge, site delete, share
links, transcripts; everything else, including costs and win % on the report, is the same for everyone; all six
accounts are Managers. Proposed: keep the one permission; a per-person Focus (Tenants, Land, Capital, Everything)
chosen on first visit that sets the starting dashboard, menu order and default views; costs and win % hidden by
default for non-Managers; a Manager badge in the account menu.

**Outside-facing** (O1): logo and confidentiality line present on the pipeline report, capital print, campus report
and guest pages; guest pages hide internal notes; Owner off by default. Leaks: report defaults show cost, $ PSF and
win %; the site brief PDF prints the site's notes ("Source & broker notes") unless hidden, and notes are where the
team writes "owner is motivated". Proposed: an "Outside copy" switch on every Print / PDF / Excel / share that drops
cost, win %, owner and notes, stamps the logo and previews; split "Internal notes" from "Notes for the brief".

**Rough edges**: X1 recently opened records live in one browser → UserPrefs; X2 Companies has scope chips plus the
views bar's "All" and a ten-field filter panel → built-in views and a Filters button, same on People; X3 map (own
saved views, ten-button toolbar, filter panel first on a phone) and contract (raw form beside the timeline) not
reworked; X4 bugs: calendar legend renders as a page-wide bar; Mail review suggestion text overlaps the next column and
suggested names are domain fragments; the report scrolls sideways at 1440 px; Capital shows the print logo on screen;
no favicon; deal breadcrumb repeats the company name; the welcome tip shows through the open ☰ menu on a phone.

### Decisions (desktop session's eight questions; recommended default first)

1. Timings: done on scratch data at live scale; a pass on a backup copy needs the laptop.
2. Capture: auto-link to known records; Needs-filing inbox with one-click Accept; auto-create directory firms at high confidence, and at medium when a meeting exists; never a Company; read forwarded invites. Alt: suggest only.
3. Granola: desktop-app scheduled task on the laptop, weekday mornings, last 3 days, first run 30 days; shared tasks. Alt: Granola Business API, or a cloud routine writing to the shared drive.
4. Duplicates and undo: attendee-based meeting merge (Granola wins); nightly firm and person checks; 30-day Trash and reversible merges; live pairs one by one with Kellen's OK. Alt: dialogs only.
5. Roles: one permission plus a per-person Focus; costs and win % hidden for non-Managers by default. Alt: three groups.
6. Branches: one per area in order (capture + Granola; duplicates + undo; accessibility + phone + states + wording; roles + outside + edges), each through CI. Alt: one branch.
7. Report form: this section plus `docs/ux-review-2/` and the web page.
8. Help: Help page plus a five-step tour saved per person. Alt: Help page only.

### Not covered
The live database and its real timings; the Granola connector (not connected in the cloud session); the map's outside
services (blocked in the container); sending anything; the Windows sync task; screen-reader behaviour beyond axe.
