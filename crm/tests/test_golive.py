"""Production plumbing: Cloud Run jobs for send batches and the sync, Cloud Storage for files."""
import importlib.util
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from crm import cloudrun
from crm.models import SendBatch


class FakeResp:
    def __init__(self, text="", data=None):
        self.text, self._data = text, data or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class CloudRunJobTests(TestCase):
    @override_settings(GCP_PROJECT="acmi-prod", GCP_REGION="us-central1")
    def test_run_job_posts_the_override_with_the_service_token(self):
        with mock.patch("crm.cloudrun.requests.get", return_value=FakeResp(data={"access_token": "tok"})), \
                mock.patch("crm.cloudrun.requests.post", return_value=FakeResp(data={"name": "op/1"})) as post:
            self.assertEqual(cloudrun.run_job("acmi-crm-send", ["manage.py", "send_batch", "7"]), "op/1")
        url = post.call_args[0][0]
        self.assertEqual(url, "https://run.googleapis.com/v2/projects/acmi-prod/locations/us-central1/jobs/acmi-crm-send:run")
        self.assertEqual(post.call_args[1]["json"], {"overrides": {"containerOverrides": [
            {"args": ["manage.py", "send_batch", "7"]}]}})
        self.assertEqual(post.call_args[1]["headers"], {"Authorization": "Bearer tok"})

    def test_metadata_failure_is_a_job_error(self):
        import requests

        with mock.patch("crm.cloudrun.requests.get", side_effect=requests.ConnectionError("no metadata server")):
            with self.assertRaises(cloudrun.JobError):
                cloudrun.run_job("acmi-crm-send", [])

    @override_settings(SEND_JOB="acmi-crm-send")
    def test_send_uses_the_job_in_production_and_a_failed_start_releases_the_batch(self):
        from crm.outreach.sender import start_in_background

        user = get_user_model().objects.create(username="kellen")
        batch = SendBatch.objects.create(created_by=user, campaign="Fall")
        with mock.patch("crm.cloudrun.run_job") as run, mock.patch("subprocess.Popen") as popen:
            start_in_background(batch)
        run.assert_called_once_with("acmi-crm-send", ["manage.py", "send_batch", str(batch.pk)])
        popen.assert_not_called()


class SendPageJobTests(TestCase):
    from crm.tests.test_outreach import OutreachTests

    setUp = OutreachTests.setUp  # a mailbox, templates and a queued prospect

    @override_settings(SEND_JOB="acmi-crm-send")
    def test_a_job_that_wont_start_fails_the_batch_instead_of_blocking_sends(self):
        self.client.force_login(self.user)
        with mock.patch("crm.cloudrun.run_job", side_effect=cloudrun.JobError("permission denied")):
            self.client.post("/outreach/start/", {"confirm": "SEND"})
        batch = SendBatch.objects.get()
        self.assertEqual(batch.status, SendBatch.Status.FAILED)
        self.assertIn("permission denied", batch.error)


class SyncAllCommandTests(TestCase):
    def test_mail_then_calendar_and_a_calendar_failure_doesnt_stop_it(self):
        out, err = StringIO(), StringIO()
        with mock.patch("crm.mail.sync.sync_all", return_value={"k@acmigroup.com": {"new": 3}}) as mail, \
                mock.patch("crm.gcal.sync_all", side_effect=RuntimeError("token revoked")) as cal:
            call_command("sync_all", stdout=out, stderr=err)
        mail.assert_called_once()
        cal.assert_called_once()
        self.assertIn("k@acmigroup.com: new 3", out.getvalue())
        self.assertIn("token revoked", err.getvalue())


class StorageBackendTests(TestCase):
    def test_cloud_storage_backend_imports_when_installed(self):
        # Only where requirements.txt is installed (CI, the Docker image): catches a version clash before deploy.
        if importlib.util.find_spec("storages") is None:
            self.skipTest("django-storages not installed in this environment")
        from storages.backends.gcloud import GoogleCloudStorage  # noqa: F401
