"""Mail, then Google Calendar for everyone who connected it: what scripts/sync_mail.cmd runs on the laptop, and what
the Cloud Run sync job runs every 15 minutes in production (Cloud Scheduler). A calendar problem never fails the mail
part."""
from django.core.management.base import BaseCommand

from crm import gcal
from crm.mail import sync


class Command(BaseCommand):
    help = "Sync every enabled mailbox, then every connected Google Calendar."

    def handle(self, *args, **opts):
        for mailbox, stats in sync.sync_all().items():
            self.stdout.write(f"{mailbox}: " + ", ".join(f"{k} {v}" for k, v in dict(stats).items()))
        try:
            gcal.sync_all(stdout=self.stdout)
        except Exception as exc:  # noqa: BLE001 - logged, and the mail sync above already counted
            self.stderr.write(f"calendar sync failed: {exc}")
