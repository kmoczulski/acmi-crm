"""People worth adding: addresses that keep turning up in a person's own mailbox index but aren't a Person in the CRM
(Kellen, 2026-11-05: "suggest a person to add, based on who the user emails").

Only the person's own index is read (crm.models.MailThread is private to its mailbox). An address counts when it wrote
in at least MIN_THREADS conversations in the last WINDOW_DAYS. Teammates, free-mail and no-reply senders, addresses
the CRM already has (as a primary or other email) and ones the person dismissed are left out. When the address's
domain belongs to a company, that company is offered so adding the person files them there."""
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from crm import prefs
from crm.models import Company, MailThread, Mailbox, Person
from crm.permissions import team_users

from .sync import FREE_MAIL

WINDOW_DAYS = 180
MIN_THREADS = 2
PREF = "people_suggest_dismissed"
AUTOMATED = re.compile(r"(^|[._-])(no-?reply|donotreply|do-not-reply|notifications?|mailer-daemon|postmaster|bounce[s]?|"
                       r"alerts?|support|billing|invoice[s]?|newsletter|news|updates?|team|info|hello|admin|system)([._-]|@)",
                       re.I)


def _domain(address):
    return address.rsplit("@", 1)[-1].lower()


def suggest(user, limit=5):
    """[{"email", "name", "threads", "last", "company"}] best first; [] when there is no mailbox or nothing to suggest."""
    boxes = list(Mailbox.objects.filter(user=user))
    if not boxes:
        return []
    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    mine = {b.email.lower() for b in boxes}
    internal = {d.lower() for d in settings.INTERNAL_EMAIL_DOMAINS}
    teammates = {(u.email or "").lower() for u in team_users()}
    dismissed = {e.lower() for e in prefs.get_prefs(user).get(PREF, [])}
    tally = {}
    for thread in MailThread.objects.filter(mailbox__in=boxes, last_message_at__gte=since).only("senders", "last_message_at"):
        seen = set()
        for sender in thread.senders or []:
            try:
                name, address = sender[0], (sender[1] or "").strip().lower()
            except (IndexError, TypeError):
                continue
            if "@" not in address or address in seen:
                continue
            seen.add(address)
            row = tally.setdefault(address, {"email": address, "name": "", "threads": 0, "last": None})
            row["threads"] += 1
            row["name"] = row["name"] or (name or "").strip()
            if thread.last_message_at and (row["last"] is None or thread.last_message_at > row["last"]):
                row["last"] = thread.last_message_at
    candidates = []
    for address, row in tally.items():
        domain = _domain(address)
        if (row["threads"] < MIN_THREADS or address in mine or address in teammates or address in dismissed
                or domain in FREE_MAIL or any(domain == d or domain.endswith("." + d) for d in internal)
                or AUTOMATED.search(address)):
            continue
        candidates.append(row)
    if not candidates:
        return []
    wanted = {r["email"] for r in candidates}
    known = set(Person.objects.filter(email__in=wanted).values_list("email", flat=True))
    for emails in Person.objects.exclude(other_emails=[]).values_list("other_emails", flat=True):
        known.update(str(e).lower() for e in (emails or []))
    by_domain = {}
    for pk, name, domains in Company.objects.exclude(domains=[]).values_list("pk", "name", "domains"):
        for d in domains or []:
            by_domain.setdefault(d.lower(), (pk, name))
    out = []
    for row in candidates:
        if row["email"] in known:
            continue
        company = by_domain.get(_domain(row["email"]))
        row["company"] = {"id": company[0], "name": company[1]} if company else None
        out.append(row)
    out.sort(key=lambda r: (-r["threads"], -(r["last"].timestamp() if r["last"] else 0)))
    return out[:limit]


def dismiss(user, email):
    gone = prefs.get_prefs(user).get(PREF, [])
    email = (email or "").strip().lower()[:255]
    if email and email not in gone:
        prefs.set_pref(user, PREF, [*gone, email][-500:])
