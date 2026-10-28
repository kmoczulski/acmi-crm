"""Gmail -> timeline. Spec sections 2 and 6.

For every message in a mailbox:
  1. classify it (sent / received / reply / bounce / auto-reply) per counterparty,
  2. drop internal and protected addresses,
  3. match the address to a Person, or to a Company by email domain (creating the
     Person), or else park it in the review queue (UnmatchedEmail),
  4. write one Activity per company (or per person without a company). Activities
     are keyed on the RFC Message-ID, so the same email seen in two teammates'
     mailboxes is logged once,
  5. update interaction fields and advance early company stages.
"""
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from crm.choices import Group
from crm import interactions
from crm.models import (
    Activity, Attachment, Company, CompanyStatus, EmailTemplate, MailFilter, MailThread, Mailbox, NewsDraft, Person,
    UnmatchedEmail, normalize_key, suppress_history,
)

from .attachments import categorize
from .imap import ImapClient, MailboxError, since_date
from crm.names import clean_name, name_from_address, needs_name

from .parse import NOT_INTERESTED_PHRASES, classify, new_text
from . import signature_phones

log = logging.getLogger(__name__)

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "me.com",
    "msn.com", "live.com", "comcast.net", "att.net", "verizon.net", "protonmail.com", "proton.me", "ymail.com",
}
DEFAULT_MAX_MESSAGES = 500
EARLY_STAGES = Group("CompanyStatus", [CompanyStatus.PROSPECT, CompanyStatus.CONTACTED])
POSITIVE = ("Responded",)


@dataclass
class EmailEvent:
    """Everything needed to log one counterparty on one email."""

    mailbox: Mailbox
    message_id: str
    thread_id: str
    occurred_at: datetime
    subject: str
    body: str
    kind: str
    outcome: str
    address: str
    name: str
    sender: str = ""  # who sent it, for outbound mail (may be a teammate other than the mailbox owner)
    attachments: tuple = ()  # [(filename, approx bytes, mime type)]


