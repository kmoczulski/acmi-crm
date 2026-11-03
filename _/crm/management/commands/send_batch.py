"""Send one outreach batch (started from the Send page; sent items are never sent again).

    python manage.py send_batch <batch id>
    python manage.py send_batch <batch id> --resume    # pick up a batch left 'sending' by a crashed process
"""
from django.core.management.base import BaseCommand

from crm.outreach.sender import run_batch


class Command(BaseCommand):
    help = "Send the pending emails in an outreach batch."

    def add_arguments(self, parser):
        parser.add_argument("batch_id", type=int)
        parser.add_argument("--resume", action="store_true",
                            help="Continue a batch that is marked as sending but whose process ended. Make sure no "
                                 "other sender is running it.")

    def handle(self, *args, **opts):
        batch = run_batch(opts["batch_id"], resume=opts["resume"])
        counts = batch.counts()
        self.stdout.write(f"{batch}: {batch.get_status_display()} · {dict(counts)}"
                          + (f" · {batch.error}" if batch.error else ""))
