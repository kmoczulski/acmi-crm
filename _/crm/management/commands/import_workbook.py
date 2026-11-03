"""Import ACMI_Outreach_Engine.xlsx into the CRM.

    python manage.py import_workbook                 # dry run: reports, writes nothing
    python manage.py import_workbook --commit        # writes
    python manage.py import_workbook --commit --overwrite   # workbook wins over app edits

Safe to re-run. Records are matched on email (people), normalized name
(companies), template id, and a stable source_id (activities), so a second run
only adds what is new. Without --overwrite, existing non-blank fields are left
alone so edits made in the app survive.
"""
import hashlib
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time

import openpyxl
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from crm import interactions
from crm.models import (
    Activity, Campus, Company, CompanyStatus, EmailTemplate, NewsDraft, Person, Priority, normalize_key,
    suppress_history,
)

DEFAULT_PATH = r"C:\Users\Kemoc\Desktop\Prospecting\ACMI_Outreach_Engine.xlsx"

# Workbook owner names -> user accounts. Emails link Google sign-in to the same user.
OWNERS = {
    "Kellen": {"username": "kellen", "email": "kmoczulski@acmigroup.com", "last_name": "Moczulski"},
    "Simon": {"username": "simon"},
    "Dave": {"username": "dave"},
    "Dowell": {"username": "dowell"},
    "Joe": {"username": "joe"},
}

CAMPUSES = [
    {"code": "NSIH", "name": "National Security Industrial Hub", "location": "Adjacent to NSWC Crane", "state": "IN",
     "acreage": 1100, "status": "Under construction"},
    {"code": "MEIH", "name": "Maryland Energetics Innovation Hub", "location": "Near NSWC Indian Head", "state": "MD",
     "status": "Announced"},
]

FREE_MAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "me.com", "msn.com",
    "live.com", "comcast.net", "att.net", "verizon.net", "protonmail.com", "proton.me",
}

# Outreach_Log "Response" -> (activity kind, counts as a response?)
RESPONSE_KINDS = {
    "Responded": Activity.Kind.REPLY,
    "Not Interested": Activity.Kind.REPLY,
    "Meeting Scheduled": Activity.Kind.REPLY,
    "Referred": Activity.Kind.REPLY,
    "Follow Up": Activity.Kind.REPLY,
    "Undeliverable": Activity.Kind.BOUNCE,
    "Out of Office Reply": Activity.Kind.AUTO_REPLY,
    "Automated Reply": Activity.Kind.AUTO_REPLY,
}

PRIORITY_RANK = {Priority.HIGH: 3, Priority.MID: 2, Priority.LOW: 1, Priority.TEST: 0, "": -1}
PLACEHOLDERS = {"tbd", "n/a", "na", "none", "-", "`"}


class DryRun(Exception):
    pass


def clean(value):
    if value is None:
        return ""
    s = str(value).replace("\xa0", " ").strip()
    return "" if s.lower() in PLACEHOLDERS else s


