"""Needs filing (/needs-filing/): one card per organization the CRM doesn't know yet, joining its emails waiting in
the review queue, its meetings (by attendee domain) and its emailed invites, with Claude's read of the domain as the
headline and one primary action: "Looks like a bank. File as a lender on Project Anvil?"

Accept files the firm (or adds the company, or ignores the domain), links its meetings, and for a bank, debt fund or
broker adds an Identified capital source on the guessed project. Not now hides the card for two weeks; Never ignores
the domain for good. Nothing here creates a Company unless a person accepts one.

This module also keeps each meeting's links to known records (link): its firms by attendee domain or contact email,
its companies by domain or person, and the outside attendees no record knows (Meeting.unfiled), which drive the
calendar's "no record" marker, the Outreach badge and the Managers' daily bell note.
"""
import re
from collections import Counter, defaultdict
from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from .mail.sync import FREE_MAIL  # free mail can't be filed by domain: "Same person, add this address" instead
from .models import (
    Activity, CapitalLog, CapitalSource, Company, DomainSort, FilingSnooze, Firm, MailFilter, MailFiling, Meeting,
    Notification, Person, Project, UnmatchedEmail,
)

PAST_DAYS = 90  # meetings this far back can raise a card
AHEAD_DAYS = 60  # and this far ahead
SNOOZE_DAYS = 14  # "Not now"
PER_PAGE = 25
# A capital source is added on Accept for these directory types: (CapitalSource kind, the word the headline uses).
CAPITAL_ROLES = {"bank": ("senior", "lender"), "debt_fund": ("senior", "lender"), "broker": ("other", "debt broker")}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
VERDICT_RANK = {"directory": 0, "company": 1, "unsure": 2, "ignore": 3}


def _domain(email):
    return (email or "").rsplit("@", 1)[-1].strip().lower()


def window():
    now = timezone.now()
    return now - timedelta(days=PAST_DAYS), now + timedelta(days=AHEAD_DAYS)


# ---------------------------------------------------------------------------
# Known records: which outside attendees a meeting can be linked to
# ---------------------------------------------------------------------------


class Known:
    """What the CRM already knows about email addresses, loaded once per run: company and firm domains, firm contact
    emails, filters, the team's own domains."""

    def __init__(self):
        self.internal = {d.lower() for d in settings.INTERNAL_EMAIL_DOMAINS}
        self.company_domains = {}
        for pk, domains in Company.objects.values_list("pk", "domains"):
            for d in domains or []:
                self.company_domains.setdefault(d.lower(), pk)
        self.firm_domains, self.firm_emails = {}, {}
        for pk, domains, contacts in Firm.objects.values_list("pk", "domains", "contacts"):
            for d in domains or []:
                self.firm_domains.setdefault(d.lower(), pk)
            for c in contacts or []:
                if c.get("email"):
                    self.firm_emails.setdefault(c["email"].lower(), pk)
        filters = dict(MailFilter.objects.values_list("pattern", "kind"))
        self.filtered = set(filters)  # ignored or protected: never offered for filing
        self._people = {}

    def is_internal(self, email):
        d = _domain(email)
        return d in self.internal or any(d.endswith("." + x) for x in self.internal)

    def people(self, emails):
        """{email: (person id, company id)} for the addresses, by primary or other email."""
        wanted = {e for e in emails if e not in self._people}
        if wanted:
            for pk, email, company_id in Person.objects.filter(email__in=wanted).values_list("pk", "email", "company"):
                self._people[email] = (pk, company_id)
            for e in wanted - set(self._people):
                hit = Person.objects.filter(other_emails__icontains=f'"{e}"').values_list("pk", "company").first()
                self._people[e] = hit
        return {e: self._people.get(e) for e in emails}

    def read(self, attendees):
        """(company ids, firm ids, unfiled [{name, email}]) for a meeting's attendee list."""
        outside = [a for a in attendees if a.get("email") and "@" in a["email"] and not self.is_internal(a["email"])]
        emails = [a["email"].lower() for a in outside]
        people = self.people(emails)
        companies, firms, unfiled = set(), set(), []
        for a, email in zip(outside, emails):
            d = _domain(email)
            person = people.get(email)
            firm = self.firm_emails.get(email) or self.firm_domains.get(d)
            company = (person[1] if person else None) or (None if d in FREE_MAIL else self.company_domains.get(d))
            if firm:
                firms.add(firm)
            if company:
                companies.add(company)
            if person or firm or company or d in FREE_MAIL or d in self.filtered or email in self.filtered:
                continue
            if email not in {u["email"].lower() for u in unfiled}:
                unfiled.append({"name": a.get("name", ""), "email": email})
        return companies, firms, unfiled


