"""Fill in people whose name is blank, "TBD", or just their email address.

    python manage.py fill_names            # dry run: shows what would change
    python manage.py fill_names --commit   # writes
    python manage.py fill_names --no-gmail # skip reading Gmail headers

Sources, best first: the display name on real emails (From/To/Cc headers in each
connected mailbox), names in the workbook, then an unambiguous first.last@ address.
Names that are already set are never touched.
"""
from collections import Counter, defaultdict

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction

from crm.mail.imap import ImapClient, MailboxError
from crm.management.commands.import_workbook import DEFAULT_PATH, rows, valid_email
from crm.models import Mailbox, Person
from crm.names import clean_name, is_role_address, name_from_address, needs_name


class Command(BaseCommand):
    help = "Fill missing person names (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true")
        parser.add_argument("--no-gmail", action="store_true")
        parser.add_argument("--workbook", default=DEFAULT_PATH)

    def handle(self, *args, **opts):
        targets = [p for p in Person.objects.exclude(email=None).select_related("company") if needs_name(p)]
        self.stdout.write(f"{len(targets)} people with an email but no usable name.")
        wanted = {p.email for p in targets}

        gmail = defaultdict(Counter)
        if not opts["no_gmail"]:
            for mailbox in Mailbox.objects.filter(enabled=True, shared=False):
                self.stdout.write(f"Reading From/To/Cc headers in {mailbox.email}...")
                try:
                    with ImapClient(mailbox) as client:
                        uids = client.search()
                        for name, address in client.addresses(uids):
                            if address in wanted and (parts := clean_name(name, address)):
                                gmail[address][parts] += 1
                except MailboxError as exc:
                    self.stderr.write(f"  skipped: {exc}")
            self.stdout.write(f"  found names for {len(gmail)} of them in email headers.")

        workbook = {}
        try:
            wb = openpyxl.load_workbook(opts["workbook"], read_only=True, data_only=True)
            for row in rows(wb["Outreach_Log"]):
                email = valid_email(row.get("Email"))
                if email in wanted and (parts := clean_name(str(row.get("Name") or ""), email)):
                    workbook.setdefault(email, parts)
            if "Sheet1" in wb.sheetnames:
                for row in rows(wb["Sheet1"]):
                    email = valid_email(row.get("Email"))
                    raw = f"{row.get('First Name') or ''} {row.get('Last Name') or ''}"
                    if email in wanted and (parts := clean_name(raw, email)):
                        workbook.setdefault(email, parts)
        except FileNotFoundError:
            self.stderr.write("Workbook not found; skipping it.")

        changes, by_source, role = [], Counter(), 0
        for person in targets:
            if gmail.get(person.email):
                parts, source = gmail[person.email].most_common(1)[0][0], "email headers"
            elif person.email in workbook:
                parts, source = workbook[person.email], "workbook"
            elif parts := name_from_address(person.email):
                source = "first.last address"
            else:
                role += is_role_address(person.email)
                continue
            changes.append((person, parts))
            by_source[source] += 1

        for source, n in by_source.most_common():
            self.stdout.write(f"  {n:>5} from {source}")
        self.stdout.write(f"  {len(targets) - len(changes):>5} left blank ({role} are role addresses like sales@ or info@)")
        for person, (_, _, full) in changes[:15]:
            self.stdout.write(f"    {person.email:<45} -> {full}")

        if not opts["commit"]:
            self.stdout.write(self.style.WARNING("DRY RUN: nothing was written. Re-run with --commit to save."))
            return
        with transaction.atomic():
            for person, (first, last, full) in changes:
                Person.objects.filter(pk=person.pk).update(first_name=first, last_name=last, full_name=full)
        self.stdout.write(self.style.SUCCESS(f"Filled {len(changes)} names."))
