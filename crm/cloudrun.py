"""Start a Cloud Run job from the web app (production): long work runs as a job, not in a page request.

Cloud Run only gives a service CPU while it answers a request, so a send batch started as a background process would
stall or be killed after the page returns. With ACMI_SEND_JOB set, pressing Send runs that job instead, with the
batch id as its argument (cloudbuild.yaml deploys the jobs). Credentials come from the service's own account via the
metadata server, so nothing extra is installed; that account needs permission to run the job with overrides
(roles/run.developer on the job).
"""
import requests
from django.conf import settings

METADATA = "http://metadata.google.internal/computeMetadata/v1"
HEADERS = {"Metadata-Flavor": "Google"}


class JobError(RuntimeError):
    pass


def _metadata(path):
    resp = requests.get(f"{METADATA}/{path}", headers=HEADERS, timeout=5)
    resp.raise_for_status()
    return resp


def _project():
    return settings.GCP_PROJECT or _metadata("project/project-id").text


def _region():
    if settings.GCP_REGION:
        return settings.GCP_REGION
    return _metadata("instance/region").text.rsplit("/", 1)[-1]  # "projects/123/regions/us-central1"


def run_job(job, args):
    """Execute a Cloud Run job once with these container args. Returns the operation name."""
    try:
        token = _metadata("instance/service-accounts/default/token").json()["access_token"]
        url = f"https://run.googleapis.com/v2/projects/{_project()}/locations/{_region()}/jobs/{job}:run"
        resp = requests.post(url, json={"overrides": {"containerOverrides": [{"args": list(args)}]}},
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
        resp.raise_for_status()
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise JobError(f"Could not start the Cloud Run job {job}: {exc}") from exc
    return resp.json().get("name", "")
