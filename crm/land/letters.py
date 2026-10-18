"""Landowner letters: a mail-merged letter to each owner of a site's parcels, mailing labels (Avery 5160) and a CSV
for a mail house, and a log of what went out.

Recipients come from the appraisal district's owner and mailing address on each included parcel, grouped by owner
and address, so an owner with three parcels in the site gets one letter about all three. Logging letters as sent
keeps each one as it went out, dates "Owner contacted" on those owners' parcel deals and on the site (the first
contact counts), and so moves them on to Owner outreach, never back.
"""
import csv
import io
import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import LandownerLetter, LandSiteEvent, LetterTemplate
from .tracker import apply_milestones, ensure_deals, missing_deals, site_deals

FIELDS = [
    ("owner", "the name after Dear (each owner's own, editable in the list)"),
    ("property", "the land: street address or legal description"),
    ("parcels", "“parcel R12345” or “parcels R12345 and R12346”"),
    ("acres", "the owner's acres in the site"),
    ("county", "the county's name"),
    ("site", "the site's name in the CRM"),
    ("date", "the letter's date"),
    ("sender", "your name"),
    ("sender_title", "your title"),
    ("sender_phone", "your phone"),
    ("sender_email", "your email"),
]
SENDER_FIELDS = [("sender", "Your name", 120), ("sender_title", "Title", 120), ("sender_phone", "Phone", 40),
                 ("sender_email", "Email", 254)]

BUILTINS = [
    {"key": "intro", "name": "First letter: would you consider selling?", "body": """Dear {owner},

I am writing about your land in {county} County, {property} ({acres} acres, {parcels}).

ACMI Properties develops industrial campuses for advanced manufacturing companies. We are looking for land in your \
area, and your property fits what we need. We would welcome the chance to talk with you about whether you would \
consider selling all or part of it.

There is no obligation, and a conversation costs you nothing. If you are open to it, or if someone else in your \
family handles the property, please call me at {sender_phone} or email me at {sender_email}.

Thank you for your time.

Sincerely,

{sender}
{sender_title}
ACMI Properties"""},
    {"key": "follow_up", "name": "Follow-up letter", "body": """Dear {owner},

A few weeks ago I wrote to you about your land in {county} County, {property} ({acres} acres, {parcels}). I wanted \
to follow up in case my letter went astray.

ACMI Properties is still interested in your property for an industrial campus, and we would be glad to talk on your \
schedule, whether you would sell now, later, or only part of the land. If now is not the right time, just let me know \
and I will make a note of it.

You can reach me at {sender_phone} or {sender_email}.

Sincerely,

{sender}
{sender_title}
ACMI Properties"""},
]

LABELS_PER_SHEET = 30  # Avery 5160 / 8160: 3 across, 10 down, 2 5/8" x 1"

_FIELD = re.compile(r"\{(\w+)\}")
_UPPER = {"LLC", "LC", "LP", "LLP", "LLLP", "LTD", "INC", "PLLC", "PLC", "PC", "PA", "NA", "USA", "II", "III", "IV",
          "PO", "FM", "RM", "CR", "SH", "IH", "RR", "HC", "NE", "NW", "SE", "SW", "ISD", "MUD"}
_SMALL = {"OF", "AND", "THE", "FOR"}
_CARE_OF = re.compile(r"^\s*(C/O|C/0|%|ATTN:?)\s*", re.I)


def ensure_builtins():
    """Create or update the built-in templates from BUILTINS (only what changed). To make one your own, save a copy."""
    existing = {t.key: t for t in LetterTemplate.objects.exclude(key="")}
    for spec in BUILTINS:
        current = existing.get(spec["key"])
        if current is None:
            try:
                with transaction.atomic():
                    LetterTemplate.objects.create(key=spec["key"], name=spec["name"], body=spec["body"])
            except IntegrityError:
                pass  # another request made it a moment ago
        elif (current.name, current.body) != (spec["name"], spec["body"]):
            current.name, current.body = spec["name"], spec["body"]
            current.save(update_fields=["name", "body", "updated_at"])


def nice(text):
    """'SMITH FAMILY TRUST LLC ETAL' -> 'Smith Family Trust LLC et al', for letters (appraisal rolls are in capitals).
    Text someone typed in mixed case is left as it is."""
    text = " ".join((text or "").split())
    if text != text.upper():
        return text
    out, prev = [], ""
    for i, word in enumerate(text.split()):
        core = word.strip(".,;:()&")
        if core in _UPPER:
            out.append(word)
        elif core in ("ETAL", "ETUX", "ETVIR"):
            out.append(word.replace(core, "et " + core[2:].lower()))
        elif core == "ET" or (prev == "ET" and core in ("AL", "UX", "VIR")) or (i and core in _SMALL):
            out.append(word.lower())
        elif re.fullmatch(r"\d+(ST|ND|RD|TH)", core):
            out.append(word.lower())
        elif any(ch.isdigit() for ch in word):
            out.append(word)
        else:
            out.append(re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), word))  # O'NEIL -> O'Neil
        prev = core
    return " ".join(out)