def to_datetime(value):
    """Workbook dates are naive and date-only; store them as local midnight."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time())
    else:
        s = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def stable_id(*parts):
    raw = "|".join(str(p or "").strip().lower() for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def email_domain(email):
    if not email or "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[1].lower()
    return "" if domain in FREE_MAIL else domain


def valid_email(value):
    s = clean(value).lower()
    return s if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s) else ""


def rows(ws):
    it = ws.iter_rows(values_only=True)
    header = [clean(h) for h in next(it)]
    for r in it:
        if r and any(c not in (None, "") for c in r):
            yield dict(zip(header, r))


class Command(BaseCommand):
    help = "Import the outreach workbook (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("--path", default=DEFAULT_PATH)
        parser.add_argument("--commit", action="store_true", help="Write changes (default is a dry run)")
        parser.add_argument("--overwrite", action="store_true", help="Workbook values replace non-blank app values")

    def handle(self, *args, **opts):
        try:
            wb = openpyxl.load_workbook(opts["path"], read_only=True, data_only=True)
        except FileNotFoundError:
            raise CommandError(f"Workbook not found: {opts['path']}")
        self.overwrite = opts["overwrite"]
        self.stats = Counter()
        token = suppress_history.set(True)
        try:
            with transaction.atomic():
                self.import_all(wb)
                if not opts["commit"]:
                    raise DryRun()
        except DryRun:
            self.stdout.write(self.style.WARNING("DRY RUN: nothing was written. Re-run with --commit to save."))
        finally:
            suppress_history.reset(token)
        for key in sorted(self.stats):
            self.stdout.write(f"  {key:<40} {self.stats[key]:>7}")
        if opts["commit"]:
            self.stdout.write(self.style.SUCCESS("Import committed."))

    # -- helpers ------------------------------------------------------------

    def set_field(self, obj, field, value):
        """Fill blanks; replace only with --overwrite. Returns True if changed."""
        if value in (None, "", [], {}):
            return False
        current = getattr(obj, field)
        if current in (None, "", [], {}) or (self.overwrite and current != value):
            setattr(obj, field, value)
            return True
        return False

    def company_for(self, name, email=""):
        name = clean(name)
        if not name:
            domain = email_domain(email)
            if not domain:
                return None
            name = domain.split(".")[0].capitalize()
        key = normalize_key(name)
        company = self.companies.get(key)
        if company is None:
            company = Company(name=name)
            company.save()
            self.companies[key] = company
            self.stats["companies created"] += 1
        domain = email_domain(email)
        if domain and domain not in company.domains:
            company.domains = [*company.domains, domain]
            self.dirty_companies.add(company.pk)
        return company

    def person_for(self, email, name="", company=None):
        email = valid_email(email)
        # Without an email, a person is identified by name within their company.
        key = email or ("noemail", normalize_key(clean(name)), company.pk if company else None)
        person = self.people.get(key)
        if person is None:
            first, _, last = clean(name).partition(" ")
            person = Person(email=email or None, first_name=first, last_name=last, company=company)
            person.save()
            self.people[key] = person
            self.stats["people created"] += 1
        return person

    # -- steps --------------------------------------------------------------

    def import_all(self, wb):
        self.import_users()
        self.import_campuses()
        self.companies = {c.name_key: c for c in Company.objects.all()}
        self.people = {
            p.email or ("noemail", normalize_key(p.full_name), p.company_id): p for p in Person.objects.all()
        }
        self.dirty_companies = set()
        self.existing_sources = set(
            Activity.objects.filter(source=Activity.Source.IMPORT).values_list("source_id", flat=True)
        )
        self.new_activities = []

        self.import_contacts(wb["Master_Contacts"])
        self.import_outreach_log(wb["Outreach_Log"])
        self.import_templates(wb["Email_Templates"])
        self.import_news_drafts(wb["News_Outreach_Queue"])
        self.import_news_reviews(wb["News_Review_Log"])

        for company in self.companies.values():
            if company.pk in self.dirty_companies:
                company.save()
        Activity.objects.bulk_create(self.new_activities, batch_size=1000)
        self.stats["activities created"] += len(self.new_activities)
        self.derive_company_status()
        self.stats["records with interaction fields updated"] = interactions.recompute()

    def import_users(self):
        User = get_user_model()
        self.users = {}
        for label, info in OWNERS.items():
            user, created = User.objects.get_or_create(
                username=info["username"],
                defaults={"first_name": label, "last_name": info.get("last_name", ""), "email": info.get("email", "")},
            )
            if created:
                user.set_unusable_password()
                user.save()
                self.stats["users created"] += 1
            self.users[label.lower()] = user

    def import_campuses(self):
        for data in CAMPUSES:
            _, created = Campus.objects.get_or_create(code=data["code"], defaults=data)
            self.stats["campuses created"] += int(created)

    def import_contacts(self, ws):
        company_flags = defaultdict(list)
        company_priority = {}
        for row in rows(ws):
            self.stats["Master_Contacts rows"] += 1
            email = valid_email(row.get("Email"))
            company = self.company_for(row.get("Company"), email)
            if not email and not clean(row.get("Contact Name")):
                self.stats["Master_Contacts rows skipped (no name or email)"] += 1
                continue
            if company is None and not email:
                self.stats["Master_Contacts rows skipped (no company or email)"] += 1
                continue
            person = self.person_for(email, row.get("Contact Name"), company)
            owner = self.users.get(clean(row.get("Owner")).lower())
            priority = clean(row.get("Priority"))
            priority = priority if priority in Priority.values else ""
            flag = clean(row.get("Flag"))
            name = clean(row.get("Contact Name"))
            first, _, last = name.partition(" ")

            changed = any([
                self.set_field(person, "first_name", first),
                self.set_field(person, "last_name", last),
                self.set_field(person, "full_name", name),
                self.set_field(person, "title", clean(row.get("Title"))),
                self.set_field(person, "company", company),
                self.set_field(person, "priority", priority),
                self.set_field(person, "owner", owner),
                self.set_field(person, "notes", clean(row.get("Notes"))),
            ])
            if flag and flag not in person.flags:
                person.flags = [*person.flags, flag]
                changed = True
            added = to_datetime(row.get("Date Added"))
            if added and "date_added" not in person.custom_fields:
                person.custom_fields = {**person.custom_fields, "date_added": added.date().isoformat()}
                changed = True
            if changed:
                person.save()
                self.stats["people updated"] += 1

            if company:
                company_flags[company.pk].append(flag)
                if PRIORITY_RANK.get(priority, -1) > PRIORITY_RANK.get(company_priority.get(company.pk, ""), -1):
                    company_priority[company.pk] = priority
                if self.set_field(company, "sector", clean(row.get("Sector"))):
                    self.dirty_companies.add(company.pk)
                if self.set_field(company, "owner", owner):
                    self.dirty_companies.add(company.pk)

        # Company priority = highest of its contacts. Company flags = flags every contact shares.
        for company in self.companies.values():
            if company.pk in company_priority and self.set_field(company, "priority", company_priority[company.pk]):
                self.dirty_companies.add(company.pk)
            flags = company_flags.get(company.pk)
            if flags and all(flags) and len(set(flags)) == 1 and flags[0] not in company.flags:
                company.flags = [*company.flags, flags[0]]
                self.dirty_companies.add(company.pk)

    def add_activity(self, source_id, **fields):
        if source_id in self.existing_sources:
            self.stats["activities already imported"] += 1
            return
        self.existing_sources.add(source_id)
        self.new_activities.append(Activity(source=Activity.Source.IMPORT, source_id=source_id, **fields))

    def import_outreach_log(self, ws):
        occurrences = Counter()
        for row in rows(ws):
            self.stats["Outreach_Log rows"] += 1
            email = valid_email(row.get("Email"))
            sent = to_datetime(row.get("Date Sent"))
            if not email or not sent:
                self.stats["Outreach_Log rows skipped (no email or date)"] += 1
                continue
            person = self.people.get(email)
            if person is None:
                company = self.company_for(row.get("Company"), email)
                person = self.person_for(email, row.get("Name"), company)
                self.stats["people created from Outreach_Log"] += 1
            campaign = clean(row.get("Campaign"))
            response = clean(row.get("Response"))
            base = row.get("Message ID") or stable_id(email, sent.date(), campaign)
            occurrences[base] += 1
            if occurrences[base] > 1:
                base = f"{base}#{occurrences[base]}"

            self.add_activity(
                f"log:{base}",
                kind=Activity.Kind.EMAIL_SENT, occurred_at=sent, person=person, company_id=person.company_id,
                summary=f"Outreach email ({campaign or 'no campaign'})", campaign=campaign,
                outcome=response or "No Reply", is_background=False,
            )
            kind = RESPONSE_KINDS.get(response)
            if kind:
                self.add_activity(
                    f"logresp:{base}",
                    kind=kind, occurred_at=to_datetime(row.get("Response Date")) or sent, person=person,
                    company_id=person.company_id, summary=f"{response} ({campaign or 'outreach'})",
                    campaign=campaign, outcome=response, is_background=kind == Activity.Kind.AUTO_REPLY,
                )

    def import_templates(self, ws):
        for row in rows(ws):
            tid = clean(row.get("Template ID"))
            if not tid:
                continue
            active_raw = clean(row.get("Active")).lower()
            priority = clean(row.get("Priority"))
            _, created = EmailTemplate.objects.update_or_create(
                template_id=tid,
                defaults={
                    "name": clean(row.get("Template Name")),
                    "priority": priority if priority in Priority.values else "",
                    "sector_match": clean(row.get("Sector Match")),
                    "subject": clean(row.get("Subject Line")),
                    "body_html": row.get("Body HTML") or "",
                    "body_plain": row.get("Body Plain") or "",
                    "active": active_raw not in ("no", "false", "n", "0"),
                },
            )
            self.stats["templates created" if created else "templates updated"] += 1

    def import_news_drafts(self, ws):
        for row in rows(ws):
            name = clean(row.get("Company"))
            if not name:
                continue
            company = self.company_for(name)
            link = clean(row.get("Reference Link"))
            subject = clean(row.get("Subject Line"))
            key = stable_id(name, link, subject)
            article_date = to_datetime(row.get("Article Date"))
            _, created = NewsDraft.objects.update_or_create(
                source_key=key,
                defaults={
                    "company": company, "company_name": name, "subject": subject,
                    "news_hook": clean(row.get("News Hook")), "reference_link": link[:1000],
                    "body_html": row.get("HTML Body") or "",
                    "active": clean(row.get("Active")).lower() in ("yes", "true", "y", "1"),
                    "article_date": article_date.date() if article_date else None,
                    "contact": clean(row.get("Contact")),
                },
            )
            self.stats["news drafts created" if created else "news drafts updated"] += 1
            if article_date:
                self.add_activity(
                    f"newsdraft:{key}",
                    kind=Activity.Kind.NEWS_TRIGGER, occurred_at=article_date, company=company,
                    summary=subject or f"News: {name}", body=clean(row.get("News Hook")), url=link[:1000],
                    outcome="Drafted",
                )

    def import_news_reviews(self, ws):
        for row in rows(ws):
            name = clean(row.get("Company"))
            headline = clean(row.get("Headline"))
            if not name or not headline:
                continue
            company = self.company_for(name)
            when = to_datetime(row.get("Article Date")) or to_datetime(row.get("Reviewed On"))
            if not when:
                continue
            notes = [
                f"{label}: {clean(row.get(col))}"
                for label, col in [
                    ("Section", "Section"), ("Scan", "Scan File"), ("Contact", "Contact"),
                    ("Contact email", "Contact Email"), ("Direction", "Notes / Direction"),
                    ("Draft", "Draft Action"), ("Trello", "Trello Card"), ("Outcome", "Outcome"),
                ]
                if clean(row.get(col))
            ]
            self.add_activity(
                f"newsreview:{stable_id(row.get('Scan File'), name, headline)}",
                kind=Activity.Kind.NEWS_TRIGGER, occurred_at=when, company=company, summary=headline[:500],
                body="\n".join(notes), url=clean(row.get("Source Link"))[:1000],
                outcome=clean(row.get("Decision")),
            )
            self.stats["news reviews read"] += 1

    def derive_company_status(self):
        """Advance companies still at an early stage based on their outreach history."""
        early = [CompanyStatus.PROSPECT, CompanyStatus.CONTACTED]
        sent = Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT).values("company_id")
        replied = Activity.objects.filter(outcome__in=["Responded", "Meeting Scheduled", "Referred"]).values("company_id")
        declined = Activity.objects.filter(outcome="Not Interested").values("company_id")
        self.stats["companies set to contacted"] = Company.objects.filter(
            status=CompanyStatus.PROSPECT, pk__in=sent
        ).update(status=CompanyStatus.CONTACTED)
        self.stats["companies set to engaged"] = Company.objects.filter(status__in=early, pk__in=replied).update(
            status=CompanyStatus.ENGAGED
        )
        self.stats["companies set to not interested"] = (
            Company.objects.filter(status__in=early, pk__in=declined)
            .exclude(pk__in=replied)
            .update(status=CompanyStatus.NOT_INTERESTED)
        )
