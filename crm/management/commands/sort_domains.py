"""Claude sorts the mail review queue's new domains, filing the sure consultants, vendors and lenders in the directory
(crm/directory/sort.py). Run after each mail sync by scripts/sync_mail.cmd and sync_all.

    python manage.py sort_domains              # up to 60 domains Claude hasn't seen
    python manage.py sort_domains --companies  # also flag existing Companies that look like vendors
"""
from django.core.management.base import BaseCommand

from crm.directory import sort


class Command(BaseCommand):
    help = "Sort new email domains with Claude: directory firm, prospect, or neither."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=sort.PER_RUN)
        parser.add_argument("--companies", action="store_true", help="Also check existing Companies (no move)")

    def handle(self, *args, **opts):
        stats = sort.auto_sort(limit=opts["limit"])
        self.stdout.write(", ".join(f"{k} {v}" for k, v in stats.items()))
        if opts["companies"]:
            out = sort.review_companies(limit=opts["limit"])
            self.stdout.write(f"companies checked {out['checked']}, flagged {out['flagged']}")
