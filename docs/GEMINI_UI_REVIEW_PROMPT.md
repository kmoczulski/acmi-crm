You are reviewing the user interface of the ACMI Properties CRM, a Django 5.2 app at
C:\Users\Kemoc\Desktop\ACMI_CRM, for bugs and improvements. ACMI is a small real estate developer (defense
industrial campuses); the CRM covers companies, people, tenant deals, capital, land search, mail, meetings,
contracts, a data room and Trello-style boards.

READ FIRST
docs/HANDOFF.md, docs/UX_REVIEW.md (a redesign pass that's already merged; build on it, don't redo it) and
docs/UX_REVIEW_PROMPT.md (the decisions already made: navy top bar, Ctrl K search, dashboard widgets, saved
views, the pro forma stays in Excel). Screenshots from the last review are in docs/ux-review-2/.

RUN IT AGAINST A COPY, NEVER THE LIVE DATABASE
db.sqlite3 is live (mail syncs into it every 15 minutes). Copy it and point the app at the copy:
  copy db.sqlite3 %TEMP%\acmi_ui_review.sqlite3
  set ACMI_SQLITE_PATH=%TEMP%\acmi_ui_review.sqlite3
  .venv\Scripts\python manage.py runserver 8010
Never print .env values. Don't send email, outreach, calendar invites or push notifications, even from the copy.
Don't change code: report only.

WHAT TO DO
1. Build the page list from crm/urls.py (and the app URL files it includes), not from memory. Visit every page
   at desktop width (1440) and phone width (390).
2. On each page try the main actions: create, edit, save, delete or cancel, filter, sort, search, saved views,
   drag and resize, open in split view, keyboard only (Tab, Enter, Esc, Ctrl K).
3. Watch the browser console and network tab for errors, 404s and 500s.
4. Look for:
   - Bugs: broken links and buttons, forms that lose input, wrong counts, layout overflow or overlap, things
     that don't save, double submits, stale data after an action, JS errors.
   - Usability: unclear wording, the same thing named differently on different pages, missing empty / loading /
     error states, no confirmation before anything leaves the building, no undo after delete or merge.
   - Accessibility: visible focus, keyboard reach for menus and drag-and-drop, colour contrast, labels,
     colour never the only signal.
   - Phone: tap targets, the map, quick notes after a meeting, tables that scroll sideways.
   - Speed: pages that feel slow with real data (about 1,800 companies). Note the page and how long it took.

DON'T MAKE THINGS UP
A previous Gemini audit of this repo cited files, functions and keys that don't exist. So:
- Report only what you saw. Every bug needs the URL, the steps, expected vs actual, and a screenshot or the
  exact console / network error. If you trace it to code, quote the real lines with file:line.
- If you couldn't reproduce something, or didn't visit a page, say so. Don't guess.
- Keep "bug" (it's broken, with proof) separate from "improvement" (it works but could be better).

OUTPUT
1. Bugs, worst first: severity | page URL | one line | screenshot file | file:line if found.
2. Improvements, grouped by theme, each with the page it applies to and what you'd change. Mark quick wins.
3. Pages visited and pages not reached.
