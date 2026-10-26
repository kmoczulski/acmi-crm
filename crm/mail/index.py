"""The private mail index behind the inbox (docs/EMAIL_CLIENT.md, section 3), and the CRM tags on each thread.

Gmail stays the record. MailThread keeps one row per conversation (subject, senders, first line, labels, unread,
date) so the inbox draws fast and the split-inbox tabs work; a row is refreshed whenever Gmail says the thread
changed (its historyId in a list, or the sync's history pass). Bodies and attachments are read live
(crm/mail/render.py) and never stored here.
"""
import html
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import parseaddr

from django.db.models import Q
from django.utils import timezone

from crm.models import Activity, Company, Firm, FirmEmail, MailLabel, MailThread, UnmatchedEmail

from .gmail_api import NotFound

log = logging.getLogger(__name__)

# threads.get costs 10 of Gmail's 250 quota units a second per person; three at a time stays well under it.
PARALLEL_REQUESTS = 3
KEEP_DAYS = 90  # rows not touched for this long are dropped (Gmail still has the mail)
WARM_THREADS = 300  # the inbox's newest threads, indexed in the background so the tabs have something to show
WARM_PAGE = 100
SYSTEM_ORDER = ["INBOX", "STARRED", "IMPORTANT", "SENT", "DRAFT", "SPAM", "TRASH"]
SYSTEM_NAMES = {"INBOX": "Inbox", "STARRED": "Starred", "IMPORTANT": "Important", "SENT": "Sent", "DRAFT": "Drafts",
                "SPAM": "Spam", "TRASH": "Trash"}
HIDDEN_LABELS = {"UNREAD", "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES",
                 "CATEGORY_FORUMS"}