def link(meeting, known=None):
    """Link the meeting to the firms and companies its attendees belong to, and note the ones no record knows."""
    known = known or Known()
    companies, firms, unfiled = known.read(meeting.attendees or [])
    if companies:
        meeting.companies.add(*companies)
    if firms:
        meeting.firms.add(*firms)
    domains = sorted({_domain(a["email"]) for a in unfiled})
    if (meeting.unfiled, meeting.unfiled_domains) != (unfiled, domains):
        meeting.unfiled, meeting.unfiled_domains = unfiled, domains
        Meeting.objects.filter(pk=meeting.pk).update(unfiled=unfiled, unfiled_domains=domains)
    return meeting


def refresh_domain(domain):
    """After a domain is filed, ignored or undone: re-link the meetings with someone at it. Returns how many."""
    domain = domain.strip().lower().lstrip("@")
    known = Known()
    n = 0
    for m in Meeting.objects.filter(attendees__icontains=f"@{domain}"):
        link(m, known)
        n += 1
    return n


def unlink_firm_domain(firm, domain):
    """The domain came off the firm (Undo): its meetings lose the firm unless another attendee still ties them."""
    known = Known()
    for m in Meeting.objects.filter(attendees__icontains=f"@{domain}", firms=firm):
        _, firms, _ = known.read(m.attendees or [])
        if firm.pk not in firms:
            m.firms.remove(firm)
        link(m, known)


def refresh(meetings=None):
    """Re-link meetings (default: the Needs filing window). Returns (meetings checked, meetings with unfiled people)."""
    if meetings is None:
        start, end = window()
        meetings = Meeting.objects.filter(start__gte=start, start__lte=end)
    known = Known()
    checked = flagged = 0
    for m in meetings:
        link(m, known)
        checked += 1
        flagged += bool(m.unfiled)
    return checked, flagged


# ---------------------------------------------------------------------------
# The inbox: which domains have a card
# ---------------------------------------------------------------------------


def _snoozed():
    return set(FilingSnooze.objects.filter(until__gte=timezone.localdate()).values_list("domain", flat=True))


def open_domains():
    """{domain: {emails, last, meetings: [ids]}} for every card: queued email and unfiled meetings in the window,
    without snoozed or ignored domains."""
    out = {}
    for r in UnmatchedEmail.objects.values("domain").annotate(n=Count("id"), last=Max("occurred_at")):
        out[r["domain"]] = {"emails": r["n"], "last": r["last"], "meetings": []}
    start, end = window()
    for pk, at, domains in (Meeting.objects.filter(start__gte=start, start__lte=end).exclude(unfiled_domains=[])
                            .values_list("pk", "start", "unfiled_domains")):
        for d in domains or []:
            row = out.setdefault(d, {"emails": 0, "last": at, "meetings": []})
            row["meetings"].append(pk)
            row["last"] = max(row["last"], at) if row["last"] else at
    hidden = _snoozed() | set(MailFilter.objects.values_list("pattern", flat=True))
    return {d: r for d, r in out.items() if d not in hidden}


