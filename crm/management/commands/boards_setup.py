from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from crm.boards import bound, logic
from crm.models import Board, BoardLabel, BoardList, Campus

# The MEIH board as Kellen decided it (docs/TRELLO_MAPPING.md, "Decided"): one board for the campus, The Forge and
# ARA Facility as lists, feeds for sites, dates and tasks. (name, feed)
MEIH_LISTS = [
    ("Land sites", "sites"),
    ("Critical dates", "critical_dates"),
    ("The Forge – 4390 / ETC", ""),
    ("ARA Facility – deal", ""),
    ("ARA Facility – sites and design", ""),
    ("Consultants", ""),
    ("Open tasks", "open_tasks"),
]
# Trello's MEIH labels that aren't among the hub's seven categories.
MEIH_EXTRA_LABELS = [("Ongoing", "yellow"), ("To do", "orange"), ("Contact information", "sky")]
# The MEIH Trello board's members, by full name (never pipeline_import.TEAM's old usernames).
MEIH_MEMBERS = [("Kellen", "Moczulski"), ("Simon", "Shewmaker"), ("David", "DeNardo"), ("David", "Dowell")]


class Command(BaseCommand):
    help = ("Create the Tenant pipeline record board (a list per deal stage, a card per deal) and the MEIH board "
            "(docs/TRELLO_MAPPING.md). Dry run unless --apply; boards that already exist are left alone.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the boards (default: report only)")
        parser.add_argument("--user", default="Kellen", help="Username the boards are created by")

    def handle(self, *args, **opts):
        user = User.objects.filter(username=opts["user"]).first()
        with transaction.atomic():
            self._pipeline(user)
            self._meih(user)
            if not opts["apply"]:
                transaction.set_rollback(True)
                self.stdout.write("Dry run: nothing saved. Run again with --apply.")

    def _pipeline(self, user):
        if Board.objects.filter(kind=Board.Kind.BOUND, record_type="opportunity", campus=None, project=None).exists():
            self.stdout.write("Tenant pipeline board: already there.")
            return
        board = bound.create_record_board("opportunity", user)
        n = sum(1 for _ in board.lists.all())
        cards = sum(l.cards.count() for l in board.lists.all())
        self.stdout.write(f"Tenant pipeline board: {n} stage lists, {cards} deal cards.")

    def _meih(self, user):
        campus = Campus.objects.filter(code="MEIH").first()
        if campus is None:
            self.stdout.write("MEIH board: no MEIH campus, skipped.")
            return
        if Board.objects.filter(campus=campus, name="MEIH").exists():
            self.stdout.write("MEIH board: already there.")
            return
        board = logic.create_board("MEIH", user, kind=Board.Kind.CAMPUS, campus=campus,
                                   description="Maryland Energetics Innovation Hub: The Forge and ARA Facility. Lists "
                                               "are projects, labels are disciplines (filter by Legal to see what's "
                                               "with counsel).")
        for n, (name, feed) in enumerate(MEIH_LISTS, 1):
            BoardList.objects.create(board=board, name=name, position=n * logic.GAP, feed=feed)
        start = board.labels.count()
        for n, (name, colour) in enumerate(MEIH_EXTRA_LABELS, start + 1):
            BoardLabel.objects.create(board=board, name=name, colour=colour, position=n)
        members = [u for first, last in MEIH_MEMBERS
                   for u in User.objects.filter(first_name=first, last_name=last, is_active=True)[:1]]
        board.members.add(*members)
        self.stdout.write(f"MEIH board: {len(MEIH_LISTS)} lists ({sum(1 for _, f in MEIH_LISTS if f)} filled from "
                          f"records), {board.labels.count()} labels, members {', '.join(u.username for u in members)}.")