def _header(msg, name):
    for h in (msg.get("payload") or {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fields_from_thread(data):
    """MailThread fields from a threads.get (format=metadata) answer."""
    messages = [m for m in data.get("messages", []) if "DRAFT" not in (m.get("labelIds") or [])] \
        or data.get("messages", [])
    labels, senders, seen = set(), [], set()
    for m in data.get("messages", []):
        labels |= set(m.get("labelIds") or [])
    for m in messages:
        name, addr = parseaddr(_header(m, "From"))
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            senders.append([name, addr.lower()])
    last = messages[-1] if messages else {}
    when = None
    if last.get("internalDate"):
        when = datetime.fromtimestamp(int(last["internalDate"]) / 1000, tz=dt_timezone.utc)
    return {
        "history_id": str(data.get("historyId", "")),
        "subject": " ".join(_header(messages[0], "Subject").split())[:500] if messages else "",
        "snippet": html.unescape(last.get("snippet") or "")[:1000],
        "senders": senders[:20],
        "message_count": len(messages),
        "last_message_at": when,
        "labels": sorted(labels),
        "in_inbox": "INBOX" in labels,
        "unread": "UNREAD" in labels,
        "starred": "STARRED" in labels,
        "has_attachments": any((m.get("payload") or {}).get("mimeType") == "multipart/mixed" for m in messages),
    }


def store(mailbox, answers):
    """Write threads.get answers ({gmail id: data}) to the index, with their tab flags worked out together."""
    fields = {gid: fields_from_thread(data) for gid, data in answers.items()}
    flags = crm_flags(mailbox, list(fields), {gid: f["senders"] for gid, f in fields.items()})
    out = {}
    for gid, f in fields.items():
        f.update(flags.get(gid, {}))
        out[gid], _ = MailThread.objects.update_or_create(mailbox=mailbox, gmail_id=gid, defaults=f)
    return out


def refresh(mailbox, client, thread_ids):
    """Re-read these threads from Gmail into the index (in parallel); a thread that's gone is removed."""
    thread_ids = list(dict.fromkeys(t for t in thread_ids if t))
    if not thread_ids:
        return {}
    client.prime()

    def fetch(tid):
        try:
            return tid, client.thread(tid)
        except NotFound:
            return tid, None

    with ThreadPoolExecutor(max_workers=PARALLEL_REQUESTS) as executor:
        results = list(executor.map(fetch, thread_ids))
    gone = [tid for tid, data in results if data is None or not data.get("messages")]
    MailThread.objects.filter(mailbox=mailbox, gmail_id__in=gone).delete()
    return store(mailbox, {tid: data for tid, data in results if tid not in gone})


def page(mailbox, client, label=None, query="", page_token=""):
    """One page of threads as Gmail orders them, from the index, re-reading only the ones Gmail says changed.
    Returns (MailThread list, next page token, rough total)."""
    listed, next_token, total = client.threads(label=label, query=query, page_token=page_token)
    ids = [t["id"] for t in listed]
    have = {t.gmail_id: t for t in MailThread.objects.filter(mailbox=mailbox, gmail_id__in=ids)}
    stale = [t["id"] for t in listed if t["id"] not in have or have[t["id"]].history_id != str(t.get("historyId"))]
    have.update(refresh(mailbox, client, stale))
    return [have[i] for i in ids if i in have], next_token, total


def warm_inbox(mailbox, client, limit=WARM_THREADS):
    """Index the inbox's newest threads, a page at a time, so the split-inbox tabs are complete from the start.
    Called by the sync after each read until it's done once."""
    if mailbox.inbox_indexed_at:
        return 0
    have = MailThread.objects.filter(mailbox=mailbox, in_inbox=True).count()
    token, n = "", 0
    while have + n < limit:
        listed, token, _ = client.threads(label="INBOX", page_token=token, size=WARM_PAGE)
        known = set(MailThread.objects.filter(mailbox=mailbox, gmail_id__in=[t["id"] for t in listed])
                    .values_list("gmail_id", flat=True))
        n += len(refresh(mailbox, client, [t["id"] for t in listed if t["id"] not in known]))
        if not token or not listed:
            break
    mailbox.inbox_indexed_at = timezone.now()
    return n


def prune(mailbox):
    """Drop rows Gmail hasn't changed and nobody has opened in KEEP_DAYS."""
    cutoff = timezone.now() - timedelta(days=KEEP_DAYS)
    return MailThread.objects.filter(mailbox=mailbox, fetched_at__lt=cutoff).filter(
        Q(last_message_at__lt=cutoff) | Q(last_message_at__isnull=True)).delete()[0]


def labels(mailbox, client, max_age=timedelta(minutes=10)):
    """The folder pane: system folders in Gmail's order, then the person's own labels, refreshed every 10 minutes.
    The Inbox's unread count comes from Gmail."""
    from django.core.cache import cache

    rows = list(MailLabel.objects.filter(mailbox=mailbox))
    key = f"mail-labels-checked:{mailbox.pk}"
    if not rows or not cache.get(key):
        cache.set(key, True, int(max_age.total_seconds()))
        try:
            found = client.labels()
            inbox = client.label("INBOX")
        except Exception:  # the folder pane must never stop the inbox from showing
            log.warning("Could not read labels for %s", mailbox, exc_info=True)
            found, inbox = None, {}
        if found is not None:
            keep = []
            for lb in found:
                if lb["id"] in HIDDEN_LABELS or (lb.get("type") == "system" and lb["id"] not in SYSTEM_NAMES):
                    continue
                color = lb.get("color") or {}
                obj, _ = MailLabel.objects.update_or_create(mailbox=mailbox, gmail_id=lb["id"], defaults={
                    "name": SYSTEM_NAMES.get(lb["id"], lb["name"])[:225], "system": lb.get("type") == "system",
                    "text_color": color.get("textColor", "")[:7], "bg_color": color.get("backgroundColor", "")[:7],
                    "unread": inbox.get("threadsUnread", 0) if lb["id"] == "INBOX" else 0,
                })
                keep.append(obj.pk)
            MailLabel.objects.filter(mailbox=mailbox).exclude(pk__in=keep).delete()
            rows = list(MailLabel.objects.filter(mailbox=mailbox))
    system = sorted((r for r in rows if r.system), key=lambda r: SYSTEM_ORDER.index(r.gmail_id)
                    if r.gmail_id in SYSTEM_ORDER else 99)
    return system, sorted((r for r in rows if not r.system), key=lambda r: r.name.lower())


# ---------------------------------------------------------------------------
# CRM tags: which company, deal, project or directory firm a thread is about
# ---------------------------------------------------------------------------


def _decimal(gmail_id):
    try:
        return str(int(gmail_id, 16))
    except ValueError:
        return ""


def _domain_maps():
    companies, firms = {}, {}
    for pk, name, domains in Company.objects.values_list("pk", "name", "domains"):
        for d in domains or []:
            companies.setdefault(d.lower(), (pk, name))
    for pk, name, domains in Firm.objects.values_list("pk", "name", "domains"):
        for d in domains or []:
            firms.setdefault(d.lower(), (pk, name))
    return companies, firms


def chips(mailbox, threads):
    """{gmail thread id: [(kind, name, url), ...]} for these threads. Kinds: project, deal, company, firm, new.

    From the timeline (what the sync logged for the thread: its company, deal and projects), the directory's filed
    mail, and the senders' domains; "new" marks a sender whose domain waits in Mail review."""
    from django.urls import reverse

    by_dec = {_decimal(t.gmail_id): t for t in threads}
    out = {t.gmail_id: [] for t in threads}
    seen = {t.gmail_id: set() for t in threads}

    def add(t, kind, name, url):
        if (kind, name) not in seen[t.gmail_id]:
            seen[t.gmail_id].add((kind, name))
            out[t.gmail_id].append((kind, name, url))

    acts = Activity.objects.filter(source=Activity.Source.GMAIL, thread_id__in=[d for d in by_dec if d])
    for a in acts.select_related("company", "opportunity").prefetch_related("projects"):
        t = by_dec[a.thread_id]
        for p in a.projects.all():
            add(t, "project", p.name, p.get_absolute_url())
        if a.opportunity_id:
            add(t, "deal", str(a.opportunity), a.opportunity.get_absolute_url())
        if a.company_id:
            add(t, "company", a.company.name, a.company.get_absolute_url())
    for fe in FirmEmail.objects.filter(thread_id__in=[d for d in by_dec if d]).select_related("provider"):
        add(by_dec[fe.thread_id], "firm", fe.provider.name, reverse("crm:firm", args=[fe.provider_id]))
    companies, firms = _domain_maps()
    queued = set(UnmatchedEmail.objects.values_list("domain", flat=True).distinct())
    own = mailbox.email.lower()
    for t in threads:
        for _, addr in t.senders:
            if addr == own:
                continue
            domain = addr.rsplit("@", 1)[-1]
            if domain in companies:
                pk, name = companies[domain]
                add(t, "company", name, reverse("crm:company_detail", args=[pk]))
            elif domain in firms:
                pk, name = firms[domain]
                add(t, "firm", name, reverse("crm:firm", args=[pk]))
            elif domain in queued:
                add(t, "new", "New sender", reverse("crm:mail"))
    order = {"project": 0, "deal": 1, "company": 2, "firm": 3, "new": 4}
    return {k: sorted(v, key=lambda c: order[c[0]]) for k, v in out.items()}


def crm_flags(mailbox, gmail_ids, senders_by_id):
    """{gmail id: {is_tenant, is_project, is_firm}} for the split-inbox tabs, from the same tags as chips()."""
    fake = [MailThread(mailbox=mailbox, gmail_id=g, senders=senders_by_id.get(g, [])) for g in gmail_ids]
    out = {}
    for gid, found in chips(mailbox, fake).items():
        kinds = {c[0] for c in found}
        out[gid] = {"is_tenant": bool(kinds & {"deal", "company"}), "is_project": "project" in kinds,
                    "is_firm": "firm" in kinds}
    return out


def retag(mailbox, threads):
    """Bring the stored tab flags up to date (the timeline may have learned about a thread since it was indexed)."""
    flags = crm_flags(mailbox, [t.gmail_id for t in threads], {t.gmail_id: t.senders for t in threads})
    for t in threads:
        f = flags.get(t.gmail_id, {})
        if any(getattr(t, k) != v for k, v in f.items()):
            for k, v in f.items():
                setattr(t, k, v)
            MailThread.objects.filter(pk=t.pk).update(**f)