def count():
    """How many cards wait: the Outreach badge. Three queries, as it runs on every page."""
    domains = set(UnmatchedEmail.objects.values_list("domain", flat=True).distinct())
    start, end = window()
    for ds in (Meeting.objects.filter(start__gte=start, start__lte=end).exclude(unfiled_domains=[])
               .values_list("unfiled_domains", flat=True)):
        domains.update(ds or [])
    hidden = (FilingSnooze.objects.filter(until__gte=timezone.localdate()).order_by().values_list("domain", flat=True)
              .union(MailFilter.objects.order_by().values_list("pattern", flat=True)))
    return len(domains - set(hidden))


def unfiled_meetings(days_back=7, days_ahead=7):
    """Meetings this week either side with someone no record knows."""
    now = timezone.now()
    hidden = _snoozed()
    qs = Meeting.objects.filter(start__gte=now - timedelta(days=days_back), start__lte=now + timedelta(days=days_ahead))
    return [m for m in qs.exclude(unfiled_domains=[]) if set(m.unfiled_domains) - hidden]


def daily_note(user, today=None):
    """The Managers' once-a-day bell note: meetings with outside people and no record, and cards waiting."""
    from . import notify
    from .permissions import is_manager
    from django.urls import reverse

    if not is_manager(user):
        return None
    today = today or timezone.localdate()
    meetings, cards = len(unfiled_meetings()), count()
    if not meetings and not cards:
        return None
    parts = []
    if meetings:
        parts.append(f"{meetings} meeting{'s' if meetings != 1 else ''} this week with outside people and no record")
    if cards:
        parts.append(f"{cards} organization{'s' if cards != 1 else ''} waiting")
    return notify.send(user, Notification.Kind.MEETING, "Needs filing: " + "; ".join(parts) + ".",
                       reverse("crm:needs_filing"), None, key=f"needs-filing:{today}")


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def _article(word):
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def guess_project(subjects, meetings, thread_ids, projects=None):
    """The project the organization is most likely about: (project, why) or (None, "").

    Title and subject words that name a project or its keywords count most; then projects the same email threads are
    tagged with; then projects the other firms and companies at its meetings work on."""
    projects = projects if projects is not None else list(Project.objects.filter(active=True).select_related("campus"))
    votes, why = Counter(), {}
    text = " \n ".join(subjects + [m.title for m in meetings])
    for p in projects:
        words = {p.name, *(p.keywords or [])} | ({p.campus.code} if p.campus_id else set())
        words = [w.strip() for w in words if w and len(w.strip()) >= 3]
        if not words:
            continue
        rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in words) + r")(?![\w-])", re.I)
        hit = rx.search(text)
        if hit:
            votes[p.pk] += 3
            why.setdefault(p.pk, f"“{hit.group(1)}” is in a subject or meeting title")
    if thread_ids:
        by_pk = {p.pk: p for p in projects}
        tagged = list(Activity.projects.through.objects.filter(activity__thread_id__in=thread_ids)
                      .values_list("project_id", flat=True))
        tagged += list(MailFiling.objects.filter(thread_id__in=thread_ids, project__isnull=False)
                       .values_list("project_id", flat=True))
        for pk in tagged:
            if pk in by_pk:
                votes[pk] += 2
                why.setdefault(pk, "the same email threads are filed on it")
    for m in meetings:
        for f in m.firms.all():
            for pk in {s.project_id for s in f.sources.all()} | {t.project_id for t in f.team_roles.all()}:
                votes[pk] += 1
                why.setdefault(pk, f"{f.name} at the same meeting works on it")
        for c in m.companies.all():
            for o in c.opportunities.all():
                for p in projects:
                    if o.campus_id and p.campus_id == o.campus_id and p.parent_id is None:
                        votes[p.pk] += 1
                        why.setdefault(p.pk, f"{c.name} at the same meeting has a deal there")
    if not votes:
        return None, ""
    pk = max(votes, key=lambda k: (votes[k], -k))
    return next(p for p in projects if p.pk == pk), why[pk]


