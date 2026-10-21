"""The directory's email and meetings, and the two ways firms come in from the company side: moving a Company over
(its people become contacts, its email is filed on the firm, the Company is deleted) and filing a domain waiting in
the mail review queue."""
import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import Activity, Company, Firm, FirmEmail, Meeting, UnmatchedEmail

EMAIL_KINDS = (Activity.Kind.EMAIL_SENT, Activity.Kind.EMAIL_RECEIVED, Activity.Kind.REPLY, Activity.Kind.AUTO_REPLY)

SUFFIXES = re.compile(r"\b(inc|llc|l\.l\.c|lp|llp|ltd|corp|corporation|co|company|group|holdings|partners|capital|"
                      r"management|investment|investments|advisors|n\.a|na|the)\b\.?", re.I)


def fmt_day(d):
    return f"{d:%b} {d.day}, {d.year}"


def short_name(name):
    """'Fortress Investment Group LLC' → 'Fortress': what a meeting title calls the firm."""
    words = " ".join(SUFFIXES.sub(" ", name or "").replace(",", " ").split())
    return words if len(words) >= 4 else ""


def firm_domains():
    """{email domain: firm id}, for the mail sync."""
    out = {}
    for pk, domains in Firm.objects.values_list("pk", "domains"):
        for d in domains or []:
            out.setdefault(d.lower(), pk)
    return out


def company_with_domain(domains):
    """The first Company that already has one of these domains, or None."""
    domains = {d.lower() for d in domains}
    if not domains:
        return None
    for pk, theirs in Company.objects.values_list("pk", "domains"):
        if domains & {d.lower() for d in theirs or []}:
            return Company.objects.get(pk=pk)
    return None


def names(group=""):
    """Firm names for a typed-name field: "capital" (a capital source's provider), "services" or all."""
    if group in ("capital", "services"):
        return [f.name for f in Firm.objects.all() if f.is_capital == (group == "capital")]
    return list(Firm.objects.values_list("name", flat=True))


def file_queued(firm):
    """Move emails already waiting in the company review queue from the firm's domains onto the firm, so nobody is
    offered them as a new company. Returns how many moved."""
    moved = 0
    for row in UnmatchedEmail.objects.filter(domain__in=firm.domains or []):
        _, created = FirmEmail.objects.get_or_create(
            message_id=row.message_id, address=row.address,
            defaults={"provider": firm, "mailbox": row.mailbox, "thread_id": row.thread_id,
                      "occurred_at": row.occurred_at, "kind": row.kind, "name": row.name, "subject": row.subject,
                      "snippet": row.snippet})
        moved += created
        row.delete()
    return moved


def file_domain(domain, kind, name="", user=None):
    """The mail review queue's "File in directory": the domain goes on a firm (one of that name, else a new one) and
    its queued email moves there. Returns (firm, emails moved)."""
    from ..mail.sync import guess_company_name

    domain = domain.strip().lower().lstrip("@")
    name = " ".join((name or "").split()) or guess_company_name(domain)
    with transaction.atomic():
        firm = Firm.objects.filter(name__iexact=name).first()
        if firm is None:
            firm = Firm.objects.create(name=name, kind=kind or Firm.Kind.OTHER, domains=[domain], created_by=user)
        elif domain not in firm.domains:
            firm.domains = [*firm.domains, domain]
            firm.save()
        return firm, file_queued(firm)


def emails(firm, since=None, limit=40):
    """Emails with the firm's people, newest first, one row per message."""
    qs = FirmEmail.objects.filter(provider=firm, occurred_at__lte=timezone.now()).order_by("-occurred_at")
    if since:
        qs = qs.filter(occurred_at__date__gte=since)
    out, seen = [], set()
    for e in qs[:limit * 3]:
        if e.message_id in seen:
            continue
        seen.add(e.message_id)
        out.append(e)
        if len(out) >= limit:
            break
    return out


def meetings(firm, since=None, limit=40, until=None):
    """Meetings with the firm: an attendee from its domains or one of its contacts, or its name in the title."""
    q = Q()
    for domain in firm.domains or []:
        q |= Q(attendees__icontains=f"@{domain.lower()}")
    for email in firm.emails:
        q |= Q(attendees__icontains=email)
    name = short_name(firm.name)
    if name:
        q |= Q(title__iregex=rf"(^|[^A-Za-z]){re.escape(name)}([^A-Za-z]|$)")
    if not q:
        return []
    qs = Meeting.objects.filter(q).distinct().order_by("-start")
    if since:
        qs = qs.filter(start__date__gte=since)
    if until:
        qs = qs.filter(start__lte=until)
    return list(qs[:limit])


def upcoming_meetings(firm, limit=3):
    return list(reversed(meetings(firm, since=timezone.localdate(), limit=limit)))[:limit]


