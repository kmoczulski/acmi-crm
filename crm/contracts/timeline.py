"""The closing timeline: every date an agreement sets, worked out from an effective date, side by side across
versions, and the extensions that push a date out once they're exercised.

A date is either fixed ("December 31, 2026") or a rule: so many days, business days, weeks or months after an anchor,
which is the Effective Date or another date's key ("thirty (30) days after the end of the Feasibility Period" is
closing = feasibility_end + 30 days). The same "term" dict describes a kept ContractDate and a date Claude read out of
one draft (ContractDocument.timeline), so a signed contract and every negotiation round share one viewer:

    {"key", "label", "kind", "date", "anchor", "offset", "unit", "amount", "extends", "extend_by", "extend_unit",
     "rule", "quote", "doc", "start", "end"}

Rules are read with plain pattern matching (parse_rule) when a person types them, and by Claude for a whole version
(contracts/ai.py read_timeline). Business days skip weekends only; holidays are the lawyer's call.
"""
import calendar
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from ..models import Contract, ContractDate

UNITS = ("days", "business", "weeks", "months")
# The key a date of each kind gets when nobody names one, so the other dates can count from it.
KIND_KEYS = {
    "deposit": "deposit", "hard": "deposit_hard", "feasibility": "feasibility_end", "title": "title_objection",
    "approval": "approvals", "notice": "notice", "extension": "extension", "closing": "closing",
    "expiration": "expiration", "payment": "payment", "covenant": "covenant", "other": "date",
}
# What the words after "after the …" point at.
ANCHOR_WORDS = (
    (r"effective date|date (?:of|hereof)|execution|mutual execution|signing", "effective"),
    (r"feasibility|due diligence|inspection|study period|review period", "feasibility_end"),
    (r"title (?:commitment|report|defects?|objections?)|survey", "title_objection"),
    (r"approval|entitlement|rezoning|permit", "approvals"),
    (r"closing", "closing"),
    (r"expiration|termination", "expiration"),
)

_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUM_WORD = "|".join(sorted([*_ONES, *_TENS, "hundred", "and"], key=len, reverse=True))
_NUM = rf"(?P<words>\b(?:(?:{_NUM_WORD})[\s-]*)+)?\s*(?:\(\s*(?P<paren>\d{{1,4}})\s*\)|(?P<digits>\d{{1,4}}))?"
RULE = re.compile(
    _NUM + r"\s*(?P<kind>business\s+|calendar\s+|banking\s+)?(?P<unit>days?|weeks?|months?|years?)\s+"
    r"(?:(?:immediately\s+)?(?P<dir>after|following|from|before|prior\s+to)\s+)(?P<anchor>[^.;:]{0,90})",
    re.I,
)
# "on or before the end of the Feasibility Period", "before the Feasibility Period expires": the same day as it.
RULE_AT = re.compile(
    r"(?:on or before|no later than|by|before|prior to|at)\s+(?:the\s+)?(?:end|expiration|expiry|close)\s+of\s+"
    r"(?:the\s+)?(?P<anchor>[^.;:,(]{0,60})"
    r"|(?:before|by)\s+(?:the\s+)?(?P<anchor2>[^.;:,(]{0,60}?)\s+(?:expires|ends|terminates)",
    re.I,
)
# "10 days' advance notice of the Closing Date": that many days before it.
RULE_NOTICE = re.compile(
    _NUM + r"\s*(?P<kind>business\s+|calendar\s+)?(?P<unit>days?|weeks?|months?)['’]?\s+(?:advance|prior)\s+"
    r"(?:written\s+)?notice\s+(?:of|before)\s+(?:the\s+)?(?P<anchor>[^.;:]{0,60})",
    re.I,
)
EXTEND = re.compile(
    r"extend\w*\s+(?:(?:it|them|the\s+(?P<what>[^.;]{0,60}?))\s+)?(?:(?:for|by)\s+)?(?:an?\s+|one\s+)?"
    r"(?:additional\s+)?(?:period\s+of\s+)?" + _NUM + r"\s*(?P<kind>business\s+|calendar\s+)?"
    r"(?P<unit>days?|weeks?|months?)", re.I,
)


