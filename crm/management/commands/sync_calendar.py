from django.core.management.base import BaseCommand

from crm import gcal


class Command(BaseCommand):
    help = "Read every connected Google Calendar into the Meetings calendar and company timelines."

    def handle(self, *args, **opts):
        results = gcal.sync_all(stdout=self.stdout)
        if not results:
            self.stdout.write("Nobody has connected Google yet (Meetings -> Connect Google Calendar).")
