"""The LOI / PSA terms tracker: each round of a purchase negotiation (ACMI's offer, the seller's redline, ACMI's
counter) as a set of terms, and the schedule of dates and deposits those terms produce, so the team can line up on a
closing timeline and a deposit schedule before the PSA is signed.

Modeled on the team's "LOI Tracker" workbook (Lewis Place). A round stores only the terms; this module works the rest
out, once, for the tracker page, the live preview, the print view and the critical dates:

    PSA signed -> initial deposit -> feasibility (and extensions, each with its own deposit) -> deposit after feasibility
              \\-> final zoning approval -> closing (and extensions) -> capped at the outside date

Phase II and later start a set number of days after the previous phase closes and run their own feasibility, deposits
and closing. Deposits are credited against the price, so cash to close is the price less everything deposited, per
phase. "Refundable" and "non-refundable" run as cumulative totals: what would come back and what would be lost if ACMI
walked away on that date.

The terms are plain JSON (TermsRound.terms): the deal-level keys in ROUND_FIELDS and a list of phases keyed by
PHASE_FIELDS. Blank means "not in the offer" (the workbook's N/A); zero means zero.
"""
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_PHASES = 6
MAX_OPTIONS = 9  # extensions per period that the schedule will lay out
ROMAN = ("I", "II", "III", "IV", "V", "VI")
CENTS = Decimal("0.01")
ZERO = Decimal(0)

CLOSE_BASIS = (("zoning", "final zoning approval"), ("feasibility", "end of feasibility"))


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # int | money | acres | text | yesno | choice | date
    group: str
    help: str = ""
    first_only: bool = False  # only Phase I has it
    later_only: bool = False  # only Phase II and later have it
    choices: tuple = ()
    default: object = None


ROUND_FIELDS = (
    Field("psa_days", "Days from this round to PSA signing", "int", "Timeline",
          "About 60 on a first offer, 14 once both sides are trading drafts", default=14),
    Field("psa_date", "PSA signing date", "date", "Timeline",
          "Fixes the date; leave blank to count the days above from this round's date"),
    Field("zoning_months", "Months from PSA to final zoning approval", "int", "Timeline"),
    Field("outside_months", "Outside date to close (months from PSA)", "int", "Timeline"),
    Field("subdivision", "Subdivision?", "yesno", "Timeline"),
)

PHASE_FIELDS = (
    Field("name", "Phase name", "text", "Land and price"),
    Field("acres", "Purchase size (acres)", "acres", "Land and price"),
    Field("price_per_acre", "Price per acre", "money", "Land and price"),
    Field("start_days", "Days after the previous phase closes until feasibility starts", "int", "Timeline",
          later_only=True),
    Field("feasibility_days", "Feasibility period (days)", "int", "Timeline"),
    Field("feas_ext_days", "Feasibility extension (days each)", "int", "Timeline"),
    Field("feas_ext_options", "Feasibility extensions available", "int", "Timeline"),
    Field("feas_ext_used", "Feasibility extensions used in the schedule", "int", "Timeline",
          "How many to assume are exercised when working out dates and deposits"),
    Field("close_basis", "Closing counts from", "choice", "Timeline", choices=CLOSE_BASIS,
          help="Phase I usually closes after final zoning approval; later phases after feasibility"),
    Field("close_days", "Days to close", "int", "Timeline"),
    Field("initial_deposit", "Initial deposit", "money", "Deposits", "Due when the PSA is signed"),
    Field("initial_nonref", "Initial deposit: non-refundable portion", "money", "Deposits"),
    Field("feas_ext_deposit", "Deposit for each feasibility extension", "money", "Deposits",
          "Non-refundable, paid when the extension is exercised"),
    Field("go_hard", "Deposit after feasibility expires", "money", "Deposits"),
    Field("go_hard_nonref", "Deposit after feasibility: non-refundable portion", "money", "Deposits"),
    Field("dep30", "30-day deposit (non-refundable)", "money", "Deposits"),
    Field("dep60", "60-day deposit (non-refundable)", "money", "Deposits"),
    Field("dep90", "90-day deposit (non-refundable)", "money", "Deposits"),
    Field("close_ext_days", "Closing extension (days each)", "int", "Closing extensions", first_only=True),
    Field("close_ext_options", "Closing extensions available", "int", "Closing extensions", first_only=True),
    Field("close_ext_used", "Closing extensions used in the schedule", "int", "Closing extensions", first_only=True),
    Field("close_ext_deposit", "Deposit for each closing extension", "money", "Closing extensions", first_only=True),
    Field("close_ext_nonref", "Closing extension deposit: non-refundable portion", "money", "Closing extensions",
          first_only=True),
)

