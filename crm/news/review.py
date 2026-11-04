"""Weekly news review inside the CRM.

Flow: import a scan file -> match each company to a CRM company and to its Gmail
draft -> review (decide, note, edit drafts) -> preview the plan -> apply once.

Apply writes to the CRM only (Kellen's decision, 2026-10-13): contacts become
People, decisions become timeline events, research and new leads become Tasks.
Gmail draft edits are saved and declined drafts moved to Trash, as before.
"""
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path, PurePath

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from crm import drive, merge
from crm.models import Activity, Company, CompanyStatus, Mailbox, NewsItem, NewsScan, Note, Person, Priority, Task
from crm.names import clean_name

from . import drafts as gmail_drafts
from .scan_parser import parse_scan

SCAN_SUFFIXES = (".pdf", ".docx")
UPLOAD_PREFIX = "upload:"  # NewsScan.path for a scan uploaded on the News page: "upload:<random id>/<file name>"
MAX_UPLOAD = 20 * 1024 * 1024
DRIVE_PREFIX = "drive:"  # NewsScan.path for a scan read from Drive: "drive:<file id>"


# --------------------------------------------------------------------- files

def watch_folder():
    return Path(settings.NEWS_WATCH_FOLDER)


def drive_folder():
    """The Drive folder ID the scans land in on Cloud Run (a folder link works too), or "" to read the laptop's watch
    folder."""
    return drive.folder_id_from(getattr(settings, "NEWS_DRIVE_FOLDER", ""))


def folder_label():
    """Where the scans are read from, for the News page."""
    return f"Google Drive folder {drive_folder()}" if drive_folder() else watch_folder()


def _wanted(name, suffix):
    return suffix in SCAN_SUFFIXES and not name.startswith("~$") and "REVIEWED" not in name.upper()


def available_scans():
    """Scan files in the watch folder (or the Drive folder), newest first; a .pdf wins over a .docx of the same name."""
    if drive_folder():
        return _drive_scans()
    folder = watch_folder()
    if not folder.exists():
        return []
    by_stem = {}
    for f in folder.iterdir():
        if not _wanted(f.name, f.suffix.lower()):
            continue
        current = by_stem.get(f.stem)
        if current is None or (f.suffix.lower() == ".pdf" and current.suffix.lower() != ".pdf"):
            by_stem[f.stem] = f
    imported = {s.path: s for s in NewsScan.objects.all()}
    files = sorted(by_stem.values(), key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"path": str(f), "name": f.name, "modified": f.stat().st_mtime, "scan": imported.get(str(f))}
            for f in files]


def _drive_scans():
    """The same list from the Drive folder. A scan's path is "drive:<file id>", so it stays unique when two files
    share a name."""
    by_stem = {}
    for f in drive.list_files(drive_folder()):
        name = PurePath(f.name)
        if not _wanted(f.name, name.suffix.lower()):
            continue
        current = by_stem.get(name.stem)
        if current is None or (f.is_pdf and not current.is_pdf):
            by_stem[name.stem] = f
    imported = {s.path: s for s in NewsScan.objects.filter(path__startswith=DRIVE_PREFIX)}
    files = sorted(by_stem.values(), key=lambda f: f.modified.timestamp() if f.modified else 0, reverse=True)
    return [{"path": DRIVE_PREFIX + f.id, "name": f.name, "modified": f.modified.timestamp() if f.modified else 0,
             "scan": imported.get(DRIVE_PREFIX + f.id), "drive_file": f} for f in files]


def _download(file_id, name, mime, folder):
    """Save a Drive scan into `folder` under its own name (the parser goes by the extension) and return the path."""
    response, _ = drive.client().download(file_id, mime)
    target = Path(folder) / PurePath(name).name
    with open(target, "wb") as fh:
        for chunk in response.iter_content(64 * 1024):
            fh.write(chunk)
    return target


# --------------------------------------------------------------------- import

def _company_index():
    index = {}
    for pk, name in Company.objects.values_list("pk", "name"):
        index.setdefault(merge.core_name(name), pk)
    return index


