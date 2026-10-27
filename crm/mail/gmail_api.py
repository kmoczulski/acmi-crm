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
from .parse import CALENDAR_TYPES, finish_text, parse_message

API = "https://gmail.googleapis.com/gmail/v1/users/me"
UPLOAD = "https://gmail.googleapis.com/upload/gmail/v1/users/me"
UPLOAD_TIMEOUT = 120
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


class UnconfirmedSend(MailboxError):
    """Gmail failed on its side while sending: the email may or may not have gone. Never retried."""


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


def _multipart(metadata, raw):
    """A multipart/related upload: JSON metadata, then the message itself."""
    import json
    import uuid

    boundary = f"acmi-{uuid.uuid4().hex}"
    head = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: message/rfc822\r\n\r\n").encode()
    return {"raw": head + raw + f"\r\n--{boundary}--\r\n".encode(),
            "content_type": f"multipart/related; boundary={boundary}"}


RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


def _rate_limited(response):
    """Gmail answers 403 both for "no permission" and for "slow down"; only the reason tells them apart."""
    try:
        errors = (response.json().get("error") or {}).get("errors") or []
    except (ValueError, AttributeError):
        return False
    return any(e.get("reason") in RATE_LIMIT_REASONS for e in errors)


class GmailClient:
    """The Gmail API as the mailbox owner (a person's Google sign-in), or as a shared address with its own token.
    `session` stands in for `requests` in tests."""

    def __init__(self, user=None, session=None, token=None):
        self.session = session or requests
        self.token = token or (gcal.token_for(user) if user is not None else None)
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
        return self._request("get", path, params=params)

    def post(self, path, body=None):
        """A change in Gmail (labels, Trash). Only ever called from a person's own click (crm/mail/actions.py)."""
        return self._request("post", path, body=body or {})

    def _request(self, method, path, params=None, body=None, raw=None, content_type="", resend=True):
        """One Gmail API call, retried when Gmail asks to slow down. resend=False (sending mail) never retries after a
        server error, because the email may already have gone out."""
        refresh = False
        for attempt in range(RETRIES + 1):
            headers = {"Authorization": f"Bearer {self._access(force=refresh)}"}
            if method == "upload":  # a whole message with its attachments (up to Gmail's 35 MB)
                r = self.session.post(f"{UPLOAD}{path}", data=raw, timeout=UPLOAD_TIMEOUT,
                                      headers={**headers, "Content-Type": content_type})
            elif method in ("post", "put", "patch"):
                r = getattr(self.session, method)(f"{API}{path}", json=body, headers=headers, timeout=TIMEOUT)
            elif method == "delete":
                r = self.session.delete(f"{API}{path}", headers=headers, timeout=TIMEOUT)
            else:
                r = self.session.get(f"{API}{path}", params=params, timeout=TIMEOUT, headers=headers)
            refresh = False
            if r.status_code == 401 and attempt == 0:
                refresh = True  # the access token expired early: refresh it once and try again
                continue
            slow_down = r.status_code == 429 or (r.status_code == 403 and _rate_limited(r))
            busy = slow_down or (resend and r.status_code in (500, 502, 503, 504))
            if not resend and r.status_code in (500, 502, 503, 504):
                raise UnconfirmedSend("Gmail had a problem sending. Check your Sent folder before you send it again.")
            if busy and attempt < RETRIES:
                time.sleep(2 * (attempt + 1))  # Gmail's per-second limit, or a hiccup: wait and try again
                continue
            if busy:
                raise MailboxError("Gmail is busy right now. The next sync tries again.")
            if r.status_code in (401, 403):
                raise MailboxError(f"Google refused the hub's Gmail access. {RECONNECT}")
            if r.status_code == 404:
                raise NotFound(path)
            r.raise_for_status()
            return r.json() if r.status_code != 204 and getattr(r, "content", b"x") else {}
        raise MailboxError("Gmail kept refusing requests. The next sync tries again.")

    # Sending (crm/mail/compose.py). Only ever from a person's own click on Send.

    def send(self, raw, thread_id=""):
        """Send an RFC 822 message (bytes) as the person; in a thread when thread_id is given. Returns Gmail's
        {id, threadId, labelIds}."""
        return self._request("upload", "/messages/send?uploadType=multipart", resend=False,
                             **_multipart({"threadId": thread_id} if thread_id else {}, raw))

    def sent_message_id(self, result, fallback=""):
        """The Message-ID Gmail actually sent with (it can replace the one we wrote), so the timeline's key matches
        what the sync will read later."""
        try:
            data = self.get(f"/messages/{result['id']}", {"format": "metadata", "metadataHeaders": ["Message-ID"]})
        except Exception:  # the email went; our own id is the best guess
            return fallback
        for h in (data.get("payload") or {}).get("headers", []):
            if h["name"].lower() == "message-id":
                return h["value"].strip()
        return fallback

    # Drafts, as the news review uses them (crm/news/drafts.py ApiDrafts)

    def drafts(self, limit=100):
        return self.get("/drafts", {"maxResults": limit}).get("drafts", [])

    def draft(self, draft_id):
        return self.get(f"/drafts/{draft_id}", {"format": "full"})

    def trash_message(self, message_id):
        return self.post(f"/messages/{message_id}/trash")

    def save_draft(self, raw, draft_id="", thread_id=""):
        """Create or update a draft (text only: attachments go on when it's sent)."""
        message = {"raw": base64.urlsafe_b64encode(raw).decode()}
        if thread_id:
            message["threadId"] = thread_id
        if draft_id:
            return self._request("put", f"/drafts/{draft_id}", body={"id": draft_id, "message": message})
        return self._request("post", "/drafts", body={"message": message})

    def delete_draft(self, draft_id):
        """Drop a draft once its email is sent or the person discards it (only drafts; never a message)."""
        return self._request("delete", f"/drafts/{draft_id}")

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

    def message_page(self, query, page_token="", size=25):
        """One page of message ids for a Gmail search, newest first, and the next page's token."""
        params = {"q": query, "maxResults": size}
        if page_token:
            params["pageToken"] = page_token
        data = self.get("/messages", params)
        return [m["id"] for m in data.get("messages", [])], data.get("nextPageToken", "")

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

    def create_label(self, name):
        return self.post("/labels", {"name": name, "labelListVisibility": "labelShow",
                                     "messageListVisibility": "show"})

    def update_label(self, label_id, name):
        return self._request("patch", f"/labels/{label_id}", body={"name": name})

    def delete_label(self, label_id):
        """Remove a label: Gmail takes it off its emails; the emails themselves stay."""
        return self._request("delete", f"/labels/{label_id}")

    def modify_thread(self, thread_id, add=(), remove=()):
        return self.post(f"/threads/{thread_id}/modify", {"addLabelIds": list(add), "removeLabelIds": list(remove)})

    def trash_thread(self, thread_id):
        return self.post(f"/threads/{thread_id}/trash")

    def untrash_thread(self, thread_id):
        return self.post(f"/threads/{thread_id}/untrash")

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
    attachments, calendar = [], []
    for part in _walk(payload):
        mime, body = part.get("mimeType", ""), part.get("body") or {}
        if (mime in CALENDAR_TYPES or part.get("filename", "").lower().endswith(".ics")) and body.get("data"):
            ics = _decode(body["data"]).decode(_charset(part), errors="replace")
            if "BEGIN:VEVENT" in ics and ics not in calendar:
                calendar.append(ics)  # an invite (crm/invites.py); an attached .ics with no data inline is skipped
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
    email.calendar = calendar
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


def client_for(mailbox, session=None):
    """The Gmail client for a mailbox: its owner's permission, or a shared mailbox's own."""
    if mailbox.shared:
        token = getattr(mailbox, "shared_token", None)
        if token is None:
            raise MailboxError(f"The shared mailbox {mailbox.email} isn't connected. A Manager connects it on Mail "
                               "in the hub.")
        return GmailClient(session=session, token=token)
    return GmailClient(mailbox.user, session=session)
