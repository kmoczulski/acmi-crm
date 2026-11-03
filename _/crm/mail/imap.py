"""Read a Gmail mailbox over IMAP with an app password.

Reads Gmail's "All Mail" folder (sent and received in one place, read-only)
and uses Gmail's IMAP extensions for thread ids and labels.
"""
import imaplib
import logging
import queue
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.conf import settings

from .attachments import parse_bodystructure
from .parse import parse_message

FETCH_BATCH = 100  # messages per IMAP request
PARALLEL_CONNECTIONS = 8  # Gmail allows 15 per account in total, phone and other apps included
BODY_BYTES = 100_000  # read at most this much of a message; enough for its text, skips big attachments

_META = re.compile(rb"UID (\d+)|X-GM-THRID (\d+)|RFC822\.SIZE (\d+)|X-GM-LABELS \(([^)]*)\)")


log = logging.getLogger(__name__)

CONNECT_RETRIES = 4  # when Gmail says "too many simultaneous connections"
CONNECT_RETRY_WAIT = 30  # seconds; stale connections from stopped runs expire on their own


class MailboxError(Exception):
    pass


class TooManyConnections(MailboxError):
    pass


# Gmail drops long-lived connections now and then; these mean "reconnect and retry".
TRANSIENT = (imaplib.IMAP4.abort, ssl.SSLError, socket.error, ConnectionError, TimeoutError)
CHUNK_RETRIES = 3


def _meta(prefix):
    uid = thrid = size = None
    labels = []
    for m in _META.finditer(prefix):
        if m.group(1):
            uid = int(m.group(1))
        elif m.group(2):
            thrid = m.group(2).decode()
        elif m.group(3):
            size = int(m.group(3))
        elif m.group(4) is not None:
            labels = [x.strip('"') for x in m.group(4).decode(errors="replace").split()]
    return uid, thrid, size, labels


