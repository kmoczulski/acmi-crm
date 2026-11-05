"""Mail, then Google Calendar for everyone who connected it, then new Granola files from Drive (when
settings.GRANOLA_DRIVE_FOLDER is set), then Claude sorting new email domains, then the team's due reminders (so
browser notifications arrive with the hub closed): what scripts/sync_mail.cmd runs on the laptop, and what the Cloud
Run sync job runs every 15 minutes in production (Cloud Scheduler). A calendar, Granola or sorting problem never
fails the mail part."""
from django.core.management.base import BaseCommand

from crm import gcal
from crm.mail import sync


class Command(BaseCommand):
    help = "Sync every enabled mailbox, then every connected Google Calendar."

    def handle(self, *args, **opts):
        from crm.mail import outbox

        try:  # first: any email whose Undo time ran out while every page was closed
            sent = outbox.flush_due()
            outbox.tidy()
            if sent:
                self.stdout.write(f"outbox: sent {sent} waiting email(s)")
        except Exception as exc:  # noqa: BLE001 - never blocks the sync
            self.stderr.write(f"outbox failed: {exc}")
        for mailbox, stats in sync.sync_all().items():
            self.stdout.write(f"{mailbox}: " + ", ".join(f"{k} {v}" for k, v in dict(stats).items()))
        try:
            gcal.sync_all(stdout=self.stdout)
        except Exception as exc:  # noqa: BLE001 - logged, and the mail sync above already counted
            self.stderr.write(f"calendar sync failed: {exc}")
        from .import_meetings import import_from_drive  # Granola JSON the daily task put in the Drive folder

        try:
            granola = import_from_drive()
            if granola["files"]:
                self.stdout.write(f"granola: {len(granola['files'])} new file(s), "
                                  f"{granola['stats']['calendar']} meetings")
            for name, error in granola["failed"]:
                self.stderr.write(f"granola file {name} not imported: {error}")
        except Exception as exc:  # noqa: BLE001 - never fails the sync
            self.stderr.write(f"granola import failed: {exc}")
        from crm.directory import sort  # Claude sorts the review queue's new domains

        try:
            self.stdout.write("sorting: " + ", ".join(f"{k} {v}" for k, v in sort.auto_sort().items()))
        except Exception as exc:  # noqa: BLE001 - never fails the sync
            self.stderr.write(f"domain sorting failed: {exc}")
        from crm import notify

        try:
            notify.sweep_everyone()
        except Exception as exc:  # noqa: BLE001 - never fails the sync
            self.stderr.write(f"reminders failed: {exc}")
