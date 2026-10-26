"""Read a Gmail mailbox through the Gmail API with the owner's own Google permission (docs/EMAIL_CLIENT.md, step 2).

This replaces crm/mail/imap.py for anyone who turned Mail in the hub on. It produces the same ParsedEmail objects,
so crm/mail/sync.py's process_messages logs them exactly as before: same Message-ID keys, so nothing is logged
twice, and thread ids in the decimal form IMAP's X-GM-THRID used (the API gives the same number in hex).

Each run:
  1. the first time (or after a switch from IMAP): note Gmail's change marker (historyId), then queue every message
     since the backfill date, or since the last IMAP sync less a day, oldest first;
  2. later runs: ask Gmail what was added since the marker (history.list) and queue those;
  3. read up to max_messages from the queue (Mailbox.pending_ids), the rest wait for the next run.
Message bodies are read with format=full, which leaves attachment bytes in Gmail; only their names and sizes come
back, as with IMAP's BODYSTRUCTURE.
"""
import base64
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone

import requests
from django.utils import timezone

from crm import gcal

from .imap import MailboxError, since_date
from .parse import finish_text, parse_message

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT = 30
PAGE_SIZE = 500
PARALLEL_REQUESTS = 8
RETRIES = 3
# Gmail's system labels in the names IMAP's X-GM-LABELS uses, which process_messages checks ("\\Draft", ...).
SYSTEM_LABELS = {"INBOX": "\\Inbox", "SENT": "\\Sent", "DRAFT": "\\Draft", "SPAM": "\\Spam", "TRASH": "\\Trash",
                 "STARRED": "\\Starred", "IMPORTANT": "\\Important"}
SKIPPED = {"CHAT"}  # Google Chat history, not mail
RECONNECT = "Turn on Mail again in Mail in the hub."

log = logging.getLogger(__name__)


class NotFound(Exception):
    pass


class HistoryExpired(Exception):
    """Gmail keeps its change history for about a week; older markers have to be re-read by date."""


def decimal_thread_id(hex_id):
    """Gmail API thread id (hex) -> the decimal X-GM-THRID the timeline already stores."""
    try:
        return str(int(hex_id, 16)) if hex_id else ""
    except ValueError:
        return ""


def uid_of(message_id):
    """A number for ParsedEmail.uid from Gmail's hex message id."""
    try:
        return int(message_id, 16)
    except ValueError:
        return abs(hash(message_id))


def hex_thread_id(decimal_id):
    try:
        return format(int(decimal_id), "x") if decimal_id else ""
    except ValueError:
        return ""


class GmailClient:
    """The Gmail API as the mailbox owner. `session` stands in for `requests` in tests."""

    def __init__(self, user, session=None):
        self.session = session or requests
        self.token = gcal.token_for(user)
        if self.token is None:
            raise MailboxError(f"Gmail isn't connected. {RECONNECT}")

    def _access(self, force=False):
        if force:
            self.token.expires_at = timezone.now() - timedelta(minutes=1)
        try:
            return gcal.access_token(self.token, self.session)
        except gcal.CalendarError as exc:
            raise MailboxError(f"Google didn't accept the hub's Gmail permission. {RECONNECT}") from exc

    def prime(self):
        self._access()

    def get(self, path, params=None):
        refreshed = False
        for attempt in range(RETRIES + 1):
            r = self.session.get(f"{API}{path}", params=params, timeout=TIMEOUT,
                                 headers={"Authorization": f"Bearer {self._access(force=refreshed)}"})
            if r.status_code == 401 and not refreshed:
                refreshed = True
                continue
            if r.status_code in (401, 403):
                raise MailboxError(f"Google refused the hub's Gmail access. {RECONNECT}")
            if r.status_code == 404:
                raise NotFound(path)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise MailboxError("Gmail kept refusing requests. The next sync tries again.")

    def profile(self):
        return self.get("/profile")

    def list_ids(self, query):
        """Message ids matching a Gmail search, newest first (Spam and Trash left out)."""
        ids, params = [], {"q": query, "maxResults": PAGE_SIZE}
        while True:
            data = self.get("/messages", params)
            ids += [m["id"] for m in data.get("messages", [])]
            if not data.get("nextPageToken"):
                return ids
            params = {**params, "pageToken": data["nextPageToken"]}

    def history(self, start):
        """(ids of messages added since `start`, in order; the newest marker; ids of every thread that changed: new
        mail, labels added or removed, deletions). HistoryExpired if `start` is too old."""
        ids, threads, latest = [], [], start
        params = {"startHistoryId": start, "maxResults": PAGE_SIZE,
                  "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]}
        while True:
            try:
                data = self.get("/history", params)
            except NotFound as exc:
                raise HistoryExpired(start) from exc
            for h in data.get("history", []):
                ids += [a["message"]["id"] for a in h.get("messagesAdded", [])]
                for key in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
                    threads += [a["message"].get("threadId") for a in h.get(key, []) if a["message"].get("threadId")]
            latest = data.get("historyId", latest)
            if not data.get("nextPageToken"):
                return list(dict.fromkeys(ids)), str(latest), list(dict.fromkeys(threads))
            params = {**params, "pageToken": data["nextPageToken"]}

    def message(self, message_id):
        return self.get(f"/messages/{message_id}", {"format": "full"})

    # The inbox (crm/mail/index.py, inbox_views.py)

    def threads(self, label=None, query="", page_token="", size=50):
        """One page of threads, newest first: ({id, snippet, historyId} list, next page token, rough total)."""
        params = {"maxResults": size}
        if label:
            params["labelIds"] = label
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        data = self.get("/threads", params)
        return data.get("threads", []), data.get("nextPageToken", ""), data.get("resultSizeEstimate", 0)

    def thread(self, thread_id, full=False):
        params = {"format": "full"} if full else {"format": "metadata",
                                                   "metadataHeaders": ["From", "To", "Cc", "Subject", "Date"]}
        return self.get(f"/threads/{thread_id}", params)

    def labels(self):
        return self.get("/labels").get("labels", [])

    def label(self, label_id):
        return self.get(f"/labels/{label_id}")

    def attachment(self, message_id, attachment_id):
        """The attachment's bytes (never stored)."""
        return _decode(self.get(f"/messages/{message_id}/attachments/{attachment_id}").get("data", ""))


def _decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)) if data else b""


