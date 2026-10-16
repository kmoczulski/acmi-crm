"""Remove workbook campaign sends that the Gmail sync also logged (each send was counted twice).

    python manage.py dedupe_campaign_sends            # dry run
    python manage.py dedupe_campaign_sends --commit

For every workbook (Outreach_Log) send with a Gmail-synced send to the same person on
the same or next day, the Gmail copy is kept (it has the real Message-ID, thread and
attachments) and takes the workbook's campaign and response; the workbook copy is removed.
"""
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from crm import interactions
from crm.models import Activity


class Command(BaseCommand):
    help = "Merge duplicate campaign sends (workbook + Gmail)."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **opts):
        gmail = defaultdict(list)
        for pk, person_id, at, campaign in (Activity.objects.filter(source=Activity.Source.GMAIL,
                                                                    kind=Activity.Kind.EMAIL_SENT)
                                            .exclude(person=None)
                                            .values_list("pk", "person_id", "occurred_at", "campaign")):
            gmail[(person_id, at.date())].append((pk, campaign))
        used, pairs = set(), []
        for imp in (Activity.objects.filter(source=Activity.Source.IMPORT, kind=Activity.Kind.EMAIL_SENT)
                    .exclude(person=None).only("pk", "person_id", "occurred_at", "campaign", "outcome")):
            for day in (imp.occurred_at.date(), (imp.occurred_at + timedelta(days=1)).date(),
                        (imp.occurred_at - timedelta(days=1)).date()):
                twin = next(((pk, c) for pk, c in gmail.get((imp.person_id, day), []) if pk not in used), None)
                if twin:
                    used.add(twin[0])
                    pairs.append((imp, twin[0]))
                    break
        self.stdout.write(f"{len(pairs)} duplicate campaign sends found.")
        if not opts["commit"]:
            self.stdout.write(self.style.WARNING("DRY RUN: nothing changed. Re-run with --commit."))
            return
        people = set()
        with transaction.atomic():
            for imp, gmail_pk in pairs:
                Activity.objects.filter(pk=gmail_pk).update(campaign=imp.campaign, outcome=imp.outcome,
                                                            context=Activity.Context.PROSPECTING)
                people.add(imp.person_id)
            Activity.objects.filter(pk__in=[imp.pk for imp, _ in pairs]).delete()
        self.stdout.write(f"Merged {len(pairs)}. Recomputing touch counts...")
        self.stdout.write(f"Updated {interactions.recompute()} records.")
