"""CRM labels in Gmail (docs/EMAIL_CLIENT.md, "CRM labels on your mail"; step 6).

The hub's tags become real Gmail labels under ACMI/, so they show in Gmail on the phone and work in Gmail search:
    ACMI/Projects/<project>     every campus and project a thread is about
    ACMI/Tenants/<company>      a company with an open deal, or one you filed the thread to
    ACMI/Capital/<firm>         lenders and investors (banks, debt funds, brokers, equity, agencies)
    ACMI/Directory/<firm>       consultants, lawyers, vendors, once you file a thread to them
Both ways: a label the person takes off in Gmail stays off ("not about this"), and one they add in Gmail files the
thread to that record. Renaming a record renames its label. Each person can switch it off on Mail in the hub
(UserPrefs "mail_crm_labels"); the tags in the hub stay either way.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from crm import prefs
from crm.models import (
    OPEN_STAGES, Activity, Company, Firm, FirmEmail, MailCrmLabel, MailCrmLabelOnThread, MailFiling, MailThread,
    Opportunity, Project,
)

log = logging.getLogger(__name__)

ROOT = "ACMI"
CAPITAL_KINDS = {Firm.Kind.BANK, Firm.Kind.DEBT_FUND, Firm.Kind.BROKER, Firm.Kind.INVESTOR, Firm.Kind.AGENCY}
BACKFILL_DAYS = 90


def enabled(user):
    from . import connect

    return prefs.get_prefs(user).get("mail_crm_labels", True) and connect.is_on(user)


def enabled_for(mailbox):
    """A person's mailbox follows their switch; a shared mailbox always carries the CRM labels."""
    return mailbox.shared or enabled(mailbox.user)


def _clean(name):
    return " ".join((name or "").replace("/", "-").split())[:180] or "Untitled"


def label_name(record):
    if isinstance(record, Project):
        return f"{ROOT}/Projects/{_clean(record.name)}"
    if isinstance(record, Company):
        return f"{ROOT}/Tenants/{_clean(record.name)}"
    group = "Capital" if record.kind in CAPITAL_KINDS else "Directory"
    return f"{ROOT}/{group}/{_clean(record.name)}"


def _field(record):
    return "project" if isinstance(record, Project) else "company" if isinstance(record, Company) else "firm"


class _Labels:
    """The person's Gmail labels by name, read once per pass, with the ACMI/ parents made when first needed."""

    def __init__(self, client):
        self.client, self._names = client, None

    def names(self):
        if self._names is None:
            self._names = {lb["name"]: lb["id"] for lb in self.client.labels()}
        return self._names

    def ensure(self, name):
        names = self.names()
        parts = name.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            if prefix not in names:
                names[prefix] = self.client.create_label(prefix)["id"]
        return names[name]


def ensure(mailbox, labels, record):
    """The MailCrmLabel for a record in this mailbox, made (or renamed) in Gmail as needed."""
    field = _field(record)
    want = label_name(record)
    row = MailCrmLabel.objects.filter(mailbox=mailbox, **{field: record}).first()
    if row is None:
        row = MailCrmLabel.objects.create(mailbox=mailbox, gmail_label_id=labels.ensure(want), name=want,
                                          **{field: record})
    elif row.name != want:
        rename(mailbox, labels.client, row, want)
    return row


def rename(mailbox, client, row, want):
    try:
        client.update_label(row.gmail_label_id, want)
    except Exception:  # an old label name is only cosmetic
        log.warning("Couldn't rename %s to %s for %s", row.name, want, mailbox, exc_info=True)
        return
    row.name = want
    row.save(update_fields=["name"])


def rename_all(mailbox, client):
    """Renamed records rename their labels (once per sync)."""
    for row in MailCrmLabel.objects.filter(mailbox=mailbox).select_related("company", "project", "firm"):
        record = row.company or row.project or row.firm
        if record is not None and label_name(record) != row.name:
            rename(mailbox, client, row, label_name(record))


