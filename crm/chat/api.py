"""The Google Chat API as the signed-in person, for Chat in the hub (docs/CHAT.md).

Everything goes through that person's own Google permission, so a space, member or message that isn't theirs is simply
not found at Google. `session` stands in for `requests` in tests. Words in ChatError are for the person to read.
"""
import json
import logging
import re
import time
import uuid
from datetime import timedelta

import requests
from django.utils import timezone

from crm import gcal

API = "https://chat.googleapis.com/v1"
UPLOAD = "https://chat.googleapis.com/upload/v1"
TIMEOUT = 30
UPLOAD_TIMEOUT = 300
RETRIES = 2
RECONNECT = "Turn Chat off and on again in the hub."
# How Google spells "newest first" for orderBy: its page now says ASC / DESC, the older form is "createTime desc". The
# first spelling it accepts is kept, so only the very first call can ever be refused.
ORDER_SPELLINGS = ("DESC", "createTime desc", "createTime DESC")
ORDER_DESC = list(ORDER_SPELLINGS)
# Google reads the text we send in Chat's own syntax (*bold*, _italic_, <users/ID>) only when told to: a project made
# lately defaults to Markdown. Sent with everything we write and asked of everything we read.
MARKUP = "MARKUP_SYNTAX_CHAT"
MESSAGE = re.compile(r"^spaces/[\w-]{1,64}/messages/[\w.-]{1,128}$")
REACTION = re.compile(r"^spaces/[\w-]{1,64}/messages/[\w.-]{1,128}/reactions/[\w.-]{1,128}$")
MEDIA = re.compile(r"^(?!.*\.\.)[\w./=+~-]{1,1500}$")  # an opaque name from Chat, never a path to somewhere else
USER = re.compile(r"^users/[\w.@+-]{1,128}$")

log = logging.getLogger(__name__)


class ChatError(Exception):
    """Chat said no, or couldn't be reached. `status` is Google's HTTP status when it answered, else 0."""

    def __init__(self, message="", status=0):
        super().__init__(message)
        self.status = status


class ChatNotFound(ChatError):
    """That space or message isn't there for this person."""


class BadRequest(ChatError):
    """What was asked isn't something Chat can do (a bad address, a message that isn't in the conversation): said as it is,
    not blamed on Google."""


def space_name(space_id):
    """spaces/AAAA from AAAA or spaces/AAAA; "" for anything that isn't a space id (nothing odd reaches a URL)."""
    space_id = str(space_id or "").strip()
    if space_id.startswith("spaces/"):
        space_id = space_id[len("spaces/"):]
    return f"spaces/{space_id}" if space_id and all(c.isalnum() or c in "-_" for c in space_id) and len(space_id) <= 64 else ""


def message_name(value):
    """spaces/AAAA/messages/BBBB when value is one, else "" (nothing odd reaches a URL)."""
    value = str(value or "").strip()
    return value if MESSAGE.match(value) else ""


def reaction_name(value):
    value = str(value or "").strip()
    return value if REACTION.match(value) else ""


def media_name(value):
    value = str(value or "").strip()
    return value if MEDIA.match(value) else ""


