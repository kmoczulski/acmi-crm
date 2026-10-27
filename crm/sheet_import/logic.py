"""Companies and contacts from a spreadsheet.

1. read_upload(): a .xlsx / .csv / HTML-table .xls file -> sheets of text cells (the file itself is never kept;
   the views hold the cells in the session until the import is done).
2. guess_mapping(): each column -> a CRM field, from its header.
3. build_plan(): a dry run. Every row becomes "New company / Matches <company>" and "New contact / Matches <person>",
   or "Skipped: reason", with flags. Nothing is written.
4. run(): the same plan, written in one transaction, recorded as an ImportBatch.
5. undo(): for 7 days, deletes what the import created and puts back the fields it filled.

Matching follows the workbook import (crm/management/commands/import_workbook.py): people on email, companies on
normalize_key(name), then on an email domain the company already has. A directory firm's domain never becomes a
company's (the row is skipped), free-mail domains are never a company's, and ignored domains (Mail filters) are left
off. "Fill blanks only" never changes a value the CRM already has.
"""
import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import transaction
from django.utils import timezone

from crm import choices
from crm.land.owners import _norm_header, _TableParser
from crm.mail.sync import FREE_MAIL, guess_company_name, squash
from crm.merge import core_name
from crm.models import (
    Activity, Company, CompanyStatus, Firm, ImportBatch, List, ListEntry, MailFilter, ObjectType, Person, Priority,
    normalize_key, suppress_history,
)
from crm.names import clean_name
from crm.permissions import team_users

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 80
MAX_CELLS = 400_000  # across every sheet in the file, so the session stays small
UNDO_DAYS = 7


class SheetError(ValueError):
    """A file the import can't use; the message is shown as is."""


# ---------------------------------------------------------------------------
# Fields and column guessing
# ---------------------------------------------------------------------------

# (key, label, group). The order is the dropdown's order.
FIELDS = [
    ("company_name", "Company name", "Company"),
    ("company_domain", "Website or domain", "Company"),
    ("company_sector", "Sector", "Company"),
    ("company_city", "City", "Company"),
    ("company_state", "State", "Company"),
    ("company_location", "HQ location (city, state)", "Company"),
    ("company_priority", "Priority", "Company"),
    ("company_status", "Stage", "Company"),
    ("company_owner", "Owner", "Company"),
    ("company_linkedin", "Company LinkedIn", "Company"),
    ("company_notes", "Company notes", "Company"),
    ("first_name", "First name", "Contact"),
    ("last_name", "Last name", "Contact"),
    ("full_name", "Full name", "Contact"),
    ("email", "Email", "Contact"),
    ("title", "Title", "Contact"),
    ("phone", "Phone", "Contact"),
    ("linkedin", "LinkedIn", "Contact"),
    ("notes", "Contact notes", "Contact"),
]
FIELD_LABELS = {key: label for key, label, _ in FIELDS}

