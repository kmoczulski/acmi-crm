"""Record attachment names for email already synced (new mail gets them automatically).

    python manage.py sync_attachments            # each mailbox's history window
    python manage.py sync_attachments --days 90

Reads only the structure of emails that have attachments (names and sizes), never the files.
"""
from concurrent.futures import ThreadPoolExecutor
from email import message_from_bytes

from django.core.management.base import BaseCommand

from crm.mail.attachments import parse_bodystructure
from crm.mail.imap import ImapClient, MailboxError
from crm.mail.sync import save_attachments
from crm.models import Activity, Attachment, Mailbox

CHUNK = 300


class Command(BaseCommand):
    help = "Backfill attachment names for synced email."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="How far back (default: the mailbox's history window)")

    def handle(self, *args, **opts):
        for mailbox in Mailbox.objects.filter(enabled=True):
            days = opts["days"] or mailbox.backfill_days
            self.stdout.write(f"{mailbox.email}: emails with attachments in the last {days} days...")
            try:
                with ImapClient(mailbox) as client:
                    status, data = client.conn.uid("search", None, "X-GM-RAW", f'"has:attachment newer_than:{days}d"')
                    uids = (data[0] or b"").split() if status == "OK" else []
                    self.stdout.write(f"  {len(uids)} found; reading their structure...")
                    chunks = [b",".join(uids[i : i + CHUNK]).decode() for i in range(0, len(uids), CHUNK)]
                    pool, size = client._connection_pool()

                    def read(chunk):
                        conn = pool.get()
                        try:
                            out = []
                            parts = client._fetch_parts(conn, chunk, "BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]")
                            for _, _, _, _, header, meta in parts:
                                msg_id = str(message_from_bytes(header).get("Message-ID", "")).strip()
                                files = parse_bodystructure(meta)
                                if msg_id and files:
                                    out.append((msg_id, files))
                            return out
                        finally:
                            pool.put(conn)

                    with ThreadPoolExecutor(max_workers=size) as executor:
                        found = [row for rows in executor.map(read, chunks) for row in rows]
            except MailboxError as exc:
                self.stderr.write(f"  skipped: {exc}")
                continue

            by_id = {}
            for msg_id, files in found:
                by_id.setdefault(msg_id, files)
            activities = Activity.objects.filter(source=Activity.Source.GMAIL, message_id__in=list(by_id))
            before = Attachment.objects.count()
            for activity in activities.iterator():
                save_attachments(activity, by_id[activity.message_id])
            self.stdout.write(self.style.SUCCESS(
                f"  {len(by_id)} emails had real attachments; {Attachment.objects.count() - before} files recorded "
                f"on {activities.count()} timeline entries."
            ))
