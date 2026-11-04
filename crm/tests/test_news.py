import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from crm.models import Activity, Company, Mailbox, NewsItem, NewsScan, Note, Person, Task
from crm.news import review

ME = "kmoczulski@acmigroup.com"

SCAN_TEXT = [
    "Weekly Prospect News Scan",
    "October 14, 2026",
    "This week's scan covered 3 companies.",
    "Active Discussions",
    "Firehawk Aerospace",
    "Headline: Firehawk opens Texas propellant plant",
    "Date: 2026-10-12",
    "Source: https://example.com/firehawk",
    "Synopsis: New 100k sf plant.",
    "Outreach action: Outreach draft created in Gmail - follow-up to Ben Allen",
    "Framing: Congratulate, ask about east coast capacity.",
    "Trello card: Updated",
    "Valinor",
    "Headline: Valinor raises $85M",
    "Date: 2026-10-10",
    "Source: https://example.com/valinor",
    "Synopsis: Defense holding company raise.",
    "Outreach action: Outreach draft created in Gmail - needs contact lookup",
    "Framing: Intro.",
    "Trello card: Updated",
    "Cold",
    "Old Co",
    "Headline: Old Co restructures",
    "Date: 2026-10-01",
    "Source: https://example.com/oldco",
    "Synopsis: Layoffs.",
    "Outreach action: Outreach draft created in Gmail - cold intro to Pat Smith",
    "Framing: Skip.",
    "Trello card: Updated",
]


def make_docx(folder, name="Weekly Prospect News Scan - October 14, 2026.docx"):
    import docx

    d = docx.Document()
    for line in SCAN_TEXT:
        d.add_paragraph(line)
    path = Path(folder) / name
    d.save(path)
    return path


DRAFTS = [
    {"uid": "11", "subject": "ACMI x Firehawk Aerospace: Texas plant", "to": "Ben Allen <ben@firehawkaerospace.com>",
     "cc": "", "html": "<p>Hi Ben</p>", "snippet": "Hi Ben", "too_large": False},
    {"uid": "12", "subject": "ACMI x Valinor: congrats", "to": ME, "cc": "",
     "html": "<p>No verified contact found for Valinor</p>", "snippet": "No verified contact found for Valinor",
     "too_large": False},
    {"uid": "13", "subject": "ACMI x Old Co: hello", "to": "Pat Smith <pat@oldco.com>", "cc": "",
     "html": "<p>Hi Pat</p>", "snippet": "Hi Pat", "too_large": False},
]


class FakeDrafts:
    """Stands in for crm.news.drafts.Drafts."""

    calls = []

    def __init__(self, mailbox):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list(self):
        return [dict(d) for d in DRAFTS]

    def update(self, uid, to, subject, html, cc=""):
        FakeDrafts.calls.append(("update", uid, to, subject))
        return "99"

    def trash(self, uids):
        FakeDrafts.calls.append(("trash", tuple(uids)))
        return {u: "trashed" for u in uids}

    def create(self, to, subject, html, cc=""):
        FakeDrafts.calls.append(("create", to, subject))
        return "77"

    def compose_url(self, uid):
        return f"https://mail.google.com/mail/u/x/#drafts?compose=m{uid}"


class NewsReviewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="kellen", email=ME)
        Mailbox.objects.create(user=self.user, email=ME)
        self.client.force_login(self.user)
        self.firehawk = Company.objects.create(name="Firehawk Aerospace, Inc.")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = make_docx(self.tmp.name)
        FakeDrafts.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def open_scan(self):
        with override_settings(NEWS_WATCH_FOLDER=self.tmp.name), patch("crm.news.drafts.Drafts", FakeDrafts):
            self.assertContains(self.client.get("/news/"), "October 14, 2026")
            r = self.client.post("/news/import/", {"path": str(self.path)})
        scan = NewsScan.objects.get()
        self.assertRedirects(r, scan.get_absolute_url())
        return scan

    def item(self, scan, name):
        return scan.items.get(company_name=name)

    def test_import_parses_and_matches(self):
        scan = self.open_scan()
        self.assertEqual(scan.scan_date, "October 14, 2026")
        self.assertEqual(scan.items.filter(kind="board").count(), 3)
        fh = self.item(scan, "Firehawk Aerospace")
        self.assertEqual(fh.company, self.firehawk)  # matched despite ", Inc."
        self.assertEqual((fh.section, fh.draft_uid), ("Active Discussions", "11"))
        self.assertEqual(self.item(scan, "Valinor").draft_uid, "12")
        page = self.client.get(scan.get_absolute_url())
        self.assertContains(page, "Firehawk opens Texas propellant plant")
        self.assertContains(page, "No contact identified")  # Valinor's draft is addressed only to us

    def test_upload_a_scan_opens_it_for_review(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with open(self.path, "rb") as fh:
            data = fh.read()
        with patch("crm.news.drafts.Drafts", FakeDrafts):
            r = self.client.post("/news/upload/", {"scan": SimpleUploadedFile(self.path.name, data)})
        scan = NewsScan.objects.get()
        self.assertRedirects(r, scan.get_absolute_url())
        self.assertTrue(scan.path.startswith("upload:"))
        self.assertEqual(scan.items.count(), 3)
        self.assertEqual(self.item(scan, "Firehawk Aerospace").company, self.firehawk)

    def test_import_trims_values_longer_than_their_fields(self):
        long = "x" * 400
        meta = {"title": "Scan", "scan_date": long, "stats_line": long}
        entries = [{"index": 1, "company": "Firehawk Aerospace", "date": long, "section": long, "headline": "h"}]
        prospects = {"strong_fit": [{"index": 1, "company": "NewCo", "tier": long, "hq": long, "source_name": long}]}
        with patch("crm.news.review.parse_scan", return_value=(meta, entries, prospects)):
            scan = review.import_scan(str(self.path), user=self.user)
        self.assertEqual(len(scan.scan_date), 60)
        self.assertEqual(len(scan.items.get(company_name="Firehawk Aerospace").article_date), 60)
        self.assertEqual(scan.items.count(), 2)

    def test_upload_rejects_other_file_types_and_no_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        r = self.client.post("/news/upload/", {"scan": SimpleUploadedFile("scan.txt", b"hello")}, follow=True)
        self.assertContains(r, "Upload a .pdf or .docx scan file.")
        r = self.client.post("/news/upload/", {}, follow=True)
        self.assertContains(r, "Choose a scan file")
        self.assertEqual(NewsScan.objects.count(), 0)

    def test_review_and_apply(self):
        scan = self.open_scan()
        fh, val, old = (self.item(scan, n) for n in ("Firehawk Aerospace", "Valinor", "Old Co"))
        self.client.post(f"/news/items/{fh.pk}/", {"decision": "proceed", "note": "Push for a site tour",
                                                   "edit_subject": "ACMI x Firehawk: Texas plant + Indiana"})
        self.client.post(f"/news/items/{val.pk}/", {"decision": "proceed"})
        self.client.post(f"/news/items/{old.pk}/", {"decision": "decline"})
        plan = self.client.get(f"/news/{scan.pk}/plan/")
        self.assertContains(plan, "Find contact at Valinor")
        self.assertContains(plan, "ben@firehawkaerospace.com")

        with patch("crm.news.drafts.Drafts", FakeDrafts):
            self.client.post(f"/news/{scan.pk}/plan/")
            self.client.post(f"/news/{scan.pk}/plan/")  # a second click must not apply again
        scan.refresh_from_db()
        self.assertEqual(scan.status, "applied")
        self.assertIn(("update", "11", "Ben Allen <ben@firehawkaerospace.com>", "ACMI x Firehawk: Texas plant + Indiana"),
                      FakeDrafts.calls)
        self.assertIn(("trash", ("13",)), FakeDrafts.calls)
        self.assertEqual(sum(c[0] == "trash" for c in FakeDrafts.calls), 1)

        ben = Person.objects.get(email="ben@firehawkaerospace.com")
        self.assertEqual((ben.full_name, ben.company), ("Ben Allen", self.firehawk))
        valinor = Company.objects.get(name="Valinor")  # created for the research task
        self.assertTrue(Task.objects.filter(title="Find contact at Valinor", company=valinor, assignee=self.user).exists())
        self.assertFalse(Company.objects.filter(name="Old Co").exists())  # declined, not in CRM: nothing created
        log = Activity.objects.get(source="news_scan", company=self.firehawk, kind="news_trigger")
        self.assertEqual((log.outcome, log.context), ("Approved", "prospecting"))
        self.assertTrue(Note.objects.filter(company=self.firehawk, body__contains="Push for a site tour").exists())
        self.assertEqual(scan.results["contacts_added"], 1)
        # Locked after apply.
        r = self.client.post(f"/news/items/{fh.pk}/", {"decision": "decline"})
        self.assertEqual(r.status_code, 409)

    def test_proceed_makes_a_review_task_and_hands_the_draft_to_the_owner(self):
        scan = self.open_scan()
        dave = get_user_model().objects.create(username="dave", email="dave@acmigroup.com")
        Mailbox.objects.create(user=dave, email=dave.email)
        fh, old = self.item(scan, "Firehawk Aerospace"), self.item(scan, "Old Co")
        self.client.post(f"/news/items/{fh.pk}/", {"decision": "proceed", "owner": str(dave.pk),
                                                   "edit_subject": "ACMI x Firehawk: edited"})
        self.client.post(f"/news/items/{old.pk}/", {"decision": "proceed"})
        plan = self.client.get(f"/news/{scan.pk}/plan/")
        self.assertContains(plan, "Nothing is sent.")
        self.assertEqual(Task.objects.count(), 0)  # reviewing the plan changes nothing
        with patch("crm.news.drafts.open_drafts", lambda mailbox, user: FakeDrafts(mailbox)):
            review.apply(scan, self.user)
        # Dave's copy carries the edit; the original leaves Kellen's drafts; the edit isn't saved in place.
        self.assertIn(("create", "Ben Allen <ben@firehawkaerospace.com>", "ACMI x Firehawk: edited"), FakeDrafts.calls)
        self.assertIn(("trash", ("11",)), FakeDrafts.calls)
        self.assertFalse(any(c[0] == "update" for c in FakeDrafts.calls))
        task = Task.objects.get(title="Review and send draft: Firehawk Aerospace")
        self.assertEqual(task.assignee, dave)
        self.assertIn("compose=m77", task.notes)
        mine = Task.objects.get(title="Review and send draft: Old Co")
        self.assertEqual(mine.assignee, self.user)  # no owner picked: stays with whoever applied
        self.assertIn("compose=m13", mine.notes)
        self.assertFalse(any(c[0] == "create" and "Old Co" in c[2] for c in FakeDrafts.calls))

    def test_prospect_pursue_creates_company_and_task(self):
        scan = self.open_scan()
        p = NewsItem.objects.create(scan=scan, kind="prospect", tier="strong", company_name="Aventra",
                                    hq="Virginia", what_they_do="Glide bombs", why_fits="Munitions maker",
                                    headline="Raised $17.5M", position=1000)
        self.client.post(f"/news/items/{p.pk}/", {"decision": "pursue"})
        with patch("crm.news.drafts.Drafts", FakeDrafts):
            review.apply(scan, self.user)
        aventra = Company.objects.get(name="Aventra")
        self.assertEqual((aventra.hq_location, aventra.description), ("Virginia", "Glide bombs"))
        self.assertTrue(Task.objects.filter(title="Research new lead: Aventra").exists())

    def test_bad_decision_rejected(self):
        scan = self.open_scan()
        r = self.client.post(f"/news/items/{self.item(scan, 'Valinor').pk}/", {"decision": "pursue"})
        self.assertEqual(r.status_code, 400)


class NumberedLayoutTests(TestCase):
    """The 2026-10-28 scan lists board companies as '1. Company (Section)' and has no section headings."""

    TEXT = """Weekly Prospect News Scan
October 28, 2026
Report window: October 21, 2026 - October 28, 2026
Part A: Notable News This Week
1. Zone 5 Technologies (Cold / New Leads)
Headline: Zone 5 Unveils Block 40
Date: October 24, 2026
Source: en.defence-ua.com
Synopsis: A new variant.
Outreach action: Outreach draft created in Gmail (external, threaded relationship with an existing contact) to Bill
Denzel (COO) flagging the target.
Framing: External.
Trello card: Updated. "Recent News" note added to the card
and the outreach sent.
2. Xtend (EARLY ENGAGEMENT)
Headline: Xtend wins an order
Date: October 27, 2026
Source: xtend.me
Synopsis: An order.
Outreach action: No outreach drafted.
Framing: No outreach.
Trello card: Updated.
3. Long Wall (LOI / PDA Negotiations)
Headline: Long Wall award
Date: October 22, 2026
Source: breakingdefense.com
Synopsis: An award.
Outreach action: No outreach drafted.
Framing: Internal.
Trello card: Not updated.
New Prospects to Target
Intro.
Strong Fit (1)
1. Radiant Nuclear
Company Name: Radiant Nuclear
HQ: El Segundo, California
What They Do: Builds microreactors.
Signal: Navy tapped Radiant.
Source: Tectonic Defense (Oct 21)
Fit Note: Needs factory space.
Worth Watching (0)
Weak or No U.S.-Site Fit (1)
Listed for completeness.
● Quantum Systems — Gilching, Germany. Foreign HQ.
Companies Checked - No Notable News This Week
"""

    def test_numbered_entries_and_prospects(self):
        from crm.news.scan_parser import parse_new_prospects, _parse_numbered

        entries = _parse_numbered(self.TEXT)
        self.assertEqual([(e["company"], e["section"]) for e in entries],
                         [("Zone 5 Technologies", "Cold"), ("Xtend", "Early Engagement"),
                          ("Long Wall", "LOI / PDA Negotiations")])
        self.assertTrue(entries[0]["has_draft"])
        self.assertEqual(entries[0]["contact"], "Bill Denzel")
        self.assertEqual(entries[0]["trello_note"], 'Updated. "Recent News" note added to the card and the outreach sent.')
        self.assertFalse(entries[1]["has_draft"])
        prospects = parse_new_prospects(self.TEXT)
        strong = prospects["strong_fit"][0]
        self.assertEqual((strong["company"], strong["why_fits"], strong["source_name"]),
                         ("Radiant Nuclear", "Needs factory space.", "Tectonic Defense (Oct 21)"))
        self.assertEqual(prospects["weak"], [{"company": "Quantum Systems", "reason": "Gilching, Germany. Foreign HQ."}])