def import_scan(path, user=None, drive_file=None):
    """Parse the file into a NewsScan with items, matched to CRM companies. Re-importing returns the existing scan.

    A Drive scan (`path` "drive:<file id>", `drive_file` its DriveFile) is downloaded to a temporary folder first."""
    path = str(path)
    existing = NewsScan.objects.filter(path=path).first()
    if existing:
        return existing
    if not path.startswith(DRIVE_PREFIX):
        return _import(path, Path(path), Path(path).name, user)
    with tempfile.TemporaryDirectory() as folder:
        local = _download(drive_file.id, drive_file.name, drive_file.mime, folder)
        return _import(path, local, drive_file.name, user)


def import_upload(uploaded, user=None):
    """Import a scan file uploaded on the News page (a .pdf or .docx), for when the Drive folder isn't reachable.

    The file is parsed from a temporary folder and not kept; every upload becomes its own scan."""
    name = PurePath(uploaded.name).name
    suffix = PurePath(name).suffix.lower()
    if suffix not in SCAN_SUFFIXES or name.startswith("~$"):
        raise ValueError("Upload a .pdf or .docx scan file.")
    if uploaded.size > MAX_UPLOAD:
        raise ValueError("That file is over 20 MB.")
    with tempfile.TemporaryDirectory() as folder:
        local = Path(folder) / name
        with open(local, "wb") as fh:
            for chunk in uploaded.chunks():
                fh.write(chunk)
        return _import(f"{UPLOAD_PREFIX}{uuid.uuid4().hex}/{name}", local, name, user)


@transaction.atomic
def _import(path, local, file_name, user):
    meta, entries, prospects = parse_scan(local)
    scan = NewsScan.objects.create(
        path=path, file_name=file_name, title=meta.get("title", ""), scan_date=meta.get("scan_date", ""),
        stats_line=meta.get("stats_line", ""), imported_by=user,
    )
    companies = _company_index()
    default_owner = user

    def match(name):
        return companies.get(merge.core_name(name))

    items = []
    for e in entries:
        items.append(NewsItem(
            scan=scan, kind=NewsItem.Kind.BOARD, position=int(e["index"]), section=e.get("section", ""),
            company_name=e["company"], company_id=match(e["company"]), headline=e.get("headline", ""),
            article_date=e.get("date", ""), source_url=(e.get("source_url") or "")[:1000],
            summary=e.get("summary", ""), outreach_action=e.get("outreach_action", ""), framing=e.get("framing", ""),
            contact_text=(e.get("contact") or "")[:500], needs_contact=bool(e.get("needs_contact")),
            has_draft=bool(e.get("has_draft")), owner=default_owner,
        ))
    for tier_key in ("strong_fit", "worth_watching"):
        for p in prospects.get(tier_key, []):
            items.append(NewsItem(
                scan=scan, kind=NewsItem.Kind.PROSPECT, position=1000 + int(p["index"]), tier=p.get("tier", ""),
                company_name=p["company"], company_id=match(p["company"]), headline=p.get("signal", ""),
                article_date=p.get("signal_date", ""), hq=p.get("hq", ""), what_they_do=p.get("what_they_do", ""),
                why_fits=p.get("why_fits", ""), source_name=p.get("source_name", ""), owner=default_owner,
            ))
    for i, w in enumerate(prospects.get("weak", [])):
        items.append(NewsItem(scan=scan, kind=NewsItem.Kind.WEAK, position=2000 + i, company_name=w["company"],
                              company_id=match(w["company"]), headline=w.get("reason", "")))
    NewsItem.objects.bulk_create(items)
    return scan


def mailbox_for(user):
    return Mailbox.objects.filter(user=user).first() or Mailbox.objects.filter(enabled=True, shared=False).first()


