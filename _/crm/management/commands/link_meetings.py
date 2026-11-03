"""Link every meeting to the directory firms and companies its attendees belong to, and note the outside attendees
no record knows (crm/needs_filing.py). The import and the mail sync do this for new meetings; run this once for the
meetings already on the calendar. A dry run by default: --apply writes."""
from django.core.management.base import BaseCommand
from django.db import transaction

from crm import needs_filing
from crm.models import Meeting


class Command(BaseCommand):
    help = "Link meetings to firms and companies by attendee domain, and mark who has no record (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the links (otherwise only count them)")

    def handle(self, *args, **opts):
        known = needs_filing.Known()
        firms = companies = flagged = 0
        meetings = list(Meeting.objects.prefetch_related("firms", "companies"))
        for m in meetings:
            co, fi, unfiled = known.read(m.attendees or [])
            firms += len(fi - {f.pk for f in m.firms.all()})
            companies += len(co - {c.pk for c in m.companies.all()})
            flagged += bool(unfiled)
        verb = "Linked" if opts["apply"] else "Would link"
        self.stdout.write(f"{verb} {firms} firm link{'s' if firms != 1 else ''} and {companies} company "
                          f"link{'s' if companies != 1 else ''} across {len(meetings)} meetings; {flagged} meeting"
                          f"{'s' if flagged != 1 else ''} have outside attendees with no record.")
        if not opts["apply"]:
            self.stdout.write("Dry run: nothing changed. Run again with --apply.")
            return
        with transaction.atomic():
            for m in meetings:
                needs_filing.link(m, known)