def _squash(text):
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _zip(text):
    digits = re.sub(r"\D", "", text or "")
    return f"{digits[:5]}-{digits[5:]}" if len(digits) == 9 else (text or "").strip()


def _and(items):
    items = [x for x in items if x]
    if len(items) < 2:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def acres_text(acres):
    return f"{acres or 0:,.1f}".rstrip("0").rstrip(".")


def long_date(day):
    return f"{day:%B} {day.day}, {day.year}"


def recipients(site):
    """One row per owner and mailing address among the site's included parcels, most acres first: who, where, which
    parcels, and the letters already logged to them. Excluded parcels are left out."""
    groups = {}
    for d in site_deals(site):
        if d.included:
            p = d.parcel
            groups.setdefault((_squash(p.owner_name), _squash(p.mail_address), _squash(p.mail_zip)[:5]), []).append(d)
    logged = list(site.letters.select_related("sent_by"))
    rows = []
    for deals in groups.values():
        parcels = sorted((d.parcel for d in deals), key=lambda x: -(x.legal_acres or x.acres or 0))
        p, pks = parcels[0], {x.pk for x in parcels}  # the address as the largest parcel has it
        address = " ".join((p.mail_address or "").split())
        rows.append({
            "key": str(min(pks)), "deals": deals, "parcels": parcels,
            "owner": " ".join((p.owner_name or "").split()), "name": nice(p.owner_name) or "Property Owner",
            "care_of": _CARE_OF.sub("", " ".join((p.care_of or "").split())), "address": address,
            "city": (p.mail_city or "").strip(), "state": (p.mail_state or "").strip().upper(), "zip": _zip(p.mail_zip),
            "acres": sum(x.legal_acres or x.acres or 0 for x in parcels),
            "letters": [x for x in logged if pks & set(x.parcels or [])],
            "mailable": bool(address and ((p.mail_city or "").strip() or (p.mail_zip or "").strip())),
        })
        rows[-1]["lines"] = address_lines(rows[-1], title=True)
    rows.sort(key=lambda r: -r["acres"])
    return rows


def address_lines(row, title=False):
    """The address block: owner, c/o line, street, city/state/ZIP. Capitals for labels and the mail house (as the
    post office prefers); title case in the letters."""
    fix = nice if title else (lambda s: " ".join((s or "").upper().split()))
    lines = [fix(row["owner"]) or ("Property Owner" if title else "PROPERTY OWNER")]
    if row["care_of"]:
        lines.append(("c/o " if title else "C/O ") + fix(row["care_of"]))
    if row["address"]:
        lines.append(fix(row["address"]))
    place = " ".join(x for x in [row["state"], row["zip"]] if x)
    last = ", ".join(x for x in [fix(row["city"]), place] if x)
    if last:
        lines.append(last)
    return lines


def describe(parcels):
    """The land in words: street addresses (or legal descriptions), up to three, then how many more."""
    seen = []
    for p in parcels:
        where = nice(p.situs_address)
        if where and p.situs_city:
            where += f", {nice(p.situs_city)}"
        where = where or nice(p.legal_desc) or f"property ID {p.prop_id or p.key}"
        if where not in seen:
            seen.append(where)
    if len(seen) > 3:
        return f"{seen[0]}, {seen[1]} and {len(seen) - 2} other tracts"
    return _and(seen)


def context(site, row, sender, day, dear=""):
    """The merge fields for one owner's letter."""
    ids = [p.prop_id or p.key for p in row["parcels"]]
    return {
        "owner": " ".join((dear or "").split()) or row["name"],
        "property": describe(row["parcels"]),
        "parcels": ("parcel " if len(ids) == 1 else "parcels ") + _and(ids),
        "acres": acres_text(row["acres"]),
        "county": _and(list(dict.fromkeys(p.county.name for p in row["parcels"]))),
        "site": site.name,
        "date": long_date(day),
        **{key: sender.get(key, "") for key, _, _ in SENDER_FIELDS},
    }


def merge(body, ctx):
    """The body with each {field} filled in; a field it doesn't know stays as typed."""
    return _FIELD.sub(lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else m.group(0), body or "")


