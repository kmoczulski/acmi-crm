"""Who is free when: busy times turned into a week of half-hour cells and a few good times to meet (docs/SCHEDULING.md).

Plain functions with no Google and no database in them: Google's busy times go in, data the page draws comes out, so
every rule here has a test. The grid's resolution is half an hour, and a meeting starts on the hour or the half hour.
A cell is "busy" for a person when any of their busy time overlaps it, so a 15-minute call blocks its whole half hour.
"""
from datetime import datetime, time, timedelta, timezone as dt_timezone

CELL = 30  # minutes per cell
DURATIONS = (15, 30, 45, 60, 90, 120)
DEFAULT_MINUTES = 30
DEFAULT_HOURS = (8, 18)  # the day the grid shows, in the hub's time zone
SUGGESTIONS = 8
PER_DAY = 3  # at most this many suggested times from one day
APART = 4  # cells (two hours) between two suggestions on the same day, so they differ


def monday(day):
    return day - timedelta(days=day.weekday())


def cells_for(minutes):
    """How many half-hour cells a meeting of this length covers (it rounds up)."""
    return max(1, -(-int(minutes) // CELL))


def clock(dt):
    """2:30 PM"""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def span(start, end):
    """2:00–2:30 PM, or 11:30 AM–12:30 PM when it crosses noon."""
    a, b = clock(start), clock(end)
    return f"{a[:-3]}–{b}" if a[-2:] == b[-2:] else f"{a}–{b}"


def day_label(day):
    return f"{day:%a}, {day:%b} {day.day}"


def initials(name, email=""):
    parts = [p for p in (name or "").replace(".", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return (parts[0][:2] if parts else (email.split("@")[0][:2] or "?")).upper()


def _overlaps(busy, start, end):
    return any(s < end and e > start for s, e in busy)


def availability(people, busy, first_day, ndays, hours, minutes, now, tz):
    """One week for the page.

    people: [{"email", "name"}] in the order shown; busy: {email: {"busy": [(start, end)], "problem": str}} with aware
    datetimes (what crm.gcal.freebusy returns); first_day: the first date shown; hours: (first, last) hour of the day
    shown; minutes: the meeting's length; now: an aware datetime, since a meeting can't start in the past.

    Returns {"days", "slots", "people", "cells", "suggestions", "zone"}. cells[d][s] says whether a meeting of that
    length could start in slot s of day d ("ok"), and which people (by index into "people") are busy at some point in
    it. People whose calendar couldn't be read are listed with their problem and never counted busy.
    """
    first_hour, last_hour = hours
    days = [first_day + timedelta(days=i) for i in range(ndays)]
    starts = [(h, m) for h in range(first_hour, last_hour) for m in (0, CELL)]
    need = cells_for(minutes)
    today = now.astimezone(tz).date()
    out_people = [{"email": p["email"], "name": p["name"], "initials": initials(p["name"], p["email"]),
                   "problem": (busy.get(p["email"]) or {}).get("problem", "")} for p in people]
    cells, suggestions = [], []
    for day in days:
        # busy[i][s]: person i has something on during half hour s of this day
        grid = []
        moments = []
        for h, m in starts:
            begin = datetime.combine(day, time(h, m), tzinfo=tz).astimezone(dt_timezone.utc)
            moments.append((begin, begin + timedelta(minutes=CELL)))
        for p in out_people:
            times = (busy.get(p["email"]) or {}).get("busy", [])
            grid.append([False if p["problem"] else _overlaps(times, b, e) for b, e in moments])
        row, picked = [], []
        for s, (begin, _) in enumerate(moments):
            fits = s + need <= len(moments)
            who = [i for i, g in enumerate(grid) if fits and any(g[s:s + need])]
            ok = fits and begin >= now
            row.append({"ok": ok, "busy": who})
            if ok and not who and len(picked) < PER_DAY and (not picked or s - picked[-1] >= APART):
                picked.append(s)
                end = begin + timedelta(minutes=int(minutes))  # its own length, not the cells it rounds up to
                suggestions.append({"day": day.isoformat(), "time": f"{starts[s][0]:02d}:{starts[s][1]:02d}",
                                    "label": f"{day_label(day)} · {span(begin.astimezone(tz), end.astimezone(tz))}"})
        cells.append(row)
    return {
        "days": [{"date": d.isoformat(), "label": day_label(d), "short": f"{d:%a}", "num": f"{d:%b} {d.day}",
                  "today": d == today, "past": d < today} for d in days],
        "slots": [{"time": f"{h:02d}:{m:02d}", "label": clock(datetime.combine(days[0], time(h, m))), "hour": m == 0}
                  for h, m in starts],
        "people": out_people, "cells": cells, "suggestions": suggestions[:SUGGESTIONS],
        "zone": now.astimezone(tz).tzname() or "",
    }