def _charset(part):
    for h in part.get("headers", []):
        if h["name"].lower() == "content-type" and "charset=" in h["value"].lower():
            return h["value"].lower().split("charset=", 1)[1].split(";")[0].strip().strip('"\'') or "utf-8"
    return "utf-8"


def _walk(part):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk(child)


def parse_api_message(msg):
    """A Gmail API message (format=full) -> ParsedEmail, or None for chat and empty messages."""
    labels = msg.get("labelIds", []) or []
    if SKIPPED & set(labels):
        return None
    payload = msg.get("payload") or {}
    headers = payload.get("headers", [])
    if not headers:
        return None
    # Headers go through the same parser as IMAP's, so addresses, dates and subjects come out identically.
    raw = "\r\n".join(f"{h['name']}: {h['value']}" for h in headers if "\n" not in h["value"])
    email = parse_message(raw + "\r\n\r\n", uid=uid_of(msg["id"]), thread_id=decimal_thread_id(msg.get("threadId", "")),
                          labels=[SYSTEM_LABELS.get(lb, lb) for lb in labels])
    if not any(h["name"].lower() == "date" for h in headers) and msg.get("internalDate"):
        email.occurred_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=dt_timezone.utc)
    plain = html_part = None
    attachments = []
    for part in _walk(payload):
        mime, body = part.get("mimeType", ""), part.get("body") or {}
        if part.get("filename"):
            attachments.append((part["filename"], int(body.get("size") or 0), mime))
            continue
        if not body.get("data"):
            continue
        text = _decode(body["data"])
        try:
            text = text.decode(_charset(part))
        except (LookupError, UnicodeDecodeError):
            text = text.decode("cp1252", errors="replace")
        if mime == "text/plain" and plain is None:
            plain = text
        elif mime == "text/html" and html_part is None:
            html_part = text
        elif mime in ("message/delivery-status", "text/rfc822-headers") and plain is None:
            plain = text
    email.body = finish_text(plain, html_part)
    email.attachments = attachments
    return email


def _since_query(mailbox):
    """Where a fresh read starts: the backfill window for a new mailbox, or a day before the last IMAP sync."""
    if mailbox.backfill_done and mailbox.last_synced_at:
        start = timezone.localdate(mailbox.last_synced_at) - timedelta(days=1)
    else:
        start = since_date(mailbox.backfill_days)
    return f"after:{start:%Y/%m/%d}"


def read_changes(mailbox, client, max_messages):
    """Queue what's new and read up to max_messages of it. Returns (ParsedEmail list, oldest first; the uids of
    messages that are new since the last run, as opposed to a back-read; the ids still waiting; the ids of threads
    that changed, for the inbox index). Updates the
    mailbox's marker and queue (unsaved). The queue keeps everything until the caller has logged the batch and
    sets mailbox.pending_ids to the ids still waiting, so a failure part-way loses nothing."""
    pending = list(mailbox.pending_ids or [])
    new, touched = [], []
    if not mailbox.history_id:
        profile = client.profile()
        if profile.get("emailAddress", "").lower() != mailbox.email.lower():
            raise MailboxError(f"The connected Google account is {profile.get('emailAddress')}, not {mailbox.email}.")
        queued = list(reversed(client.list_ids(_since_query(mailbox))))
        mailbox.history_id = str(profile["historyId"])
    else:
        try:
            queued, mailbox.history_id, touched = client.history(mailbox.history_id)
            new = queued
        except HistoryExpired:
            profile = client.profile()
            queued = list(reversed(client.list_ids(_since_query(mailbox))))
            mailbox.history_id = str(profile["historyId"])
    seen = set(pending)
    pending += [i for i in queued if i not in seen]
    mailbox.pending_ids = pending
    batch, rest = pending[:max_messages], pending[max_messages:]

    def fetch(message_id):
        try:
            return parse_api_message(client.message(message_id))
        except NotFound:  # deleted since it was queued
            return None

    if batch:
        client.prime()  # refresh the access token here, not in the worker threads
    with ThreadPoolExecutor(max_workers=PARALLEL_REQUESTS) as executor:
        emails = [e for e in executor.map(fetch, batch) if e is not None]
    return emails, {uid_of(i) for i in new}, rest, touched