ROUND_BY_KEY = {f.key: f for f in ROUND_FIELDS}
PHASE_BY_KEY = {f.key: f for f in PHASE_FIELDS}


def phase_fields(index):
    """The fields Phase `index` (0-based) has."""
    return [f for f in PHASE_FIELDS if not (f.first_only and index) and not (f.later_only and not index)]


# ---------------------------------------------------------------------------
# Reading numbers, dates and the stored JSON
# ---------------------------------------------------------------------------

BLANKS = {"", "n/a", "na", "-", "—", "none", "null", "tbd"}


def num(value, strict=False):
    """A Decimal from what someone typed or a cell held ("$25,000", 25000, "N/A"), or None. strict raises ValueError
    for text that isn't a number (forms); otherwise it is None (workbooks)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace(",", "").replace("$", "")
    if text.lower() in BLANKS:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        if strict:
            raise ValueError(f"{value!r} is not a number") from None
        return None


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10]) if value else None
    except ValueError:
        return None


def coerce(field, value, strict=False):
    kind = field.kind
    if kind == "int":
        d = num(value, strict)
        return int(d.to_integral_value(ROUND_HALF_UP)) if d is not None else None
    if kind in ("money", "acres"):
        return num(value, strict)
    if kind == "text":
        return str(value).strip() if value not in (None, "") else ""
    if kind == "yesno":
        s = str(value or "").strip().lower()
        return "yes" if s in ("yes", "y", "true", "1") else "no" if s in ("no", "n", "false", "0") else ""
    if kind == "choice":
        return value if value in dict(field.choices) else ""
    if kind == "date":
        return as_date(value)
    return value


def typed(raw, strict=False):
    """A round's stored terms as real values: ints, Decimals, dates, "" and None for blank, defaults for keys the
    JSON doesn't have. Every phase gets every key, so the rest of the code never checks."""
    raw = raw if isinstance(raw, dict) else {}
    out = {f.key: coerce(f, raw[f.key] if f.key in raw else f.default, strict) for f in ROUND_FIELDS}
    listed = raw.get("phases") if isinstance(raw.get("phases"), list) else []
    phases = [{f.key: coerce(f, (p if isinstance(p, dict) else {}).get(f.key), strict) for f in PHASE_FIELDS}
              for p in listed[:MAX_PHASES]]
    out["phases"] = [p for p in phases if not phase_is_empty(p)]
    return out


def _blank(value):
    return value is None or value == ""


def phase_is_empty(phase):
    """A phase block with nothing in it but maybe a name or zeros: the spare block the form always offers, or a
    workbook's unused Phase II and III."""
    return all(not v for k, v in phase.items() if k not in ("name", "close_basis"))


def dump(t):
    """The JSON to store for typed terms: only what is set, numbers as numbers, dates as ISO text, and no empty phases."""

    def plain(v):
        if isinstance(v, Decimal):
            return int(v) if v == v.to_integral_value() else float(v)
        if isinstance(v, date):
            return v.isoformat()
        return v

    out = {k: plain(t[k]) for k in ROUND_BY_KEY if not _blank(t.get(k))}
    phases = [{k: plain(v) for k, v in p.items() if not _blank(v)} for p in t.get("phases", []) if not phase_is_empty(p)]
    if phases:
        out["phases"] = phases
    return out


def phase_name(index, phase=None):
    given = (phase or {}).get("name")
    return given or (f"Phase {ROMAN[index]}" if index < len(ROMAN) else f"Phase {index + 1}")


def money_value(v):
    return v.quantize(CENTS, ROUND_HALF_UP) if isinstance(v, Decimal) else v


