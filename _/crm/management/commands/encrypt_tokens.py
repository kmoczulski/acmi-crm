from django.core.management.base import BaseCommand, CommandError

from crm import tokens


class Command(BaseCommand):
    help = "Encrypt the stored Google tokens with ACMI_TOKEN_KEY (crm/tokens.py). Safe to run again."

    def handle(self, *args, **options):
        if not tokens.configured():
            raise CommandError("ACMI_TOKEN_KEY isn't set (or isn't a valid key), so there is nothing to encrypt with.")
        n = tokens.encrypt_all()
        self.stdout.write(f"Encrypted {n} Google token{'s' if n != 1 else ''}.")
