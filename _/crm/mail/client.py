"""Mail in the hub: the email client's side of each refresh (docs/EMAIL_CLIENT.md).

on_new_mail is the one place new mail raises an alert (section 7): the Gmail API reader calls it with the messages
that arrived since its last run, never with a back-read, and crm.notify.new_mail decides per person whether that
shows in the hub, the browser or neither.
"""
from datetime import timedelta

from django.utils import timezone

from . import connect
from .gmail_api import hex_thread_id

# Only mail that is actually new: a sync that was off for days doesn't replay it as alerts.
RECENT = timedelta(days=2)
# Gmail's other tabs; Primary mail (no category, or CATEGORY_PERSONAL) alerts.
QUIET_CATEGORIES = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS")
QUIET_LABELS = ("\\Draft", "\\Spam", "\\Trash", "\\Sent")


def thread_url(thread_id, mailbox=None):
    """The thread in the hub's inbox (switching to the shared mailbox it's in)."""
    from django.urls import reverse

    mb = f"mb={mailbox.pk}&" if mailbox is not None and mailbox.shared else ""
    return f"{reverse('crm:inbox')}?{mb}t={hex_thread_id(thread_id)}"


def wants_alert(mailbox, email, now=None):
    now = now or timezone.now()
    labels = set(email.labels)
    return ("\\Inbox" in labels and not labels & set(QUIET_LABELS + QUIET_CATEGORIES)
            and email.from_addr != mailbox.email.lower() and email.occurred_at >= now - RECENT)


def on_new_mail(mailbox, emails):
    """New messages from a refresh pass: one alert per new Primary inbox message, for the mailbox's owner while their
    Mail is on, or for everyone given a shared mailbox. Returns how many were sent on to crm.notify."""
    from crm import notify

    from . import shared

    if mailbox.shared:
        people = shared.recipients(mailbox)
    else:
        people = [mailbox.user] if connect.is_on(mailbox.user) else []
    n = 0
    for email in emails:
        if not wants_alert(mailbox, email):
            continue
        sender = email.from_name or email.from_addr
        subject = f"[{mailbox.label}] {email.subject}" if mailbox.shared else email.subject
        for person in people:
            notify.new_mail(person, sender, subject, thread_url(email.thread_id, mailbox), email.message_id)
            n += 1
    return n