def emoji_ok(value):
    """One emoji (or one emoji sequence) and nothing that could change a filter or a URL."""
    value = str(value or "")
    return bool(value) and len(value) <= 16 and not any(c in '"\\' or ord(c) < 32 for c in value)


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

    def _request(self, method, path, params=None, body=None, *, base=API, data=None, headers=None, timeout=TIMEOUT,
                 stream=False):
        """One call. body is sent as JSON, data as it is (an upload); stream returns the response itself (a download)."""
        refresh = False
        for attempt in range(RETRIES + 1):
            sent = {"Authorization": f"Bearer {self._access(force=refresh)}", **(headers or {})}
            if method == "get":
                extra = {"stream": True} if stream else {}
                r = self.session.get(f"{base}{path}", params=params, headers=sent, timeout=timeout, **extra)
            elif data is not None:
                r = getattr(self.session, method)(f"{base}{path}", params=params, data=data, headers=sent, timeout=timeout)
            else:
                r = getattr(self.session, method)(f"{base}{path}", params=params, json=body, headers=sent, timeout=timeout)
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
            if stream:
                return r
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

    def messages(self, space, *, after=None, before=None, limit=50, deleted=False):
        """The newest `limit` messages, or the ones after / before a createTime stamp; oldest first. deleted: also the
        ones taken back (they come with a deleteTime and no content), so a conversation can say "message deleted"."""
        params = {"pageSize": max(1, min(int(limit), 1000)), "markupSyntax": MARKUP}
        if deleted:
            params["showDeleted"] = "true"
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

    def get_message(self, name):
        return self._request("get", f"/{name}")

    def read_state(self, space):
        """{lastReadTime: ...} for this person in that space ({} if Chat has none yet)."""
        try:
            return self._request("get", f"/users/me/{space}/spaceReadState")
        except ChatNotFound:
            return {}

    # Changing: only ever from the person's own click (sending, opening a conversation)

    def send(self, space, text, thread=None, quote=None, attachments=None):
        """Post as the person. thread: the name of a thread to answer in (falls back to a new thread). quote: {"name",
        "lastUpdateTime"} of the message this one quotes. attachments: upload tokens from upload()."""
        body = {"text": text, "markupSyntax": MARKUP}
        params = {"requestId": str(uuid.uuid4())}  # a repeat of the same request never posts twice
        if thread:
            body["thread"] = {"name": thread}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        if quote:
            body["quotedMessageMetadata"] = {"name": quote["name"], "lastUpdateTime": quote["lastUpdateTime"]}
        if attachments:
            body["attachment"] = [{"attachmentDataRef": {"attachmentUploadToken": token}} for token in attachments]
        try:
            return self._request("post", f"/{space}/messages", params=params, body=body)
        except ChatError as exc:
            if exc.status != 400 or "markup" not in str(exc).lower():
                raise
            del body["markupSyntax"]  # a Google that doesn't take the setting: Chat's syntax is the usual default
            return self._request("post", f"/{space}/messages", params=params, body=body)

    def update_message(self, name, text):
        """Change the text of one of the person's own messages."""
        return self._request("patch", f"/{name}", params={"updateMask": "text"}, body={"text": text, "markupSyntax": MARKUP})

    def delete_message(self, name, force=False):
        """Take back one of the person's own messages; force also deletes the replies in its thread."""
        return self._request("delete", f"/{name}", params={"force": "true"} if force else None)

    # Reactions

    def reactions(self, message, *, emoji=None, user=None):
        """The reactions on a message (who gave what), optionally only one emoji and / or one person's."""
        parts = []
        if emoji:
            parts.append(f'emoji.unicode = "{emoji}"')
        if user:
            parts.append(f'user.name = "{user}"')
        params = {"pageSize": 200}
        if parts:
            params["filter"] = " AND ".join(parts)
        return self._pages(f"/{message}/reactions", "reactions", params)

    def react(self, message, emoji):
        return self._request("post", f"/{message}/reactions", body={"emoji": {"unicode": emoji}})

    def unreact(self, name):
        return self._request("delete", f"/{name}")

    # Files

    def upload(self, space, filename, content_type, data):
        """Upload a file to a space so a message can carry it: returns Chat's reference to it, which holds the token to
        put on the message (attachmentUploadToken)."""
        boundary = uuid.uuid4().hex
        head = json.dumps({"filename": filename}).encode()
        body = b"".join([
            b"--", boundary.encode(), b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n", head,
            b"\r\n--", boundary.encode(), b"\r\nContent-Type: ", (content_type or "application/octet-stream").encode(),
            b"\r\n\r\n", data, b"\r\n--", boundary.encode(), b"--\r\n"])
        answer = self._request("post", f"/{space}/attachments:upload", params={"uploadType": "multipart"}, data=body,
                               base=UPLOAD, timeout=UPLOAD_TIMEOUT,
                               headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return answer.get("attachmentDataRef") or {}

    def download(self, resource_name):
        """The response (to stream) for a file uploaded to Chat."""
        return self._request("get", f"/media/{resource_name}", params={"alt": "media"}, stream=True)

    # Starting conversations

    def find_dm(self, email):
        """The direct message with this person, or None if there isn't one yet."""
        try:
            return self._request("get", "/spaces:findDirectMessage", params={"name": f"users/{email}"})
        except ChatNotFound:
            return None

    def set_up(self, kind, emails, display_name=""):
        """A direct message, a group chat or a named space with these people (the person is added by Chat)."""
        space = {"spaceType": kind}
        if kind == "SPACE":
            space["displayName"] = display_name
        if kind == "DIRECT_MESSAGE":
            space["singleUserBotDm"] = False
        body = {"space": space, "requestId": str(uuid.uuid4()),
                "memberships": [{"member": {"name": f"users/{e}", "type": "HUMAN"}} for e in emails]}
        return self._request("post", "/spaces:setup", body=body)

    # Searching

    def search(self, query, *, limit=25, page_token=None):
        """Messages in every conversation the person is in that match some words (Chat's own search)."""
        body = {"filter": query, "pageSize": max(1, min(int(limit), 100)), "orderBy": "createTime desc", "markupSyntax": MARKUP}
        if page_token:
            body["pageToken"] = page_token
        return self._request("post", "/spaces/-/messages:search", body=body)

    def mark_read(self, space, when):
        """Say the person has read everything up to `when` (an RFC 3339 time later than the newest message)."""
        return self._request("patch", f"/users/me/{space}/spaceReadState", params={"updateMask": "lastReadTime"},
                             body={"lastReadTime": when})