def check_drafts(scan, user):
    """Read Gmail drafts and attach the best match to each board item that the scan says has one."""
    mailbox = mailbox_for(user)
    if mailbox is None:
        scan.drafts_error = "No mailbox connected. Set one up on the Mail page."
        scan.save(update_fields=["drafts_error"])
        return
    try:
        with gmail_drafts.open_drafts(mailbox, user) as box:
            found = box.list()
    except (gmail_drafts.DraftError, OSError) as exc:
        scan.drafts_error = str(exc)
        scan.save(update_fields=["drafts_error"])
        return
    items = list(scan.items.filter(kind=NewsItem.Kind.BOARD, has_draft=True))
    matches = gmail_drafts.match_drafts(items, found)
    for item in items:
        draft, score = matches.get(item.pk, (None, 0))
        item.draft_uid = draft["uid"] if draft else ""
        item.draft_score = score
        item.draft_to = draft["to"] if draft else ""
        item.draft_cc = draft["cc"] if draft else ""
        item.draft_subject = draft["subject"][:500] if draft else ""
        item.draft_html = draft["html"] if draft else ""
        item.draft_snippet = draft["snippet"] if draft else ""
        item.draft_too_large = draft["too_large"] if draft else False
        item.save()
    scan.drafts_checked_at = timezone.now()
    scan.drafts_error = ""
    scan.save(update_fields=["drafts_checked_at", "drafts_error"])


# --------------------------------------------------------------------- plan

def own_address(user):
    mailbox = mailbox_for(user)
    return mailbox.email.lower() if mailbox else ""


def item_recipients(item, user):
    return gmail_drafts.recipients(item.to, item.draft_cc, exclude=[own_address(user)])


def research_needed(item, user):
    """No contact yet: the scan says so, or the draft is addressed only to ourselves (the placeholder pattern)."""
    if item.needs_contact:
        return True
    return bool(item.has_draft and item.draft_uid and not item_recipients(item, user))


def build_plan(scan, user):
    """What Apply will do, item by item, without doing it."""
    existing_emails = set(Person.objects.exclude(email=None).values_list("email", flat=True))
    plan = {"proceed": [], "decline": [], "noted": [], "pursue": [], "pass": [], "undecided": [],
            "contacts": [], "tasks": [], "trash": [], "draft_saves": [], "new_companies": [], "moves": [],
            "draft_tasks": []}
    mailbox = mailbox_for(user)
    mailbox_user_id = mailbox.user_id if mailbox else None
    for item in scan.items.exclude(kind=NewsItem.Kind.WEAK).select_related("company", "owner"):
        if item.kind == NewsItem.Kind.PROSPECT:
            if item.decision == NewsItem.Decision.PURSUE:
                plan["pursue"].append(item)
                plan["tasks"].append((item, f"Research new lead: {item.company_name}"))
                if not item.company_id:
                    plan["new_companies"].append(item.company_name)
            elif item.decision == NewsItem.Decision.PASS:
                plan["pass"].append(item)
            else:
                plan["undecided"].append(item)
            continue
        if item.decision == NewsItem.Decision.PROCEED:
            plan["proceed"].append(item)
            if item.draft_uid:
                plan["draft_tasks"].append(item)
                if item.owner_id and item.owner_id != mailbox_user_id:
                    plan["moves"].append(item)  # the draft goes to their Gmail instead of being edited here
                elif item.edited:
                    plan["draft_saves"].append(item)
            if not item.company_id:
                plan["new_companies"].append(item.company_name)
            people = item_recipients(item, user)
            for person in people:
                person["new"] = person["email"] not in existing_emails
                plan["contacts"].append((item, person))
            if research_needed(item, user) and not people:
                plan["tasks"].append((item, f"Find contact at {item.company_name}"))
        elif item.decision == NewsItem.Decision.DECLINE:
            plan["decline"].append(item)
            if item.draft_uid:
                plan["trash"].append(item)
        elif item.note or item.edited:
            plan["noted"].append(item)
            if item.edited and item.draft_uid:
                plan["draft_saves"].append(item)
        else:
            plan["undecided"].append(item)
    return plan


# --------------------------------------------------------------------- apply

def _company_for(item, user, prospect=False):
    if item.company_id:
        return item.company
    company = Company.objects.filter(name__iexact=item.company_name).first()
    if company is None:
        company = Company.objects.create(
            name=item.company_name, owner=item.owner or user, priority=Priority.MID, created_by=user,
            status=CompanyStatus.PROSPECT, hq_location=item.hq[:200] if prospect else "",
            description=item.what_they_do if prospect else "",
        )
        Activity.objects.create(
            kind=Activity.Kind.RECORD_CREATED, occurred_at=timezone.now(), actor=user, company=company,
            summary=f"Company created from news scan {item.scan}", is_background=True, source=Activity.Source.NEWS_SCAN,
        )
    item.company = company
    return company