# Header names, compared lower-case with everything but letters and digits stripped (as in crm/land/owners.py).
ALIASES = {
    "company_name": ["company", "companyname", "organization", "organisation", "org", "orgname", "account",
                     "accountname", "employer", "business", "businessname", "firm", "firmname", "legalname",
                     "companylegalname", "tenant", "prospect"],
    "company_domain": ["website", "web", "webaddress", "url", "domain", "companywebsite", "companydomain", "websiteurl",
                       "site", "homepage", "companyurl", "emaildomain"],
    "company_sector": ["sector", "industry", "vertical", "category", "companyindustry", "segment", "market"],
    "company_city": ["city", "hqcity", "companycity", "town"],
    "company_state": ["state", "st", "hqstate", "companystate", "province", "stateprovince"],
    "company_location": ["location", "hq", "hqlocation", "headquarters", "citystate", "companylocation", "address"],
    "company_priority": ["priority", "tier", "companypriority"],
    "company_status": ["stage", "status", "companystatus", "companystage", "leadstatus", "pipelinestage"],
    "company_owner": ["owner", "accountowner", "companyowner", "leadowner", "assignedto", "acmiowner", "lead",
                      "acmilead", "rep"],
    "company_linkedin": ["companylinkedin", "linkedincompany", "companylinkedinurl", "linkedincompanyurl"],
    "company_notes": ["companynotes", "description", "companydescription", "about", "overview"],
    "first_name": ["firstname", "first", "givenname", "fname", "contactfirstname"],
    "last_name": ["lastname", "last", "surname", "familyname", "lname", "contactlastname"],
    "full_name": ["name", "fullname", "contact", "contactname", "person", "personname", "contactfullname"],
    "email": ["email", "emailaddress", "email1", "workemail", "contactemail", "primaryemail", "businessemail",
              "emails", "mail"],
    "title": ["title", "jobtitle", "position", "role", "contacttitle", "jobrole"],
    "phone": ["phone", "phonenumber", "mobile", "cell", "cellphone", "mobilephone", "workphone", "telephone", "tel",
              "directphone", "officephone", "contactphone", "directdial"],
    "linkedin": ["linkedin", "linkedinurl", "linkedinprofile", "personlinkedin", "linkedinprofileurl",
                 "contactlinkedin"],
    "notes": ["notes", "note", "comments", "comment", "contactnotes", "remarks"],
}
_ALIAS_TO_FIELD = {alias: key for key, aliases in ALIASES.items() for alias in aliases}
# When no header matches exactly: the first word found in it decides (most specific first).
CONTAINS = [
    ("companylinkedin", "company_linkedin"), ("linkedin", "linkedin"), ("email", "email"), ("phone", "phone"),
    ("mobile", "phone"), ("website", "company_domain"), ("domain", "company_domain"), ("firstname", "first_name"),
    ("lastname", "last_name"), ("company", "company_name"), ("organi", "company_name"), ("industry", "company_sector"),
    ("sector", "company_sector"), ("title", "title"), ("note", "notes"), ("owner", "company_owner"),
    ("priority", "company_priority"), ("city", "company_city"), ("state", "company_state"),
]


def guess_field(header):
    h = _norm_header(header)
    if not h:
        return ""
    if h in _ALIAS_TO_FIELD:
        return _ALIAS_TO_FIELD[h]
    for word, key in CONTAINS:
        if word in h:
            return key
    return ""


def guess_mapping(header):
    """One field key (or "" = ignore) per column; each field goes to the first column that claims it."""
    taken, out = set(), []
    for h in header:
        key = guess_field(h)
        if key in taken:
            key = ""
        if key:
            taken.add(key)
        out.append(key)
    return out


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time() else value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # phone numbers and ZIPs typed as numbers
    return str(value).replace("\xa0", " ").strip()


def _column_name(i):
    name = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        name = chr(65 + rem) + name
    return name


def _sheet(name, raw_rows):
    """{name, header, rows, numbers, too_big}: the first non-blank row is the header; blank rows are dropped."""
    rows, numbers, too_big = [], [], 0
    header, width = None, 0
    for number, raw in raw_rows:
        cells = [_cell(v) for v in raw][:MAX_COLUMNS]
        if not any(cells):
            continue
        if header is None:
            header = cells
            continue
        if len(rows) >= MAX_ROWS:
            too_big = len(rows) + 1  # stop reading: the sheet is over the limit either way
            break
        rows.append(cells)
        numbers.append(number)
    header = header or []
    for r in [header, *rows]:
        last = max((i for i, v in enumerate(r) if v), default=-1)
        width = max(width, last + 1)
    header = [(header[i] if i < len(header) and header[i] else f"Column {_column_name(i)}") for i in range(width)]
    rows = [(r + [""] * width)[:width] for r in rows]
    if too_big:
        rows, numbers = [], []
    return {"name": str(name)[:120], "header": header, "rows": rows, "numbers": numbers, "too_big": too_big}


def read_upload(filename, data):
    """The file's sheets as text. Raises SheetError with a message for the person."""
    if len(data) > MAX_BYTES:
        raise SheetError(f"That file is {len(data) / 1024 / 1024:.1f} MB. The limit is {MAX_BYTES // 1024 // 1024} MB: "
                         "split it into smaller files, or save just the sheet you need.")
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    head = data[:2048].lstrip().lower()
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        try:
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:
            raise SheetError("That file couldn't be opened as an Excel workbook. Re-save it as .xlsx or .csv.")
        sheets = [_sheet(ws.title, enumerate(ws.iter_rows(values_only=True), start=1)) for ws in book.worksheets]
        book.close()
    elif head.startswith((b"<", b"\xef\xbb\xbf<")):
        # Older web viewers "export to Excel" as an HTML table saved as .xls.
        parser = _TableParser()
        parser.feed(data.decode("utf-8", errors="replace"))
        sheets = [_sheet("Sheet1", enumerate(parser.rows, start=1))]
    elif suffix == ".xls":
        raise SheetError("Old binary .xls files can't be read. Open it in Excel and save it as .xlsx (or .csv).")
    elif suffix in (".csv", ".txt", ".tsv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        sheets = [_sheet(filename.rsplit(".", 1)[0], enumerate(csv.reader(io.StringIO(text), dialect), start=1))]
    else:
        raise SheetError("Upload an Excel workbook (.xlsx) or a .csv file.")
    sheets = [s for s in sheets if s["header"]]
    if not sheets:
        raise SheetError("That file has no rows in it.")
    if sum(len(s["rows"]) * len(s["header"]) for s in sheets) > MAX_CELLS:
        raise SheetError("That file is too big to import in one go. Save the sheet you need on its own, or split it.")
    return sheets


# ---------------------------------------------------------------------------
# Cleaning values
# ---------------------------------------------------------------------------

_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}")
_url_ok = URLValidator(schemes=["http", "https"])