def words_to_int(text):
    total, current = 0, 0
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if w in _ONES:
            current += _ONES[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = max(current, 1) * 100
    return total + current if (total or current) else None


def _number(m):
    for g in ("paren", "digits"):
        if m.group(g):
            return int(m.group(g))
    return words_to_int(m.group("words"))


def _unit(m):
    unit = m.group("unit").lower().rstrip("s")
    if unit == "day":
        return "business" if (m.group("kind") or "").strip().lower() in ("business", "banking") else "days"
    return {"week": "weeks", "month": "months", "year": "months"}[unit]


def anchor_key(words):
    words = (words or "").lower()
    for pattern, key in ANCHOR_WORDS:
        if re.search(pattern, words):
            return key
    return ""


def parse_rule(text):
    """{"offset", "unit", "anchor"} for "three business days after the Effective Date", or None."""
    for m in RULE.finditer(text or ""):
        n = _number(m)
        if n is None:
            continue
        unit = _unit(m)
        if m.group("unit").lower().startswith("year"):
            n *= 12
        anchor = anchor_key(m.group("anchor"))
        if not anchor:
            continue
        sign = -1 if m.group("dir").lower().startswith(("before", "prior")) else 1
        return {"offset": sign * n, "unit": unit, "anchor": anchor}
    for m in RULE_NOTICE.finditer(text or ""):
        n, anchor = _number(m), anchor_key(m.group("anchor"))
        if n is not None and anchor:
            return {"offset": -n, "unit": _unit(m), "anchor": anchor}
    for m in RULE_AT.finditer(text or ""):
        anchor = anchor_key(m.group("anchor") or m.group("anchor2"))
        if anchor and anchor != "effective":
            return {"offset": 0, "unit": "days", "anchor": anchor}
    return None


def parse_extension(text):
    """{"extends", "extend_by", "extend_unit"} for "extend the Feasibility Period by thirty (30) days" or "notice by
    the end of the Feasibility Period extends it 120 days", or None. extends is "" when the text doesn't say what."""
    for m in EXTEND.finditer(text or ""):
        n = _number(m)
        if n is None:
            continue
        rest = (text or "").replace(m.group(0), " ")
        target = anchor_key(m.group("what")) or anchor_key(rest)
        return {"extends": "" if target == "effective" else target, "extend_by": n, "extend_unit": _unit(m)}
    return None


def add(d, n, unit="days"):
    """`d` moved `n` units (negative goes back). Business days skip Saturdays and Sundays."""
    if d is None or n is None:
        return None
    if unit == "business":
        step, left = (1 if n >= 0 else -1), abs(n)
        while left:
            d += timedelta(days=step)
            if d.weekday() < 5:
                left -= 1
        return d
    if unit == "weeks":
        return d + timedelta(weeks=n)
    if unit == "months":
        m = d.month - 1 + n
        y, m = d.year + m // 12, m % 12 + 1
        return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
    return d + timedelta(days=n)


def describe(t):
    """"90 days after Effective Date" for a term's rule, from its parts."""
    if t.get("offset") is None or not t.get("anchor"):
        return t.get("rule") or ""
    n, unit = t["offset"], dict(ContractDate.Unit.choices).get(t.get("unit") or "days", "days")
    if abs(n) == 1:
        unit = unit[:-1]
    anchor = "the Effective Date" if t["anchor"] == "effective" else re.sub(r"_\d+$", "", t["anchor"]).replace("_", " ")
    return f"{abs(n)} {unit} {'before' if n < 0 else 'after'} {anchor}" if n else f"on {anchor}"


# ---------------------------------------------------------------------------
# Terms from kept dates, and filling in a date's rule
# ---------------------------------------------------------------------------


def unique_key(base, taken):
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


def fill_rule(d, siblings=None):
    """Give a ContractDate a key, and read its anchor, offset and unit (or what an extension pushes) from its rule
    and quote when nobody set them. Doesn't save."""
    siblings = list(d.contract.dates.exclude(pk=d.pk)) if siblings is None else [s for s in siblings if s.pk != d.pk]
    if not d.key:
        d.key = unique_key(KIND_KEYS.get(d.kind, "date"), {s.key for s in siblings if s.key})
    text = " ".join(x for x in (d.rule, d.quote) if x)
    if not d.anchor and d.offset is None:
        found = parse_rule(text)
        if found and found["anchor"] != d.key:
            d.anchor, d.offset, d.unit = found["anchor"], found["offset"], found["unit"]
    if d.kind == ContractDate.Kind.EXTENSION and not d.extends:
        found = parse_extension(" ".join(x for x in (d.label, text) if x))
        if found:
            d.extends = found["extends"] or d.extends
            if d.extend_by is None:
                d.extend_by, d.extend_unit = found["extend_by"], found["extend_unit"]
    keys = {s.key for s in siblings if s.key}
    for field in ("anchor", "extends"):
        value = getattr(d, field)
        if value and value != "effective" and value not in keys:
            # Named by kind ("feasibility_end") but the contract's date has another key: find it by kind.
            kind = next((k for k, v in KIND_KEYS.items() if v == value), None)
            match = next((s.key for s in siblings if s.key and (s.key.startswith(value) or s.kind == kind)), "")
            setattr(d, field, match or value)
    return d


def ensure_keys(contract):
    """Dates entered before rules existed (by hand, or from Trello) get their key and rule read now, in the order
    they were added, so other dates can count from them and extensions know what they push."""
    dates = list(contract.dates.filter(confirmed=True).order_by("pk"))
    for d in dates:
        if not d.key or (d.kind == ContractDate.Kind.EXTENSION and not d.extends):
            fill_rule(d, dates)
            d.save(update_fields=["key", "anchor", "offset", "unit", "extends", "extend_by", "extend_unit"])
    return dates


def term(d):
    return {
        "key": d.key or f"date-{d.pk}", "label": d.label, "kind": d.kind, "date": d.date, "anchor": d.anchor,
        "offset": d.offset, "unit": d.unit or "days", "amount": d.amount, "extends": d.extends,
        "extend_by": d.extend_by, "extend_unit": d.extend_unit or "days", "rule": d.rule, "quote": d.quote,
        "doc": d.source_id, "start": d.source_start, "end": d.source_end, "pk": d.pk, "done": d.done,
        "history": d.history or [], "critical": d.critical,
    }


def from_dates(contract):
    return [term(d) for d in contract.dates.filter(confirmed=True).select_related("source")]


def from_document(doc):
    out = []
    for t in doc.timeline or []:
        t = dict(t)
        if isinstance(t.get("date"), str):
            try:
                t["date"] = date.fromisoformat(t["date"])
            except ValueError:
                t["date"] = None
        try:
            t["amount"] = Decimal(str(t["amount"])) if t.get("amount") not in (None, "") else None
        except InvalidOperation:
            t["amount"] = None
        t.setdefault("doc", doc.pk)
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Working the dates out
# ---------------------------------------------------------------------------


def compute(terms, effective, prefer_rules=True, today=None):
    """Each term with "when" (its date) and "computed" (worked out from its rule rather than written down), in date
    order. prefer_rules: work a date out from its rule even when one is written down (a what-if effective date, or a
    draft); otherwise a written date wins and a rule only fills a blank."""
    today = today or timezone.localdate()
    by_key = {t["key"]: t for t in terms}
    when, computed = {}, {}

    def resolve(key, seen):
        if key in when:
            return when[key]
        t = by_key.get(key)
        if t is None or key in seen:
            return None
        seen.add(key)
        out, from_rule = None, False
        if t.get("anchor") and t.get("offset") is not None and (prefer_rules or not t.get("date")):
            base = effective if t["anchor"] == "effective" else resolve(t["anchor"], seen)
            out = add(base, t["offset"], t.get("unit") or "days")
            from_rule = out is not None
        if out is None:
            out = t.get("date")
        when[key], computed[key] = out, from_rule
        return out

    rows = []
    for t in terms:
        d = resolve(t["key"], set())
        left = (d - today).days if d else None
        rows.append({**t, "when": d, "computed": computed.get(t["key"], False), "how": describe(t), "left": left,
                     "late": -left if left is not None and left < 0 else None})
    rows.sort(key=lambda r: (r["when"] is None, r["when"] or today, r["label"]))
    return rows


def _held(d):
    """A date the effective date doesn't move: it's done, or a person moved it (a deadline pushed out, an extension
    exercised: see push). The moves the app makes when the effective date changes are marked "auto" and don't count."""
    return d.done or any((h.get("to") or h.get("exercised")) and not h.get("auto") for h in d.history or [])


def _on(d):
    return f"{d:%b} {d.day}, {d.year}"


@transaction.atomic
def recompute_dates(contract, old_effective=None, user=None, today=None):
    """Move the stored dates that count from the Effective Date (straight from it, or through other dates) to where
    their rules put them now that it has changed. Returns ([(date, old, new)], left): the moves made, and how many
    dates stayed where they stand though their rules would have moved them.

    A fixed date has no rule and stays. So does a date that's done or that a person moved (see _held): nobody can say
    what they'd want, and the dates that count from it are worked out from where it stands. Claude's suggestions that
    nobody has kept yet move like the rest. Every move goes in the date's history, marked "auto"."""
    effective = contract.effective_date
    dates = list(contract.dates.all())
    if not effective or not dates:
        return [], 0
    today = today or timezone.localdate()
    held = {d.pk for d in dates if _held(d)}
    full, seen = [], set()
    # Kept dates first: when a suggestion has a kept date's key, the kept date is the one other dates count from.
    for d in sorted(dates, key=lambda d: (not d.confirmed, d.pk)):
        t = term(d)
        if t["key"] in seen:
            t["key"] = f"{t['key']}~{d.pk}"
        seen.add(t["key"])
        full.append(t)
    stand = [{**t, "anchor": "", "offset": None} if t["pk"] in held else t for t in full]  # held: fixed where it is
    now = {r["pk"]: r for r in compute(stand, effective, prefer_rules=True, today=today)}
    why = "Effective date changed " + (f"from {_on(old_effective)} to " if old_effective else "to ") + _on(effective)
    moved = []
    for d in dates:
        row = now[d.pk]
        if d.pk not in held and row["computed"] and row["when"] != d.date:
            moved.append(_move(d, row["when"], why, user, today, auto=True))
    by_rule = {r["pk"]: r for r in compute(full, effective, prefer_rules=True, today=today)} if held else {}
    left = sum(1 for d in dates if d.pk in held and by_rule[d.pk]["computed"] and by_rule[d.pk]["when"] != d.date)
    return moved, left


def compare(versions):
    """(rows, (first, last)) for the side-by-side table: [{"key", "label", "kind", "cells": [row or None per
    version], "changed"}] in date order, and the span of dates shown. `versions` is [{"rows": compute(...)}, ...]."""
    order, seen = [], {}
    for v in reversed(versions):
        for r in v["rows"]:
            if r["key"] not in seen:
                seen[r["key"]] = r
                order.append(r["key"])
    out = []
    for key in order:
        cells = [next((r for r in v["rows"] if r["key"] == key), None) for v in versions]
        present = [c for c in cells if c]
        changed = len(present) < len(cells) or len({(c["when"], c.get("amount")) for c in present}) > 1
        out.append({"key": key, "label": seen[key]["label"], "kind": seen[key]["kind"], "cells": cells,
                    "changed": changed})
    whens = [c["when"] for row in out for c in row["cells"] if c and c["when"]]
    out.sort(key=lambda row: min((c["when"] for c in row["cells"] if c and c["when"]), default=date.max))
    return out, (min(whens), max(whens)) if whens else (None, None)


def lanes(versions, span, today=None):
    """Each version's dates as markers along one axis: "pos", the % between the earliest and latest date shown."""
    first, last = span
    if not first:
        return [], []
    today = today or timezone.localdate()
    first, last = min(first, today), max(last, today)
    width = max(1, (last - first).days)
    pct = lambda d: round((d - first).days * 100 / width, 2)  # noqa: E731
    out = []
    for v in versions:
        groups = {}
        for r in v["rows"]:
            if r["when"]:
                groups.setdefault(r["when"], []).append(r)
        marks, prev = [], None
        for when in sorted(groups):
            rows = groups[when]
            pos = pct(when)
            # Close to the one before (and that one sits low): this label goes up a row so they don't overlap.
            up = bool(prev and not prev["up"] and pos - prev["pos"] < 20)
            prev = {**rows[0], "pos": pos, "rows": rows, "up": up, "done": all(r.get("done") for r in rows),
                    "computed": all(r.get("computed") for r in rows)}
            marks.append(prev)
        out.append({**v, "marks": marks})
    ticks, d = [], date(first.year, first.month, 1)
    while d <= last:
        if d >= first:
            ticks.append({"pos": pct(d), "label": f"{d:%b} ’{d:%y}" if d.month == 1 else f"{d:%b}"})
        d = add(d, 1, "months")
    now = pct(today)
    ticks = [t for t in ticks if abs(t["pos"] - now) > 5]  # a month label under "Today" would overlap it
    return out, ticks + [{"pos": now, "label": "Today", "today": True}]


# ---------------------------------------------------------------------------
# Exercising an extension, or pushing a date
# ---------------------------------------------------------------------------


def dependents(target, dates, skip=()):
    """Open dates that count from `target` (directly or down the chain), in order. `skip`: pks never moved (the
    extension being exercised counts from the date it extends, but it doesn't move with it)."""
    out, frontier = [], [target]
    while frontier:
        t = frontier.pop(0)
        for d in dates:
            if (t.key and d.anchor == t.key and d.offset is not None and not d.done and d not in out and d != target
                    and d.pk not in skip):
                out.append(d)
                frontier.append(d)
    return out


def proposal(ext_or_target, today=None, target_pk=None):
    """What exercising an extension (or pushing a date) would do, worked out so nobody has to type a date:
    {"target", "new" (its new date), "follow": [(date, new)] (dates that count from it), "also": [(date, new,
    same_day)] (other open dates after it, offered to move by the same number of days; same-day ones ticked),
    "choices": dates an extension might push, when the agreement doesn't say which}."""
    d = ext_or_target
    dates = ensure_keys(d.contract)
    d = next((x for x in dates if x.pk == d.pk), d)
    choices = []
    if d.kind == ContractDate.Kind.EXTENSION:
        target = next((x for x in dates if target_pk and x.pk == int(target_pk)), None) or next(
            (x for x in dates if d.extends and x.key == d.extends), None)
        if target is None:
            choices = [x for x in dates if x.pk != d.pk and x.date and x.kind != ContractDate.Kind.EXTENSION]
        new = add(target.date, d.extend_by, d.extend_unit or "days") if target and target.date else None
    else:
        target, new = d, None
    follow, also = [], []
    if target is not None:
        moved_to = {target.key: new}  # down the chain: each date counts from its own anchor's new date
        follow = []
        for x in dependents(target, dates, skip={d.pk}):
            when = add(moved_to.get(x.anchor), x.offset, x.unit or "days") if new else None
            moved_to[x.key] = when
            follow.append((x, when))
        moved = {target.pk, d.pk, *(x.pk for x, _ in follow)}
        shift = timedelta(days=(new - target.date).days) if new and target.date else None
        also = [(x, x.date + shift if shift else None, x.date == target.date) for x in dates
                if x.date and target.date and x.date >= target.date and x.pk not in moved and not x.done]
    return {"ext": d, "target": target, "new": new, "follow": follow, "also": also, "choices": choices}


def _move(d, new, why, user, today, auto=False):
    old = d.date
    if new == old:
        return None
    entry = {"from": old.isoformat() if old else None, "to": new.isoformat(), "why": why,
             "by": (user.get_full_name() or user.username) if user else "", "on": today.isoformat()}
    if auto:  # the app moved it (recompute_dates), not a person: a later change of the effective date still moves it
        entry["auto"] = True
    d.history = [*(d.history or []), entry]
    d.date = new
    d.done, d.done_at, d.done_by = False, None, None  # a deadline pushed out is open again
    d.save(update_fields=["date", "history", "done", "done_at", "done_by"])
    return d, old, new


@transaction.atomic
def push(target, new, user, why, also=(), ext=None, notice_on=None):
    """Move `target` to `new`, the dates that count from it with it, and `also` (other dates, by the same number of
    days). With `ext`, that extension option is marked exercised. Returns [(date, old, new)]; the site timeline and
    the feasibility decision follow."""
    from . import dates as dates_mod

    today = timezone.localdate()
    contract = target.contract
    target = next(x for x in ensure_keys(contract) if x.pk == target.pk)  # its key, so the chain can follow
    shift = (new - target.date).days if target.date else 0
    moved = [m for m in [_move(target, new, why, user, today)] if m]
    all_dates = list(contract.dates.filter(confirmed=True))
    for d in dependents(target, all_dates, skip={ext.pk} if ext is not None else ()):
        base = next((x.date for x in [target, *all_dates] if x.key == d.anchor), None)
        m = _move(d, add(base, d.offset, d.unit or "days"), f"{why} (counts from {target.label})", user, today)
        if m:
            moved.append(m)
            next(x for x in all_dates if x.pk == d.pk).date = d.date  # later dependents count from the new date
    for d in also:
        if d.date and shift:
            m = _move(d, d.date + timedelta(days=shift), f"{why} (moved with {target.label})", user, today)
            if m:
                moved.append(m)
    if ext is not None:
        ext.done, ext.done_at, ext.done_by = True, timezone.now(), user
        ext.history = [*(ext.history or []), {"exercised": (notice_on or today).isoformat(), "why": why,
                                              "by": (user.get_full_name() or user.username) if user else "",
                                              "on": today.isoformat()}]
        ext.save(update_fields=["done", "done_at", "done_by", "history"])
    if target.kind == ContractDate.Kind.FEASIBILITY and ext is not None:
        contract.decision, contract.decision_on, contract.decision_by = Contract.Decision.EXTEND, notice_on or today, user
        contract.decision_note = (contract.decision_note + "\n" if contract.decision_note else "") + why
        contract.save(update_fields=["decision", "decision_on", "decision_by", "decision_note", "updated_at"])
    dates_mod.sync_site(contract, user)
    return moved
