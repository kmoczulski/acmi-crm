"""Re-tag all correspondence as project / prospecting (runs automatically for new mail).

    python manage.py tag_projects
"""
from django.core.management.base import BaseCommand

from crm import projects


class Command(BaseCommand):
    help = "Tag every email thread as about a project, prospecting, or neither."

    def handle(self, *args, **opts):
        counts = projects.tag()
        for context, n in sorted(counts.items()):
            self.stdout.write(f"  {context or 'untagged':<15} {n:>7}")