def domain_of_site(value):
    """'https://www.acme.com/about' / 'acme.com' / 'jane@acme.com' -> 'acme.com'; '' when it isn't one."""
    s = (value or "").strip().lower()
    if not s:
        return ""
    if "@" in s and "/" not in s:
        s = s.rsplit("@", 1)[1]
    if "://" not in s:
        s = "http://" + s
    host = (urlparse(s).hostname or "").removeprefix("www.")
    return host if _DOMAIN.fullmatch(host) else ""


def clean_url(value):
    s = (value or "").strip()
    if not s or "@" in s.split("/")[0]:
        return ""
    if "://" not in s:
        s = "https://" + s
    try:
        _url_ok(s)
    except ValidationError:
        return ""
    return s[:200]


def clean_email(value):
    s = (value or "").strip().lower().removeprefix("mailto:").strip("<>")
    try:
        validate_email(s)
    except ValidationError:
        return ""
    return s


def _limit(model, name, value):
    length = model._meta.get_field(name).max_length
    return value[:length] if length else value


# ---------------------------------------------------------------------------
# What's already in the CRM (loaded once per plan)
# ---------------------------------------------------------------------------


class Context:
    def __init__(self):
        self.companies = {c.pk: c for c in Company.objects.all()}
        self.by_key, self.by_domain, self.by_squash, self.by_core = {}, {}, {}, defaultdict(list)
        for c in self.companies.values():
            self.by_key.setdefault(c.name_key, c)
            for d in c.domains or []:
                self.by_domain.setdefault(d.lower(), c)
            self.by_squash.setdefault(squash(c.name), c)
            core = core_name(c.name)
            if len(core) >= 3:
                self.by_core[core].append(c)
        self.other_emails = {}
        for pk, emails in Person.objects.exclude(other_emails=[]).values_list("pk", "other_emails"):
            for e in emails or []:
                self.other_emails.setdefault(str(e).lower(), pk)
        self._people_by_email = {}
        self._people_at = {}
        firms = dict(Firm.objects.values_list("pk", "name"))
        self.firm_domains = {}
        for pk, domains in Firm.objects.values_list("pk", "domains"):
            for d in domains or []:
                self.firm_domains.setdefault(d.lower(), (pk, firms[pk]))
        filters = dict(MailFilter.objects.values_list("pattern", "kind"))
        self.protected = {p for p, k in filters.items() if k == MailFilter.Kind.PROTECT}
        self.ignored = {p for p, k in filters.items() if k == MailFilter.Kind.IGNORE}
        self.internal = {d.lower() for d in settings.INTERNAL_EMAIL_DOMAINS}
        self.users = {}
        for u in team_users():
            for name in (u.username, u.email, u.first_name, u.get_full_name(), (u.email or "").split("@")[0]):
                if name:
                    self.users.setdefault(name.strip().lower(), u)
        self.statuses = {}
        for value, label in choices.pairs(CompanyStatus):
            self.statuses[value.lower()] = value
            self.statuses[label.lower()] = value
        self.priorities = {"h": Priority.HIGH, "m": Priority.MID, "med": Priority.MID, "medium": Priority.MID,
                           "l": Priority.LOW}
        for value, label in choices.pairs(Priority):
            self.priorities[value.lower()] = value
            self.priorities[label.lower()] = value

    def load_people(self, emails):
        emails = [e for e in emails if e and e not in self._people_by_email]
        for i in range(0, len(emails), 500):
            for p in Person.objects.filter(email__in=emails[i:i + 500]).select_related("company"):
                self._people_by_email[p.email] = p

    def person_for_email(self, email):
        person = self._people_by_email.get(email)
        if person is None and email in self.other_emails:
            person = Person.objects.select_related("company").filter(pk=self.other_emails[email]).first()
        return person

    def person_named(self, company, full_name):
        """A person without an email is matched by name within their (existing) company."""
        if company.pk not in self._people_at:
            self._people_at[company.pk] = {normalize_key(p.full_name): p for p in company.people.all()}
        return self._people_at[company.pk].get(normalize_key(full_name))

    def is_protected(self, email):
        return email in self.protected or email.rsplit("@", 1)[-1] in self.protected

    def is_internal(self, domain):
        return domain in self.internal or any(domain.endswith("." + d) for d in self.internal)

    def firm_for(self, domain):
        return self.firm_domains.get(domain) if domain else None