# ---------------------------------------------------------------------------
# Date arithmetic (Excel's EOMONTH, and "N days after" that tolerates a blank)
# ---------------------------------------------------------------------------


def eomonth(d, months):
    """The last day of the month `months` after d's month (Excel's EOMONTH)."""
    if d is None or months is None:
        return None
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, calendar.monthrange(y, m)[1])


def plus(d, days):
    return None if d is None else d + timedelta(days=days or 0)


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------

# Where each event sits among the others on the same day, and across rounds in the side-by-side table.
ORDER = {"start": 0, "initial": 1, "dep30": 2, "dep60": 3, "dep90": 4, "feas_end0": 5, "feas_end": 20, "go_hard": 21,
         "zoning": 22, "close0": 23, "outside": 40, "closing": 41}


def _order(key):
    if key in ORDER:
        return ORDER[key]
    if key.startswith("feas_ext"):
        return 5 + int(key[len("feas_ext"):])
    if key.startswith("close_ext"):
        return 23 + int(key[len("close_ext"):])
    return 30


def _clamp(used, available):
    return max(0, min(used or 0, available or 0, MAX_OPTIONS))


def _event(phase, key, label, kind, when, amount=ZERO, nonref=ZERO, **extra):
    amount = amount or ZERO
    nonref = min(max(nonref or ZERO, ZERO), amount) if amount else ZERO
    return {"phase": phase, "key": f"p{phase}.{key}", "base": key, "label": label, "kind": kind, "date": when,
            "amount": amount, "nonref": nonref, "refundable": amount - nonref, "order": phase * 100 + _order(key),
            "money": bool(amount), **extra}