def _log(item, company, outcome, user, body=""):
    Activity.objects.update_or_create(
        source=Activity.Source.NEWS_SCAN, source_id=f"newsitem:{item.pk}",
        defaults={
            "occurred_at": timezone.now(), "kind": Activity.Kind.NEWS_TRIGGER, "actor": user, "company": company,
            "summary": (item.headline or f"News: {item.company_name}")[:500], "url": item.source_url,
            "outcome": outcome, "body": body, "context": Activity.Context.PROSPECTING,
        },
    )


def _body(item):
    lines = [f"Scan: {item.scan}", f"Pipeline stage: {item.section}" if item.section else "",
             f"Article date: {item.article_date}" if item.article_date else "", item.summary,
             f"Direction: {item.note}" if item.note else ""]
    if item.kind == NewsItem.Kind.PROSPECT:
        lines += [f"HQ: {item.hq}", f"What they do: {item.what_they_do}", f"Why it fits: {item.why_fits}",
                  f"Source: {item.source_name}"]
    return "\n".join(line for line in lines if line)


def _hand_draft_over(item, source_box):
    """Recreate the item's draft (with any edits) as an unsent draft in its owner's Gmail, then trash the original.
    Returns the link to the new draft. Nothing is sent."""
    owner_mailbox = Mailbox.objects.filter(user=item.owner).first()
    if owner_mailbox is None:
        raise gmail_drafts.DraftError(f"{item.owner} has no mailbox connected")
    to = item.edit_to if item.edited else item.draft_to
    subject = item.edit_subject if item.edited else item.draft_subject
    html = item.edit_html if item.edited else item.draft_html
    with gmail_drafts.open_drafts(owner_mailbox, item.owner) as owner_box:
        new_id = owner_box.create(to, subject, html, cc=item.draft_cc)
        link = owner_box.compose_url(new_id)
    source_box.trash([item.draft_uid])
    item.result["draft"] = f"moved to {item.owner}'s Gmail drafts"
    return link