# ---------------------------------------------------------------------------
# The plan (a dry run)
# ---------------------------------------------------------------------------


@dataclass
class Target:
    """A company or person the import creates or touches. Several rows can point at the same company."""

    kind: str  # "company" / "person"
    existing: object = None
    name: str = ""
    key: object = None
    how: str = ""  # "by name", "by domain", "by email"...
    values: dict = field(default_factory=dict)
    extras: list = field(default_factory=list)  # company: domains; person: phones
    company: "Target" = None  # person: their company
    rows: list = field(default_factory=list)
    changes: dict = field(default_factory=dict)  # existing: {field: new value}
    new_extras: list = field(default_factory=list)
    close: list = field(default_factory=list)  # new company: existing companies with a close name
    obj: object = None  # after run()
    email: str = ""  # person

    @property
    def is_new(self):
        return self.existing is None

    @property
    def record(self):
        return self.obj or self.existing


@dataclass
class Row:
    number: int
    company: Target = None
    person: Target = None
    skipped: str = ""
    flags: list = field(default_factory=list)
    person_skipped: str = ""
    firm_id: int = None  # skipped because the domain is a directory firm's

    @property
    def company_first(self):
        return self.company is not None and self.company.rows[0] == self.number


@dataclass
class Plan:
    rows: list
    companies: list
    people: list
    mode: str

    @property
    def counts(self):
        companies_new = sum(1 for t in self.companies if t.is_new)
        people_new = sum(1 for t in self.people if t.is_new)
        return {
            "rows": len(self.rows),
            "companies_new": companies_new,
            "companies_matched": len(self.companies) - companies_new,
            "companies_changed": sum(1 for t in self.companies if not t.is_new and (t.changes or t.new_extras)),
            "people_new": people_new,
            "people_matched": len(self.people) - people_new,
            "people_changed": sum(1 for t in self.people if not t.is_new and (t.changes or t.new_extras)),
            "skipped": sum(1 for r in self.rows if r.skipped),
            "flagged": sum(1 for r in self.rows if r.flags and not r.skipped),
        }


def _parse_choice(raw, table, label, r):
    if not raw:
        return ""
    value = table.get(raw.strip().lower())
    if value is None:
        r.flags.append(f"{label} “{raw}” isn't one the CRM has: left out")
        return ""
    return value


COMPANY_FIELDS = ["sector", "hq_location", "priority", "status", "owner_id", "website", "linkedin", "notes"]
PERSON_FIELDS = ["first_name", "last_name", "full_name", "title", "linkedin", "notes"]
FIELD_NAMES = {"sector": "sector", "hq_location": "HQ location", "priority": "priority", "status": "stage",
               "owner_id": "owner", "website": "website", "linkedin": "LinkedIn", "notes": "notes",
               "first_name": "first name", "last_name": "last name", "full_name": "name", "title": "title",
               "company_id": "company", "domains": "domain", "phones": "phone"}


def _diff(obj, values, mode):
    """The fields to write on an existing record: blanks only, or (update) every value that differs."""
    out = {}
    for name, value in values.items():
        if value in ("", None):
            continue
        current = getattr(obj, name)
        if current in ("", None):
            out[name] = value
        elif mode == ImportBatch.Mode.UPDATE and current != value:
            if name == "notes":
                if value not in current:  # notes are added to, never replaced
                    out[name] = f"{current}\n\n{value}"
            else:
                out[name] = value
    return out


