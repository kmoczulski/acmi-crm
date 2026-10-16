"""Compose and send outreach email over Gmail SMTP with the sender's app password.

Each email gets a Message-ID and is written to the timeline straight away, keyed
the same way the Gmail sync keys it, so the sync later recognises the email (and
any reply to it) instead of logging it twice.
"""
import html
import mimetypes
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from crm import interactions
from crm.models import Activity, Mailbox, OutreachFile, OutreachSettings, SendBatch, SendItem, suppress_history

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

SIGNATURE_HTML = """<p style="margin: 0; color: #104C88;">
{logo}<strong>{name}</strong><br>
{title}<br>
{phone}<br>
</p>"""


class SendError(RuntimeError):
    pass


def fill(text, person, greeting):
    company = person.company
    return (text or "").replace("{greeting}", greeting).replace(
        "{first_name}", greeting if greeting != "To whom it may concern" else "there"
    ).replace("{company}", company.name if company else "").replace("{sector}", company.sector if company else "")


def compose(template, person, greeting, settings=None):
    """(subject, html, plain) for one person, with the sender's signature."""
    settings = settings or OutreachSettings.load()
    logo = (f'<img src="{html.escape(settings.signature_logo_url)}" alt="ACMI Logo" '
            'style="max-width: 100px; margin-bottom: 6px;"><br>') if settings.signature_logo_url else ""
    signature = SIGNATURE_HTML.format(logo=logo, name=html.escape(settings.sender_name),
                                      title=html.escape(settings.sender_title), phone=html.escape(settings.sender_phone))
    subject = fill(template.subject, person, greeting)
    body_html = ('<html><body style="font-family: Arial, sans-serif; line-height: 1.3; margin: 0; padding: 5px;">'
                 + fill(template.body_html, person, greeting) + signature + "</body></html>")
    plain = (fill(template.body_plain, person, greeting) + "\n\n"
             + f"{settings.sender_name}\n{settings.sender_title}\n{settings.sender_phone}")
    return subject, body_html, plain


def attachments(settings=None):
    settings = settings or OutreachSettings.load()
    if not settings.include_attachments:
        return []
    files = []
    for f in OutreachFile.objects.filter(active=True):
        try:
            with f.file.open("rb") as fh:
                data = fh.read()
        except (FileNotFoundError, OSError):
            continue
        ctype, _ = mimetypes.guess_type(f.file.name)
        files.append((f.file.name.rsplit("/", 1)[-1], data, ctype or "application/octet-stream"))
    return files


def build_message(mailbox, to_addr, subject, body_html, plain, files, settings):
    msg = EmailMessage()
    msg["From"] = formataddr((settings.sender_name, mailbox.email))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=mailbox.email.rsplit("@", 1)[-1])
    msg.set_content(plain)
    msg.add_alternative(body_html, subtype="html")
    for name, data, ctype in files:
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg


class Smtp:
    def __init__(self, mailbox):
        self.mailbox = mailbox
        self.conn = None

    def __enter__(self):
        self.connect()
        return self

    def connect(self):
        password = self.mailbox.get_password()
        if not password:
            raise SendError(f"No Gmail app password: add {self.mailbox.password_env_var} to .env.")
        try:
            self.conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
            self.conn.starttls()
            self.conn.login(self.mailbox.email, password)
        except smtplib.SMTPException as exc:
            raise SendError(f"Gmail refused the SMTP sign-in: {exc}") from exc

    def send(self, msg):
        try:
            self.conn.noop()
        except (smtplib.SMTPException, OSError):
            self.connect()  # the connection dropped between messages
        self.conn.send_message(msg)

    def __exit__(self, *exc):
        try:
            self.conn.quit()
        except Exception:
            pass


def log_sent(item, msg, subject, user):
    """Timeline entry for a sent email, keyed exactly as the Gmail sync would key it."""
    person = item.person
    company = person.company if person else None
    key = f"gmail:{msg['Message-ID']}|c{company.pk}" if company else f"gmail:{msg['Message-ID']}|p{person.pk}"
    activity, _ = Activity.objects.get_or_create(
        source=Activity.Source.GMAIL, source_id=key[:255],
        defaults={
            "occurred_at": timezone.now(), "kind": Activity.Kind.EMAIL_SENT, "actor": user, "company": company,
            "person": person, "summary": f"Email to {person.full_name or person.email}: {subject}"[:500],
            "campaign": item.batch.campaign, "message_id": msg["Message-ID"],
            "context": Activity.Context.PROSPECTING, "outcome": "No Reply",
        },
    )
    activity.team_members.add(user)
    return activity


