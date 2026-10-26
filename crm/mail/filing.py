"""File a thread to a record (docs/EMAIL_CLIENT.md, step 6): a company, a deal, a project or a directory firm.

Filing is the deliberate step that shares a thread with the team (section 5): its emails go on the record's timeline
where everyone sees them, like the ones the sync logs. Where the sync already logged an email, filing only adds the
deal or project; where it didn't (a gmail.com sender, a domain still in Mail review, an internal thread), filing
writes the timeline entries with the sync's own keys, so nothing is ever logged twice. A lender's or consultant's
thread goes on the directory firm (FirmEmail), never on a Company. Unfiling takes off exactly what the filing added.
"""
import logging

from django.db import transaction
from django.urls import reverse

from crm import interactions
from crm.models import Activity, Company, Firm, FirmEmail, MailFiling, Opportunity, Person, Project, UnmatchedEmail

from . import render
from .gmail_api import decimal_thread_id, parse_api_message

log = logging.getLogger(__name__)

KINDS = {"company": Company, "opportunity": Opportunity, "project": Project, "firm": Firm}


class FilingError(Exception):
    pass


def target_for(kind, pk):
    model = KINDS.get(kind)
    obj = model.objects.filter(pk=pk).first() if model and str(pk).isdigit() else None
    if obj is None:
        raise FilingError("Pick a company, deal, project or firm.")
    return obj


def kind_of(obj):
    return next(k for k, m in KINDS.items() if isinstance(obj, m))


def options(text, limit=6):
    """What "File to…" offers for a search: [(kind, pk, name, detail)]."""
    text = (text or "").strip()
    if len(text) < 2:
        return []
    out = []
    for p in Project.objects.filter(name__icontains=text, active=True)[:limit]:
        out.append(("project", p.pk, p.name, "Project"))
    for o in Opportunity.objects.filter(name__icontains=text).select_related("company")[:limit]:
        out.append(("opportunity", o.pk, str(o), f"Deal · {o.company.name}"))
    for c in Company.objects.filter(name__icontains=text)[:limit]:
        out.append(("company", c.pk, c.name, "Company"))
    for f in Firm.objects.filter(name__icontains=text)[:limit]:
        out.append(("firm", f.pk, f.name, f.get_kind_display()))
    return out


def filings_for(mailbox, gmail_id):
    return list(MailFiling.objects.filter(mailbox=mailbox, gmail_id=gmail_id)
                .select_related("company", "opportunity__company", "project", "firm"))


def _emails(mailbox, client, gmail_id):
    data = render.get_thread(mailbox, client, gmail_id)
    out = []
    for raw in data.get("messages", []):
        email = parse_api_message(raw)
        if email and "\\Draft" not in email.labels:
            out.append(email)
    subject = next((e.subject for e in out if e.subject), "")
    return out, subject


def _counterpart(mailbox, email):
    """The other side of an email: its sender, or for one you sent, the first person it went to."""
    own = mailbox.email.lower()
    if email.from_addr != own:
        return email.from_addr, email.from_name
    for name, addr in email.to + email.cc:
        if addr and addr != own:
            return addr, name
    return "", ""