def build_plan(header, rows, mapping, options=None, numbers=None, ctx=None):
    """What importing these rows would do. Nothing is written.

    options: mode ("fill" / "update"), owner / status / priority (defaults for new companies), and use
    ({name_key: company pk}: a close-named existing company picked on the preview instead of a new one).
    """
    options = options or {}
    ctx = ctx or Context()
    mode = options.get("mode") or ImportBatch.Mode.FILL
    use = {k: int(v) for k, v in (options.get("use") or {}).items() if str(v).isdigit()}
    numbers = numbers or [i + 2 for i in range(len(rows))]
    cols = {key: i for i, key in enumerate(mapping) if key}
    companies, people, out = {}, {}, []
    new_cores = {}  # core name -> the first new company with it in this file

    email_col = cols.get("email")
    if email_col is not None:
        ctx.load_people([clean_email(r[email_col]) for r in rows if email_col < len(r)])

    def company_target(name, domain, site_domain, values, r):
        key = normalize_key(name) if name else None
        existing, how = None, ""
        if key and key in use and use[key] in ctx.companies:
            existing, how = ctx.companies[use[key]], "picked on the preview"
        elif key and key in ctx.by_key:
            existing, how = ctx.by_key[key], "by name"
        elif domain and domain in ctx.by_domain:
            existing, how = ctx.by_domain[domain], "by website" if domain == site_domain else "by email domain"
            if name and squash(name) != squash(existing.name):
                r.flags.append(f"The sheet calls it “{name}”; {domain} is on {existing.name} in the CRM")
        elif not name and domain:
            existing = ctx.by_squash.get(squash(domain.split(".")[0]))
            how = "by domain name" if existing else ""
        tkey = ("pk", existing.pk) if existing else ("new", key)
        t = companies.get(tkey)
        if t is None:
            t = companies[tkey] = Target("company", existing=existing, name=existing.name if existing else name,
                                         key=key, how=how)
            if existing is None:
                core = core_name(name)
                t.close = [c for c in ctx.by_core.get(core, []) if c.name_key != key] if len(core) >= 3 else []
                if len(core) >= 3 and core in new_cores:
                    r.flags.append(f"Close to “{new_cores[core].name}” on row {new_cores[core].rows[0]}")
                new_cores.setdefault(core, t)
        t.rows.append(r.number)
        for k, v in values.items():
            if v and not t.values.get(k):
                t.values[k] = v
        if domain and domain not in t.extras:
            owner = ctx.by_domain.get(domain)
            if owner is not None and owner is not existing:
                r.flags.append(f"{domain} is already on {owner.name}: not added")
            else:
                t.extras.append(domain)
        return t

    for cells, number in zip(rows, numbers):
        def get(key):
            i = cols.get(key)
            return cells[i].strip() if i is not None and i < len(cells) else ""

        r = Row(number)
        out.append(r)
        raw_email = get("email")
        email = clean_email(raw_email)
        if raw_email and not email:
            r.flags.append(f"Invalid email “{raw_email}”: left out")
        raw_site = get("company_domain")
        site_domain = domain_of_site(raw_site)
        if raw_site and not site_domain:
            r.flags.append(f"“{raw_site}” isn't a website: left out")
        mail_domain = email.rsplit("@", 1)[1] if email else ""

        if email and ctx.is_protected(email):
            r.skipped = f"{email} is protected (Settings › Mail filters): never stored"
        elif mail_domain and ctx.is_internal(mail_domain):
            r.skipped = f"{email} is an ACMI address"
        else:
            for d in (site_domain, mail_domain):
                firm = ctx.firm_for(d)
                if firm:
                    r.skipped = f"{d} belongs to {firm[1]} in the directory, which never becomes a company"
                    r.firm_id = firm[0]
                    break
        if r.skipped:
            continue

        if site_domain in FREE_MAIL:
            site_domain = ""
        domain = site_domain or ("" if mail_domain in FREE_MAIL else mail_domain)
        if domain and (domain in ctx.ignored or (email and email in ctx.ignored and domain == mail_domain)):
            r.flags.append(f"{domain} is on the ignore list (Settings › Mail filters): not added to the company")
            if domain == site_domain:
                site_domain = ""
            domain = ""

        first, last, full = get("first_name"), get("last_name"), get("full_name")
        if full and not (first or last):
            parsed = clean_name(full, email)
            if parsed:
                first, last, full = parsed
            else:
                first, _, last = full.partition(" ")
        elif (first or last) and not full:
            full = " ".join(p for p in (first, last) if p)

        # The company
        name = get("company_name")
        location = get("company_location") or ", ".join(p for p in (get("company_city"), get("company_state")) if p)
        values = {
            "sector": _limit(Company, "sector", get("company_sector")),
            "hq_location": _limit(Company, "hq_location", location),
            "priority": _parse_choice(get("company_priority"), ctx.priorities, "Priority", r),
            "status": _parse_choice(get("company_status"), ctx.statuses, "Stage", r),
            "website": clean_url(raw_site) if site_domain else "",
            "linkedin": clean_url(get("company_linkedin")),
            "notes": get("company_notes"),
        }
        owner = get("company_owner")
        if owner:
            user = ctx.users.get(owner.lower())
            if user is None:
                r.flags.append(f"Owner “{owner}” has no account in the hub: left out")
            values["owner_id"] = user.pk if user else None
        if get("company_linkedin") and not values["linkedin"]:
            r.flags.append(f"“{get('company_linkedin')}” isn't a link: left out")
        if name:
            r.company = company_target(_limit(Company, "name", name), domain, site_domain, values, r)
        elif domain:
            if domain in ctx.by_domain or ctx.by_squash.get(squash(domain.split(".")[0])):
                r.company = company_target("", domain, site_domain, values, r)
            elif site_domain:
                r.company = company_target(guess_company_name(domain), domain, site_domain, values, r)
                r.flags.append(f"No company name: called it “{r.company.name}” from {domain}")
            else:
                r.flags.append(f"No company name, and no company in the CRM has {domain}: contact added without one")

        # The contact
        if not (email or full):
            if r.company is None:
                r.skipped = "No company name, contact name or email"
            continue
        pkey = email or ("name", normalize_key(full), r.company.key if r.company else None,
                         r.company.existing.pk if r.company and r.company.existing else None)
        if pkey in people:
            r.person_skipped = f"Same {'email' if email else 'name'} as row {people[pkey].rows[0]}: skipped"
            r.flags.append(r.person_skipped)
            continue
        existing, how = None, ""
        if email:
            existing, how = ctx.person_for_email(email), "by email"
        elif r.company and r.company.existing:
            existing, how = ctx.person_named(r.company.existing, full), f"by name at {r.company.existing.name}"
        linkedin = clean_url(get("linkedin"))
        if get("linkedin") and not linkedin:
            r.flags.append(f"“{get('linkedin')}” isn't a link: left out")
        t = people[pkey] = Target("person", existing=existing, name=(existing.full_name or existing.email)
                                  if existing else (full or email), key=pkey, how=how, company=r.company)
        t.rows.append(number)
        t.values = {
            "first_name": _limit(Person, "first_name", first), "last_name": _limit(Person, "last_name", last),
            "full_name": _limit(Person, "full_name", full), "title": _limit(Person, "title", get("title")),
            "linkedin": linkedin, "notes": get("notes"),
        }
        t.email = email
        phone = get("phone")
        if phone:
            t.extras.append(phone[:40])
        if existing and r.company and existing.company_id and (r.company.is_new
                                                               or r.company.existing.pk != existing.company_id):
            where = existing.company.name if existing.company else "another company"
            r.flags.append(f"{t.name} is at {where} in the CRM: "
                           + ("moves to this row's company" if mode == ImportBatch.Mode.UPDATE else "left there"))
        r.person = t

    # What each record gets
    defaults = {"owner_id": int(options["owner"]) if str(options.get("owner") or "").isdigit() else None,
                "status": options.get("status") or "", "priority": options.get("priority") or ""}
    for t in companies.values():
        if t.is_new:
            for k, v in defaults.items():
                if v and not t.values.get(k):
                    t.values[k] = v
            t.new_extras = list(t.extras)
        else:
            t.changes = _diff(t.existing, t.values, mode)
            t.new_extras = [d for d in t.extras if d not in (t.existing.domains or [])]
    for t in people.values():
        if t.is_new:
            t.new_extras = list(t.extras)
            continue
        p = t.existing
        values = dict(t.values)
        if mode == ImportBatch.Mode.FILL and (p.first_name or p.last_name or p.full_name):
            # A name is one thing: don't fill a missing last name onto a different full name.
            values.pop("first_name"), values.pop("last_name"), values.pop("full_name")
        t.changes = _diff(p, values, mode)
        if t.company is not None and (mode == ImportBatch.Mode.UPDATE or p.company_id is None):
            if t.company.is_new or t.company.existing.pk != p.company_id:
                t.changes["company_id"] = t.company  # resolved to a pk when written
        have = {re.sub(r"\D", "", x) for x in p.phones or []}
        t.new_extras = [x for x in t.extras if re.sub(r"\D", "", x) not in have]
    return Plan(rows=out, companies=list(companies.values()), people=list(people.values()), mode=mode)


