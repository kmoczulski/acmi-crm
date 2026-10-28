"""Mail search beyond Gmail's literal words (David review, 2026-10-26): a company, firm or person typed by name also
finds mail to and from their addresses, and the address book behind the To / Cc / Bcc suggestions.

Gmail only matches the words in a message, so "Firehawk" misses a thread where nobody wrote the company's name.
`expand` ORs in the addresses the CRM knows for that name (Gmail's {a b c} is "any of"). Queries that already use
Gmail operators (from:, has:, ...) are left as typed.
"""
import re

from django.db.models import Q, TextField
from django.db.models.functions import Cast

from ..models import Company, Firm, MailThread, Person
from ..permissions import team_users

OPERATOR = re.compile(r"\b[a-z_]+:", re.I)
MAX_TERMS = 12  # addresses ORed in; Gmail rejects very long queries


def _terms(addresses):
    """Gmail terms matching mail from, to or cc an address or a whole domain."""
    out = []
    for a in addresses:
        a = a.strip().lower()
        if a:
            out += [f"from:{a}", f"to:{a}", f"cc:{a}"]
    return out


def matches(words):
    """[(kind, label, addresses)] the CRM knows for these words: companies and firms by name (their domains),
    people by name (their emails). Two or more letters; the closest few of each."""
    words = " ".join(words.split())
    if len(words) < 2:
        return []
    found = []
    starts = Q(name__istartswith=words) | Q(name__icontains=f" {words}")
    for c in Company.objects.filter(starts).exclude(domains=[]).order_by("name")[:3]:
        found.append(("company", c.name, list(c.domains)))
    for f in Firm.objects.filter(starts).exclude(domains=[]).order_by("name")[:2]:
        found.append(("firm", f.name, list(f.domains)))
    people = Person.objects.filter(Q(full_name__istartswith=words) | Q(full_name__icontains=f" {words}")) \
        .exclude(email__isnull=True).exclude(email="").order_by("full_name")[:3]
    for p in people:
        found.append(("person", p.full_name, [p.email, *(p.other_emails or [])]))
    return found


def expand(query):
    """(gmail query, [(kind, label)] it was widened with). Plain words only; operators pass through untouched."""
    q = (query or "").strip()
    if not q or OPERATOR.search(q) or any(ch in q for ch in '{}()"'):
        return q, []
    found = matches(q)
    terms, used = [], []
    for kind, label, addresses in found:
        extra = _terms(addresses)
        if len(terms) + len(extra) > MAX_TERMS * 3:
            break
        terms += extra
        used.append((kind, label))
    if not terms:
        return q, []
    words = q if " " not in q else f'"{q}"'
    return "{" + " ".join([words, *terms]) + "}", used


def recipients(user, words, limit=8):
    """Address-book suggestions for the To / Cc / Bcc fields: [{"name", "email", "detail"}], best first.

    Teammates, CRM people, directory firm contacts, then people who have written to this mailbox; names or
    addresses starting with the words rank above ones that only contain them. A company's or firm's name finds its
    people too ("booz" lists Booz Allen's contacts), with an "Everyone at <company>" row ({"emails": [...]}) first
    when it has more than one.
    """
    w = " ".join((words or "").split()).lower()
    if len(w) < 2:
        return []
    seen, out, groups = set(), [], []

    def add(name, email, detail="", rank=None):
        email = (email or "").strip().lower()
        name = (name or "").strip()
        if not email or "@" not in email:
            return
        if rank is None:
            if w not in f"{name.lower()} {email}":
                return
            starts = name.lower().startswith(w) or email.startswith(w) or f" {w}" in f" {name.lower()}"
            rank = 0 if starts else 1
        if email in seen:  # found twice (by address and by company): keep the better place
            for r in out:
                if r["email"] == email and rank < r["_rank"]:
                    r["_rank"] = rank
            return
        seen.add(email)
        out.append({"name": name, "email": email, "detail": detail, "_rank": rank})

    def group(org, contacts, rank):
        emails = []
        for name, email in contacts:
            email = (email or "").strip().lower()
            if email and "@" in email and email not in emails:
                emails.append(email)
        if len(emails) > 1:
            groups.append({"name": f"Everyone at {org} ({len(emails)})", "email": ", ".join(emails), "detail": "",
                           "emails": emails, "_rank": rank})

    def org_rank(org):
        # By the company's name: just after the people whose own name matches.
        return 0.5 if org.lower().startswith(w) or f" {w}" in f" {org.lower()}" else 1.5

    for u in team_users().filter(  # the team only: a capital partner's account is never an "ACMI" address
            Q(first_name__icontains=w) | Q(last_name__icontains=w) | Q(email__icontains=w)).exclude(email="")[:10]:
        add(u.get_full_name(), u.email, "ACMI")
    people = Person.objects.filter(Q(full_name__icontains=w) | Q(email__icontains=w)) \
        .exclude(email__isnull=True).exclude(email_valid=False).select_related("company") \
        .order_by("-last_interaction_at", "full_name")[:25]
    for p in people:
        add(p.full_name, p.email, p.company.name if p.company else "")
    for co in Company.objects.filter(name__icontains=w).order_by("-last_interaction_at", "name")[:3]:
        staff = list(co.people.exclude(email__isnull=True).exclude(email="").exclude(email_valid=False)
                     .order_by("-last_interaction_at", "full_name").values_list("full_name", "email")[:25])
        group(co.name, staff, org_rank(co.name) - 0.1)
        for name, email in staff:
            add(name, email, co.name, rank=org_rank(co.name))
    firms = Firm.objects.annotate(c=Cast("contacts", TextField())).filter(Q(c__icontains=w) | Q(name__icontains=w))[:10]
    for f in firms:
        by_name = w in f.name.lower()
        if by_name:
            group(f.name, [(c.get("name"), c.get("email")) for c in f.contacts or []], org_rank(f.name) - 0.1)
        for c in f.contacts or []:
            add(c.get("name"), c.get("email"), f.name, rank=org_rank(f.name) if by_name else None)
    mailbox = getattr(user, "mailbox", None)
    if mailbox is not None:
        threads = MailThread.objects.filter(mailbox=mailbox).annotate(s=Cast("senders", TextField())) \
            .filter(s__icontains=w).order_by("-last_message_at").values_list("senders", flat=True)[:40]
        for senders in threads:
            for name, address in senders or []:
                if (address or "").lower() != mailbox.email.lower():
                    add(name, address, "")
    out.sort(key=lambda r: r["_rank"])  # stable: keeps teammates, then CRM, then mail order within each rank
    if groups:
        limit = max(limit, 12)  # a company's people fill the list
    out = sorted(groups[:2] + out, key=lambda r: r["_rank"])[:limit]
    return [{k: v for k, v in r.items() if k != "_rank"} for r in out]