def send_test(user, template, person, to_addr):
    """One sample email to your own address, rendered for `person`."""
    from .queue import greeting_for

    settings = OutreachSettings.load()
    mailbox = Mailbox.objects.filter(user=user).first()
    if mailbox is None:
        raise SendError("Connect your mailbox on the Mail page first.")
    subject, body_html, plain = compose(template, person, greeting_for(person), settings)
    msg = build_message(mailbox, to_addr, f"[TEST] {subject}", body_html, plain, attachments(settings), settings)
    with Smtp(mailbox) as smtp:
        smtp.send(msg)


def run_batch(batch_id, sleep=time.sleep):
    """Send every pending item in a batch. Safe to call again: sent items are never resent."""
    from .queue import greeting_for

    with transaction.atomic():
        batch = SendBatch.objects.select_for_update().get(pk=batch_id)
        if batch.status not in (SendBatch.Status.QUEUED, SendBatch.Status.SENDING):
            return batch
        batch.status, batch.started_at = SendBatch.Status.SENDING, batch.started_at or timezone.now()
        batch.save(update_fields=["status", "started_at"])

    settings = OutreachSettings.load()
    user = batch.created_by
    mailbox = Mailbox.objects.filter(user=user).first()
    files = attachments(settings)
    person_ids = []
    try:
        if mailbox is None:
            raise SendError("Connect your mailbox on the Mail page first.")
        with Smtp(mailbox) as smtp:
            for item in batch.items.filter(status=SendItem.Status.PENDING).select_related("person__company", "template"):
                batch.refresh_from_db(fields=["stop_requested"])
                if batch.stop_requested:
                    batch.status = SendBatch.Status.STOPPED
                    break
                if item.person is None or item.template is None:
                    item.status, item.error = SendItem.Status.SKIPPED, "Person or template was deleted"
                    item.save(update_fields=["status", "error"])
                    continue
                subject, body_html, plain = compose(item.template, item.person, greeting_for(item.person), settings)
                msg = build_message(mailbox, item.email, subject, body_html, plain, files, settings)
                try:
                    smtp.send(msg)
                except (smtplib.SMTPException, OSError) as exc:
                    item.status, item.error = SendItem.Status.FAILED, str(exc)[:1000]
                    item.save(update_fields=["status", "error"])
                    continue
                item.status, item.message_id, item.sent_at = SendItem.Status.SENT, msg["Message-ID"], timezone.now()
                item.save(update_fields=["status", "message_id", "sent_at"])
                token = suppress_history.set(True)
                try:
                    log_sent(item, msg, subject, user)
                finally:
                    suppress_history.reset(token)
                person_ids.append(item.person_id)
                sleep(settings.delay_seconds)
            else:
                batch.status = SendBatch.Status.DONE
    except SendError as exc:
        batch.status, batch.error = SendBatch.Status.FAILED, str(exc)
    except Exception as exc:  # network etc.: keep what was sent, report the rest
        batch.status, batch.error = SendBatch.Status.FAILED, f"{type(exc).__name__}: {exc}"
    finally:
        batch.finished_at = timezone.now()
        batch.save(update_fields=["status", "error", "finished_at"])
        if person_ids:
            from crm.models import Person

            companies = set(Person.objects.filter(pk__in=person_ids).exclude(company=None)
                            .values_list("company_id", flat=True))
            interactions.recompute(company_ids=companies, person_ids=person_ids)
            # Prospects that just got their first email move to Contacted.
            from crm.models import Company, CompanyStatus

            for c in Company.objects.filter(pk__in=companies, status=CompanyStatus.PROSPECT):
                c.status = CompanyStatus.CONTACTED
                c.save()
    return batch


def start_in_background(batch):
    """Run the batch in a separate process so the web page doesn't wait (or die) on a long send."""
    import subprocess
    import sys

    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [sys.executable, str(django_settings.BASE_DIR / "manage.py"), "send_batch", str(batch.pk)],
        cwd=str(django_settings.BASE_DIR), creationflags=flags, close_fds=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
