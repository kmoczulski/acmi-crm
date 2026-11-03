"""Write every saved meeting transcript to its .txt file (only new or changed ones), under ACMI_TRANSCRIPT_DIR.

    python manage.py export_transcripts
    python manage.py export_transcripts --to "G:/Shared drives/<folder>"
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from crm import transcripts
from crm.models import MeetingTranscript


class Command(BaseCommand):
    help = "Write saved meeting transcripts as .txt files (one folder per campus)."

    def add_arguments(self, parser):
        parser.add_argument("--to", help="Folder to write to (default: settings.TRANSCRIPT_DIR)")

    def handle(self, *args, **opts):
        root = opts["to"] or settings.TRANSCRIPT_DIR
        written = transcripts.export_all(root)
        total = MeetingTranscript.objects.count()
        self.stdout.write(f"{len(written)} .txt files written ({total} transcripts saved in all) under {root}")