def _phase(index, p, t, psa, prev_close, zoning, outside, warnings, signed=False):
    n = index + 1
    name = phase_name(index, p)
    acres, ppa = p["acres"], p["price_per_acre"]
    price = (acres * ppa).quantize(CENTS, ROUND_HALF_UP) if acres is not None and ppa is not None else ZERO
    start = psa if index == 0 else plus(prev_close, p["start_days"]) if prev_close is not None else None
    ev = []

    def add(key, label, kind, when, amount=ZERO, nonref=ZERO, **extra):
        ev.append(_event(n, key, label, kind, when, amount, nonref, **extra))

    if n == 1:
        add("start", "PSA signed" if signed else "PSA signed (anticipated)", "other", start, milestone=True)
    else:
        add("start", f"{name} feasibility starts", "other", start, milestone=True)
    if p["initial_deposit"]:
        add("initial", "Initial deposit", "deposit", start, p["initial_deposit"], p["initial_nonref"])
    for days, key in ((30, "dep30"), (60, "dep60"), (90, "dep90")):
        if p[key]:
            add(key, f"{days}-day deposit", "deposit", plus(start, days), p[key], p[key])

    # Feasibility and its extensions: each extension's deposit is paid on the day it has to be exercised, the end of
    # the period it extends.
    f_end = plus(start, p["feasibility_days"]) if p["feasibility_days"] is not None else start
    used_f = _clamp(p["feas_ext_used"], p["feas_ext_options"]) if (p["feas_ext_days"] or p["feas_ext_deposit"]) else 0
    if p["feas_ext_used"] and p["feas_ext_options"] is not None and p["feas_ext_used"] > p["feas_ext_options"]:
        warnings.append(f"{name}: {p['feas_ext_used']} feasibility extensions used but only {p['feas_ext_options']} available.")
    if used_f:
        add("feas_end0", "Initial feasibility period ends", "feasibility", f_end, milestone=True)
    for k in range(1, used_f + 1):
        add(f"feas_ext{k}", f"Feasibility extension {k}", "extension", f_end, p["feas_ext_deposit"],
            p["feas_ext_deposit"], extra_days=p["feas_ext_days"], to=plus(f_end, p["feas_ext_days"]))
        f_end = plus(f_end, p["feas_ext_days"])
    if p["feasibility_days"] is not None:
        add("feas_end", "Feasibility period ends", "feasibility", f_end, milestone=True)
    if p["go_hard"]:
        add("go_hard", "Deposit after feasibility expires", "hard", f_end, p["go_hard"], p["go_hard_nonref"])

    # Closing: counted from final zoning approval or from the end of feasibility.
    basis = p["close_basis"] or ("zoning" if index == 0 and zoning is not None else "feasibility")
    if n == 1 and zoning is not None:
        add("zoning", "Final zoning approval", "approval", zoning, milestone=True)
    base = zoning if basis == "zoning" else f_end
    close = plus(base, p["close_days"]) if base is not None and (p["close_days"] is not None or basis == "zoning") \
        else None
    if basis == "zoning" and zoning is None and (acres or price):
        warnings.append(f"{name}: closing counts from final zoning approval, but no months to approval are set.")
    used_c = _clamp(p["close_ext_used"], p["close_ext_options"]) \
        if index == 0 and (p["close_ext_days"] or p["close_ext_deposit"]) else 0
    if index == 0 and p["close_ext_used"] and p["close_ext_options"] is not None and p["close_ext_used"] > p["close_ext_options"]:
        warnings.append(f"{name}: {p['close_ext_used']} closing extensions used but only {p['close_ext_options']} available.")
    if used_c:
        add("close0", "Initial closing date", "closing", close, milestone=True)
    final = close
    for k in range(1, used_c + 1):
        nxt = plus(final, p["close_ext_days"])
        add(f"close_ext{k}", f"Closing extension {k}", "extension", final, p["close_ext_deposit"],
            p["close_ext_nonref"], extra_days=p["close_ext_days"], to=nxt)
        final = nxt
    if index == 0 and outside is not None:
        add("outside", "Outside date to close", "expiration", outside, milestone=True)
        if final is not None and final > outside:
            warnings.append(f"{name}: the extended closing ({final:%b} {final.day}, {final.year}) is after the outside "
                            f"date ({outside:%b} {outside.day}, {outside.year}); closing is held at the outside date.")
            final = outside
    deposit_rows = [e for e in ev if e["money"]]
    deposits = sum((e["amount"] for e in deposit_rows), ZERO)
    nonref = sum((e["nonref"] for e in deposit_rows), ZERO)
    cash = price - deposits
    add("closing", "Closing and cash to close", "closing", final, cash=cash, milestone=True)
    if price and deposits > price:
        warnings.append(f"{name}: deposits ({deposits:,.0f}) are more than the price ({price:,.0f}).")
    elif not price and deposits:
        warnings.append(f"{name}: there are deposits but no price; add the acres and the price per acre.")
    for part, whole, what in (("initial_nonref", "initial_deposit", "the initial deposit"),
                              ("go_hard_nonref", "go_hard", "the deposit after feasibility"),
                              ("close_ext_nonref", "close_ext_deposit", "a closing extension deposit")):
        if p[part] and p[part] > (p[whole] or ZERO):
            warnings.append(f"{name}: the non-refundable part of {what} is more than the deposit.")

    # In date order (closing last) with running totals: what comes back and what is lost if we walk on that day.
    far = date.max
    ev.sort(key=lambda e: (e["base"] == "closing", e["date"] or far, e["order"]))
    ref = lost = ZERO
    for e in ev:
        ref += e["refundable"]
        lost += e["nonref"]
        e["cum_ref"], e["cum_nonref"], e["cum"] = ref, lost, ref + lost
    by_feas = [e for e in deposit_rows if f_end is None or e["date"] is None or e["date"] <= f_end]
    return {
        "n": n, "name": name, "acres": acres, "ppa": ppa, "price": price, "start": start, "feasibility_end": f_end
        if p["feasibility_days"] is not None else None, "zoning": zoning if n == 1 else None, "closing": final,
        "outside": outside if n == 1 else None, "events": ev, "deposits": deposits, "nonrefundable": nonref,
        "refundable": deposits - nonref, "cash_to_close": cash, "feas_ext_used": used_f, "close_ext_used": used_c,
        "by_feasibility": {"deposits": sum((e["amount"] for e in by_feas), ZERO),
                           "nonrefundable": sum((e["nonref"] for e in by_feas), ZERO)},
    }


