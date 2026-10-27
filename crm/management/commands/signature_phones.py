"""Phone numbers from the signatures of mail already in the hub onto People (crm/mail/signature_phones.py).

    python manage.py signature_phones            # dry run: counts only
    python manage.py signature_phones --commit   # writes

Newest email first, so a person's current numbers come before old ones. Safe to re-run: numbers already on a
person are skipped.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from crm.mail import signature_phones
from crm.models import Activity, Person


class Command(BaseCommand):
    help = "Add phone numbers from email signatures to the people who sent them (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", help="Write the numbers (default: only count them)")

    def handle(self, *args, commit=False, **opts):
        skip = signature_phones.team_numbers()
        stats = Counter()
        people = {}
        # On a received email or reply the person is its sender (crm/mail/sync.py matches them by the From address).
        emails = (Activity.objects.filter(kind__in=[Activity.Kind.EMAIL_RECEIVED, Activity.Kind.REPLY],
                                          person__isnull=False)
                  .exclude(body="").select_related("person").order_by("-occurred_at"))
        for act in emails.iterator(chunk_size=500):
            stats["emails read"] += 1
            p = people.setdefault(act.person_id, act.person)
            found = signature_phones.phones_in(signature_phones.signature(act.body, p.full_name or ""))
            if not found:
                continue
            stats["emails with a signature number"] += 1
            phones = signature_phones.merge(p, found, skip)
            if phones is not None:
                stats["numbers added"] += len(phones) - len(p.phones or [])
                if not (p.phones or []):
                    stats["people with a first number"] += 1
                p.phones = phones
                p._changed = True
        changed = [p for p in people.values() if getattr(p, "_changed", False)]
        stats["people changed"] = len(changed)
        if commit and changed:
            with transaction.atomic():
                Person.objects.bulk_update(changed, ["phones"], batch_size=500)
        for k in ("emails read", "emails with a signature number", "people changed", "people with a first number",
                  "numbers added"):
            self.stdout.write(f"{k}: {stats[k]:,}")
        self.stdout.write(f"Numbers never added (ACMI's own, or in 3+ domains' signatures): {len(skip)}")
        self.stdout.write("Written." if commit else "Dry run: nothing written. Add --commit to save.")
