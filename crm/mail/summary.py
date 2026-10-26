"""Summarize a long thread with Claude (docs/EMAIL_CLIENT.md, "Ideas taken from other email clients"; step 3b).

Only when the person clicks Summarize: the thread's own words (quoted history and signatures left out, as on the
timelines), numbered oldest first, go to Claude, which says where things stand, who owes what, and the latest
version of each document, citing message numbers. The answer is cached in memory for an hour (keyed on the thread's
Gmail historyId, so new mail asks again), never stored. Needs ANTHROPIC_API_KEY, like crm/ai.py.
"""
import json
import logging

from django.core.cache import cache

from ..ai import enabled
from .gmail_api import parse_api_message
from .parse import new_text

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MESSAGE_CHARS = 3000
TOTAL_CHARS = 200_000
CACHE_SECONDS = 3600

SYSTEM = """You read email threads for ACMI Properties, which assembles land and develops purpose-built \
manufacturing campuses for defense, aerospace and energetics companies. The team uses you to catch up on long \
threads (lawyers, brokers, lenders, tenants, consultants) without reading every message.

The messages are numbered oldest first, each with who sent it, when, its attachments and its new text (quoted \
history left out). Say:
- where the thread stands now, in two or three sentences;
- what is still open: who owes whom what, and since which message;
- the latest version of each document that was passed around, and which message has it;
- the next step, if one is clear.
Cite message numbers for every point. Use names as they appear. Be brief and plain; don't guess at anything the \
messages don't say."""

SCHEMA = {
    "type": "object",
    "properties": {
        "where_it_stands": {"type": "string"},
        "open_items": {"type": "array", "items": {"type": "object", "properties": {
            "who": {"type": "string"}, "what": {"type": "string"}, "message": {"type": "integer"}},
            "required": ["who", "what", "message"], "additionalProperties": False}},
        "documents": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "message": {"type": "integer"}, "note": {"type": "string"}},
            "required": ["name", "message", "note"], "additionalProperties": False}},
        "next_step": {"type": "string"},
    },
    "required": ["where_it_stands", "open_items", "documents", "next_step"],
    "additionalProperties": False,
}


class Unavailable(Exception):
    pass


def thread_text(thread):
    """The thread as Claude reads it, and the Gmail message ids in the order they're numbered."""
    msgs = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate") or 0))
    lines, ids, total = [], [], 0
    from . import render

    for raw in msgs:
        email = parse_api_message(raw)
        if email is None or "\\Draft" in email.labels:
            continue
        n = len(ids) + 1
        files = ", ".join(a["name"] for a in render.attachments(raw)) or "none"
        text = new_text(email.body).strip()[:MESSAGE_CHARS]
        block = (f"[{n}] From {email.from_name or email.from_addr} <{email.from_addr}>, "
                 f"{email.occurred_at:%b %d %Y %H:%M}\nAttachments: {files}\n{text}\n")
        total += len(block)
        if total > TOTAL_CHARS:
            lines.append(f"[{n}…] (the remaining messages are left out: the thread is too long)")
            break
        lines.append(block)
        ids.append(raw["id"])
    return "\n".join(lines), ids


def summarize(mailbox, thread):
    key = f"mail-summary:{mailbox.pk}:{thread.get('id')}:{thread.get('historyId')}"
    cached = cache.get(key)
    if cached:
        return cached
    if not enabled():
        raise Unavailable("Add ANTHROPIC_API_KEY to the .env file to turn on Claude.")
    import anthropic

    text, ids = thread_text(thread)
    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=MODEL, max_tokens=4000, system=SYSTEM,
            messages=[{"role": "user", "content": text}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            # If a safety classifier declines, the API retries on a fallback model in the same call.
            betas=[FALLBACK_BETA], fallbacks="default",
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise Unavailable("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.") from exc
    except anthropic.RateLimitError as exc:
        raise Unavailable("Anthropic is rate-limiting requests. Try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        log.warning("Thread summary failed: %s", exc)
        raise Unavailable(f"Anthropic API error ({exc.status_code}). Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise Unavailable("Could not reach Anthropic. Check the internet connection.") from exc
    if message.stop_reason == "refusal":
        raise Unavailable("Claude declined to summarize this thread.")
    if message.stop_reason == "max_tokens":
        raise Unavailable("The summary was cut off. Try again.")
    try:
        data = json.loads(next(b.text for b in message.content if getattr(b, "type", "") == "text"))
    except (StopIteration, ValueError) as exc:
        raise Unavailable("Claude's summary couldn't be read. Try again.") from exc
    data["message_ids"] = ids  # message number n is ids[n - 1], for the links
    cache.set(key, data, CACHE_SECONDS)
    return data
