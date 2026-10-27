from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from crm.boards import trello_import

BOARDS = {"prospecting": ("Properties-Prospecting", trello_import.import_prospecting),
          "meih": ("Maryland / MEIH", trello_import.import_meih)}


class Command(BaseCommand):
    help = ("Bring a Trello board's history (comments, checklists, attachments, card activity) into the hub from "
            "scripts/trello_fetch.py's output. Dry run unless --apply. See crm/boards/trello_import.py.")

    def add_arguments(self, parser):
        parser.add_argument("folder", help="The fetch folder, e.g. data/imports/trello/2026-10-26")
        parser.add_argument("--board", default="prospecting", choices=sorted(BOARDS))
        parser.add_argument("--apply", action="store_true", help="Write it (default: report only)")
        parser.add_argument("--user", default="Kellen", help="Username new cards are created by")

    def handle(self, *args, **opts):
        name, run = BOARDS[opts["board"]]
        try:
            data = trello_import.find_board_file(opts["folder"], name)
        except FileNotFoundError as e:
            raise CommandError(str(e))
        user = User.objects.filter(username=opts["user"]).first()
        with transaction.atomic():
            try:
                imp = run(data, user)
            except ValueError as e:
                raise CommandError(str(e))
            self.stdout.write(f"{name}:")
            for key, n in sorted(imp.counts.items()):
                self.stdout.write(f"  {key}: {n}")
            if imp.outsiders:
                self.stdout.write("  outside the team (name kept in the comment): "
                                  + ", ".join(f"{k} {v}" for k, v in imp.outsiders.most_common()))
            if not opts["apply"]:
                transaction.set_rollback(True)
                self.stdout.write("Dry run: nothing saved. Run again with --apply.")
