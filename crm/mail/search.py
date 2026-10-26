"""Mail search beyond Gmail's literal words (David review, 2026-10-26): a company, firm or person typed by name also
finds mail to and from their addresses, and the address book behind the To / Cc / Bcc suggestions.

Gmail only matches the words in a message, so "Firehawk" misses a thread where nobody wrote the company's name.
`expand` ORs in the addresses the CRM knows for that name (Gmail's {a b c} is "any of"). Queries that already use
Gmail operators (from:, has:, ...) are left as typed.
"""
import re

from django.contrib.auth import get_user_model
from django.db.models import Q, TextField
from django.db.models.functions import Cast

from ..models import Company, Firm, MailThread, Person

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
    addresses starting with the words rank above ones that only contain them.
    """
    w = " ".join((words or "").split()).lower()
    if len(w) < 2:
        return []
    seen, out = set(), []

    def add(name, email, detail=""):
        email = (email or "").strip().lower()
        name = (name or "").strip()
        if not email or "@" not in email or email in seen or w not in f"{name.lower()} {email}":
            return
        seen.add(email)
        starts = name.lower().startswith(w) or email.startswith(w) or f" {w}" in f" {name.lower()}"
        out.append({"name": name, "email": email, "detail": detail, "_rank": 0 if starts else 1})

    for u in get_user_model().objects.filter(is_active=True).filter(
            Q(first_name__icontains=w) | Q(last_name__icontains=w) | Q(email__icontains=w)).exclude(email="")[:10]:
        add(u.get_full_name(), u.email, "ACMI")
    people = Person.objects.filter(Q(full_name__icontains=w) | Q(email__icontains=w)) \
        .exclude(email__isnull=True).select_related("company").order_by("-last_interaction_at", "full_name")[:25]
    for p in people:
        add(p.full_name, p.email, p.company.name if p.company else "")
    firms = Firm.objects.annotate(c=Cast("contacts", TextField())).filter(Q(c__icontains=w) | Q(name__icontains=w))[:10]
    for f in firms:
        for c in f.contacts or []:
            add(c.get("name"), c.get("email"), f.name)
    mailbox = getattr(user, "mailbox", None)
    if mailbox is not None:
        threads = MailThread.objects.filter(mailbox=mailbox).annotate(s=Cast("senders", TextField())) \
            .filter(s__icontains=w).order_by("-last_message_at").values_list("senders", flat=True)[:40]
        for senders in threads:
            for name, address in senders or []:
                if (address or "").lower() != mailbox.email.lower():
                    add(name, address, "")
    out.sort(key=lambda r: r["_rank"])  # stable: keeps teammates, then CRM, then mail order within each rank
    return [{k: v for k, v in r.items() if k != "_rank"} for r in out[:limit]]
