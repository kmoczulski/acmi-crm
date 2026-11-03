You're joining work on the ACMI Properties CRM, a custom Django app at C:\Users\Kemoc\Desktop\ACMI_CRM
(GitHub: kmoczulski/acmi-crm). Before anything else, read docs/HANDOFF.md ("Pick up here" and "How to work in this
repo"), docs/attio_spec.md and docs/UX_REVIEW.md. UX_REVIEW.md records a first redesign pass that is already merged.
Build on it; don't redo it.

THE ASK
Approach the whole web app as a brand-new member of the ACMI Properties team using it for the first time. You're
lost and don't know how anything works. Review how every page is designed and how every action is done, then make
it the easiest app to get around: jump anywhere fast, find a company we're talking to in seconds, and use colour to
show what matters or what something does. Every page should have customizable views, the dashboard most of all
(resize the calendar, add and remove sections). Hold it to the standard of an expert design and development team
with 20+ years of building custom software for real estate companies.

ALREADY DONE AND DECIDED (keep these)
- CoStar-style navy top bar with drop-down menus (no left sidebar), Ctrl K / "/" search, a "+ New" menu, and a big
  search box at the top of the dashboard.
- "Tenant pipeline" is one page with Board / Table / Report tabs. The report has a Columns picker that applies to the
  screen, the print/PDF and the Excel export, which follows the Fortress pipeline tracker layout.
- Dashboard widgets: drag, resize, hide, add, reset. Saved per person on the server (UserPrefs).
- A saved-views bar on every list; Companies opens on "Active".
- Colours for deal stages, site statuses and due dates.
- Decided, don't reopen: the pro forma stays in Excel; investor updates are skipped for now; contracts emulate
  Notessa.

LOOK HARDEST AT WHAT THE FIRST PASS DIDN'T COVER
1. Capture gaps, the most important. On Oct 21 two lender calls for The Forge (Shore United Bank, B&H Bank) were
   invisible because neither bank was in the CRM: unknown email domains sit in Mail review until someone files them,
   and a forwarded invite never reaches the synced calendar. Design how new lenders, brokers, tenants and consultants
   get recognized and linked to the right project without manual filing. For example: suggest "looks like a bank:
   file as a lender on <project>?" from the domain or signature; link meetings by attendee domain and by title
   keywords; nudge when a meeting has outside attendees but no linked record.
2. Granola. There's still no daily import: meetings, notes and action items reach the CRM only when someone runs it.
   Propose the scheduled task (see "Transcript archive" in HANDOFF.md) and how action items become tasks, including
   one set of tasks shared by several meetings.
3. Duplicate records: the same call shows twice when the other side sends its own invite under a different title.
   Also companies vs directory firms, and people with two emails.
4. Accessibility: keyboard-only use of menus, palette and drag-and-drop widgets; focus outlines; colour contrast;
   screen-reader labels; colour never the only signal.
5. Speed with real data (about 1,800 companies, 600 MB database): slow pages, N+1 queries, search ranking, pagination.
6. Phone and tablet use in the field: site visits, the map, quick notes after a meeting.
7. Forms and editing: inline edit vs dialogs, required fields, unsaved-change warnings, undo after delete or merge,
   clear confirmations before anything is sent outside.
8. Empty, loading and error states on every page, and plain-language help or a short first-week tour.
9. Wording: one name for each thing everywhere (menus, page titles, buttons, emails, reports).
10. Roles: what a Manager sees vs everyone else, and per-role starting dashboards (land, tenants, capital).
11. Outside-facing output: share links, guest pages, PDFs and Excel. ACMI logo on every report; nothing internal
    (cost, win %, owners, notes) leaks unless chosen.
12. Remaining rough edges: recently opened records live only in the browser, not per person; Companies has both
    scope chips and the views bar's "All"; the map and contract pages weren't reworked.

HOW TO WORK (from HANDOFF.md; these protect live data)
- The real database (db.sqlite3) is live: a Windows task syncs mail into it every 15 minutes using the main folder's
  code. Query it read-only, and report counts, not contents. Never print .env values.
- Before you change anything, give me the access you need (have / need / off-limits) and a short Q&A on scope as
  structured questions with a recommended default. Then wait for my go.
- Report findings first, as a prioritized list with screenshots. We choose fixes together.
- Code changes go on a branch in .claude/worktrees/<name>, never in the main folder. Commit as Kellen Moczulski
  <kmoczulski@acmigroup.com>, push the branch, wait for CI (gh run watch), then fast-forward main.
- Before any migration or data write on the real database, back it up with SQLite's backup API (command in
  HANDOFF.md) and get my OK.
- Test in a browser against a scratch database with made-up data, never the real one. If you use an auto-login
  middleware for that, form posts may fail CSRF checks; that's the test setup, not the app.
- No outbound email, posts or calendar changes without my OK. Keep tool installs out of .venv.
- At the end, update docs/HANDOFF.md and docs/UX_REVIEW.md with what changed and what's left.