def squash(text):
    """Lower-case letters and digits only: "Firehawk Aerospace, Inc." -> "firehawkaerospaceinc"."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def domain_of(address):
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


class Matcher:
    """Address -> (person, company), with filters loaded once per run."""

    def __init__(self):
        filters = dict(MailFilter.objects.values_list("pattern", "kind"))
        self.protected = {p for p, k in filters.items() if k == MailFilter.Kind.PROTECT}
        self.ignored = {p for p, k in filters.items() if k == MailFilter.Kind.IGNORE}
        self.internal = {d.lower() for d in settings.INTERNAL_EMAIL_DOMAINS}
        self._company_by_domain = None
        from crm.directory.logic import firm_domains

        # The directory's domains (lenders, consultants, vendors): their mail is filed on the firm, never queued.
        self.directory = firm_domains()

    def is_protected(self, address):
        return address in self.protected or domain_of(address) in self.protected

    def is_internal(self, address):
        domain = domain_of(address)
        return domain in self.internal or any(domain.endswith("." + d) for d in self.internal)

    def company_for_domain(self, domain):
        """The company that owns this domain, or one whose name the domain spells exactly."""
        if self._company_by_domain is None:
            self._company_by_domain = {}
            self._company_by_squashed = {}
            for pk, name, domains in Company.objects.values_list("pk", "name", "domains"):
                for d in domains or []:
                    self._company_by_domain.setdefault(d.lower(), pk)
                self._company_by_squashed.setdefault(squash(name), pk)
        pk = self._company_by_domain.get(domain)
        if pk is None and domain not in self.directory:  # a directory firm's domain never becomes a company's
            # firehawkaerospace.com -> "Firehawk Aerospace". Exact match only, so no guessing.
            pk = self._company_by_squashed.get(squash(domain.split(".")[0]))
            if pk is not None:
                company = Company.objects.get(pk=pk)
                if domain not in company.domains:
                    company.domains = [*company.domains, domain]
                    Company.objects.filter(pk=pk).update(domains=company.domains)
                self._company_by_domain[domain] = pk
                return company
        return Company.objects.get(pk=pk) if pk else None

    def find_person(self, address):
        person = Person.objects.select_related("company").filter(email=address).first()
        if person is None:
            person = Person.objects.select_related("company").filter(other_emails__icontains=f'"{address}"').first()
        return person


class Recorder:
    """Writes activities for EmailEvents and remembers what to recompute."""

    def __init__(self, matcher=None):
        self.matcher = matcher or Matcher()
        self.stats = Counter()
        self.company_ids = set()
        self.person_ids = set()
        self.stage_updates = {}  # company_id -> strongest event seen this run
        self.thread_ids = set()  # threads to (re)tag as project / prospecting
        self._team_numbers = None  # ACMI's own phone numbers, never put on a contact (loaded on first use)

    def teammate(self, address):
        if not hasattr(self, "_teammates"):
            from django.contrib.auth import get_user_model

            from ..permissions import PARTNERS

            self._teammates = {u.email.lower(): u for u in get_user_model().objects.exclude(email="")
                               .exclude(groups__name=PARTNERS)}
            self._teammates.update({m.email.lower(): m.user for m in Mailbox.objects.select_related("user")})
        return self._teammates.get((address or "").lower())

    def resolve(self, event, allow_new_company_people=True):
        """(person, company) for the event, or (None, None) when it should not be logged."""
        address = event.address
        person = self.matcher.find_person(address)
        if person:
            if needs_name(person) and (parts := clean_name(event.name, address)):
                person.first_name, person.last_name, person.full_name = parts
                Person.objects.filter(pk=person.pk).update(
                    first_name=person.first_name, last_name=person.last_name, full_name=person.full_name
                )
                self.stats["names filled in"] += 1
            return person, person.company
        domain = domain_of(address)
        if domain in FREE_MAIL:
            return None, None
        company = self.matcher.company_for_domain(domain)
        if company and allow_new_company_people:
            first, last, full = clean_name(event.name, address) or name_from_address(address) or ("", "", "")
            person = Person.objects.create(
                email=address, first_name=first, last_name=last, full_name=full, company=company,
            )
            self.stats["people created"] += 1
            return person, company
        return None, None

    def record(self, event, person, company):
        if company:
            key = f"gmail:{event.message_id}|c{company.pk}"
        else:
            key = f"gmail:{event.message_id}|p{person.pk}"
        sent = event.kind == Activity.Kind.EMAIL_SENT
        verb = {
            Activity.Kind.EMAIL_SENT: "Email to",
            Activity.Kind.EMAIL_RECEIVED: "Email from",
            Activity.Kind.REPLY: "Reply from",
            Activity.Kind.BOUNCE: "Bounced:",
            Activity.Kind.AUTO_REPLY: "Auto-reply from",
        }.get(event.kind, "Email")
        who = person.full_name or person.email if person else event.address
        activity, created = Activity.objects.get_or_create(
            source=Activity.Source.GMAIL,
            source_id=key[:255],
            defaults={
                "occurred_at": event.occurred_at,
                "kind": event.kind,
                "actor": (self.teammate(event.sender) or event.mailbox.user) if sent and event.mailbox else None,
                "company": company,
                "person": person,
                "is_background": event.kind == Activity.Kind.AUTO_REPLY,
                "summary": f"{verb} {who}: {event.subject or '(no subject)'}"[:500],
                "body": event.body,
                "outcome": event.outcome,
                "message_id": event.message_id,
                "thread_id": event.thread_id,
            },
        )
        self.stats["activities created" if created else "activities already logged"] += 1
        if created and activity.thread_id:
            self.thread_ids.add(activity.thread_id)
        if event.attachments:
            save_attachments(activity, event.attachments)
        if event.mailbox is not None and event.mailbox.user_id:  # a shared mailbox has no one person
            activity.team_members.add(event.mailbox.user)
            if not created:
                # Seen again in a teammate's mailbox: their connection counts too.
                self.company_ids.update([activity.company_id] if activity.company_id else [])
                self.person_ids.update([activity.person_id] if activity.person_id else [])
        if not created:
            return
        if person:
            self.person_ids.add(person.pk)
            if event.kind == Activity.Kind.BOUNCE and person.email == event.address and person.email_valid is not False:
                Person.objects.filter(pk=person.pk).update(email_valid=False)
            if (event.kind in (Activity.Kind.EMAIL_RECEIVED, Activity.Kind.REPLY) and event.body
                    and event.address in [person.email, *(person.other_emails or [])]):
                # Phone numbers in the sender's own signature go on their record (added, never replacing).
                if self._team_numbers is None:
                    self._team_numbers = signature_phones.team_numbers()
                if signature_phones.learn(person, event.body, self._team_numbers):
                    self.stats["phones from signatures"] += 1
        if company:
            self.company_ids.add(company.pk)
            self._note_stage(company, event)

    def _note_stage(self, company, event):
        if company.status not in EARLY_STAGES:
            return
        if event.kind == Activity.Kind.REPLY and event.outcome in POSITIVE:
            target = CompanyStatus.ENGAGED
        elif event.kind == Activity.Kind.REPLY and event.outcome == "Not Interested":
            target = CompanyStatus.NOT_INTERESTED
        elif event.kind == Activity.Kind.EMAIL_SENT and company.status == CompanyStatus.PROSPECT:
            target = CompanyStatus.CONTACTED
        else:
            return
        rank = {CompanyStatus.CONTACTED: 1, CompanyStatus.NOT_INTERESTED: 2, CompanyStatus.ENGAGED: 3}
        current = self.stage_updates.get(company.pk)
        if current is None or rank[target] > rank[current]:
            self.stage_updates[company.pk] = target

    def finish(self):
        """Recompute interaction fields and apply stage changes (with history)."""
        interactions.recompute(company_ids=self.company_ids, person_ids=self.person_ids)
        if self.thread_ids:
            from crm import projects

            projects.tag(thread_ids=self.thread_ids)
        for company in Company.objects.filter(pk__in=self.stage_updates):
            if company.status in EARLY_STAGES and company.status != self.stage_updates[company.pk]:
                company.status = self.stage_updates[company.pk]
                company.save()  # TrackedModel writes FieldHistory + a stage-change timeline event
                self.stats["company stages advanced"] += 1


def save_attachments(activity, attachments):
    Attachment.objects.bulk_create(
        [Attachment(activity=activity, filename=name[:300], size=min(size, 2**31 - 1), content_type=ctype[:120],
                    category=categorize(name)) for name, size, ctype in attachments],
        ignore_conflicts=True,
    )


def _reply_subjects():
    subjects = set(EmailTemplate.objects.values_list("subject", flat=True))
    subjects |= set(NewsDraft.objects.values_list("subject", flat=True))
    return {" ".join(s.lower().split()) for s in subjects if s and len(s) > 8}


def _our_thread_checker(mailbox):
    subjects = _reply_subjects()

    def is_our_thread(email):
        refs = [r for r in [email.in_reply_to, *email.references] if r]
        if refs and Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT, message_id__in=refs).exists():
            return True
        if email.thread_id and Activity.objects.filter(
            kind=Activity.Kind.EMAIL_SENT, thread_id=email.thread_id, source=Activity.Source.GMAIL,
        ).exists():
            return True
        # Workbook-era sends have no message ids; fall back to our template subjects,
        # as scan_responses.py did.
        subject = " ".join(email.subject.lower().split())
        return any(s in subject for s in subjects)

    return is_our_thread


def process_messages(mailbox, emails, recorder=None):
    """Log a batch of ParsedEmail for one mailbox. Returns the Recorder."""
    from crm import invites

    recorder = recorder or Recorder()
    matcher = recorder.matcher
    own = {mailbox.email.lower()}
    is_our_thread = _our_thread_checker(mailbox)
    token = suppress_history.set(True)
    try:
        for email in emails:
            recorder.stats["messages read"] += 1
            if "\\Draft" in email.labels or "\\Spam" in email.labels or "\\Trash" in email.labels:
                recorder.stats["messages skipped (draft/spam/trash)"] += 1
                continue
            if matcher.is_protected(email.from_addr):
                recorder.stats["messages skipped (protected)"] += 1
                continue
            # Mail a teammate sent (e.g. Kellen, with Simon cc'd) is ACMI outreach in every
            # teammate's mailbox, not "received" internal mail.
            senders = own | ({email.from_addr} if matcher.is_internal(email.from_addr) else set())
            if email.calendar or invites.is_invitation(email.subject):
                try:  # a meeting invite, maybe forwarded: onto the calendar (crm/invites.py)
                    added, merged = invites.from_email(email, mailbox)
                    recorder.stats["meetings from invites"] += added
                    recorder.stats["invites merged into meetings"] += merged
                except Exception:  # an odd invite must never fail the sync
                    log.exception("Couldn't read the invite in %s", email.message_id)
            envelopes = classify(email, senders, is_our_thread)
            bulk = email.from_addr not in own and email.is_bulk
            for env in envelopes:
                address = env.address.lower()
                if not address or matcher.is_internal(address) or matcher.is_protected(address):
                    continue
                event = EmailEvent(
                    mailbox=mailbox, message_id=email.message_id, thread_id=email.thread_id,
                    occurred_at=email.occurred_at, subject=email.subject,
                    body="" if env.kind == Activity.Kind.EMAIL_SENT else email.body,
                    kind=env.kind, outcome=env.outcome, address=address, name=env.name,
                    sender=email.from_addr, attachments=tuple(email.attachments),
                )
                with transaction.atomic():
                    person, company = recorder.resolve(event, allow_new_company_people=not bulk)
                    if person or company:
                        recorder.record(event, person, company)
                    elif bulk:
                        recorder.stats["skipped (newsletter/notification)"] += 1
                    else:
                        _park(event, matcher, recorder)
    finally:
        suppress_history.reset(token)
    return recorder


def _park(event, matcher, recorder):
    domain = domain_of(event.address)
    if domain in FREE_MAIL and event.kind != Activity.Kind.BOUNCE and same_person(event):
        recorder.stats["same-person suggestions"] += 1
        return
    if not domain or domain in FREE_MAIL or domain in matcher.ignored or event.kind == Activity.Kind.BOUNCE:
        recorder.stats["skipped (unknown personal/ignored address)"] += 1
        return
    if domain in matcher.directory:
        from crm.models import FirmEmail

        _, created = FirmEmail.objects.get_or_create(
            message_id=event.message_id, address=event.address,
            defaults={
                "provider_id": matcher.directory[domain], "mailbox": event.mailbox, "thread_id": event.thread_id,
                "occurred_at": event.occurred_at, "kind": event.kind, "name": event.name[:255],
                "subject": event.subject[:500], "snippet": event.body[:1000],
            },
        )
        if created:
            recorder.stats["filed in the directory"] += 1
        return
    _, created = UnmatchedEmail.objects.get_or_create(
        message_id=event.message_id, address=event.address,
        defaults={
            "mailbox": event.mailbox, "thread_id": event.thread_id, "occurred_at": event.occurred_at,
            "kind": event.kind, "outcome": event.outcome, "name": event.name[:255], "domain": domain,
            "subject": event.subject, "snippet": event.body[:1000],
        },
    )
    if created:
        recorder.stats["sent to review queue"] += 1


def same_person(event):
    """A free-mail sender whose display name is exactly one Person's name: "Same person, add this address" on Needs
    filing. Returns the suggestion, or None."""
    from crm.models import AddressSuggestion

    name = " ".join((event.name or "").split())
    if len(name.split()) < 2:  # a first name alone matches too many people
        return None
    people = list(Person.objects.filter(full_name__iexact=name)[:2])
    if len(people) != 1 or AddressSuggestion.objects.filter(
            address=event.address, person=people[0], status=AddressSuggestion.Status.DISMISSED).exists():
        return None  # nobody, more than one, or someone already said "not them"
    suggestion, _ = AddressSuggestion.objects.get_or_create(
        message_id=event.message_id, address=event.address,
        defaults={"person": people[0], "name": name[:255], "mailbox": event.mailbox, "thread_id": event.thread_id,
                  "occurred_at": event.occurred_at, "kind": event.kind, "outcome": event.outcome,
                  "subject": event.subject[:500], "snippet": event.body[:1000]})
    return suggestion


def add_address(suggestion, user=None):
    """Yes, same person: the address goes on the person and their waiting emails from it onto their timeline."""
    from crm.models import AddressSuggestion

    person = suggestion.person
    with transaction.atomic():
        if not person.email:
            person.email = suggestion.address
        elif suggestion.address not in [e.lower() for e in person.other_emails or []]:
            person.other_emails = [*(person.other_emails or []), suggestion.address]
        person.save()
        waiting = AddressSuggestion.objects.filter(address=suggestion.address, person=person,
                                                   status=AddressSuggestion.Status.OPEN).select_related("mailbox")
        recorder = Recorder()
        for s in waiting:
            if s.mailbox_id:
                recorder.record(EmailEvent(
                    mailbox=s.mailbox, message_id=s.message_id, thread_id=s.thread_id, occurred_at=s.occurred_at,
                    subject=s.subject, body=s.snippet, kind=s.kind, outcome=s.outcome, address=s.address,
                    name=s.name), person, person.company)
        n = waiting.update(status=AddressSuggestion.Status.ADDED, decided_by=user)
        recorder.finish()
    return n


LOCK_MINUTES = 15
INDEX_REFRESH_MAX = 300  # threads re-read for the inbox per sync


class SyncBusy(Exception):
    pass


def _acquire_lock(mailbox):
    now = timezone.now()
    taken = Mailbox.objects.filter(pk=mailbox.pk).filter(
        Q(sync_lock_until__isnull=True) | Q(sync_lock_until__lt=now)
    ).update(sync_lock_until=now + timedelta(minutes=LOCK_MINUTES))
    if not taken:
        raise SyncBusy(f"A sync of {mailbox.email} is already running. Wait for it to finish.")


def sync_mailbox(mailbox, client=None, max_messages=DEFAULT_MAX_MESSAGES):
    """Read new mail for one mailbox. Safe to call repeatedly; resumes where it stopped.

    Raises SyncBusy if another sync of this mailbox is in progress.
    """
    _acquire_lock(mailbox)
    mailbox.refresh_from_db()
    choose_backend(mailbox)
    recorder = Recorder()
    try:
        if mailbox.backend == Mailbox.Backend.GMAIL_API:
            _read_gmail(mailbox, client, max_messages, recorder)
        else:
            _read_imap(mailbox, client, max_messages, recorder)
        mailbox.last_error = ""
    except MailboxError as exc:
        mailbox.last_error = str(exc)
        log.warning("Mail sync failed for %s: %s", mailbox, exc)
    except Exception as exc:  # network errors etc.; keep the message for the Mail page
        mailbox.last_error = f"{type(exc).__name__}: {exc}"
        log.exception("Mail sync crashed for %s", mailbox)
    finally:
        recorder.finish()
        mailbox.last_synced_at = timezone.now()
        mailbox.sync_lock_until = None
        mailbox.save()
    return recorder.stats


def choose_backend(mailbox):
    """Mail in the hub on (for the mailbox's own address) -> the Gmail API; off -> back to IMAP. A switch clears
    the API's marker, so the next read starts again from a day before the last sync; Message-ID keys keep the
    overlap from being logged twice."""
    from . import connect

    if mailbox.shared:  # a shared mailbox is only ever read through its own Gmail permission
        mailbox.backend = Mailbox.Backend.GMAIL_API
        return
    conn = connect.connection(mailbox.user)
    own = mailbox.email.lower() in {(mailbox.user.email or "").lower(), (conn.google_email if conn else "").lower()}
    want = Mailbox.Backend.GMAIL_API if own and connect.is_on(mailbox.user) else Mailbox.Backend.IMAP
    if mailbox.backend != want:
        log.info("Mail sync for %s switches to %s", mailbox, want)
        mailbox.backend, mailbox.history_id, mailbox.pending_ids = want, "", []


def _read_gmail(mailbox, client, max_messages, recorder):
    from . import client as mail_client, gmail_api, index

    api = client or gmail_api.client_for(mailbox)
    emails, new, rest, touched = gmail_api.read_changes(mailbox, api, max_messages)
    if emails:
        process_messages(mailbox, emails, recorder)
        mailbox.messages_seen += len(emails)
    mailbox.pending_ids = rest
    mailbox.backfill_done = not mailbox.pending_ids
    recorder.stats["messages remaining"] = len(mailbox.pending_ids)
    try:
        recorder.stats["new mail alerts"] = mail_client.on_new_mail(mailbox, [e for e in emails if e.uid in new])
    except Exception:  # an alert problem must never fail the sync
        log.exception("New-mail alerts failed for %s", mailbox)
    try:  # the inbox index: threads Gmail says changed, the inbox's first few hundred once, old rows out
        touched += [gmail_api.hex_thread_id(e.thread_id) for e in emails if e.uid in new]
        recorder.stats["inbox threads refreshed"] = len(index.refresh(mailbox, api, touched[:INDEX_REFRESH_MAX]))
        recorder.stats["inbox threads indexed"] = index.warm_inbox(mailbox, api)
        from . import actions

        recorder.stats["snoozed threads back"] = actions.wake_due(mailbox, api)
        from . import crm_labels

        crm_labels.rename_all(mailbox, api)
        refreshed = list(MailThread.objects.filter(mailbox=mailbox, gmail_id__in=touched[:INDEX_REFRESH_MAX]))
        recorder.stats["CRM labels added"] = crm_labels.sync_threads(mailbox, api, refreshed)             + crm_labels.backfill(mailbox, api)
        index.prune(mailbox)
    except Exception:  # the inbox index must never fail the timeline sync
        log.exception("Inbox index refresh failed for %s", mailbox)


def _read_imap(mailbox, client, max_messages, recorder):
    with client or ImapClient(mailbox) as conn:
        if conn.uid_validity and conn.uid_validity != mailbox.uid_validity:
            # Gmail renumbered the folder; start over (activities dedupe on Message-ID).
            mailbox.uid_validity = conn.uid_validity
            mailbox.last_uid = 0
            mailbox.backfill_done = False
        since = None if mailbox.backfill_done else since_date(mailbox.backfill_days)
        uids = conn.search(since=since, after_uid=mailbox.last_uid)
        batch = uids[:max_messages]
        if batch:
            process_messages(mailbox, conn.fetch(batch), recorder)
            mailbox.last_uid = batch[-1]
            mailbox.messages_seen += len(batch)
        mailbox.backfill_done = len(uids) <= max_messages
        recorder.stats["messages remaining"] = max(0, len(uids) - len(batch))


def sync_all(max_messages=DEFAULT_MAX_MESSAGES):
    results = {}
    from crm.permissions import team_users

    # Only the team's mailboxes and the connected shared ones: a capital partner's account never has its mail read.
    for mb in Mailbox.objects.filter(enabled=True).filter(
            Q(user__in=team_users()) | Q(shared=True, shared_token__isnull=False)).distinct():
        try:
            results[str(mb)] = sync_mailbox(mb, max_messages=max_messages)
        except SyncBusy:
            results[str(mb)] = {"skipped (already syncing)": 1}
    # Inbox-only mailboxes (Mail on, no timeline sync): only snoozed threads coming back.
    from . import actions, connect, gmail_api

    for mb in Mailbox.objects.filter(enabled=False, backend=Mailbox.Backend.GMAIL_API, user__in=team_users(),
                                     snoozes__isnull=False).distinct():
        if connect.is_on(mb.user):
            try:
                results[str(mb)] = {"snoozed threads back": actions.wake_due(mb, gmail_api.client_for(mb))}
            except Exception as exc:  # noqa: BLE001 - one person's Gmail must not stop the others
                log.warning("Snooze wake-up failed for %s: %s", mb, exc)
    return results


# ---------------------------------------------------------------------------
# Review queue actions
# ---------------------------------------------------------------------------


def review_queue():
    """Unmatched domains, busiest first."""
    groups = {}
    for row in UnmatchedEmail.objects.order_by("-occurred_at"):
        g = groups.setdefault(row.domain, {"domain": row.domain, "count": 0, "addresses": {}, "subjects": [],
                                           "last": row.occurred_at, "sent": 0, "received": 0})
        g["count"] += 1
        g["addresses"].setdefault(row.address, row.name)
        if len(g["subjects"]) < 3 and row.subject not in g["subjects"]:
            g["subjects"].append(row.subject)
        if row.kind == Activity.Kind.EMAIL_SENT:
            g["sent"] += 1
        else:
            g["received"] += 1
    for g in groups.values():
        g["suggested_name"] = guess_company_name(g["domain"])
    return sorted(groups.values(), key=lambda g: -g["count"])


def guess_company_name(domain):
    base = domain.split(".")[0]
    return base.replace("-", " ").title() if len(base) > 4 else base.upper()


def approve_domain(domain, company=None, company_name="", user=None):
    """Attach the domain to a company (new or existing) and log its queued emails. A directory firm's domain is
    refused (ValueError): it's a consultant, vendor or lender, never a company."""
    domain = domain.lower()
    from crm.directory.logic import firm_domains

    if domain in firm_domains():
        raise ValueError(f"{domain} belongs to a firm in the directory.")
    with transaction.atomic():
        name = company_name.strip() or guess_company_name(domain)
        if company is None:
            # Same name as an existing company: attach the domain rather than duplicate it.
            company = Company.objects.filter(name_key=normalize_key(name)).first()
        if company is None:
            company = Company.objects.create(name=name, domains=[domain], created_by=user)
            Activity.objects.create(
                kind=Activity.Kind.RECORD_CREATED, occurred_at=timezone.now(), actor=user, company=company,
                summary=f"Company created from email domain {domain}", is_background=True,
                source=Activity.Source.SYSTEM,
            )
        elif domain not in company.domains:
            company.domains = [*company.domains, domain]
            company.save()

        recorder = Recorder()
        token = suppress_history.set(True)
        try:
            for row in UnmatchedEmail.objects.filter(domain=domain).select_related("mailbox"):
                event = EmailEvent(
                    mailbox=row.mailbox, message_id=row.message_id, thread_id=row.thread_id,
                    occurred_at=row.occurred_at, subject=row.subject, body=row.snippet, kind=row.kind,
                    outcome=row.outcome, address=row.address, name=row.name,
                )
                person, comp = recorder.resolve(event)
                if person or comp:
                    recorder.record(event, person, comp)
            UnmatchedEmail.objects.filter(domain=domain).delete()
        finally:
            suppress_history.reset(token)
        recorder.finish()
        reclassify_replies(company_ids=[company.pk])
    return company, recorder.stats


def reclassify_replies(company_ids=None):
    """Turn "received" emails into replies when ACMI sent earlier in the same Gmail thread.

    Needed after a domain is approved: at sync time the sent half of the thread was
    still in the review queue, so the answer could not be recognised as a reply.
    """
    received = Activity.objects.filter(source=Activity.Source.GMAIL, kind=Activity.Kind.EMAIL_RECEIVED).exclude(
        thread_id=""
    )
    if company_ids is not None:
        received = received.filter(company_id__in=company_ids)
    recorder = Recorder()
    changed = 0
    for act in received.select_related("company", "person", "actor"):
        first_sent = (
            Activity.objects.filter(source=Activity.Source.GMAIL, kind=Activity.Kind.EMAIL_SENT, thread_id=act.thread_id,
                                    occurred_at__lte=act.occurred_at)
            .order_by("occurred_at").first()
        )
        if first_sent is None:
            continue
        text = new_text(act.body).lower()
        act.kind = Activity.Kind.REPLY
        act.outcome = "Not Interested" if any(p in text for p in NOT_INTERESTED_PHRASES) else "Responded"
        act.summary = act.summary.replace("Email from", "Reply from", 1)
        Activity.objects.filter(pk=act.pk).update(kind=act.kind, outcome=act.outcome, summary=act.summary)
        changed += 1
        if act.person_id:
            recorder.person_ids.add(act.person_id)
        if act.company:
            recorder.company_ids.add(act.company_id)
            recorder._note_stage(act.company, EmailEvent(
                mailbox=None, message_id=act.message_id, thread_id=act.thread_id, occurred_at=act.occurred_at,
                subject="", body="", kind=act.kind, outcome=act.outcome, address="", name="",
            ))
    recorder.finish()
    return changed, recorder.stats.get("company stages advanced", 0)


def auto_resolve_queue():
    """Attach queued domains that exactly spell an existing company's name. Returns {domain: company}."""
    matcher = Matcher()
    resolved = {}
    for domain in UnmatchedEmail.objects.values_list("domain", flat=True).distinct():
        company = matcher.company_for_domain(domain)
        if company:
            approve_domain(domain, company=company)
            resolved[domain] = company.name
    return resolved


def dismiss_domain(domain, kind=MailFilter.Kind.IGNORE, user=None, note=""):
    MailFilter.objects.update_or_create(pattern=domain.lower(), defaults={"kind": kind, "created_by": user, "note": note})
    UnmatchedEmail.objects.filter(domain=domain.lower()).delete()
