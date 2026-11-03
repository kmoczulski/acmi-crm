"""Pull new Gmail messages into the CRM timeline.

    python manage.py sync_mail                  # every enabled mailbox
    python manage.py sync_mail --user kellen    # one person
    python manage.py sync_mail --all-backlog    # keep going until the first-sync backlog is done

Each run reads up to --max messages per mailbox and resumes where the last run
stopped, so it is safe to schedule every few minutes.
"""
import time

from django.core.management.base import BaseCommand, CommandError

from crm.mail import sync
from crm.models import Mailbox


MAX_FAILURES = 10
RETRY_WAIT = 30


class Command(BaseCommand):
    help = "Sync Gmail into the CRM."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Username to sync (default: all enabled mailboxes)")
        parser.add_argument("--max", type=int, default=2000, help="Messages per mailbox per batch (default 2000)")
        parser.add_argument("--all-backlog", action="store_true", help="Repeat until nothing is left")

    def handle(self, *args, **opts):
        mailboxes = Mailbox.objects.filter(enabled=True).select_related("user")
        if opts["user"]:
            mailboxes = mailboxes.filter(user__username=opts["user"])
            if not mailboxes:
                raise CommandError(f"No enabled mailbox for {opts['user']}. Set one up on the Mail page.")
        for mailbox in mailboxes:
            self.stdout.write(f"{mailbox.email}:")
            failures = 0
            while True:
                try:
                    stats = sync.sync_mailbox(mailbox, max_messages=opts["max"])
                except sync.SyncBusy as exc:
                    raise CommandError(str(exc))
                mailbox.refresh_from_db()
                for key in sorted(stats):
                    self.stdout.write(f"  {key:<45} {stats[key]:>7}")
                if mailbox.last_error:
                    self.stderr.write(self.style.ERROR(f"  {mailbox.last_error}"))
                    # Wrong password or missing setup won't fix itself; network drops usually do.
                    permanent = "rejected the sign-in" in mailbox.last_error or "No app password" in mailbox.last_error
                    failures += 1
                    if permanent or not opts["all_backlog"] or failures > MAX_FAILURES:
                        break
                    self.stdout.write(f"  retrying in {RETRY_WAIT}s ({failures}/{MAX_FAILURES})...")
                    time.sleep(RETRY_WAIT)
                    continue
                failures = 0
                if not opts["all_backlog"] or not stats.get("messages remaining"):
                    self.stdout.write(self.style.SUCCESS("  caught up." if mailbox.backfill_done else "  batch done."))
                    break
                self.stdout.write("  ...continuing")