def schedule(raw, dated=None, signed_on=None):
    """The dates and deposits a round's terms produce.

    Returns {"psa", "phases": [...], "totals", "head", "warnings"}; each phase has its "events" in date order (closing
    last) with running refundable / non-refundable totals, its price, deposits and cash to close. `dated` is the
    round's date, which the days to PSA signing count from; `signed_on` is the day a signed contract's PSA really took
    effect, which stands in for the anticipated date unless the round fixes its own."""
    t = typed(raw)
    warnings = []
    psa = t["psa_date"] or signed_on or (dated + timedelta(days=t["psa_days"] or 0) if dated else None)
    signed = bool(signed_on and not t["psa_date"])
    zoning = eomonth(psa, t["zoning_months"])
    outside = eomonth(psa, t["outside_months"])
    phases, prev_close = [], None
    for i, p in enumerate(t["phases"]):
        ph = _phase(i, p, t, psa, prev_close, zoning, outside, warnings, signed)
        phases.append(ph)
        prev_close = ph["closing"]
    if phases and psa is None:
        warnings.insert(0, "Add this round's date (or the PSA signing date) to work out the dates.")
    totals = {k: sum((ph[k] for ph in phases), ZERO)
              for k in ("price", "deposits", "refundable", "nonrefundable", "cash_to_close")}
    totals["acres"] = sum((ph["acres"] or ZERO for ph in phases), ZERO)
    totals["ppa"] = (totals["price"] / totals["acres"]).quantize(CENTS, ROUND_HALF_UP) if totals["acres"] else None
    first, last = (phases[0], phases[-1]) if phases else (None, None)
    head = {
        "price": totals["price"], "ppa": totals["ppa"], "acres": totals["acres"], "psa": psa,
        "feasibility_end": first["feasibility_end"] if first else None, "zoning": zoning,
        "closing": last["closing"] if last else None, "outside": outside,
        "deposits": totals["deposits"], "refundable": totals["refundable"], "nonrefundable": totals["nonrefundable"],
        "at_risk_feas": first["by_feasibility"]["nonrefundable"] if first else ZERO,
        "committed_feas": first["by_feasibility"]["deposits"] if first else ZERO,
        "cash_to_close": totals["cash_to_close"],
        "months_to_close": round((last["closing"] - psa).days / 30.4375, 1)
        if last and last["closing"] and psa else None,
    }
    return {"psa": psa, "phases": phases, "totals": totals, "head": head, "warnings": warnings}


# ---------------------------------------------------------------------------
# Side by side
# ---------------------------------------------------------------------------

# (key in head, label, kind) of what the two sides are apart on.
GAP_ROWS = (
    ("ppa", "Price per acre", "money"), ("price", "Price", "money"), ("deposits", "All deposits", "money"),
    ("committed_feas", "Deposited by the end of feasibility", "money"),
    ("at_risk_feas", "Non-refundable by the end of feasibility", "money"),
    ("nonrefundable", "Non-refundable, all deposits", "money"), ("psa", "PSA signed", "date"),
    ("feasibility_end", "Feasibility ends", "date"), ("zoning", "Final zoning approval", "date"),
    ("closing", "Closing", "date"), ("outside", "Outside date", "date"), ("cash_to_close", "Cash to close", "money"),
)


def usd(v):
    """$1,234,567 in whole dollars, like the workbook; blank for nothing."""
    if v is None:
        return ""
    n = int(Decimal(v).to_integral_value(ROUND_HALF_UP))
    return f"-${-n:,}" if n < 0 else f"${n:,}"


def day_text(d):
    return f"{d:%b} {d.day}, {d.year}" if d else ""


def _delta_text(kind, delta):
    if delta is None:
        return ""
    if kind == "date":
        n = int(delta)
        return "same day" if n == 0 else f"{'+' if n > 0 else '−'}{abs(n)} day{'s' if abs(n) != 1 else ''}"
    n = int(Decimal(delta).to_integral_value(ROUND_HALF_UP))
    return "$0" if n == 0 else f"{'+' if n > 0 else '−'}${abs(n):,}"