def problems(body, sender):
    """What would print wrong: merge fields that don't exist, and sender fields the letter uses that are blank."""
    used = {m.group(1) for m in _FIELD.finditer(body or "")}
    out = []
    unknown = sorted(used - {key for key, _ in FIELDS})
    if unknown:
        out.append("The letter has merge fields that don't exist: " + ", ".join("{" + u + "}" for u in unknown)
                   + ". Fix the spelling or take out the braces.")
    blank = [label.lower() for key, label, _ in SENDER_FIELDS if key in used and not (sender.get(key) or "").strip()]
    if blank:
        out.append(f"The letter uses {_and(blank)}, but {'it is' if len(blank) == 1 else 'they are'} blank.")
    return out


def build(site, rows, body, sender, day, dears=None):
    """The merged letters, in the order of rows: [{row, lines (the address block), text}]."""
    dears = dears or {}
    return [{"row": r, "lines": address_lines(r, title=True),
             "text": merge(body, context(site, r, sender, day, dears.get(r["key"], "")))} for r in rows]


def label_sheets(rows, start=1):
    """Labels in sheets of 30 for Avery 5160, beginning at label `start` on the first sheet (to finish a used one).
    Each is {lines, small}: long or five-line addresses print smaller to fit."""
    blocks = [None] * (min(max(start, 1), LABELS_PER_SHEET) - 1)
    for r in rows:
        lines = address_lines(r)
        blocks.append({"lines": lines, "small": len(lines) > 4 or max(len(x) for x in lines) > 34})
    return [blocks[i:i + LABELS_PER_SHEET] for i in range(0, len(blocks), LABELS_PER_SHEET)]


def _cell(value):
    """A CSV cell that a spreadsheet won't run as a formula."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def mail_csv(rows, dears=None):
    """A CSV for a mail house: name and address in capitals, the salutation, and what each letter is about."""
    dears = dears or {}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Care of", "Address", "City", "State", "ZIP", "Dear", "Parcels", "Acres", "County"])
    for r in rows:
        lines = address_lines(r)
        w.writerow([_cell(x) for x in [
            lines[0], r["care_of"].upper(), r["address"].upper(), r["city"].upper(), r["state"], r["zip"],
            " ".join((dears.get(r["key"]) or "").split()) or r["name"],
            ", ".join(p.prop_id or p.key for p in r["parcels"]), acres_text(r["acres"]),
            _and(list(dict.fromkeys(p.county.name for p in r["parcels"])))]])
    return "﻿" + out.getvalue()  # the byte-order mark makes Excel read accents right


def export_name(site, kind, ext, day=None):
    """ACMI_Letters_<Site_Name_Words>_2026.10.18.pdf"""
    words = "_".join(w for w in re.split(r"[^A-Za-z0-9]+", site.name) if w)
    return f"ACMI_{kind}_{words}_{(day or timezone.localdate()):%Y.%m.%d}.{ext}"


def _first_contact(target, when, user, why="", log=True):
    """Date "Owner contacted" unless an earlier contact is already dated."""
    current = (target.milestones or {}).get("contacted")
    if not current or current > when.isoformat():
        return apply_milestones(target, {"contacted": when}, user, why=why, log=log)
    return 0


def log_sent(site, picked, body, sender, day, dears=None, method=LandownerLetter.Method.MAIL, sent_on=None,
             notes="", template=None, user=None):
    """Log a letter to each picked owner, as it went out, and date "Owner contacted" on their parcel deals and the
    site. Returns (logged, skipped: already logged to that owner and address that day, the same way)."""
    sent_on = sent_on or timezone.localdate()
    if missing_deals(site):
        ensure_deals(site)
    stored = {d.parcel_id: d for d in site.parcel_deals.select_related("parcel", "site")}
    made, skipped = [], 0
    for letter in build(site, picked, body, sender, day, dears):
        r = letter["row"]
        owner, mailing = letter["lines"][0][:255], "\n".join(letter["lines"])[:400]
        if site.letters.filter(owner_name=owner, mailing=mailing, sent_on=sent_on, method=method).exists():
            skipped += 1
            continue
        made.append(LandownerLetter.objects.create(
            site=site, owner_name=owner, mailing=mailing, parcels=sorted(p.pk for p in r["parcels"]),
            template=template, body=letter["text"], method=method, sent_on=sent_on, sent_by=user, notes=notes[:300]))
        for p in r["parcels"]:
            if p.pk in stored:
                _first_contact(stored[p.pk], sent_on, user, log=False)  # logged once for the batch, below
    if made:
        names = [x.owner_name for x in made[:4]] + ([f"{len(made) - 4} more"] if len(made) > 4 else [])
        how = LandownerLetter.Method(method).label.lower()
        LandSiteEvent.objects.create(site=site, user=user, kind="letter", text=(
            f"Letter{'s' if len(made) != 1 else ''} sent ({how}, {sent_on:%b} {sent_on.day}) to {_and(names)}")[:500])
        _first_contact(site, sent_on, user, why="letters sent")
    return len(made), skipped