def file_thread(mailbox, client, gmail_id, target, user=None, source=MailFiling.Source.HUB, attach_domain=False,
                after=True):
    """File the thread to `target`. Returns the MailFiling (the existing one if it's already filed there). after=False
    skips the index and label refresh (the CRM-label pass that calls it does its own)."""
    kind = kind_of(target)
    existing = MailFiling.objects.filter(mailbox=mailbox, gmail_id=gmail_id, **{kind: target}).first()
    if existing:
        return existing
    emails, subject = _emails(mailbox, client, gmail_id)
    thread_id = decimal_thread_id(gmail_id)
    filing = MailFiling(mailbox=mailbox, gmail_id=gmail_id, thread_id=thread_id, subject=subject[:500],
                        filed_by=user, source=source, **{kind: target})
    company = target.company if kind == "opportunity" else target if kind == "company" else None
    added, firm_rows, people = [], [], set()
    with transaction.atomic():
        if company is not None and attach_domain:
            _attach_queued_domains(mailbox, emails, company, user)
        for email in emails:
            addr, name = _counterpart(mailbox, email)
            sent = email.from_addr == mailbox.email.lower()
            if kind == "firm":
                row, created = FirmEmail.objects.get_or_create(
                    message_id=email.message_id, address=addr or email.from_addr, defaults={
                        "provider": target, "mailbox": mailbox, "thread_id": thread_id,
                        "occurred_at": email.occurred_at, "name": (name or "")[:255],
                        "kind": Activity.Kind.EMAIL_SENT if sent else Activity.Kind.EMAIL_RECEIVED,
                        "subject": email.subject[:500], "snippet": email.body[:1000]})
                if created:
                    firm_rows.append(row.pk)
                continue
            if company is not None:
                key = f"gmail:{email.message_id}|c{company.pk}"
            else:  # a project: entries of their own unless the sync already logged the email somewhere
                if Activity.objects.filter(source=Activity.Source.GMAIL, message_id=email.message_id).exists():
                    continue
                key = f"gmail:{email.message_id}|j{target.pk}"
            person = Person.objects.filter(email__iexact=addr).first() if addr else None
            if person:
                people.add(person.pk)
            act, created = Activity.objects.get_or_create(source=Activity.Source.GMAIL, source_id=key[:255], defaults={
                "occurred_at": email.occurred_at,
                "kind": Activity.Kind.EMAIL_SENT if sent else Activity.Kind.EMAIL_RECEIVED,
                "actor": mailbox.user if sent else None, "company": company, "person": person,
                "summary": f"{'Email to' if sent else 'Email from'} {name or addr}: {email.subject or '(no subject)'}"[:500],
                "body": "" if sent else email.body, "message_id": email.message_id, "thread_id": thread_id,
            })
            if created:
                added.append(act.pk)
                if mailbox.user_id:
                    act.team_members.add(mailbox.user)
        acts = Activity.objects.filter(source=Activity.Source.GMAIL, thread_id=thread_id)
        if kind == "opportunity":
            acts.filter(company=company, opportunity__isnull=True).update(opportunity=target)
        if kind == "project":
            for act in acts:
                act.projects.add(target)
                if act.context != Activity.Context.PROJECT:
                    Activity.objects.filter(pk=act.pk).update(context=Activity.Context.PROJECT)
        filing.added_activities, filing.added_firm_emails = added, firm_rows
        filing.save()
    if company is not None:
        interactions.recompute(company_ids=[company.pk], person_ids=people)
        from .sync import reclassify_replies

        reclassify_replies(company_ids=[company.pk])  # an answer in a thread ACMI started is a reply
    if after:
        _after(mailbox, client, gmail_id)
    return filing


def _attach_queued_domains(mailbox, emails, company, user):
    """The thread's senders whose domain waits in Mail review: attach the domain to the company (Mail review's own
    step, so the rest of that domain's waiting mail follows)."""
    from .sync import approve_domain

    domains = {addr.rsplit("@", 1)[-1] for e in emails for addr, _ in [_counterpart(mailbox, e)] if addr}
    for domain in UnmatchedEmail.objects.filter(domain__in=domains).values_list("domain", flat=True).distinct():
        try:
            approve_domain(domain, company=company, user=user)
        except ValueError:  # a directory firm's domain never becomes a company's
            pass


def unfile(filing, client=None):
    """Take a filing off: what it added to timelines goes, and the deal or project link with it."""
    with transaction.atomic():
        Activity.objects.filter(pk__in=filing.added_activities or []).delete()
        FirmEmail.objects.filter(pk__in=filing.added_firm_emails or []).delete()
        acts = Activity.objects.filter(source=Activity.Source.GMAIL, thread_id=filing.thread_id)
        if filing.opportunity_id:
            acts.filter(opportunity=filing.opportunity).update(opportunity=None)
        if filing.project_id:
            for act in acts:
                act.projects.remove(filing.project)
        filing.delete()
    if filing.company_id or filing.opportunity_id:
        interactions.recompute(company_ids=[(filing.company or filing.opportunity.company).pk])
    if filing.project_id:
        from crm import projects

        projects.tag(thread_ids=[filing.thread_id])
    if client is not None:
        _after(filing.mailbox, client, filing.gmail_id)


def _after(mailbox, client, gmail_id):
    from . import crm_labels, index

    render.forget(mailbox, gmail_id)
    try:
        rows = index.refresh(mailbox, client, [gmail_id])
        index.retag(mailbox, list(rows.values()))
        crm_labels.sync_threads(mailbox, client, list(rows.values()))
    except Exception:  # the filing is made; labels and tabs catch up on the next sync
        log.warning("After-filing refresh failed for %s", mailbox, exc_info=True)


def filed_url(filing):
    t = filing.target
    if filing.firm_id:
        return reverse("crm:firm", args=[filing.firm_id])
    return t.get_absolute_url()
