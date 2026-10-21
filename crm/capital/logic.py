"""Where each capital source stands and what's next: the stack per project, the latest term sheet, the last touch
(emails and Granola meetings with the provider, plus the log), who has the ball, term sheets side by side, and the
lines the dashboard, reminders, calendar and campus summary show."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Q
from django.utils import timezone

from ..directory.logic import emails, file_queued, meetings, short_name, upcoming_meetings  # noqa: F401
from ..models import CapitalLog, CapitalSource, Notification, TermSheet
from ..templatetags.crm_tags import money

EXPIRY_LEADS = (14, 7, 1, 0)  # reminder days before a term sheet offer expires
STEP_LEADS = (1, 0)  # before a next step's date
SOON = 14  # an expiry or next step this close is a flag on the campus summary
QUIET_DAYS = 21  # an active source with no touch this long is a flag
TOUCH_DAYS = 120  # how far back the touches list looks

# The order a stack reads, top (first money out) to bottom: equity, then the debt in order of seniority, then the rest.
STACK_ORDER = ["equity", "pref", "mezz", "construction", "senior", "bridge", "perm", "cpace", "bonds", "seller",
               "grant", "tax_credit", "other"]
GROUPS = [("Equity", ("equity", "pref")),
          ("Debt", ("mezz", "construction", "senior", "bridge", "perm", "cpace", "bonds", "seller")),
          ("Grants and incentives", ("grant", "tax_credit")), ("Other", ("other",))]

# The key terms in the order the comparison shows them: (field, kind, applies to). kind: money, pct, bps, x, months,
# text, long (several lines), date. Applies to "debt", "equity" or "" (both).
TERMS = [
    ("amount", "money", ""), ("rate", "text", "debt"), ("spread_bps", "bps", "debt"), ("fixed_rate_pct", "pct", "debt"),
    ("floor_pct", "pct", "debt"), ("term_months", "months", ""), ("extensions", "text", "debt"),
    ("amortization", "text", "debt"), ("ltc_pct", "pct", "debt"), ("ltv_pct", "pct", "debt"),
    ("dscr_min", "x", "debt"), ("debt_yield_pct", "pct", "debt"), ("origination_fee_pct", "pct", ""),
    ("exit_fee_pct", "pct", ""), ("other_fees", "text", ""), ("interest_reserve", "text", "debt"),
    ("recourse", "text", "debt"), ("guaranty", "text", ""), ("prepayment", "text", "debt"),
    ("equity_required", "text", "debt"), ("pref_return_pct", "pct", "equity"), ("promote", "text", "equity"),
    ("covenants", "long", ""), ("deposit", "money", ""), ("conditions", "long", ""), ("expires_on", "date", ""),
    ("close_by", "date", ""),
]
TERM_KINDS = {f: k for f, k, _ in TERMS}
OPEN_SHEETS = (TermSheet.Status.RECEIVED, TermSheet.Status.COUNTERED, TermSheet.Status.ACCEPTED)


def fmt(d):
    return f"{d:%b} {d.day}"


def term_label(field):
    return TermSheet._meta.get_field(field).verbose_name


def show(ts, field):
    """A term as the comparison prints it."""
    value = getattr(ts, field)
    if value in (None, ""):
        return ""
    kind = TERM_KINDS[field]
    if kind == "money":
        return money(value)
    if kind == "pct":
        return f"{value.normalize():f}%"
    if kind == "bps":
        return f"{value.normalize():f} bps"
    if kind == "x":
        return f"{value.normalize():f}x"
    if kind == "months":
        return f"{value} mo"
    if kind == "date":
        return f"{value:%b} {value.day}, {value.year}"
    return str(value)


def sort_key(source):
    return (STACK_ORDER.index(source.kind) if source.kind in STACK_ORDER else 99, source.pk)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def set_status(source, status, user=None, today=None, note=""):
    """Change the status, date it, and log the change. Returns True when it changed."""
    if status == source.status or status not in CapitalSource.Status.values:
        return False
    today = today or timezone.localdate()
    old = source.get_status_display()
    source.status, source.status_since = status, today
    source.save(update_fields=["status", "status_since", "updated_at"])
    CapitalLog.objects.create(source=source, kind=CapitalLog.Kind.STATUS, happened_on=today, user=user,
                              text=f"{old} → {source.get_status_display()}" + (f": {note}" if note else ""))
    return True


def advance(source, status, user=None, today=None):
    """Move an active source forward to `status` (never back, never off closed or passed)."""
    order = CapitalSource.STATUS_ORDER
    if source.status in CapitalSource.ACTIVE and status in order and order.index(status) > order.index(source.status):
        return set_status(source, status, user, today)
    return False


def next_version(source):
    last = source.term_sheets.order_by("-version").first()
    return (last.version + 1) if last else 1


# ---------------------------------------------------------------------------
# Touches: the log, emails filed on the provider, and Granola / calendar meetings
# ---------------------------------------------------------------------------


def touches(source, today=None, days=TOUCH_DAYS, limit=30, provider=None):
    """Everything that happened with the provider, newest first: [{date, kind, text, url, who}]. For a source: its
    log plus the provider's emails and meetings; for a provider (no source): every source's log."""
    today = today or timezone.localdate()
    since = today - timedelta(days=days)
    provider = provider or source.provider
    rows = []
    log = CapitalLog.objects.filter(source=source) if source else CapitalLog.objects.filter(source__provider=provider)
    for e in log.filter(happened_on__gte=since).select_related("user", "source__project"):
        rows.append(SimpleNamespace(date=e.happened_on, kind=e.get_kind_display(), text=e.text, url="", who=e.user,
                                    log=e, project=e.source.project))
    for e in emails(provider, since):
        rows.append(SimpleNamespace(date=timezone.localtime(e.occurred_at).date(), kind=e.get_kind_display(),
                                    text=e.subject or "(no subject)", url="", who=None, log=None,
                                    detail=f"{e.name or e.address}: {' '.join(e.snippet.split())[:300]}"))
    for m in meetings(provider, since, until=timezone.now()):
        rows.append(SimpleNamespace(date=timezone.localtime(m.start).date(), kind="Meeting", text=m.title,
                                    url=m.get_absolute_url(), who=None, log=None, meeting=m))
    rows.sort(key=lambda r: r.date, reverse=True)
    return rows[:limit]


def last_touch(source):
    """(date, what) of the latest email, meeting or log entry (not status changes), or (None, "")."""
    best = (None, "")
    e = source.log.exclude(kind=CapitalLog.Kind.STATUS).order_by("-happened_on").first()
    if e:
        best = (e.happened_on, e.get_kind_display().lower())
    mail = emails(source.provider, limit=1)
    if mail:
        d = timezone.localtime(mail[0].occurred_at).date()
        if not best[0] or d > best[0]:
            best = (d, mail[0].get_kind_display().lower())
    m = meetings(source.provider, limit=1, until=timezone.now())
    if m:
        d = timezone.localtime(m[0].start).date()
        if not best[0] or d > best[0]:
            best = (d, "meeting")
    return best


# ---------------------------------------------------------------------------
# Per source and per project
# ---------------------------------------------------------------------------


def with_related(qs):
    return qs.select_related("project", "project__campus", "provider", "owner", "contract").prefetch_related(
        "term_sheets")


def latest(source):
    """The newest term sheet still in play (else the newest at all)."""
    sheets = sorted(source.term_sheets.all(), key=lambda t: (t.version, t.pk))
    live = [t for t in sheets if t.status in OPEN_SHEETS]
    return (live or sheets or [None])[-1]


def annotate(sources, today=None, touch=True):
    """Adds .ts (latest term sheet), .expiring_in, .waiting_days, .next_left, .touch_on, .touch_what, .quiet."""
    today = today or timezone.localdate()
    for s in sources:
        s.ts = latest(s)
        s.expiring_in = ((s.ts.expires_on - today).days
                         if s.ts and s.ts.expires_on and s.ts.status in OPEN_SHEETS and s.is_active else None)
        s.waiting_days = (today - s.waiting_since).days if s.waiting_on and s.waiting_since else None
        s.next_left = (s.next_step_on - today).days if s.next_step_on else None
        s.versions = len(s.term_sheets.all())
        if touch:
            s.touch_on, s.touch_what = last_touch(s)
            s.touch_days = (today - s.touch_on).days if s.touch_on else None
            s.quiet = s.is_active and (s.touch_days is None or s.touch_days >= QUIET_DAYS)
    return sources


def stack(project, sources=None):
    """The capital stack: groups (equity, debt, grants) of sources in stack order, with sought and committed totals
    (passed and declined sources are listed but don't count)."""
    sources = sorted(sources if sources is not None else with_related(project.capital_sources.all()), key=sort_key)
    groups, total = [], {"sought": Decimal(0), "committed": Decimal(0)}
    for name, kinds in GROUPS:
        rows = [s for s in sources if s.kind in kinds or (name == "Other" and s.kind not in STACK_ORDER)]
        if not rows:
            continue
        live = [s for s in rows if s.status not in (CapitalSource.Status.PASSED, CapitalSource.Status.DECLINED)]
        # Several lenders quoting the same senior loan are alternatives, not layers: count the largest per kind.
        sought = sum((max((s.amount_sought or 0 for s in live if s.kind == k), default=0)
                      for k in {s.kind for s in live}), Decimal(0))
        committed = sum((s.amount_committed or 0 for s in live), Decimal(0))
        groups.append({"name": name, "rows": rows, "sought": sought, "committed": committed})
        total["sought"] += sought
        total["committed"] += committed
    return {"groups": groups, "total": total, "count": len(sources)}


def compare(sheets):
    """Term sheets side by side: [{"label", "values": [str], "changed"}] for every term at least one of them states,
    debt or equity terms as the sources call for."""
    sheets = list(sheets)
    kinds = {"debt" if t.source.is_debt else "equity" if t.source.kind in CapitalSource.EQUITY else "" for t in sheets}
    rows = []
    for field, _, applies in TERMS:
        if applies and kinds and applies not in kinds and "" not in kinds:
            continue
        values = [show(t, field) for t in sheets]
        if not any(values):
            continue
        cells = [{"value": v, "source": (t.sources or {}).get(field)} for t, v in zip(sheets, values)]
        rows.append({"field": field, "label": term_label(field), "values": values, "cells": cells,
                     "changed": len(set(values)) > 1, "long": TERM_KINDS[field] == "long"})
    return rows


def project_rollup(project, today=None):
    """The project page's Capital panel: stack totals, the active sources and the next expiry."""
    today = today or timezone.localdate()
    sources = annotate(list(with_related(project.capital_sources.all())), today, touch=False)
    st = stack(project, sources)
    active = sorted([s for s in sources if s.is_active], key=sort_key)
    expiring = sorted((s for s in active if s.expiring_in is not None), key=lambda s: s.expiring_in)
    return {"stack": st, "active": active, "expiring": expiring[0] if expiring else None, "empty": not sources}


# ---------------------------------------------------------------------------
# Deadlines, reminders, the calendar and the campus summary
# ---------------------------------------------------------------------------


def _live_sheets():
    return (TermSheet.objects.filter(status__in=OPEN_SHEETS, source__status__in=CapitalSource.ACTIVE)
            .select_related("source", "source__project", "source__provider", "source__owner"))


def _current(sheets):
    """Only the newest open version per source: an older version's expiry no longer matters."""
    best = {}
    for t in sheets:
        if t.source_id not in best or (t.version, t.pk) > (best[t.source_id].version, best[t.source_id].pk):
            best[t.source_id] = t
    return list(best.values())


def due_rows(days=14, today=None):
    """Term sheet expiries, outside closing dates and next steps within `days` (or missed), as dashboard deadline
    rows (tag Capital)."""
    today = today or timezone.localdate()
    until = today + timedelta(days=days)
    rows = []
    for t in _current(_live_sheets().filter(Q(expires_on__lte=until) | Q(close_by__lte=until))):
        s = t.source
        what = f"{s.project.name}: {s.provider}"
        if t.expires_on and t.expires_on <= until and t.status != TermSheet.Status.ACCEPTED:
            rows.append(SimpleNamespace(date=t.expires_on, label=f"{t.get_kind_display()} v{t.version} expires",
                                        amount=t.amount, url=t.get_absolute_url(), what=what, who=s.owner,
                                        tag="Capital"))
        if t.close_by and t.close_by <= until:
            rows.append(SimpleNamespace(date=t.close_by, label="Close by", amount=t.amount, url=t.get_absolute_url(),
                                        what=what, who=s.owner, tag="Capital"))
    for s in (CapitalSource.objects.filter(status__in=CapitalSource.ACTIVE, next_step_on__isnull=False,
                                           next_step_on__lte=until).select_related("project", "provider", "owner")):
        rows.append(SimpleNamespace(date=s.next_step_on, label=s.next_step or "Next step", amount=None,
                                    url=s.get_absolute_url(), what=f"{s.project.name}: {s.provider}", who=s.owner,
                                    tag="Capital"))
    return rows


def critical_rows(first, last, today=None):
    """For the critical dates calendar (executed agreements only): a signed or accepted term sheet or commitment's
    expiry and outside closing date."""
    today = today or timezone.localdate()
    rows = []
    qs = (TermSheet.objects.filter(status=TermSheet.Status.ACCEPTED)
          .filter(Q(expires_on__range=(first, last)) | Q(close_by__range=(first, last)))
          .select_related("source", "source__project", "source__provider", "source__owner"))
    for t in qs:
        s = t.source
        done = s.status == CapitalSource.Status.CLOSED
        for when, label in ((t.expires_on, f"{t.get_kind_display()} expires"), (t.close_by, "Close by")):
            if when and first <= when <= last:
                rows.append(SimpleNamespace(date=when, label=label, amount=t.amount, url=t.get_absolute_url(),
                                            what=f"{s.project.name}: {s.provider}", who=s.owner, done=done,
                                            tag="Loan" if s.is_debt else "Capital"))
    return rows


def _bucket(left, leads):
    return "late" if left < 0 else min(lead for lead in leads if lead >= left)


def _when(left, d):
    if left < 0:
        return f"was {fmt(d)}"
    return "today" if left == 0 else f"in {left} day{'s' if left != 1 else ''} ({fmt(d)})"


def reminders(user, today=None):
    """A term sheet offer expiring (14, 7 and 1 days ahead, on the day, once lapsed) and a next step's date (the day
    before, on the day, once missed), for whoever runs the source (else whoever created it). From notify.due_sweep."""
    from .. import notify

    today = today or timezone.localdate()
    mine = Q(source__owner=user) | Q(source__owner__isnull=True, source__created_by=user)
    sheets = _current(_live_sheets().filter(mine).exclude(status=TermSheet.Status.ACCEPTED)
                      .filter(expires_on__isnull=False, expires_on__lte=today + timedelta(days=EXPIRY_LEADS[0])))
    for t in sheets:
        left = (t.expires_on - today).days
        verb = "expired" if left < 0 else "expires"
        notify.send(user, Notification.Kind.DEADLINE,
                    f"Capital, {t.source.project.name}: {t.source.provider}'s {t.get_kind_display().lower()} {verb} "
                    f"{_when(left, t.expires_on)}", t.get_absolute_url(), None,
                    key=f"tsexp:{t.pk}:{t.expires_on:%Y%m%d}:{_bucket(left, EXPIRY_LEADS)}")
    theirs = Q(owner=user) | Q(owner__isnull=True, created_by=user)
    for s in (CapitalSource.objects.filter(theirs, status__in=CapitalSource.ACTIVE, next_step_on__isnull=False,
                                           next_step_on__lte=today + timedelta(days=STEP_LEADS[0]))
              .select_related("project", "provider")):
        left = (s.next_step_on - today).days
        notify.send(user, Notification.Kind.DEADLINE,
                    f"Capital, {s.project.name}: {s.provider}, {s.next_step or 'next step'} {_when(left, s.next_step_on)}",
                    s.get_absolute_url(), None, key=f"capstep:{s.pk}:{s.next_step_on:%Y%m%d}:{_bucket(left, STEP_LEADS)}")


def campus_lines(project, today):
    """(facts, flags) for a campus summary project item: each active source's stage, latest terms and who has the
    ball; flags for an offer expiring within two weeks, a next step due or missed, and a source gone quiet."""
    facts, flags = [], []
    sources = annotate(sorted(with_related(project.capital_sources.filter(status__in=CapitalSource.ACTIVE)),
                              key=sort_key), today)
    for s in sources:
        fact = f"{s.get_kind_display()} with {s.provider}: {s.get_status_display().lower()}"
        if s.amount_sought:
            fact += f", {money(s.amount_sought)} sought"
        if s.ts and (s.ts.rate or s.ts.amount):
            fact += f"; latest {s.ts.get_kind_display().lower()} " + ", ".join(
                x for x in (money(s.ts.amount), s.ts.rate) if x)
        if s.waiting_on:
            fact += f"; {s.get_waiting_on_display().lower()}" + (f" for {s.waiting_days} days" if s.waiting_days else "")
        if s.next_step:
            fact += f"; next: {s.next_step}" + (f" by {fmt(s.next_step_on)}" if s.next_step_on else "")
        facts.append(fact)
        if s.expiring_in is not None and s.expiring_in <= SOON and s.ts.status != TermSheet.Status.ACCEPTED:
            flags.append(f"{s.provider}'s {s.ts.get_kind_display().lower()} "
                         + (f"expired {fmt(s.ts.expires_on)}" if s.expiring_in < 0
                            else f"expires {'today' if s.expiring_in == 0 else f'in {s.expiring_in} days'}"))
        if s.next_left is not None and s.next_left <= SOON:
            flags.append(f"{s.provider}: {s.next_step or 'next step'} "
                         + (f"was due {fmt(s.next_step_on)}" if s.next_left < 0 else f"due {fmt(s.next_step_on)}"))
        if s.quiet:
            flags.append(f"{s.provider} ({s.get_kind_display().lower()}): "
                         + (f"no touch in {s.touch_days} days" if s.touch_days is not None else "no touch recorded"))
    return facts, flags
