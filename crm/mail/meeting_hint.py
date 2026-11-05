"""Spot a message that settles on a meeting time, so the reading pane can offer "Schedule" (Kellen, 2026-11-05: "when someone
agrees to a time for a meeting... there should be an option to click to schedule which then pulls open the calendar
scheduler with all the people on the email chain plugged in and to confirm the time").

Plain rules, no AI, so the same words always give the same answer and nothing leaves the hub. `find(text, sent)` reads the
new words of one message (not the quoted thread under it) and returns:

    {"kind": "agreed",  "date", "time", "label"}   one day and one time were named: "12 noon tomorrow", "Tuesday at 2pm"
    {"kind": "offer",   "dates": [...]}            days were named without a single time ("Monday or Tuesday works")
    {"kind": "request"}                            "can you send out a meeting planner / invite"
    {"kind": "mention", ...}                       a meeting word with no time ("let's schedule a call")
    None                                           nothing about a meeting

Dates are worked out from when the message was sent ("tomorrow" means the day after it was sent), in the hub's time zone.
Names that follow "include / add / invite / loop in" come back in "names", for the caller to match against the team."""
import re
from datetime import date, datetime, time, timedelta

MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
          "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
          "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}
WEEKDAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2, "wednesday": 2, "thu": 3,
            "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}

_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_DAY_WORD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
# "November 30", "Nov. 30th", "30 November", "12/1", "12/1/2026", "2026-12-01"
RE_MONTH_DAY = re.compile(rf"\b(?P<m>{_MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<y>20\d\d))?", re.I)
RE_DAY_MONTH = re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m>{_MONTH})\b\.?(?:,?\s+(?P<y>20\d\d))?", re.I)
RE_SLASH = re.compile(r"(?<![\d/])(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?(?![\d/])")
RE_ISO = re.compile(r"\b(?P<y>20\d\d)-(?P<m>\d{2})-(?P<d>\d{2})\b")
RE_RELATIVE = re.compile(r"\b(?P<w>day after tomorrow|tomorrow|today|tonight)\b", re.I)
RE_WEEKDAY = re.compile(rf"\b(?:(?P<mod>next|this|coming)\s+)?(?P<w>{_DAY_WORD})\b", re.I)
# A clock time: "noon", "12 noon", "2pm", "2:30 p.m.", "14:00", or a bare "at 10" / "for 3".
RE_TIME = re.compile(
    r"(?<![\w:./$-])(?:(?P<noon>(?:12\s+)?noon)|(?P<h>[01]?\d|2[0-3])(?::(?P<min>[0-5]\d))?\s*(?P<ap>[ap]\.?m\.?)?)(?![\w:/%])",
    re.I)
RE_AT = re.compile(r"\b(?:at|@|for|around|by|from|starting|say)\s*$", re.I)
RE_REQUEST = re.compile(
    r"\b(?:send\s+(?:out\s+)?(?:a\s+|the\s+)?(?:meeting\s+)?(?:planner|invite|invitation|calendar\s+(?:invite|invitation)|"
    r"calendar\s+hold)|(?:put|get)\s+(?:something|it|a\s+hold|a\s+meeting)\s+on\s+(?:the\s+)?calendar|"
    r"(?:schedule|set\s+up|book)\s+(?:a\s+|the\s+)?(?:call|meeting|time|zoom|teams|site\s+visit|tour))", re.I)
RE_MEETING_WORD = re.compile(r"\b(?:meeting|call|zoom|teams|catch\s*up|chat|tour|visit|sync|conversation)\b", re.I)
RE_AVAILABLE = re.compile(r"\b(?:available|free|open|works?|work\s+for|good\s+for|can\s+do|could\s+do|how\s+about|what\s+about|"
                          r"let'?s|shoot\s+for|in\s+town|travel)", re.I)
RE_NAMES = re.compile(r"\b(?:include|add|invite|loop\s+in|cc|copy|and)\s+([A-Z][a-z]{1,20})(?:\s+([A-Z][a-z]{1,25}))?\b")
RE_RANGE_END = re.compile(r"\d{1,2}(?::\d\d)?\s*(?:[ap]\.?m\.?)?\s*(?:to|-|–|—|until|and)\s*$", re.I)
RE_BEFORE = re.compile(r"\b(?:before|after|until|by|no\s+later\s+than|earlier\s+than|later\s+than)\s*$", re.I)


def _clean_year(y, today):
    if y is None:
        return None
    y = int(y)
    return y + 2000 if y < 100 else y


def _dated(d, m, y, base):
    """A date from a day, month and optional year; with no year, the next time that day comes round on or after `base`."""
    try:
        if y:
            return date(y, m, d)
        pick = date(base.year, m, d)
        return pick if pick >= base - timedelta(days=1) else date(base.year + 1, m, d)
    except ValueError:
        return None