def headline(card):
    """(headline, the primary action's question or "") from Claude's verdict."""
    s, name = card.sort, card.name
    if s is None:
        return "Not sorted yet.", ""
    if s.verdict == DomainSort.Verdict.DIRECTORY:
        kind = (s.kind_label or "firm").split(" / ")[0].lower()
        head = f"Looks like {_article(kind)}."
        if s.kind in CAPITAL_ROLES and card.project:
            return head, f"File {name} as {_article(CAPITAL_ROLES[s.kind][1])} on {card.project.name}?"
        return head, f"File {name} in the Directory as {_article(kind)}?"
    if s.verdict == DomainSort.Verdict.COMPANY:
        return "Looks like a prospect.", f"Add {name} as a company?"
    if s.verdict == DomainSort.Verdict.IGNORE:
        return "Looks like a newsletter or a one-off.", "Ignore it?"
    return "Claude isn't sure who this is.", ""


def _order(domains, sorts):
    def key(item):
        d, r = item
        s = sorts.get(d)
        return (0 if r["meetings"] else 1,
                VERDICT_RANK.get(s.verdict, 2) if s else 2,
                CONFIDENCE_RANK.get(s.confidence, 2) if s else 2,
                -(r["last"].timestamp() if r["last"] else 0))
    return [d for d, _ in sorted(domains.items(), key=key)]


def cards(page=1, only=""):
    """The page of cards, best first: ones with a meeting, then Claude's surest verdicts, then the latest."""
    from .directory import sort as dsort
    from .mail.sync import guess_company_name

    domains = open_domains()
    if only:
        domains = {d: r for d, r in domains.items() if d == only}
    sorts = dsort.suggestions()
    pager = Paginator(_order(domains, sorts), PER_PAGE).get_page(page)
    shown = list(pager.object_list)
    rows = defaultdict(list)
    for e in UnmatchedEmail.objects.filter(domain__in=shown).order_by("-occurred_at"):
        rows[e.domain].append(e)
    meeting_ids = {pk for d in shown for pk in domains[d]["meetings"]}
    by_pk = Meeting.objects.prefetch_related("firms__sources", "firms__team_roles", "companies__opportunities").in_bulk(
        meeting_ids)
    projects = list(Project.objects.filter(active=True).select_related("campus"))
    out = []
    for d in shown:
        emails = rows.get(d, [])
        mtgs = sorted((by_pk[pk] for pk in domains[d]["meetings"] if pk in by_pk), key=lambda m: m.start, reverse=True)
        people = {}
        for e in emails:
            people.setdefault(e.address, e.name)
        for m in mtgs:
            for a in m.unfiled:
                if _domain(a["email"]) == d:
                    people.setdefault(a["email"], a.get("name", ""))
        subjects = []
        for e in emails:
            if e.subject and e.subject not in subjects:
                subjects.append(e.subject)
        s = sorts.get(d)
        name = (s.name if s and s.name else "") or guess_company_name(d)
        card = SimpleNamespace(
            domain=d, name=name, sort=s, emails=emails, count=len(emails), meetings=mtgs,
            invites=[m for m in mtgs if m.source == "invite"], last=domains[d]["last"],
            sent=sum(1 for e in emails if e.kind == Activity.Kind.EMAIL_SENT),
            people=list(people.items()), subjects=subjects[:3],
        )
        card.received = card.count - card.sent
        threads = {e.thread_id for e in emails if e.thread_id}
        card.project, card.project_why = guess_project(subjects[:20], mtgs, threads, projects)
        card.headline, card.question = headline(card)
        card.kind = (s.kind if s and s.verdict == DomainSort.Verdict.DIRECTORY else "") or "consultant"
        card.as_ = s.verdict if s and s.verdict in ("directory", "company", "ignore") else "directory"
        out.append(card)
    return out, pager, len(domains)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _decide(domain, user, status=DomainSort.Status.DONE):
    DomainSort.objects.filter(domain=domain, company__isnull=True, status=DomainSort.Status.SUGGESTED).update(
        status=status, decided_by=user, decided_at=timezone.now())


