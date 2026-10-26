"""Google tokens encrypted at rest (docs/EMAIL_CLIENT.md, section 5).

allauth keeps each person's Google access and refresh token in SocialToken. With ACMI_TOKEN_KEY set, both are stored
encrypted ("enc1:" + Fernet) and decrypted when a row is loaded, so allauth and crm/gcal.py keep reading plain values
and a copy of the database (a backup, say) is useless without the key. The key lives in .env (Secret Manager on Cloud
Run), never in the database, the repo or a log. Several keys, comma-separated, let a new key take over: the first one
encrypts, any of them decrypts.

Without a key, tokens stay as they are, and Mail in the hub can't be turned on (crm/mail/connect.py), so a Gmail
token is never stored unencrypted.
"""
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.db.models.signals import post_init, post_save, pre_save
from django.dispatch import receiver

PREFIX = "enc1:"
FIELDS = ("token", "token_secret")

log = logging.getLogger(__name__)


def _fernet():
    keys = [k.strip() for k in settings.TOKEN_KEYS if k.strip()]
    if not keys:
        return None
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except ValueError:
        log.error("ACMI_TOKEN_KEY is not a valid key; Google tokens are not being encrypted.")
        return None


def configured():
    return _fernet() is not None


def encrypt(value):
    f = _fernet()
    if not value or value.startswith(PREFIX) or f is None:
        return value
    return PREFIX + f.encrypt(value.encode()).decode()


def decrypt(value):
    """The plain value, or "" when it can't be read (no key, or a different key): the person reconnects Google."""
    if not value or not value.startswith(PREFIX):
        return value
    f = _fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        log.warning("A Google token can't be decrypted with ACMI_TOKEN_KEY; that person has to reconnect.")
        return ""


def _social_token():
    from allauth.socialaccount.models import SocialToken

    return SocialToken


@receiver(post_init, sender="socialaccount.SocialToken")
def _decrypt_on_load(sender, instance, **kwargs):
    for name in FIELDS:
        setattr(instance, name, decrypt(getattr(instance, name, "")))


@receiver(pre_save, sender="socialaccount.SocialToken")
def _encrypt_on_save(sender, instance, **kwargs):
    instance._plain_tokens = {name: getattr(instance, name) for name in FIELDS}
    for name in FIELDS:
        setattr(instance, name, encrypt(getattr(instance, name)))


@receiver(post_save, sender="socialaccount.SocialToken")
def _plain_after_save(sender, instance, **kwargs):
    # The row is encrypted; the object in memory goes back to plain values for whoever saved it.
    for name, value in getattr(instance, "_plain_tokens", {}).items():
        setattr(instance, name, value)


def encrypt_all():
    """Encrypt every stored token that isn't yet (manage.py encrypt_tokens). Returns how many were encrypted."""
    if not configured():
        return 0
    SocialToken = _social_token()
    n = 0
    for pk, token, secret in SocialToken.objects.values_list("pk", "token", "token_secret"):
        if (token and not token.startswith(PREFIX)) or (secret and not secret.startswith(PREFIX)):
            SocialToken.objects.filter(pk=pk).update(token=encrypt(token), token_secret=encrypt(secret))
            n += 1
    return n
