# ACMI CRM

Django app replacing the outreach workbook, send scripts, news review app and
Trello hand-offs. Design: `docs/attio_spec.md`. Status and decisions:
`docs/HANDOFF.md`.

## Run it on the laptop

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py import_workbook            # dry run, prints counts
.venv/Scripts/python manage.py import_workbook --commit   # writes
.venv/Scripts/python manage.py changepassword kellen      # local login password
.venv/Scripts/python manage.py runserver
```

Then open http://localhost:8000.

The importer reads `Desktop\Prospecting\ACMI_Outreach_Engine.xlsx` (override with
`--path`) and is safe to re-run. It matches on email, normalized company name,
template id and a stable id per log row, and only fills blank fields unless
`--overwrite` is passed.

## Gmail sync

1. Copy `.env.example` to `.env` and set `ACMI_IMAP_PASSWORD_<USERNAME>` to that
   person's Gmail app password. Restart the server.
2. On the **Mail** page, enter the Gmail address and pick how much history to read.
3. Click **Sync now**, or run the first (large) sync from the terminal:
   `.venv/Scripts/python manage.py sync_mail --user kellen --all-backlog`
4. After that, each `sync_mail` run reads only new mail. In production a scheduler
   POSTs to `/internal/sync-mail/` with header `X-Cron-Secret: $ACMI_CRON_SECRET`.

Emails with outside contacts land on the matching person's and company's timeline.
Unknown domains wait in the review queue on the Mail page. Mail between ACMI
addresses, newsletters and protected addresses is never logged.

## Tests

`.venv/Scripts/python manage.py test crm`

## Layout

| Path | What |
|---|---|
| `crm/models.py` | Company, Person, Opportunity, Campus, Activity, Task, Note, List, ListEntry, SavedView, FieldHistory, EmailTemplate, NewsDraft |
| `crm/interactions.py` | Computed interaction fields (last touch, touches, last response) derived from `Activity` |
| `crm/tables.py` | Filters, sorting and columns for the Companies and People tables (state lives in the query string) |
| `crm/management/commands/import_workbook.py` | Workbook importer |
| `crm/auth.py` | Google sign-in restricted to acmigroup.com; the data room's partner sign-in |
| `crm/tokens.py` | Google tokens encrypted at rest with `ACMI_TOKEN_KEY` |
| `crm/mail/` | Gmail sync (IMAP or the Gmail API), review queue, and Mail in the hub: inbox, reading pane, compose, filing to records, CRM labels, shared mailboxes (`docs/EMAIL_CLIENT.md`) |
| `crm/boards/` | Trello-style boards on the CRM's records; record boards bound to a status; feed lists |
| `crm/directory/` | The directory: lenders, consultants, vendors (never Companies); Claude sorting new email domains |
| `crm/capital/` | Capital stack per project, term sheets read by Claude, portfolio page |
| `crm/contracts/` | Contracts modeled on Notessa: documents, redlines, dates and the closing timeline |
| `crm/dataroom/` | Capital partners' data room: partner accounts, deal switches, approved files, view log (`docs/DATA_ROOM.md`) |
| `crm/push.py` | Browser notifications (Web Push) and each person's notification choices |
| `crm/news/` | Weekly news review (scan parser, Gmail draft matching/editing, apply) |
| `crm/outreach/` | Send queue, templates, background sending (`send_batch`), weekly report |
| `crm/projects.py` | Tags correspondence as project (MEIH, NSIH, sites) or prospecting |
| `crm/merge.py` | Duplicate-company suggestions and merging |
| `crm/ai.py` | AI summaries (needs `ANTHROPIC_API_KEY`) |
| `scripts/` | Windows scheduled-task launcher for the 15-minute mail sync |

## Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | dev key | Required in production |
| `DJANGO_DEBUG` | `true` | |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | unset → SQLite | Cloud SQL |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | unset → no Google button | OAuth client from the Google Cloud project |
| `ACMI_ALLOWED_EMAIL_DOMAINS` | `acmigroup.com` | Who may sign in with Google |
| `ACMI_LOCAL_LOGIN` | `true` | Username/password sign-in (sign-in page and `/admin/login/`); `false` in production makes Google the only way in |
| `ACMI_SQLITE_PATH` | `db.sqlite3` | Point at a copy for testing |
| `ACMI_IMAP_PASSWORD_<USERNAME>` | unset | Gmail app password per mailbox |
| `ACMI_INTERNAL_DOMAINS` | `acmigroup.com,acmiproperties.com` | Never logged |
| `ACMI_CRON_SECRET` | unset | Protects `/internal/sync-mail/` |
| `DJANGO_HSTS_SECONDS`, `DJANGO_HSTS_INCLUDE_SUBDOMAINS`, `DJANGO_SSL_REDIRECT` | 1 year, on, on | HTTPS rules when `DJANGO_DEBUG=false` (`DJANGO_SECRET_KEY` must then be 50+ characters) |
| `ACMI_CSP` | scripts from this site and unpkg | Content-Security-Policy header |
| `ACMI_LOG_LEVEL` | `WARNING` | Console logging level for the app (errors always print) |
| `ACMI_CHROME`, `ACMI_CHROME_NO_SANDBOX` | found on PATH; off | Chrome / Edge for PDF briefs; `1` in containers (set in the Dockerfile) |
| `ACMI_TRANSCRIPT_DIR` | `data/transcripts` | Where meeting transcripts are written as `.txt` (one folder per campus); use a restricted shared-drive folder |
| `ACMI_SEND_JOB`, `ACMI_GCP_PROJECT`, `ACMI_GCP_REGION` | unset | Production: the Cloud Run job that sends outreach batches (project / region default to the metadata server) |
| `ACMI_GCS_BUCKET` | unset | Production: Cloud Storage bucket for uploads and outreach attachments |
