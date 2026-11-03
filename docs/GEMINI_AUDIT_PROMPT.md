You are re-doing a security and code audit of the ACMI Properties CRM, a Django 5.2 app at
C:\Users\Kemoc\Desktop\ACMI_CRM (single app `crm`; templates in `crm/templates/`, JS in `crm/static/`).

WHY THIS IS A RE-DO
Your last report was checked line by line against the code and almost none of it was real:
- No `pickle` anywhere (the Trello import reads JSON). No `sk_live_` or Clearbit key anywhere.
- No raw SQL: there is no `cursor.execute`, `.raw(` or f-string SQL in `crm/`.
- `Person.company` is already `on_delete=models.SET_NULL` (crm/models.py:363), not CASCADE.
- The only `@csrf_exempt` is `cron_sync` in crm/mail/views.py:231, which checks an HMAC secret. Nothing in
  `crm/dataroom/views.py` is exempt.
- These functions don't exist: `fetch_news_article`, `clean_email_html`, `dispatch_queued_items`,
  `import_trello_backup`, `update_company_score`, `view_thread`, `upload_document`, `dashboard_stats`.
- `templates/crm/company_list.html` doesn't exist.
- There's no "team" access model. ACMI is one small firm; everyone signed in sees every deal by design.
  `Opportunity.team` is "everyone working the deal", not a permission boundary. The only role is the
  Managers group (crm/permissions.py).
A finding that names code that isn't there is worse than no finding: it costs us hours to disprove.

RULES (break any of these and the report gets thrown out)
1. Open the file before you write about it. Every finding must quote the exact lines, copied verbatim
   (no paraphrase, no "e.g." code), with the real path and line numbers.
2. For every finding, give a command that proves it, such as
   `grep -n "pattern" crm/path.py` or `python manage.py shell -c "..."`, and say what it prints.
   If you can run commands, run it and paste the real output.
3. Never invent names, keys, paths, line numbers or example payloads. If you didn't see it, don't write it.
4. Show the whole path from input to harm: where untrusted input comes in (URL, form, email, upload, county
   service), each function it passes through (file:line), and where the damage happens. If any step is a
   guess, say so and mark the finding UNVERIFIED.
5. Check whether it's already handled before you report it: middleware (crm/middleware.py), decorators,
   `crm/permissions.py`, form validation, the HTML sanitizer, and the earlier fixes in docs/AUDIT_REPORT.md.
   If a guard exists, either drop the finding or explain exactly how it is bypassed.
6. Rate severity by what an attacker can actually do here (an internal tool behind Google sign-in limited to
   acmigroup.com), not by the bug class.
7. "Found nothing" is a valid result for an area. Don't pad the report to look thorough.
8. List every file you actually opened, so we can see what was and wasn't covered.

CONTEXT
- Read first: docs/HANDOFF.md ("Pick up here", "How to work in this repo"), docs/AUDIT_BRIEF.md (where to
  look hardest), docs/AUDIT_REPORT.md (the October 16 audit and its fixes, commit f764116).
- Since that audit there are about 160 commits and roughly 51,000 new lines. Spend most of your time on code
  added since then: `git diff --stat f764116..HEAD -- crm config`. Big areas: crm/mail/ (email client, shared
  mailboxes, comments), crm/contracts/, crm/boards/ (Trello import), crm/dataroom/ (outside logins),
  crm/capital/, crm/directory/, crm/entitlements/, crm/news/, crm/sheet_import/, push notifications, split
  view, dashboard widgets.
- Outside-facing surfaces to check first: PUBLIC_PREFIXES in crm/middleware.py, `/g/<token>/` guest pages,
  the data room's own login, shared mailbox tokens, push subscriptions, uploaded files, and the PDF export.

GROUND RULES (they protect live data)
- db.sqlite3 is live: a scheduled task syncs mail into it every 15 minutes. Don't open it for writing. If you
  need real data, copy it and point the app at the copy with ACMI_SQLITE_PATH. Report counts, never contents.
- Never print values from .env.
- No calls to Gmail, Google Calendar, the Anthropic API, or anything that sends outreach.
- Don't change code. Report only; we choose fixes together.

OUTPUT
1. Summary table: severity | file:line | one line | VERIFIED or UNVERIFIED.
2. Each finding: the verbatim quote, the input-to-harm path, the proving command and its output, the guards
   you checked and why they don't stop it, and a fix.
3. Areas reviewed with nothing found, and areas you didn't get to.
