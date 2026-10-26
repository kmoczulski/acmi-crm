"""Due and timed reminders for the whole team, so they reach browsers with the hub closed (crm/notify.py)."""
from django.core.management.base import BaseCommand

from crm import notify


class Command(BaseCommand):
    help = "Send today's due and timed task reminders to everyone (keyed, so nothing repeats). Runs with the mail sync."

    def handle(self, *args, **options):
        notify.sweep_everyone()
