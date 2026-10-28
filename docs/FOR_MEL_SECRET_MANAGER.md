# For Mel: Secret Manager for the ACMI CRM

The hosted CRM (Cloud Run, set up by `cloudbuild.yaml`) reads its keys from Google Cloud Secret Manager. Nothing is
set up yet: on 2026-10-28 neither ACMI project had billing or Secret Manager switched on.

## 1. Pick the project
There are two projects named "ACMI Properties Web App":
- `sincere-passage-511621-j3`: already has the CRM's Google sign-in (OAuth client), Gmail and Calendar APIs.
  **Recommended.**
- `acmi-properties-web-app`: nothing CRM-related switched on.

## 2. In that project
1. Turn billing on.
2. Enable the APIs: Secret Manager, Cloud Run, Cloud Build, Artifact Registry, Cloud SQL Admin, Google Drive.
3. Give Kellen (`kmoczulski@acmigroup.com`) the **Secret Manager Admin** role, so the laptop can upload the keys
   below without anyone copying them by hand.

## 3. The secrets `cloudbuild.yaml` expects
| Secret name | Who fills it |
|---|---|
| `acmi-token-key` | **Kellen's laptop** (must be the laptop's exact key, or the copied database's Google tokens can't be read) |
| `acmi-vapid-public-key`, `acmi-vapid-private-key` | **Kellen's laptop** (the same pair, or every browser has to subscribe to notifications again) |
| `acmi-django-secret-key` | New: 50+ random characters |
| `acmi-postgres-password` | New: the Cloud SQL user's password |
| `acmi-cron-secret` | New: random string |
| `acmi-google-client-secret` | The OAuth client's secret (Google Cloud console, Credentials) |
| `acmi-anthropic-api-key` | The Anthropic API key |
| `acmi-imap-password-kellen` | Only if the IMAP fallback is kept; ask Kellen |

The runtime service account Cloud Run uses needs **Secret Manager Secret Accessor** on these.

Once step 2 is done, tell Kellen; Claude on the laptop uploads the first three from `.env` and checks them.