def _days(text, base):
    """[(start, end, date)] for every day named in the text, in order."""
    found = []
    for rx in (RE_ISO, RE_MONTH_DAY, RE_DAY_MONTH):
        for m in rx.finditer(text):
            g = m.groupdict()
            month = int(g["m"]) if g["m"].isdigit() else MONTHS[g["m"].lower().rstrip(".")]
            d = _dated(int(g["d"]), month, _clean_year(g.get("y"), base), base)
            if d:
                found.append((m.start(), m.end(), d))
    for m in RE_SLASH.finditer(text):
        month, day = int(m["m"]), int(m["d"])
        if 1 <= month <= 12 and 1 <= day <= 31:
            d = _dated(day, month, _clean_year(m["y"], base), base)
            if d:
                found.append((m.start(), m.end(), d))
    for m in RE_RELATIVE.finditer(text):
        word = m["w"].lower()
        found.append((m.start(), m.end(), base + timedelta(days={"today": 0, "tonight": 0, "tomorrow": 1}.get(word, 2))))
    taken = [(s, e) for s, e, _ in found]
    for m in RE_WEEKDAY.finditer(text):
        if any(s <= m.start() < e for s, e in taken):
            continue
        if any(0 <= s - m.end() <= 3 or 0 <= m.start() - e <= 3 for s, e in taken):
            continue  # "Monday, November 30" or "November 30 (Monday)": the date already says it
        wd = WEEKDAYS[m["w"].lower()]
        ahead = (wd - base.weekday()) % 7 or 7
        d = base + timedelta(days=ahead)
        if (m["mod"] or "").lower() == "next" and ahead <= 6 - base.weekday():
            d += timedelta(days=7)  # "next Tuesday" from a Wednesday: not the Tuesday in a few days, the one a week on
        found.append((m.start(), m.end(), d))
    found.sort()
    out, last_end = [], -1
    for s, e, d in found:  # "Monday, November 30" gives one day, not two
        if s >= last_end:
            out.append((s, e, d))
            last_end = e
        elif out and out[-1][2] == d:
            continue
    return out


def _times(text):
    """[(start, end, time)] for every start time named. "before 11:00 AM" and a bare number that is only a date are left out."""
    out = []
    for m in RE_TIME.finditer(text):
        if m["noon"]:
            out.append((m.start(), m.end(), time(12, 0)))
            continue
        hour, minute, ap = int(m["h"]), int(m["min"] or 0), (m["ap"] or "").lower().replace(".", "")
        before = text[max(0, m.start() - 14):m.start()]
        if RE_BEFORE.search(before):
            continue  # a limit, not a start: "free before 11:00"
        if RE_RANGE_END.search(text[max(0, m.start() - 22):m.start()]):
            continue  # the far end of "9 to 10 AM" / "between 1:30 and 3:00 PM": not another start
        if not (ap or m["min"] or RE_AT.search(before)):
            continue  # a bare number ("December 1", "30 people") is not a time
        if ap == "":
            # "between 1:30 and 3:00 PM": the first time borrows the PM from the second
            tail = text[m.end(): m.end() + 24]
            borrow = re.match(r"\s*(?:and|to|-|–|—)\s*\d{1,2}(?::\d\d)?\s*([ap])\.?m", tail, re.I)
            ap = (borrow.group(1).lower() + "m") if borrow else ""
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
        elif not ap and hour < 7:
            hour += 12  # "at 2" in a business email is 2 in the afternoon
        if hour > 23:
            continue
        out.append((m.start(), m.end(), time(hour, minute)))
    return out


def _label(d, t):
    return f"{d:%a, %b} {d.day}" + (f" at {(t.hour % 12) or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}" if t else "")


def names(text):
    """First names (and a following surname) written after include / add / invite / loop in, for the caller to match."""
    out = []
    for m in RE_NAMES.finditer(text or ""):
        first, last = m.group(1), m.group(2)
        if first.lower() in {"me", "you", "us", "all", "the", "a", "an", "this", "that"} or first in MONTHS_TITLE:
            continue
        out.append((first, last or ""))
    return out


MONTHS_TITLE = {k.title() for k in MONTHS} | {k.title() for k in WEEKDAYS}


def find(text, sent):
    """What a message says about a meeting, as described in the module notes; None when it says nothing about one."""
    text = " ".join((text or "").split())
    if not text:
        return None
    base = sent.date() if isinstance(sent, datetime) else sent
    days, times = _days(text, base), _times(text)
    request = bool(RE_REQUEST.search(text))
    talks_about_meeting = bool(RE_MEETING_WORD.search(text)) or request
    who = [f"{a} {b}".strip() for a, b in names(text)] if (request or days or times) else []

    if days and times:
        # Bind each time to the nearest day in the same sentence; one clear pair is an agreement.
        pairs = set()
        for ts, te, t in times:
            nearest = min(days, key=lambda d: min(abs(d[0] - te), abs(d[1] - ts)))
            gap = min(abs(nearest[0] - te), abs(nearest[1] - ts))
            if gap <= 60:
                pairs.add((nearest[2], t))
        distinct_days = {d for _, _, d in days}
        if len(pairs) == 1 and len(distinct_days) == 1:
            d, t = next(iter(pairs))
            return {"kind": "agreed", "date": d, "time": t, "label": _label(d, t), "names": who, "request": request}
        ds = sorted(distinct_days)
        clock = sorted({t for _, _, t in times})
        when = " / ".join(f"{d:%a, %b} {d.day}" for d in ds[:3]) + (f" · {', '.join(_label(ds[0], t).split(' at ')[1] for t in clock[:3])}" if clock else "")
        # Days and times were offered, not matched up ("Monday or Tuesday... free 1:30 to 3"): the person picks in the scheduler.
        return {"kind": "offer", "dates": ds, "times": clock, "date": ds[0], "time": None, "label": when, "names": who,
                "request": request}
    if days and (talks_about_meeting or RE_AVAILABLE.search(text)):
        ds = sorted({d for _, _, d in days})
        return {"kind": "offer", "dates": ds, "date": ds[0], "time": None,
                "label": " / ".join(f"{d:%a, %b} {d.day}" for d in ds[:3]), "names": who, "request": request}
    if request:
        return {"kind": "request", "names": who}
    return None