def add_capital_source(firm, project, user=None):
    """An Identified capital source for a bank, debt fund or broker on the project (once). Returns it or None."""
    role = CAPITAL_ROLES.get(firm.kind)
    if role is None or project is None:
        return None
    source = CapitalSource.objects.filter(project=project, provider=firm).first()
    if source:
        return source
    today = timezone.localdate()
    source = CapitalSource.objects.create(project=project, provider=firm, kind=role[0], status_since=today,
                                          owner=user, created_by=user,
                                          label="Debt placement" if firm.kind == "broker" else "")
    CapitalLog.objects.create(source=source, kind=CapitalLog.Kind.NOTE, happened_on=today, user=user,
                              text=f"Added from Needs filing ({', '.join(firm.domains[:2])}).")
    return source


def accept(domain, user, as_=None, kind="", name="", project=None, existing=None):
    """Do what the card proposes, or what Change picked: file the firm, add the company, or ignore the domain.
    Returns the message to show."""
    from .directory import logic, sort as dsort
    from .mail import sync

    domain = domain.strip().lower().lstrip("@")
    s = dsort.suggestions().get(domain)
    as_ = as_ or (s.verdict if s else "")
    meetings_n = Meeting.objects.filter(attendees__icontains=f"@{domain}").count()
    if as_ == "directory":
        kind = kind or (s.kind if s and s.kind else Firm.Kind.OTHER)
        name = " ".join((name or "").split())
        with transaction.atomic():
            if s and (not name or name == s.name) and kind == (s.kind or Firm.Kind.OTHER):
                firm, moved = dsort.file(s, user)
            else:
                firm, moved = logic.file_domain(domain, kind, name or (s.name if s else ""), user)
                _decide(domain, user)
            FilingSnooze.objects.filter(domain=domain).delete()
            refresh_domain(domain)
            source = add_capital_source(firm, project, user)
        text = f"{domain} → {firm.name} in the Directory ({firm.get_kind_display().lower()})"
        bits = [f"{moved} email{'s' if moved != 1 else ''} filed"] if moved else []
        if meetings_n:
            bits.append(f"{meetings_n} meeting{'s' if meetings_n != 1 else ''} linked")
        if source:
            bits.append(f"added as an identified source on {project.name}'s Capital")
        return text + (": " + ", ".join(bits) if bits else "") + "."
    if as_ == "company":
        with transaction.atomic():
            company, stats = sync.approve_domain(domain, company=existing, company_name=name or (s.name if s else ""),
                                                 user=user)
            _decide(domain, user)
            FilingSnooze.objects.filter(domain=domain).delete()
            refresh_domain(domain)
        return (f"{domain} → {company.name}: {stats['activities created']} emails logged"
                + (f", {meetings_n} meeting{'s' if meetings_n != 1 else ''} linked" if meetings_n else "") + ".")
    if as_ == "ignore":
        never(domain, user, note="Ignored from Needs filing")
        return f"{domain} is ignored. Mail with people already in the CRM is still logged."
    raise ValueError("Pick what to file it as.")


def not_now(domain, user):
    FilingSnooze.objects.update_or_create(domain=domain.strip().lower(), defaults={
        "until": timezone.localdate() + timedelta(days=SNOOZE_DAYS), "created_by": user})


def never(domain, user, note="Never, from Needs filing"):
    from .mail import sync

    domain = domain.strip().lower().lstrip("@")
    sync.dismiss_domain(domain, kind=MailFilter.Kind.IGNORE, user=user, note=note)
    _decide(domain, user, DomainSort.Status.DISMISSED)
    FilingSnooze.objects.filter(domain=domain).delete()
    refresh_domain(domain)
