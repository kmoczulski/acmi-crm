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