def gap(a, b):
    """Where round `a` and round `b` (their schedule()["head"]s) differ: [{"key", "label", "kind", "a", "b", "delta",
    "same", and the same as text: "a_text", "b_text", "delta_text"}] for the rows that are set in either, money in
    dollars and dates in days; the delta is b - a."""
    out = []
    for key, label, kind in GAP_ROWS:
        x, y = a.get(key), b.get(key)
        if x in (None, ZERO) and y in (None, ZERO):
            continue
        delta = None
        if x is not None and y is not None:
            delta = (y - x).days if kind == "date" else y - x
        text = day_text if kind == "date" else usd
        out.append({"key": key, "label": label, "kind": kind, "a": x, "b": y, "delta": delta, "same": x == y,
                    "a_text": text(x) or "–", "b_text": text(y) or "–", "delta_text": _delta_text(kind, delta)})
    return out


def compare_events(results):
    """The side-by-side schedule: [{"key", "label", "kind", "phase", "milestone", "cells": [event or None per round],
    "changed"}] in the order the events happen, grouped by phase."""
    seen = {}
    for res in results:
        for ph in res["phases"]:
            for e in ph["events"]:
                seen.setdefault(e["key"], e)
    out = []
    for key, e in sorted(seen.items(), key=lambda kv: kv[1]["order"]):
        cells = []
        for res in results:
            cells.append(next((x for ph in res["phases"] for x in ph["events"] if x["key"] == key), None))
        present = [c for c in cells if c]
        sig = {(c["date"], c["amount"], c["nonref"], c.get("cash")) for c in present}
        out.append({"key": key, "label": e["label"], "kind": e["kind"], "phase": e["phase"],
                    "milestone": e.get("milestone", False), "cells": cells,
                    "changed": len(present) < len(cells) or len(sig) > 1})
    return out


def display(field, value):
    """A term as the grid shows it."""
    if _blank(value):
        return ""
    if field.kind == "money":
        return f"${value:,.0f}" if value == value.to_integral_value() else f"${value:,.2f}"
    if field.kind == "acres":
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if field.kind == "date":
        return f"{value:%b} {value.day}, {value.year}"
    if field.kind == "yesno":
        return "Yes" if value == "yes" else "No"
    if field.kind == "choice":
        return dict(field.choices).get(value, "")
    return str(value)


def compare_terms(rounds):
    """The inputs side by side: [{"title", "rows": [{"label", "key", "cells": [text per round], "changed"}]}] by
    group, with rows nobody filled in left out. `rounds` is a list of stored terms (JSON)."""
    ts = [typed(r) for r in rounds]
    sections = []

    def section(title, fields, getter):
        rows = []
        for f in fields:
            vals = [getter(t, f) for t in ts]
            if all(_blank(v) for v in vals):
                continue
            rows.append({"label": f.label, "key": f.key, "cells": [display(f, v) for v in vals],
                         "changed": len({repr(v) for v in vals}) > 1})
        if rows:
            sections.append({"title": title, "rows": rows})

    section("The deal", ROUND_FIELDS, lambda t, f: t[f.key])
    most = max((len(t["phases"]) for t in ts), default=0)
    for i in range(most):
        def getter(t, f, i=i):
            ph = t["phases"][i] if i < len(t["phases"]) else {}
            return ph.get(f.key)

        title = phase_name(i, next((t["phases"][i] for t in ts if i < len(t["phases"])), None))
        section(title, [f for f in phase_fields(i) if f.key != "name"], getter)
    return sections


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def thin_ticks(ticks):
    """The month marks along the lanes' axis, fewer of them over a long span so the labels don't run together: every
    month up to 18 of them, then quarters, then half years, then years. "Today" always stays."""
    months = [t for t in ticks if not t.get("today")]
    step = 1 if len(months) <= 18 else 3 if len(months) <= 40 else 6 if len(months) <= 80 else 12
    if step == 1:
        return ticks
    return [t for t in ticks if t.get("today") or _MONTHS.index(t["label"][:3]) % step == 0]


def lane_rows(result):
    """A round's dated events as the rows the contract timeline's lanes draw (crm/contracts/timeline.lanes)."""
    multi = len(result["phases"]) > 1
    rows = []
    for ph in result["phases"]:
        for e in ph["events"]:
            if e["date"] is None:
                continue
            label = f"{ph['name']}: {e['label']}" if multi else e["label"]
            rows.append({"key": e["key"], "label": label, "kind": e["kind"], "when": e["date"],
                         "amount": e["amount"] or None, "done": False, "computed": False})
    return rows