class ImapClient:
    def __init__(self, mailbox):
        self.mailbox = mailbox
        self.conn = None
        self.uid_validity = ""
        self._workers = []
        self._folder = None

    def _connect(self):
        password = self.mailbox.get_password()
        if not password:
            raise MailboxError(
                f"No app password. Add {self.mailbox.password_env_var}=<16-letter app password> to the .env file."
            )
        try:
            conn = imaplib.IMAP4_SSL(settings.IMAP_HOST)
            conn.login(self.mailbox.email, password)
        except imaplib.IMAP4.error as exc:
            if "simultaneous" in str(exc).lower():
                raise TooManyConnections(str(exc)) from exc
            raise MailboxError(f"Gmail rejected the sign-in for {self.mailbox.email}: {exc}") from exc
        if self._folder is None:
            self._folder = self._all_mail_folder(conn)
        status, _ = conn.select(self._folder, readonly=True)
        if status != "OK":
            raise MailboxError(f"Could not open {self._folder}")
        return conn

    def __enter__(self):
        for attempt in range(CONNECT_RETRIES + 1):
            try:
                self.conn = self._connect()
                break
            except TooManyConnections:
                if attempt == CONNECT_RETRIES:
                    raise MailboxError(
                        "Gmail says too many connections are open. Stopped runs release theirs within a few "
                        "minutes; try again shortly."
                    )
                log.warning("Gmail: too many connections; retrying in %ss", CONNECT_RETRY_WAIT)
                print(f"  Gmail has too many connections open; waiting {CONNECT_RETRY_WAIT}s and retrying...")
                time.sleep(CONNECT_RETRY_WAIT)
        self.uid_validity = (self.conn.response("UIDVALIDITY")[1] or [b""])[0].decode()
        return self

    def __exit__(self, *exc):
        for conn in [self.conn, *self._workers]:
            try:
                conn.logout()
            except Exception:
                pass

    def _connection_pool(self):
        """The main connection plus as many extra ones as Gmail will give us (up to the limit)."""
        while len(self._workers) < PARALLEL_CONNECTIONS - 1:
            try:
                self._workers.append(self._connect())
            except TooManyConnections:
                break
        pool = queue.Queue()
        for conn in [self.conn, *self._workers]:
            pool.put(conn)
        return pool, 1 + len(self._workers)

    @staticmethod
    def _all_mail_folder(conn):
        # The folder's name is localized; its \All flag is not.
        _, folders = conn.list()
        for line in folders or []:
            if b"\\All" in line:
                name = line.decode().split(' "/" ')[-1]
                return name if name.startswith('"') else f'"{name}"'
        return '"[Gmail]/All Mail"'

    def search(self, since=None, after_uid=0):
        """UIDs above after_uid (and on/after `since`), ascending."""
        criteria = [f"UID {after_uid + 1}:*"]
        if since:
            criteria.append(f"SINCE {since.strftime('%d-%b-%Y')}")
        status, data = self.conn.uid("search", None, *criteria)
        if status != "OK":
            raise MailboxError(f"Search failed: {data}")
        # "N:*" always matches the newest message even when it is <= N, so filter.
        return sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > after_uid)

    def fetch(self, uids):
        """ParsedEmail for each uid, in uid order. Chunks are fetched over parallel connections."""
        chunks = [",".join(str(u) for u in uids[i : i + FETCH_BATCH]) for i in range(0, len(uids), FETCH_BATCH)]
        if len(chunks) <= 1 or PARALLEL_CONNECTIONS <= 1:
            results = []
            for chunk in chunks:
                result, self.conn = self._fetch_with_retry(self.conn, chunk)
                results.append(result)
        else:
            pool, size = self._connection_pool()

            def run(chunk):
                conn = pool.get()
                try:
                    result, conn = self._fetch_with_retry(conn, chunk)
                    return result
                finally:
                    pool.put(conn)

            with ThreadPoolExecutor(max_workers=size) as executor:
                results = list(executor.map(run, chunks))
        return sorted((e for chunk in results for e in chunk), key=lambda e: e.uid)

    def addresses(self, uids):
        """(display name, address) pairs from the From/To/Cc lines of these messages. Headers only, fast."""
        from email.utils import getaddresses

        def read(conn, chunk):
            pairs = []
            for _, _, _, _, raw, _ in self._fetch_parts(conn, chunk, "BODY.PEEK[HEADER.FIELDS (FROM TO CC)]"):
                text = raw.decode("utf-8", errors="replace")
                from email import message_from_string, policy

                msg = message_from_string(text, policy=policy.default)
                values = [str(v) for h in ("From", "To", "Cc") for v in msg.get_all(h, [])]
                pairs.extend((n, a.lower()) for n, a in getaddresses(values) if a)
            return pairs

        chunks = [",".join(str(u) for u in uids[i : i + 500]) for i in range(0, len(uids), 500)]
        pool, size = self._connection_pool()

        def run(chunk):
            conn = pool.get()
            try:
                for attempt in range(CHUNK_RETRIES + 1):
                    try:
                        return read(conn, chunk)
                    except TRANSIENT:
                        if attempt == CHUNK_RETRIES:
                            raise
                        time.sleep(2 * (attempt + 1))
                        conn = self._connect()
                        self._workers.append(conn)
            finally:
                pool.put(conn)

        with ThreadPoolExecutor(max_workers=size) as executor:
            return [pair for chunk in executor.map(run, chunks) for pair in chunk]

    def _fetch_with_retry(self, conn, chunk):
        """Fetch a chunk; if the connection drops, open a fresh one and try again."""
        for attempt in range(CHUNK_RETRIES + 1):
            try:
                return self._fetch_chunk(conn, chunk), conn
            except TRANSIENT as exc:
                if attempt == CHUNK_RETRIES:
                    raise MailboxError(f"Gmail connection kept dropping: {exc}") from exc
                log.warning("Gmail connection dropped (%s); reconnecting", exc)
                try:
                    conn.logout()
                except Exception:
                    pass
                time.sleep(2 * (attempt + 1))
                while True:
                    try:
                        conn = self._connect()
                        break
                    except TooManyConnections:
                        time.sleep(CONNECT_RETRY_WAIT)
                self._workers.append(conn)  # so __exit__ logs it out

    def _fetch_chunk(self, conn, chunk):
        # Pass 1: metadata + headers for the whole chunk (small).
        parsed = {}
        for uid, thrid, _, labels, header, meta in self._fetch_parts(conn, chunk, "BODYSTRUCTURE BODY.PEEK[HEADER]"):
            parsed[uid] = parse_message(header, uid=uid, thread_id=thrid or "", labels=labels)
            parsed[uid].attachments = parse_bodystructure(meta)
        # Pass 2: text only where the sync reads it: inbound, person-to-person mail.
        # Sent mail and newsletters are logged from headers alone.
        own = self.mailbox.email.lower()
        need_body = [str(uid) for uid, e in parsed.items() if e.from_addr != own and not e.is_bulk]
        if need_body:
            for uid, thrid, _, labels, raw, _ in self._fetch_parts(conn, ",".join(need_body), f"BODY.PEEK[]<0.{BODY_BYTES}>"):
                if uid in parsed:
                    old = parsed[uid]
                    parsed[uid] = parse_message(raw, uid=uid, thread_id=thrid or old.thread_id, labels=labels or old.labels)
                    parsed[uid].attachments = old.attachments
        return list(parsed.values())

    @staticmethod
    def _fetch_parts(conn, uid_set, part):
        """Yield (uid, thread_id, size, labels, literal bytes, metadata bytes) for a FETCH of `part`."""
        status, data = conn.uid("fetch", uid_set, f"(UID X-GM-THRID X-GM-LABELS RFC822.SIZE {part})")
        if status != "OK":
            raise MailboxError(f"Fetch failed: {data}")
        for i, item in enumerate(data):
            if not isinstance(item, tuple):
                continue
            # Metadata can come before the literal (item[0]) or after it (the next bytes item).
            prefix = item[0]
            if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                prefix += b" " + data[i + 1]
            uid, thrid, size, labels = _meta(prefix)
            if uid is not None:
                yield uid, thrid, size, labels, item[1], prefix


def since_date(days):
    from datetime import timedelta

    return date.today() - timedelta(days=days)