def apply(scan, user):
    """Carry out every decision once. Gmail steps run first; CRM writes in one transaction."""
    with transaction.atomic():
        scan = NewsScan.objects.select_for_update().get(pk=scan.pk)
        if scan.status == NewsScan.Status.APPLIED:
            return scan.results
        plan = build_plan(scan, user)
        scan.status = NewsScan.Status.APPLIED  # claim it so a second click cannot apply twice
        scan.applied_at, scan.applied_by = timezone.now(), user
        scan.save(update_fields=["status", "applied_at", "applied_by"])

    results = {"drafts_saved": 0, "drafts_trashed": 0, "gmail_errors": [], "contacts_added": 0,
               "contacts_known": 0, "tasks": 0, "drafts_moved": 0, "companies_created": 0, "logged": 0}
    # Gmail first: if it fails, the CRM records still go in and the error is reported.
    mailbox = mailbox_for(user)
    draft_links = {}  # item.pk -> where to open its draft in Gmail
    if mailbox and (plan["draft_saves"] or plan["trash"] or plan["draft_tasks"]):
        try:
            with gmail_drafts.open_drafts(mailbox, user) as box:
                for item in plan["draft_saves"]:
                    try:
                        new_uid = box.update(item.draft_uid, item.edit_to, item.edit_subject, item.edit_html,
                                             cc=item.draft_cc)
                        item.draft_uid = new_uid or item.draft_uid
                        item.result["draft"] = f"saved edits (new draft {new_uid})"
                        results["drafts_saved"] += 1
                    except Exception as exc:  # report and keep going
                        item.result["draft"] = f"edit FAILED: {exc}"
                        results["gmail_errors"].append(f"{item.company_name}: {exc}")
                for item in plan["moves"]:
                    try:
                        draft_links[item.pk] = _hand_draft_over(item, box)
                        results["drafts_moved"] += 1
                    except Exception as exc:  # the draft stays in this mailbox and the task says so
                        item.result["draft"] = f"not handed to {item.owner}: {exc}"
                        results["gmail_errors"].append(f"{item.company_name}: {exc}")
                if plan["trash"]:
                    outcome = box.trash([i.draft_uid for i in plan["trash"]])
                    for item in plan["trash"]:
                        item.result["draft"] = outcome.get(item.draft_uid, "not trashed")
                        results["drafts_trashed"] += outcome.get(item.draft_uid) == "trashed"
                for item in plan["draft_tasks"]:
                    if item.pk not in draft_links:
                        try:
                            draft_links[item.pk] = box.compose_url(item.draft_uid)
                        except Exception:  # a link is a convenience; the task still says where the draft is
                            pass
        except (gmail_drafts.DraftError, OSError) as exc:
            results["gmail_errors"].append(str(exc))

    before = Company.objects.count()
    with transaction.atomic():
        for item in plan["proceed"]:
            company = _company_for(item, user)
            added = 0
            for _, person in [p for p in plan["contacts"] if p[0] is item]:
                existing = Person.objects.filter(email=person["email"]).first()
                if existing:
                    results["contacts_known"] += 1
                    continue
                parts = clean_name(person["name"], person["email"]) or ("", "", "")
                Person.objects.create(
                    email=person["email"], first_name=parts[0], last_name=parts[1], full_name=parts[2],
                    company=company, owner=item.owner or user, priority=Priority.MID,
                    notes=f"Added from {item.scan} - {item.headline}"[:480],
                    custom_fields={"source": "news review", "scan": str(item.scan)},
                )
                added += 1
            results["contacts_added"] += added
            item.result["contacts_added"] = added
            _log(item, company, "Approved", user, _body(item))
            results["logged"] += 1
        for item in plan["decline"]:
            if item.company_id:
                _log(item, item.company, "Rejected", user, _body(item))
                results["logged"] += 1
        for item in plan["noted"]:
            if item.company_id:
                _log(item, item.company, "Noted", user, _body(item))
                results["logged"] += 1
        for item in plan["pursue"]:
            company = _company_for(item, user, prospect=True)
            _log(item, company, "Pursued", user, _body(item))
            results["logged"] += 1
        for item in plan["pass"]:
            if item.company_id:
                _log(item, item.company, "Passed", user, _body(item))
                results["logged"] += 1
        for item, title in plan["tasks"]:
            company = _company_for(item, user, prospect=item.kind == NewsItem.Kind.PROSPECT)
            Task.objects.create(
                title=title, company=company, assignee=item.owner or user, created_by=user,
                due_date=timezone.localdate() + timedelta(days=3), notes=_body(item),
            )
            item.result["task"] = title
            results["tasks"] += 1
        for item in plan["draft_tasks"]:
            company = _company_for(item, user)
            owner = item.owner or user
            link = draft_links.get(item.pk, "")
            where = f"Draft in Gmail: {link}" if link else "The draft is in Gmail Drafts."
            if item.result.get("draft", "").startswith("not handed"):
                where += f"\nIt is still in {mailbox.email}'s Drafts: {item.result['draft']}"
            Task.objects.create(
                title=f"Review and send draft: {item.company_name}", company=company, assignee=owner,
                created_by=user, due_date=timezone.localdate() + timedelta(days=3),
                notes=f"{where}\nNothing has been sent.\n\n{_body(item)}",
            )
            if link:
                item.result["draft_link"] = link
            item.result["draft_task"] = str(owner)
            results["tasks"] += 1
        for item in plan["proceed"] + plan["noted"] + plan["decline"] + plan["pursue"]:
            if item.note and item.company_id:
                Note.objects.create(company=item.company, author=user,
                                    body=f"News review ({item.scan}): {item.note}")
        for item in {i.pk: i for i in plan["proceed"] + plan["decline"] + plan["noted"] + plan["pursue"]
                     + plan["pass"]}.values():
            item.save(update_fields=["company", "result"])
        results["companies_created"] = Company.objects.count() - before
        scan.results = results
        scan.save(update_fields=["results"])
    return results


# --------------------------------------------------------------------- context for cards

def history_for(company_ids, exclude_scan):
    """Earlier news decisions per company: {company_id: [NewsItem, ...]} newest first."""
    out = {}
    for item in (NewsItem.objects.filter(company_id__in=company_ids).exclude(scan=exclude_scan)
                 .exclude(decision="").select_related("scan").order_by("-scan__imported_at")):
        out.setdefault(item.company_id, []).append(item)
    return out
