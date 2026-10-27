"""Phone numbers from email signatures onto People.

Only the sender's own signature counts: the part of the email above the quoted thread (so a forwarded or quoted
colleague's signature never lands on the wrong person), from the sign-off ("Best," "Thanks," "--") or the sender's
name down, or else the last lines. Fax numbers are skipped; a mobile / office / direct label is kept when the
signature gives one ("301-555-0142 (mobile)"). Numbers are only ever added, never replace one already there, and a
person keeps at most MAX_PHONES. ACMI's own numbers (from the team's sent mail), and any number seen in signatures
from three or more email domains, are never put on anyone.

The mail sync calls learn() for each new email from a known person; `manage.py signature_phones` reads the mail
already in the hub (a dry run unless --commit).
"""
import re

from .parse import new_text

MAX_PHONES = 4
SHARED_DOMAINS = 3  # a number in signatures from this many email domains belongs to none of them
TAIL_LINES = 14  # with no sign-off or name to start from, the signature is in the last lines
SIGN_OFF = re.compile(r"^\s*(--\s*|_{3,}|best|thanks|thank you|many thanks|regards|kind regards|best regards|sincerely|"
                      r"cheers|v/r|vr|respectfully|warm regards|all the best)\b[\s,!.]*", re.I)
# 301-555-0142, (301) 555-0142, 301.555.0142, +1 301 555 0142, 1-301-555-0142, with an optional extension.
PHONE = re.compile(r"(?<![\w/.-])(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?([2-9]\d{2})[\s.-]?(\d{4})"
                   r"(?:\s*(?:x|ext\.?|extension)\s*(\d{1,6}))?(?![\w/-])", re.I)
LABELS = [  # (pattern just before the number on its line, label); first match wins
    (re.compile(r"\b(f|fax)\b\W*$", re.I), None),
    (re.compile(r"\b(m|c|mobile|cell|cellular|mob)\b\W*$", re.I), "mobile"),
    (re.compile(r"\b(d|direct|dir)\b\W*$", re.I), "direct"),
    (re.compile(r"\b(o|office|t|tel|telephone|p|phone|main|w|work|ph)\b\W*$", re.I), "office"),
]
# Lines that are never someone's own signature: legal footers and "sent from" lines.
NOISE = re.compile(r"confidential|privileged|intended (only )?for|disclaimer|unsubscribe|sent from my", re.I)


def signature(body, name=""):
    """The sender's signature lines from an email body (their own part only)."""
    text = new_text(body or "").replace("\r\n", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return []
    start = None
    for i in range(len(lines) - 1, max(-1, len(lines) - 40), -1):
        if SIGN_OFF.match(lines[i]):
            start = i
            break
    names = [w for w in (name or "").lower().split() if len(w) > 2]
    if start is None and names:
        for i in range(len(lines) - 1, max(-1, len(lines) - 25), -1):
            if any(w in lines[i].lower() for w in names) and len(lines[i]) < 60:
                start = i
                break
    if start is None:
        start = max(0, len(lines) - TAIL_LINES)
    return [ln for ln in lines[start:start + 20] if ln and not NOISE.search(ln)]


def fmt(area, prefix, line, ext=None):
    return f"{area}-{prefix}-{line}" + (f" x{ext}" if ext else "")


def digits(phone):
    """The number's own ten digits (and extension), for comparing "(301) 555-0142 (mobile)" with "301.555.0142"."""
    m = PHONE.search(phone or "")
    return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4) or ''}" if m else re.sub(r"\D", "", phone or "")[-10:]


def phones_in(lines):
    """[(number, label)] in signature lines, fax left out, each number once."""
    out, seen = [], set()
    for ln in lines:
        for m in PHONE.finditer(ln):
            before = ln[:m.start()]
            label = ""
            for pattern, lab in LABELS:
                if pattern.search(before):
                    label = lab
                    break
            if label is None:
                continue  # fax
            number = fmt(*m.groups())
            key = digits(number)
            if key in seen:
                continue
            seen.add(key)
            out.append((number, label))
    return out


def team_numbers():
    """Numbers never to put on a contact: ACMI's own (the team's signatures on mail they sent) and any number that
    turns up in signatures from SHARED_DOMAINS or more email domains (a quoted signature the cut missed, a shared
    switchboard), so it isn't anyone's own."""
    from collections import defaultdict

    from ..models import Activity

    keys = set()
    sent = Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT).exclude(body="").order_by("-occurred_at") \
        .values_list("body", flat=True)[:400]
    for body in sent:
        for number, _ in phones_in(signature(body)):
            keys.add(digits(number))
    domains = defaultdict(set)
    received = Activity.objects.filter(kind__in=[Activity.Kind.EMAIL_RECEIVED, Activity.Kind.REPLY],
                                       person__isnull=False).exclude(body="").order_by("-occurred_at") \
        .values_list("body", "person__full_name", "person__email")[:3000]
    for body, name, email in received:
        for number, _ in phones_in(signature(body, name or "")):
            domains[digits(number)].add((email or "").rsplit("@", 1)[-1])
    return keys | {k for k, ds in domains.items() if len(ds) >= SHARED_DOMAINS}


def merge(person, found, skip=frozenset()):
    """The person's phones with the newly found ones added (labels kept); None when nothing changes."""
    have = list(person.phones or [])
    known = {digits(p) for p in have}
    added = False
    for number, label in found:
        key = digits(number)
        if key in known or key in skip or len(have) >= MAX_PHONES:
            continue
        have.append(f"{number} ({label})" if label else number)
        known.add(key)
        added = True
    return have if added else None


def learn(person, body, skip=frozenset()):
    """Add the phone numbers in this email's signature to the person who sent it. True when any were added."""
    found = phones_in(signature(body, person.full_name or ""))
    phones = merge(person, found, skip) if found else None
    if phones is None:
        return False
    person.phones = phones
    person.save(update_fields=["phones", "updated_at"])
    return True
