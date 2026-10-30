"""The Google Chat API as the signed-in person, for Chat in the hub (docs/CHAT.md).

Everything goes through that person's own Google permission, so a space, member or message that isn't theirs is simply
not found at Google. `session` stands in for `requests` in tests. Words in ChatError are for the person to read.
"""
import logging
import time
import uuid
from datetime import timedelta

import requests
from django.utils import timezone

from crm import gcal

API ="https://chat.googleapis.com/v1"
TIMEOUT = 30
RETRIES = 2
RECONNECT = "Turn Chat off and on again in the hub."
# How Google spells "newest first" for orderBy: its page says ASC / DESC, the usual Google form is "createTime desc". The
# first spelling it accepts is kept, so only the very first call can ever be refused.
ORDER_SPELLINGS = ("createTime desc", "DESC", "createTime DESC")
ORDER_DESC = list(ORDER_SPELLINGS)

log = logging.getLogger(__name__)


class ChatError(Exception):
    """Chat said no, or couldn't be reached. `status` is Google's HTTP status when it answered, else 0."""

    def __init__(self, message="", status=0):
        super().__init__(message)
        self.status = status


class ChatNotFound(ChatError):
    """That space or message isn't there for this person."""


def space_name(space_id):
    """spaces/AAAA from AAAA or spaces/AAAA; "" for anything that isn't a space id (nothing odd reaches a URL)."""
    space_id = str(space_id or "").strip()
    if space_id.startswith("spaces/"):
        space_id = space_id[len("spaces/"):]
    return f"spaces/{space_id}" if space_id and all(c.isalnum() or c in "-_" for c in space_id) and len(space_id) <= 64 else ""


def _explain(response):
    """What went wrong in plain words, from Google's error answer."""
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    message = str(error.get("message", ""))
    reasons = {d.get("reason") for d in error.get("details", []) if isinstance(d, dict)}
    low = message.lower()
    if "SERVICE_DISABLED" in reasons or "has not been used in project" in low or "api is disabled" in low:
        return ("Google Chat isn't switched on for the hub's Google project yet. Ask Mel to turn on the Google Chat "
                "API (docs/FOR_MEL_GOOGLE_CHAT.md).")
    if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reasons or "insufficient authentication scopes" in low or "insufficient scope" in low:
        return f"The hub doesn't have the Chat permission yet. {RECONNECT}"
    if "chat app not found" in low or "configure the app" in low:
        return ("Google needs the Chat API's Configuration page filled in (app name and icon) before the hub can post. "
                "Ask Mel (docs/FOR_MEL_GOOGLE_CHAT.md).")
    if response.status_code in (401, 403):
        return f"Google refused the hub's Chat access. {RECONNECT}"
    return message or "Google Chat didn't accept that."


class ChatClient:
    """The Chat API as one person."""

    def __init__(self, user=None, session=None, token=None):
        self.session = session or requests
        self.token = token or (gcal.token_for(user) if user is not None else None)
        if self.token is None:
            raise ChatError(f"Google isn't connected. {RECONNECT}")
        self.owner = getattr(user, "pk", None) or getattr(self.token.account, "user_id", 0)  # keys the caches per person

    def _access(self, force=False):
        if force:
            self.token.expires_at = timezone.now() - timedelta(minutes=1)  # counts as expired: refresh it
        try:
            return gcal.access_token(self.token, self.session)
        except gcal.CalendarError as exc:
            raise ChatError(f"Google didn't accept the hub's sign-in. {RECONNECT}") from exc

    def prime(self):
        """Refresh the access token here, before worker threads share the client."""
        self._access()

    def _request(self, method, path, params=None, body=None):
        refresh = False
        for attempt in range(RETRIES + 1):
            headers = {"Authorization": f"Bearer {self._access(force=refresh)}"}
            if method == "get":
                r = self.session.get(f"{API}{path}", params=params, headers=headers, timeout=TIMEOUT)
            else:
                r = getattr(self.session, method)(f"{API}{path}", params=params, json=body, headers=headers,
                                                  timeout=TIMEOUT)
            refresh = False
            if r.status_code == 401 and attempt == 0:
                refresh = True  # the access token expired early: refresh it once and try again
                continue
            busy = r.status_code == 429 or r.status_code in (500, 502, 503, 504)
            if busy and attempt < RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            if busy:
                raise ChatError("Google Chat is busy right now. Try again in a moment.", r.status_code)
            if r.status_code == 404 and "app not found" not in str(getattr(r, "text", "")).lower():
                raise ChatNotFound(path, 404)
            if r.status_code >= 400:
                raise ChatError(_explain(r), r.status_code)
            return r.json() if r.status_code != 204 and getattr(r, "content", b"x") else {}
        raise ChatError("Google Chat kept refusing requests. Try again in a moment.")

    def _pages(self, path, key, params, limit=20):
        out, params = [], dict(params)
        for _ in range(limit):
            data = self._request("get", path, params=params)
            out += data.get(key, [])
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        return out

    # Reading

    def spaces(self):
        """Every space the person is in: named spaces, group chats and direct messages (those appear once somebody has
        written in them)."""
        return self._pages("/spaces", "spaces", {"pageSize": 200})

    def get_space(self, space):
        return self._request("get", f"/{space}")

    def members(self, space):
        return self._pages(f"/{space}/members", "memberships", {"pageSize": 200})

    def messages(self, space, *, after=None, before=None, limit=50):
        """The newest `limit` messages, or the ones after / before a createTime stamp; oldest first."""
        params = {"pageSize": max(1, min(int(limit), 1000))}
        if after:  # Chat's own order for a filter on createTime is oldest first
            params["filter"] = f'createTime > "{after}"'
            return self._request("get", f"/{space}/messages", params=params).get("messages", [])
        if before:
            params["filter"] = f'createTime < "{before}"'
        for i, order in enumerate(ORDER_DESC):
            try:
                data = self._request("get", f"/{space}/messages", params={**params, "orderBy": order})
            except ChatError as exc:
                if exc.status != 400 or i == len(ORDER_DESC) - 1:  # not about the order, or the last spelling
                    raise
                continue
            if i:
                ORDER_DESC.insert(0, ORDER_DESC.pop(i))  # remember the spelling that worked
            return list(reversed(data.get("messages", [])))

    def read_state(self, space):
        """{lastReadTime: ...} for this person in that space ({} if Chat has none yet)."""
        try:
            return self._request("get", f"/users/me/{space}/spaceReadState")
        except ChatNotFound:
            return {}

    # Changing: only ever from the person's own click (sending, opening a conversation)

    def send(self, space, text, thread=None):
        """Post as the person. thread: the name of a thread to answer in (falls back to a new thread)."""
        body = {"text": text}
        params = {"requestId": str(uuid.uuid4())}  # a repeat of the same request never posts twice
        if thread:
            body["thread"] = {"name": thread}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        return self._request("post", f"/{space}/messages", params=params, body=body)

    def mark_read(self, space, when):
        """Say the person has read everything up to `when` (an RFC 3339 time later than the newest message)."""
        return self._request("patch", f"/users/me/{space}/spaceReadState", params={"updateMask": "lastReadTime"},
                             body={"lastReadTime": when})
