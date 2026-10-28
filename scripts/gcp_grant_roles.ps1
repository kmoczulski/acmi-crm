# One-time: gives the Cloud Run / Cloud Build service account the roles cloudbuild.yaml needs.
# Run by Kellen (project Owner) in PowerShell after `gcloud auth login`.
$P = "sincere-passage-511621-j3"
$SA = "46824423159-compute@developer.gserviceaccount.com"
foreach ($r in "secretmanager.secretAccessor", "cloudsql.client", "run.admin", "iam.serviceAccountUser",
           "artifactregistry.writer", "cloudbuild.builds.builder", "logging.logWriter") {
    gcloud projects add-iam-policy-binding $P --member="serviceAccount:$SA" --role="roles/$r" --condition=None --quiet | Out-Null
    "granted $r"
}
gcloud storage buckets add-iam-policy-binding "gs://acmi-uploads-$P" --member="serviceAccount:$SA" --role="roles/storage.objectAdmin" | Out-Null
"granted storage.objectAdmin on the uploads bucket"
