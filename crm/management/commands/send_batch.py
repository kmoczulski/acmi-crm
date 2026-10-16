"""Send one outreach batch (started from the Send page; safe to re-run, sent items are skipped).

    python manage.py send_batch <batch id>
"""
from django.core.management.base import BaseCommand

from crm.outreach.sender import run_batch


class Command(BaseCommand):
    help = "Send the pending emails in an outreach batch."

    def add_arguments(self, parser):
        parser.add_argument("batch_id", type=int)

    def handle(self, *args, **opts):
        batch = run_batch(opts["batch_id"])
        counts = batch.counts()
        self.stdout.write(f"{batch}: {batch.get_status_display()} · {dict(counts)}"
                          + (f" · {batch.error}" if batch.error else ""))
