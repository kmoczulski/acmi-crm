"""Team notes on an email thread (docs/EMAIL_CLIENT.md, "Comments and assign"; step 6).

"Simon, can you take the survey questions?": a note on the thread with @mentions, using the hub's mention rules and
bell. Only its author and the people it mentions see it, each in their own mailbox: notes are matched to a thread
by the emails' Message-IDs, because the same conversation has a different Gmail id in every mailbox.
"""
from django.db.models import Q
from django.urls import reverse

from crm import notify
from crm.models import MailComment, MailCommentKey, Notification

from .gmail_api import parse_api_message


def message_ids(thread):
    out = []
    for raw in thread.get("messages", []):
        email = parse_api_message(raw)
        if email and email.message_id and not email.message_id.startswith("<uid-"):
            out.append(email.message_id)
    return out


def for_thread(user, thread):
    """The notes this person may see on this thread, oldest first."""
    ids = message_ids(thread)
    if not ids:
        return []
    keyed = MailCommentKey.objects.filter(message_id__in=ids).values("comment")
    return list(MailComment.objects.filter(pk__in=keyed).filter(Q(author=user) | Q(mentions=user)).distinct()
                .select_related("author").prefetch_related("mentions"))


def add(user, thread, subject, body):
    """Save a note and tell the people it mentions. Returns the MailComment."""
    body = (body or "").strip()[:4000]
    if not body:
        return None
    ids = message_ids(thread)
    comment = MailComment.objects.create(author=user, body=body, subject=subject[:500])
    MailCommentKey.objects.bulk_create([MailCommentKey(comment=comment, message_id=m[:500]) for m in ids])
    mentioned = notify.mentioned_users(body) - {user}
    comment.mentions.set(mentioned)
    url = f"{reverse('crm:inbox_open')}?m={ids[-1]}" if ids else reverse("crm:inbox")
    name = user.get_full_name() or user.username
    for person in mentioned:
        notify.send(person, Notification.Kind.MENTION, f"{name} on “{subject}”: {body[:160]}", url, actor=user,
                    key=f"mail-comment:{comment.pk}")
    return comment