def change_names(t):
    names = [FIELD_NAMES.get(k, k) for k in t.changes]
    if t.new_extras:
        names.append(FIELD_NAMES["domains" if t.kind == "company" else "phones"])
    return names


# ---------------------------------------------------------------------------
# Writing it
# ---------------------------------------------------------------------------


def _json(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _add_to_list(spec, object_type, records, user, undo):
    """spec: an existing List pk, or {"new": name}. Adds every record not on it yet."""
    if not spec or not records:
        return None
    if isinstance(spec, dict):
        name = (spec.get("new") or "").strip()[:200]
        if not name:
            return None
        lst = List.objects.filter(name=name).first()
        if lst is None:
            lst = List.objects.create(name=name, object_type=object_type, created_by=user)
            undo["lists"].append(lst.pk)
    else:
        lst = List.objects.filter(pk=spec, object_type=object_type).first()
        if lst is None:
            return None
    attr = "company" if object_type == ObjectType.COMPANY else "person"
    have = set(lst.entries.values_list(f"{attr}_id", flat=True))
    for rec in records:
        if rec.pk not in have:
            entry = ListEntry.objects.create(list=lst, added_by=user, stage=(lst.stages or [""])[0], **{attr: rec})
            undo["list_entries"].append(entry.pk)
            have.add(rec.pk)
    return lst


def run(sheet, mapping, options, user, filename):
    """Import the sheet: the plan from build_plan(), written in one transaction. Returns the ImportBatch."""
    now = timezone.now()
    undo = {"companies": [], "people": [], "company_changes": {}, "person_changes": {}, "added_domains": {},
            "added_phones": {}, "list_entries": [], "lists": [], "activities": []}
    token = suppress_history.set(True)  # one "Imported from" / "Updated from" line per record instead
    try:
        with transaction.atomic():
            plan = build_plan(sheet["header"], sheet["rows"], mapping, options, sheet.get("numbers"))

            def log(**kwargs):
                a = Activity.objects.create(occurred_at=now, actor=user, is_background=True, **kwargs)
                undo["activities"].append(a.pk)

            for t in plan.companies:
                if t.is_new:
                    c = Company(name=t.name, domains=list(t.new_extras), created_by=user,
                                **{k: v for k, v in t.values.items() if v not in ("", None)})
                    c.save()
                    t.obj = c
                    undo["companies"].append(c.pk)
                    log(kind=Activity.Kind.RECORD_CREATED, company=c, summary=f"Imported from {filename}")
                elif t.changes or t.new_extras:
                    c = t.existing
                    undo["company_changes"][str(c.pk)] = {k: [_json(getattr(c, k)), _json(v)]
                                                          for k, v in t.changes.items()}
                    for k, v in t.changes.items():
                        setattr(c, k, v)
                    if t.new_extras:
                        c.domains = [*(c.domains or []), *t.new_extras]
                        undo["added_domains"][str(c.pk)] = list(t.new_extras)
                    c.save()
                    log(kind=Activity.Kind.FIELD_CHANGE, company=c,
                        summary=f"Updated from {filename}: {', '.join(change_names(t))}")
            for t in plan.people:
                company = t.company.record if t.company else None
                if t.is_new:
                    p = Person(email=t.email or None, company=company, phones=list(t.new_extras), created_by=user,
                               **{k: v for k, v in t.values.items() if v})
                    p.save()
                    t.obj = p
                    undo["people"].append(p.pk)
                    log(kind=Activity.Kind.RECORD_CREATED, person=p, company=company,
                        summary=f"Imported from {filename}")
                elif t.changes or t.new_extras:
                    p = t.existing
                    changes = dict(t.changes)
                    if "company_id" in changes:
                        changes["company_id"] = company.pk
                    undo["person_changes"][str(p.pk)] = {k: [getattr(p, k), v] for k, v in changes.items()}
                    for k, v in changes.items():
                        setattr(p, k, v)
                    if {"first_name", "last_name"} & set(changes) and "full_name" not in changes:
                        p.full_name = " ".join(x for x in (p.first_name, p.last_name) if x)
                    if t.new_extras:
                        p.phones = [*(p.phones or []), *t.new_extras]
                        undo["added_phones"][str(p.pk)] = list(t.new_extras)
                    p.save()
                    log(kind=Activity.Kind.FIELD_CHANGE, person=p, company_id=p.company_id,
                        summary=f"Updated from {filename}: {', '.join(change_names(t))}")
            _add_to_list(options.get("company_list"), ObjectType.COMPANY, [t.record for t in plan.companies], user,
                         undo)
            _add_to_list(options.get("person_list"), ObjectType.PERSON, [t.record for t in plan.people], user, undo)
            batch = ImportBatch.objects.create(
                filename=filename[:255], sheet=sheet.get("name", "")[:120], mode=plan.mode, rows_read=len(plan.rows),
                counts=plan.counts, results=[result_row(r) for r in plan.rows], undo=undo, created_by=user,
            )
    finally:
        suppress_history.reset(token)
    return batch


def _ref(t):
    if t is None:
        return None
    rec = t.record
    state = "new" if t.is_new else ("updated" if (t.changes or t.new_extras) else "matched")
    return {"pk": rec.pk if rec else None, "name": str(rec) if rec else t.name, "state": state,
            "first": t.rows[0], "how": t.how, "fields": change_names(t)}


def result_row(r):
    return {"row": r.number, "company": _ref(r.company), "person": _ref(r.person), "skipped": r.skipped,
            "flags": r.flags}


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def undo_until(batch):
    return batch.created_at + timedelta(days=UNDO_DAYS)


def can_undo(batch):
    return batch.undone_at is None and timezone.now() < undo_until(batch)


def touched_since(batch):
    """How many of the records this import created have had anything else logged on them since (it goes too)."""
    log = batch.undo or {}
    others = Activity.objects.exclude(pk__in=log.get("activities", []))
    companies = set(others.filter(company_id__in=log.get("companies", [])).values_list("company_id", flat=True))
    people = set(others.filter(person_id__in=log.get("people", [])).values_list("person_id", flat=True))
    return len(companies) + len(people)


def _revert(model, changes, added, extra_field, summary, user, now):
    reverted = 0
    for pk in set(changes) | set(added):
        obj = model.objects.filter(pk=int(pk)).first()
        if obj is None:
            continue
        put_back = []
        for name, (old, new) in changes.get(pk, {}).items():
            if _json(getattr(obj, name)) == new:  # only where nobody has changed it since
                setattr(obj, name, old)
                put_back.append(FIELD_NAMES.get(name, name))
        mine = added.get(pk, [])
        if mine:
            kept = [x for x in getattr(obj, extra_field) or [] if x not in mine]
            if kept != getattr(obj, extra_field):
                setattr(obj, extra_field, kept)
                put_back.append(FIELD_NAMES[extra_field])
        if put_back:
            obj.save()
            reverted += 1
            links = {"company": obj} if model is Company else {"person": obj, "company_id": obj.company_id}
            Activity.objects.create(occurred_at=now, actor=user, is_background=True, kind=Activity.Kind.FIELD_CHANGE,
                                    summary=f"{summary}: {', '.join(put_back)} put back", **links)
    return reverted


def undo(batch, user):
    """Delete what the import created and put back what it filled. Returns counts."""
    log = batch.undo or {}
    now = timezone.now()
    summary = f"Import of {batch.filename} undone"
    token = suppress_history.set(True)
    try:
        with transaction.atomic():
            Activity.objects.filter(pk__in=log.get("activities", [])).delete()
            ListEntry.objects.filter(pk__in=log.get("list_entries", [])).delete()
            people = _revert(Person, log.get("person_changes", {}), log.get("added_phones", {}), "phones", summary,
                             user, now)
            companies = _revert(Company, log.get("company_changes", {}), log.get("added_domains", {}), "domains",
                                summary, user, now)
            deleted_people = Person.objects.filter(pk__in=log.get("people", [])).delete()[1].get("crm.Person", 0)
            deleted_companies = Company.objects.filter(pk__in=log.get("companies", [])).delete()[1].get(
                "crm.Company", 0)
            List.objects.filter(pk__in=log.get("lists", []), entries=None).delete()
            batch.undone_at, batch.undone_by = now, user
            batch.save(update_fields=["undone_at", "undone_by"])
    finally:
        suppress_history.reset(token)
    return {"companies": deleted_companies, "people": deleted_people, "reverted": people + companies}
