"""Make a VAPID key pair for browser notifications (crm/push.py) and print the .env lines."""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_keys():
    key = ec.generate_private_key(ec.SECP256R1())
    private = _b64(key.private_numbers().private_value.to_bytes(32, "big"))
    public = _b64(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    return public, private


class Command(BaseCommand):
    help = "Print a new VAPID key pair for .env. Changing keys later means everyone turns browser notifications on again."

    def handle(self, *args, **options):
        public, private = make_keys()
        self.stdout.write(f"ACMI_VAPID_PUBLIC_KEY={public}\nACMI_VAPID_PRIVATE_KEY={private}")
