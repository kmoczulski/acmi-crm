"""Sending the hub's own emails (outreach batches and test sends, the weekly outreach report, campus updates) from a
person's Gmail (docs/EMAIL_CLIENT.md, step 8).

Through the Gmail API for anyone whose Mail in the hub is on (and for shared mailboxes); over SMTP with the old
app password otherwise. Once everyone who sends has Mail on, the app password can go. Both look the same to the
callers: `with outbound(mailbox) as conn: conn.send(msg)`, where msg is an email.message.EmailMessage.
"""
from . import connect
from .gmail_api import UnconfirmedSend, client_for
from .imap import MailboxError


def uses_api(mailbox):
    """Send through the Gmail API: the owner's Mail is on (for their own address), or it's a shared mailbox."""
    if mailbox.shared:
        return True
    if not mailbox.user_id or not connect.is_on(mailbox.user):
        return False
    conn = connect.connection(mailbox.user)
    own = {(mailbox.user.email or "").lower(), (conn.google_email if conn else "").lower()}
    return mailbox.email.lower() in own


class GmailOutbound:
    """The Gmail API with the same shape as outreach's Smtp: a context manager whose send(msg) sends one email."""

    def __init__(self, mailbox, session=None):
        self.mailbox, self.session, self.client = mailbox, session, None

    def __enter__(self):
        from crm.outreach.sender import SendError

        try:
            self.client = client_for(self.mailbox, session=self.session)
        except MailboxError as exc:
            raise SendError(str(exc)) from exc
        return self

    def send(self, msg):
        """Send it; afterwards msg["Message-ID"] is the one Gmail sent with. UnconfirmedSend when Gmail failed on
        its side (it may have gone), MailboxError when the permission is gone."""
        result = self.client.send(msg.as_bytes())
        actual = self.client.sent_message_id(result, fallback=msg["Message-ID"])
        if actual and actual != msg["Message-ID"]:
            del msg["Message-ID"]
            msg["Message-ID"] = actual
        return result

    def __exit__(self, *exc):
        return False


def outbound(mailbox, session=None):
    from crm.outreach.sender import Smtp

    return GmailOutbound(mailbox, session=session) if uses_api(mailbox) else Smtp(mailbox)


__all__ = ["GmailOutbound", "UnconfirmedSend", "outbound", "uses_api"]