def last_emails():
    """{firm id: date of its latest email}, for the directory list."""
    from django.db.models import Max

    return {r["provider"]: timezone.localtime(r["at"]).date()
            for r in FirmEmail.objects.filter(occurred_at__lte=timezone.now()).values("provider")
            .annotate(at=Max("occurred_at"))}


# ---------------------------------------------------------------------------
# Moving a Company into the directory
# ---------------------------------------------------------------------------


def blockers(company):
    """What stops a Company moving: records that would be lost, or that say it's a tenant prospect, not a vendor."""
    out = []
    from ..models import Task

    tasks = Task.objects.filter(Q(company=company) | Q(person__company=company), done=False).count()
    for label, n in (("tenant deal", company.opportunities.count()), ("document", company.documents.count()),
                     ("contract", company.contracts.count()), ("open task", tasks)):
        if n:
            out.append(f"{n} {label}{'s' if n != 1 else ''}")
    return out


def _activities(company):
    """The company's activities and its people's."""
    return Activity.objects.filter(Q(company=company) | Q(person__company=company))


def _notes(company):
    from ..models import Note

    return Note.objects.filter(Q(company=company) | Q(person__company=company))


def move_preview(company):
    """What a move carries over, for the confirmation page."""
    acts = _activities(company)
    return {
        "people": list(company.people.all()),
        "emails": acts.filter(kind__in=EMAIL_KINDS).exclude(message_id="").count(),
        "notes": (acts.exclude(kind__in=EMAIL_KINDS).exclude(kind=Activity.Kind.MEETING).filter(is_background=False)
                  .count() + _notes(company).count() + company.comments.count()),
        "meetings": company.meetings.count(),
        "lists": company.list_entries.count(),
        "blockers": blockers(company),
        "existing": Firm.objects.filter(name__iexact=company.name).first(),
    }


class MoveBlocked(Exception):
    pass


def move_company(company, kind, user=None):
    """Turn a Company into a directory firm: its domains and website, its people as contacts (then deleted, so
    they're out of every list and send queue), its emails filed as FirmEmail, and its notes, calls and comments added
    to the firm's notes. The Company is then deleted; its meetings stay on the calendar and show on the firm through
    the domains and contacts. Joins a firm of the same name if there is one. Returns (firm, stats)."""
    reasons = blockers(company)
    if reasons:
        raise MoveBlocked(", ".join(reasons))
    stats = {"contacts": 0, "emails": 0, "notes": 0}
    today = timezone.localdate()
    with transaction.atomic():
        firm = Firm.objects.filter(name__iexact=company.name).first() or Firm(
            name=company.name, kind=kind or Firm.Kind.OTHER, created_by=user)
        firm.domains = [*(firm.domains or []), *(company.domains or [])]
        if company.website and not firm.website:
            firm.website = company.website
        known = set(firm.emails)
        for p in company.people.all():
            if p.email and p.email.lower() in known:
                continue
            firm.contacts = [*(firm.contacts or []), {"name": p.full_name or p.email or "", "title": p.title,
                                                       "email": p.email or "", "phone": ", ".join(p.phones or [])}]
            stats["contacts"] += 1
        firm.save()
        lines = [x for x in (company.description, company.notes) if x and x.strip()]
        acts = _activities(company).select_related("person")
        fallback = f"unknown@{(company.domains or ['unknown.invalid'])[0]}"
        for a in acts.filter(kind__in=EMAIL_KINDS).exclude(message_id=""):
            address = a.person.email if a.person and a.person.email else fallback
            _, created = FirmEmail.objects.get_or_create(
                message_id=a.message_id, address=address,
                defaults={"provider": firm, "thread_id": a.thread_id, "occurred_at": a.occurred_at, "kind": a.kind,
                          "name": (a.person.full_name if a.person else "")[:255], "subject": a.summary[:500],
                          "snippet": a.body[:1000]})
            stats["emails"] += created
        for a in (acts.exclude(kind__in=EMAIL_KINDS).exclude(kind=Activity.Kind.MEETING)
                  .filter(is_background=False).order_by("occurred_at")):
            body = a.body.strip()
            lines.append(f"{fmt_day(timezone.localtime(a.occurred_at))}, {a.get_kind_display().lower()}: {a.summary}"
                         + (f"\n{body}" if body else ""))
            stats["notes"] += 1
        for n in _notes(company).order_by("created_at"):
            lines.append(f"{fmt_day(timezone.localtime(n.created_at))}, note: {n.body.strip()}")
            stats["notes"] += 1
        for c in company.comments.order_by("created_at"):
            lines.append(f"{fmt_day(timezone.localtime(c.created_at))}, comment: {c.body.strip()}")
            stats["notes"] += 1
        if lines:
            firm.notes = "\n\n".join(x for x in [firm.notes.strip(), f"From the Company record ({fmt_day(today)}):",
                                                 *lines] if x)
            firm.save(update_fields=["notes"])
        file_queued(firm)
        acts.delete()
        company.people.all().delete()
        company.delete()
    return firm, stats
