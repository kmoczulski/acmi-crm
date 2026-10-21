"""Read contract documents' files again with the current reader, e.g. to pick up Word tracked changes in files uploaded
before the CRM kept them. Comments and date sources pinned to the old text are found again by their quotes.

    manage.py reread_contract_files              # Word files with no tracked changes recorded
    manage.py reread_contract_files --contract 2 # just one contract's documents
    manage.py reread_contract_files --dry-run    # say what would change
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from crm.contracts import ai
from crm.contracts import text as textmod
from crm.models import Comment, ContractDate, ContractDocument


class Command(BaseCommand):
    help = "Re-read contract document files (picks up Word tracked changes)."

    def add_arguments(self, parser):
        parser.add_argument("--contract", type=int)
        parser.add_argument("--all", action="store_true", help="Every file, not just Word files with no changes kept")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        qs = ContractDocument.objects.exclude(file="")
        if opts["contract"]:
            qs = qs.filter(contract_id=opts["contract"])
        if not opts["all"]:
            qs = qs.filter(file__iendswith=".docx", tracked=[])
        for doc in qs:
            try:
                with doc.file.open("rb") as fh:
                    data = fh.read()
            except (FileNotFoundError, OSError):
                self.stdout.write(f"{doc.pk} {doc.title}: file missing, skipped")
                continue
            text, starts, status, tracked = textmod.extract_full(data, doc.file.name)
            self.stdout.write(f"{doc.pk} {doc.title}: {status}, {len(tracked)} tracked change(s), "
                              f"text {len(doc.text):,} -> {len(text):,} characters")
            if opts["dry_run"] or status != ContractDocument.TextStatus.OK:
                continue
            with transaction.atomic():
                old = doc.text
                doc.text, doc.page_starts, doc.pages, doc.text_status, doc.tracked = text, starts, len(starts), status, tracked
                doc.save(update_fields=["text", "page_starts", "pages", "text_status", "tracked"])
                for c in Comment.objects.filter(document=doc, start__isnull=False):
                    c.start, c.end = ai.anchor(text, None, None, c.quote or old[c.start:c.end])
                    c.save(update_fields=["start", "end"])
                for d in ContractDate.objects.filter(source=doc, source_start__isnull=False):
                    d.source_start, d.source_end = ai.anchor(text, None, None, d.quote or old[d.source_start:d.source_end])
                    d.save(update_fields=["source_start", "source_end"])