def wanted(mailbox, threads):
    """{gmail thread id: {records}} the CRM labels each thread should carry."""
    by_dec = {t.thread_id: t for t in threads if t.thread_id}
    out = {t.gmail_id: set() for t in threads}
    for f in MailFiling.objects.filter(mailbox=mailbox, gmail_id__in=list(out)).select_related(
            "company", "opportunity__company", "project", "firm"):
        out[f.gmail_id].add(f.opportunity.company if f.opportunity_id else f.target)
    open_companies = set(Opportunity.objects.filter(stage__in=OPEN_STAGES).values_list("company_id", flat=True))
    acts = Activity.objects.filter(source=Activity.Source.GMAIL, thread_id__in=list(by_dec))
    for a in acts.select_related("company", "opportunity__company").prefetch_related("projects"):
        t = by_dec[a.thread_id]
        out[t.gmail_id].update(a.projects.all())
        if a.opportunity_id:
            out[t.gmail_id].add(a.opportunity.company)
        elif a.company_id in open_companies:
            out[t.gmail_id].add(a.company)
    for fe in FirmEmail.objects.filter(thread_id__in=list(by_dec)).select_related("provider"):
        if fe.provider.kind in CAPITAL_KINDS:
            out[by_dec[fe.thread_id].gmail_id].add(fe.provider)
    return out


def sync_threads(mailbox, client, threads):
    """Bring these index rows' CRM labels up to date, both ways. Returns how many threads got a label."""
    threads = [t for t in threads if t is not None]
    if not threads or not enabled_for(mailbox):
        return 0
    from . import filing

    labels = _Labels(client)
    crm_rows = {r.gmail_label_id: r for r in MailCrmLabel.objects.filter(mailbox=mailbox)
                .select_related("company", "project", "firm")}
    want = wanted(mailbox, threads)
    changed = 0
    for t in threads:
        current = set(t.labels or [])
        applied = {a.label_id: a for a in MailCrmLabelOnThread.objects.filter(label__mailbox=mailbox, gmail_id=t.gmail_id)
                   .select_related("label")}
        for a in applied.values():  # taken off in Gmail: about something else, so it stays off
            if not a.removed and a.label.gmail_label_id not in current:
                a.removed = True
                a.save(update_fields=["removed"])
        for label_id in current & set(crm_rows):  # put on in Gmail: file the thread there
            row = crm_rows[label_id]
            if row.pk in applied:
                continue
            MailCrmLabelOnThread.objects.create(label=row, gmail_id=t.gmail_id)
            record = row.company or row.project or row.firm
            if record is not None:
                filing.file_thread(mailbox, client, t.gmail_id, record, user=mailbox.user,
                                   source=filing.MailFiling.Source.GMAIL, after=False)
        add = []
        for record in want.get(t.gmail_id, ()):
            row = ensure(mailbox, labels, record)
            crm_rows[row.gmail_label_id] = row
            on = applied.get(row.pk) or MailCrmLabelOnThread.objects.filter(label=row, gmail_id=t.gmail_id).first()
            if on is not None and on.removed:
                continue
            if row.gmail_label_id not in current:
                add.append(row.gmail_label_id)
            if on is None:
                MailCrmLabelOnThread.objects.create(label=row, gmail_id=t.gmail_id)
        if add:
            client.modify_thread(t.gmail_id, add=add)
            MailThread.objects.filter(pk=t.pk).update(labels=sorted(current | set(add)))
            changed += 1
    return changed


def backfill(mailbox, client):
    """Once: label the indexed threads of the last 90 days."""
    if mailbox.crm_labels_done_at or not enabled_for(mailbox):
        return 0
    rows = list(MailThread.objects.filter(mailbox=mailbox,
                                          last_message_at__gte=timezone.now() - timedelta(days=BACKFILL_DAYS)))
    n = sync_threads(mailbox, client, rows)
    mailbox.crm_labels_done_at = timezone.now()
    return n
