# ACMI Hub: hosting on Google Cloud, step by step

Written 2026-10-28 for the working session with Mel. Sources: `docs/HANDOFF.md`, `docs/ACMI_CRM_Hosting_Budget.docx`,
`cloudbuild.yaml`, `Dockerfile`, `config/settings.py`, `.env` (key names only). Nothing in this guide has been run.

**Decision (2026-10-28):** put everything in the cloud first, then audit the hosted copy and fix what turns up. The
laptop stops being the working copy.

## 0. The picture

| Piece | Google service | Name used in this guide |
|---|---|---|
| The web app | Cloud Run service | `acmi-crm` |
| Database | Cloud SQL, Postgres 16 (CI tests on 16) | instance `acmi-db`, database `acmi`, user `acmi` |
| Uploads and attachments | Cloud Storage bucket | `acmi-uploads-PROJECT_ID` |
| Keys and passwords | Secret Manager | `acmi-...` (list in step 11) |
| Built app images | Artifact Registry | repository `acmi` (the name `cloudbuild.yaml` expects) |
| Deploys | Cloud Build trigger on GitHub `main` | runs `cloudbuild.yaml` |
| Background jobs | Cloud Run jobs | `acmi-crm-migrate`, `acmi-crm-send`, `acmi-crm-sync` |
| 15-minute clock | Cloud Scheduler | `acmi-crm-sync` |
| Address | Cloud Run domain mapping | `hub.acmigroup.com` |

Region everywhere: `us-central1`. Expected cost: about $30 to $45 a month plus Anthropic usage billed separately.

Placeholders to replace: `PROJECT_ID` (the Google Cloud project ID), `PROJECT_NUMBER` (its number, shown on the
console home page), `RUNTIME_SA` (the Cloud Run runtime service account; with no `--service-account` flag in
`cloudbuild.yaml` that is the default one, `PROJECT_NUMBER-compute@developer.gserviceaccount.com`).

## 1. Who does what

**Mel (Google Workspace and Cloud admin):** approve the spend, create the billing account, add the DNS record, approve
organization policies, share the Drive folder. Needs: Workspace super admin and Billing Account Administrator.

**Kellen:** run the commands, own the GitHub repo, move the data.

## 2. Bring to the meeting

- A company card for billing.
- Login to wherever acmigroup.com DNS is managed (registrar or DNS host). Without it, step 15 cannot finish.
- GitHub login with admin rights on the repo (to connect Cloud Build).
- The laptop, with `.env` open (never paste values into chat or email; copy them straight into Secret Manager).
- This guide and `docs/ACMI_CRM_Hosting_Budget.docx` (its database size is out of date: the file is 1.1 GB, not 264 MB).
- `gcloud` and `gh` installed and signed in (`gcloud auth login`, `gh auth status`). HANDOFF says both were signed in on 2026-10-14.

## 3. Part A: approvals and billing (Mel, about 15 minutes)

1. **Approve the budget.** About $30 to $45 a month plus a few dollars of Anthropic usage.
2. **Create the billing account.** Console > Billing > Create account > organization acmigroup.com > add the card.
3. **Link it to the CRM project.** Console > Billing > Account management > link the project. Check the project shows
   a billing account at the top of its dashboard.
4. **Budget alert.** Billing > Budgets and alerts > create a $50 monthly budget, email alerts at 50%, 90% and 100%.
5. **Anthropic spending limit.** In the Anthropic console, set a monthly limit on the API key the hub will use.

## 4. Part B: set up Google Cloud (Kellen runs, Mel watches)

Run these in PowerShell or Cloud Shell. Set a few shortcuts first (PowerShell syntax):

```powershell
$PROJECT_ID = "PROJECT_ID"
$REGION = "us-central1"
gcloud config set project $PROJECT_ID
```

**Step 6. Enable the services:**
```powershell
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com drive.googleapis.com
```
`drive.googleapis.com` is for reading the shared-drive folders (news scans, tenancy documents).

**Step 7. Image registry:**
```powershell
gcloud artifacts repositories create acmi --repository-format=docker --location=$REGION
```

