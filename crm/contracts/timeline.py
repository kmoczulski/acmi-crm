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
    (r"title (?:commitment|report)|survey", "title_objection"),
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
EXTEND = re.compile(
    r"extend\w*\s+(?:the\s+)?(?P<what>[^.;]{0,60}?)\s+(?:for|by)\s+(?:an?\s+|one\s+)?(?:additional\s+)?"
    r"(?:period\s+of\s+)?" + _NUM + r"\s*(?P<kind>business\s+|calendar\s+)?(?P<unit>days?|weeks?|months?)", re.I,
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
    return None


def parse_extension(text):
    """{"extends", "extend_by", "extend_unit"} for "extend the Feasibility Period by thirty (30) days", or None."""
    m = EXTEND.search(text or "")
    if not m:
        return None
    n = _number(m)
    target = anchor_key(m.group("what")) or "closing"
    if n is None or target == "effective":
        return None
    return {"extends": target, "extend_by": n, "extend_unit": _unit(m)}


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
    anchor = "the Effective Date" if t["anchor"] == "effective" else t["anchor"].replace("_", " ")
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
        found = parse_extension(text)
        if found:
            d.extends, d.extend_by, d.extend_unit = found["extends"], found["extend_by"], found["extend_unit"]
    keys = {s.key for s in siblings if s.key}
    if d.anchor and d.anchor != "effective" and d.anchor not in keys:
        # Counted from a date the contract doesn't have under that key: the kind's own key may be taken by "_2".
        match = next((s.key for s in siblings if s.key and s.key.startswith(d.anchor)), "")
        d.anchor = match or d.anchor
    return d


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
    return out, ticks + [{"pos": pct(today), "label": "Today", "today": True}]


# ---------------------------------------------------------------------------
# Exercising an extension, or pushing a date
# ---------------------------------------------------------------------------


def dependents(target, dates):
    """Open dates that count from `target` (directly or down the chain), in order."""
    out, frontier = [], [target]
    while frontier:
        t = frontier.pop(0)
        for d in dates:
            if t.key and d.anchor == t.key and d.offset is not None and not d.done and d not in out and d != target:
                out.append(d)
                frontier.append(d)
    return out


def proposal(ext_or_target, today=None):
    """What exercising an extension (or pushing a date) would do: {"target", "new", "follow": [(date, new)],
    "same_day": [dates on the old day that don't count from it]}."""
    d = ext_or_target
    dates = list(d.contract.dates.filter(confirmed=True))
    if d.kind == ContractDate.Kind.EXTENSION and d.extends:
        target = next((x for x in dates if x.key == d.extends), None)
        new = add(target.date, d.extend_by, d.extend_unit or "days") if target and target.date else None
    else:
        target, new = d, None
    if target is None:
        return None
    follow = [(x, add(new, x.offset, x.unit or "days") if new else None) for x in dependents(target, dates)]
    moved = {target.pk, *(x.pk for x, _ in follow)}
    same_day = [x for x in dates if x.date and target.date and x.date == target.date and x.pk not in moved
                and not x.done and x.pk != d.pk]
    return {"target": target, "new": new, "follow": follow, "same_day": same_day}


def _move(d, new, why, user, today):
    old = d.date
    if new == old:
        return None
    d.history = [*(d.history or []), {"from": old.isoformat() if old else None, "to": new.isoformat(), "why": why,
                                      "by": (user.get_full_name() or user.username) if user else "", "on": today.isoformat()}]
    d.date = new
    d.save(update_fields=["date", "history"])
    return d, old, new


@transaction.atomic
def push(target, new, user, why, also=(), ext=None, notice_on=None):
    """Move `target` to `new`, the dates that count from it with it, and `also` (other dates, by the same number of
    days). With `ext`, that extension option is marked exercised. Returns [(date, old, new)]; the site timeline and
    the feasibility decision follow."""
    from . import dates as dates_mod

    today = timezone.localdate()
    contract = target.contract
    shift = (new - target.date).days if target.date else 0
    moved = [m for m in [_move(target, new, why, user, today)] if m]
    all_dates = list(contract.dates.filter(confirmed=True))
    for d in dependents(target, all_dates):
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