**Step 8. Database.** Smallest shared-core size, 10 GB disk, automatic backups and point-in-time recovery:
```powershell
gcloud sql instances create acmi-db --database-version=POSTGRES_16 --tier=db-g1-small --region=$REGION --storage-size=10 --storage-auto-increase --backup-start-time=08:00 --enable-point-in-time-recovery
gcloud sql databases create acmi --instance=acmi-db
```
Create the database user with a long random password (generate it, do not invent one). Keep the password for step 11:
```powershell
$DBPW = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
gcloud sql users create acmi --instance=acmi-db --password=$DBPW
```
The instance connection name, needed later, is `PROJECT_ID:us-central1:acmi-db`.

**Step 9. Uploads bucket** (without it, uploads and outreach attachments live on the container's disk and vanish on each deploy):
```powershell
gcloud storage buckets create gs://acmi-uploads-$PROJECT_ID --location=$REGION --uniform-bucket-level-access
```

**Step 10. Find the service accounts.** Console > IAM. Note (a) the Cloud Run runtime account, normally
`PROJECT_NUMBER-compute@developer.gserviceaccount.com`, and (b) the account the Cloud Build trigger will run as (shown
on the trigger page in step 13; on new projects it is the same compute account).

**Step 11. Secrets.** Create each in Secret Manager with `gcloud secrets create NAME --data-file=-` or in the
console. Names must match `cloudbuild.yaml` exactly.

| Secret | Value | Where it comes from |
|---|---|---|
| `acmi-django-secret-key` | 50+ random characters | generate new, never reuse the laptop's |
| `acmi-postgres-password` | the password from step 8 | `$DBPW` |
| `acmi-cron-secret` | long random string | generate new |
| `acmi-google-client-secret` | OAuth client secret | `.env` `GOOGLE_CLIENT_SECRET` |
| `acmi-anthropic-api-key` | Anthropic key | `.env` `ANTHROPIC_API_KEY` (or a new key) |
| `acmi-token-key` | token encryption key | `.env` `ACMI_TOKEN_KEY` (**must be the same key**, or stored Google tokens cannot be read) |
| `acmi-vapid-public`, `acmi-vapid-private` | push keys | `.env` `ACMI_VAPID_PUBLIC_KEY`, `ACMI_VAPID_PRIVATE_KEY` |
| `acmi-imap-password-kellen` | see note | `cloudbuild.yaml` still references it, but HANDOFF says the Gmail app password was removed 2026-10-26 |

The last three rows need the `cloudbuild.yaml` fixes in section 9 before they do anything. For the IMAP secret: either
remove the reference from `cloudbuild.yaml` (preferred, if mail now uses Gmail sign-in tokens) or create the secret with
a placeholder value. A deploy fails if a referenced secret does not exist.

**Step 12. Permissions.** Console > IAM, or with `gcloud projects add-iam-policy-binding`:

| Account | Roles |
|---|---|
| Cloud Build trigger account | Cloud Run Admin, Service Account User, Artifact Registry Writer, Cloud SQL Client |
| Runtime service account | Secret Manager Secret Accessor, Cloud SQL Client, Storage Object Admin on the bucket, Cloud Run Developer on the `acmi-crm-send` job (so Send can start it), Cloud Run Invoker (so Cloud Scheduler can start the sync job) |

Roles on the send job can only be set after the first deploy creates it. Come back to that one.

## 5. Part C: sign-in (Mel)

**Step 13a. OAuth client.** Console > APIs and Services > Credentials > the existing OAuth client:
- Authorized redirect URIs: add `https://hub.acmigroup.com/accounts/google/login/callback/`. Add the `run.app`
  address too after the first deploy (step 14), so sign-in can be tested before DNS is ready.
- OAuth consent screen: user type **Internal** (acmigroup.com only).
- Confirm the Gmail and Calendar scopes the hub requests are listed.

## 6. Part D: connect GitHub and deploy

**Step 13b. Fix `cloudbuild.yaml` first** (section 9). Commit and push to `main` before creating the trigger, or the
first deploy will be missing keys.

**Step 13c. Cloud Build trigger.** Console > Cloud Build > Triggers > Connect repository (GitHub, the private repo) >
Create trigger: event "push to a branch", branch `^main$`, config file `cloudbuild.yaml`. Substitution variables:
- `_SQL_INSTANCE` = `PROJECT_ID:us-central1:acmi-db`
- `_GOOGLE_CLIENT_ID` = the client ID (not secret)
- `_BUCKET` = `acmi-uploads-PROJECT_ID`
- `_HOSTS` and `_ORIGINS` can list several values separated by commas, for example
  `hub.acmigroup.com,acmi-crm-xxxx-uc.a.run.app` and `https://hub.acmigroup.com,https://acmi-crm-xxxx-uc.a.run.app`
  while DNS is being set up.

**Step 14. First deploy.** Run the trigger by hand. What happens, in order: build the image (Dockerfile installs
Chromium for PDF briefs, then collects static files) > push > run migrations as a job against the empty database >
deploy the send and sync jobs > deploy the web service. If the migrate step fails, the build stops and nothing new is
served. Watch the build log in the console.

**Step 14b. Let browsers reach the service.** `cloudbuild.yaml` does not pass `--allow-unauthenticated`, so the first
deploy creates a service that answers 403 to everyone. Allow public access (the app itself restricts sign-in to
acmigroup.com Google accounts):
```powershell
gcloud run services add-iam-policy-binding acmi-crm --region=$REGION --member=allUsers --role=roles/run.invoker
```
If Mel's organization has "Domain restricted sharing" turned on, this command is refused. Mel then has to allow it for
this project, or the service must sit behind a load balancer. Decide at the meeting.

**Step 14c. Smoke test on the run.app address** (find it with `gcloud run services describe acmi-crm --region=$REGION --format="value(status.url)"`):
sign-in page loads, no certificate warning, sign-in with an acmigroup.com account works.

## 7. Part E: address and clock

**Step 15. Domain.**
```powershell
gcloud beta run domain-mappings create --service acmi-crm --domain hub.acmigroup.com --region=$REGION
gcloud beta run domain-mappings describe --domain hub.acmigroup.com --region=$REGION
```
The second command prints the DNS record to add (a CNAME to `ghs.googlehosted.com` for a subdomain). Mel adds it at the
DNS host. Google issues the HTTPS certificate on its own, usually 15 to 60 minutes. Domain mapping may require the
domain to be verified in Google Search Console under the acmigroup.com Workspace account first.

**Step 16. Sync schedule** (replaces the Windows task "ACMI CRM mail sync"). Replace `RUNTIME_SA`:
```powershell
gcloud scheduler jobs create http acmi-crm-sync --location=$REGION --schedule="*/15 * * * *" --uri="https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/acmi-crm-sync:run" --http-method=POST --oauth-service-account-email=RUNTIME_SA
```
Do not turn it on for real until step 20 (the data is loaded). Pause it right after creating if needed:
`gcloud scheduler jobs pause acmi-crm-sync --location=$REGION`.

## 8. Part F: move the data, then go live

The laptop database is `db.sqlite3`, 1.1 GB. The cloud database after step 14 has the right tables and no data.

**Step 17. Freeze and back up.**
1. Stop the Windows task "ACMI CRM mail sync" so nothing writes to the laptop database during the move.
2. Copy `db.sqlite3` into `backups/` with today's date and keep it.
3. Rebuild nothing on the laptop from this point; it is read-only until go-live is confirmed.

**Step 18. Rehearse on a scratch copy first.** Create a throwaway Cloud SQL database (`acmi_rehearsal`), load into it,
check counts, then delete it. Two routes; pick one in the rehearsal:
- **Django route:** `manage.py dumpdata --natural-foreign --natural-primary -e contenttypes -e auth.permission` to
  JSON, then `loaddata` through the Cloud SQL Auth Proxy. Simple, but JSON for 1.1 GB is large and slow, and may need
  to be done in chunks per app. `loaddata` resets Postgres sequences itself.
- **pgloader route:** copy rows straight from SQLite to Postgres (run it in WSL or Cloud Shell, not plain Windows).
  Faster, but sequences must be reset afterwards (`manage.py sqlsequencereset <apps>`).
I have not tested either route on your data. Treat the first run as a rehearsal.

**Step 19. The real load.** Same steps as the rehearsal, against database `acmi`. Compare table row counts between the
laptop and Cloud SQL (companies, people, deals, emails, tasks, meetings, documents, parcels). Counts only, not
contents.

**Step 20. Files.**
- Copy `media/` (32 MB) into the bucket: `gcloud storage cp -r media/* gs://acmi-uploads-$PROJECT_ID/`
- Land data (`data/land`, 55 MB for `data/`): check whether the parcels came across inside the database; if the land
  search reads any file from `data/`, ship that too.
- News scans: upload manually to the restricted shared-drive folder (needs the code change in section 9, item 5).
- Granola transcripts: pick a shared-drive location (HANDOFF "waiting on Kellen").

**Step 21. Go live.**
1. Resume the scheduler: `gcloud scheduler jobs resume acmi-crm-sync --location=$REGION`
2. Run the sync job once by hand and read its log: `gcloud run jobs execute acmi-crm-sync --region=$REGION --wait`
3. Leave the Windows task disabled. Two syncs at once would file mail twice.
4. Tell the team the address. iPhones: open `https://hub.acmigroup.com` in Safari > Share > Add to Home Screen (needed
   for push notifications; Apple rule).

## 9. Fix these before the first deploy (not fixed yet)

All in `cloudbuild.yaml` unless noted.

1. Pass `ACMI_TOKEN_KEY` (secret `acmi-token-key`) to the web service and the sync job.
2. Add `ANTHROPIC_API_KEY` to the sync job (Claude domain sorting runs inside `sync_all`).
3. Pass `ACMI_VAPID_PUBLIC_KEY` and `ACMI_VAPID_PRIVATE_KEY` to the web service and the sync job.
4. Check whether the send and migrate jobs need `GOOGLE_CLIENT_SECRET` and `ACMI_TOKEN_KEY` (outreach sending).
5. Set `ACMI_GCS_BUCKET` on the sync job if sync stores attachments.
6. Remove or replace the `acmi-imap-password-kellen` references if the app password is really retired.
7. Add `--allow-unauthenticated` to the deploy step (or keep the manual step 14b).
8. Set memory on the web service: Cloud Run's default is 512 MiB, and Chromium printing PDF briefs probably needs
   1 to 2 GiB (`--memory=2Gi`). Untested; confirm with a real brief.
9. News watcher (`crm/news/review.py`): add Drive folder support (`ACMI_NEWS_DRIVE_FOLDER`), and set
   `ACMI_DRIVE_ADC=true` on the web service. Share the folder with the runtime service account as Viewer.
10. Correct the database size (1.1 GB) in `docs/ACMI_CRM_Hosting_Budget.docx`.

## 10. Checklist for the first week after launch

- Open the app on a phone and a second computer; sign in as two different team members.
- Check: a company page, a land parcel and PDF brief, the Mail page, send a test outreach batch to yourself,
  upload a document, confirm it is in the bucket.
- Read the Cloud Run logs for errors (Console > Cloud Run > acmi-crm > Logs).
- Check the Cloud SQL backup exists (Console > SQL > acmi-db > Backups).
- After one month, compare the invoice with the budget doc and write the real numbers into it.

## 11. Rolling back

- **Bad deploy:** Console > Cloud Run > acmi-crm > Revisions > send 100% traffic to the previous revision. Images stay
  in Artifact Registry by commit hash.
- **Bad data load:** drop and recreate database `acmi`, rerun the migrate job, repeat steps 18 to 19 (the laptop file
  is untouched).
- **Back to the laptop:** pause the scheduler, re-enable the Windows task.

## 12. Open questions for the meeting

- Is "Domain restricted sharing" on? (affects step 14b)
- Who manages acmigroup.com DNS, and who can add a CNAME today?
- Is the domain already verified in Search Console under the Workspace account?
- Which shared-drive folder holds the news scans and transcripts, and who can see it?
- Dedicated runtime service account, or the default compute one? (Default is simpler; dedicated is tidier.)
