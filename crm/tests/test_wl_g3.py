"""W3 (worklist 2026-10-28): pages wider than a 390 px phone.

A browser isn't available to the tests, so these check the two causes UI review bug 3 names: the phone grid rules
that let the single column grow to its widest table, and wide tables with no scroll frame (.table-wrap)."""
import re
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from crm.models import Approval, Campus, CapitalSource, Company, Contract, Firm, Opportunity, Project
from crm.tests.utils import make_manager

User = get_user_model()
CSS = Path(settings.BASE_DIR, "crm", "static", "crm", "app.css")


class TableFrames(HTMLParser):
    """Each table on the page, keyed by its header cells, and whether a .table-wrap holds it."""
    def __init__(self):
        super().__init__()
        self.stack, self.tables = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ("input", "br", "img", "meta", "link", "hr", "source", "col", "wbr"):
            return
        classes = (dict(attrs).get("class") or "").split()
        if tag == "table":
            self.tables.append({"heads": [], "wrapped": any("table-wrap" in c for _, c in self.stack)})
        self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][0] == "th" and self.tables and data.strip():
            self.tables[-1]["heads"].append(data.strip())


def table_with(html, head):
    """The table whose header row has a cell reading `head`."""
    parser = TableFrames()
    parser.feed(html)
    found = [t for t in parser.tables if head in t["heads"]]
    assert found, f"no table headed {head!r}"
    return found[0]


class PhoneGridTests(TestCase):
    def test_phone_columns_stay_at_the_screen_width(self):
        css = CSS.read_text(encoding="utf-8")
        for rule in (r"@media \(max-width: 1000px\) \{ \.campus-layout \{ grid-template-columns: minmax\(0, 1fr\); \} \}",
                     r"@media \(max-width: 1000px\) \{ \.proj-layout \{ grid-template-columns: minmax\(0, 1fr\); \} \}",
                     r"@media \(max-width: 1100px\) \{ \.ct-layout, \.cr-layout \{ grid-template-columns: minmax\(0, 1fr\); \}",
                     r"\.dd-areas \{[^}]*minmax\(min\(380px, 100%\), 1fr\)"):
            self.assertRegex(css, rule)
        self.assertNotRegex(css, r"\.dd-areas \{[^}]*minmax\(380px, 1fr\)")


class WideTablesScrollTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.project = Project.objects.create(name="ARA Facility", campus=self.campus)
        company = Company.objects.create(name="Thales")
        self.opp = Opportunity.objects.create(name="Thales building", company=company, campus=self.campus)

    def assertWrapped(self, url, *heads):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, url)
        html = r.content.decode()
        for head in heads:
            self.assertTrue(table_with(html, head)["wrapped"], f"{url}: the {head!r} table has no .table-wrap")

    def test_named_tables_scroll_in_their_own_frame(self):
        Contract.objects.create(title="Thales lease", kind="lease", status="signed", campus=self.campus,
                                opportunity=self.opp, owner=self.user)
        self.assertWrapped(reverse("crm:opportunity_fit", args=[self.opp.pk]), "Hazard division")
        self.assertWrapped(reverse("crm:rent_roll"), "Renewal notice")
        self.assertWrapped(reverse("crm:home"), "Win %")
        self.assertWrapped(reverse("crm:weekly_report"), "Status")

    def test_tables_in_the_phone_column_scroll(self):
        # With the column held to the screen, these would otherwise run off the side of their panel.
        CapitalSource.objects.create(provider=Firm.objects.create(name="Truist Bank"), project=self.project,
                                     owner=self.user)
        Approval.objects.create(title="Sign height variance", kind="variance", project=self.project,
                                campus=self.campus, owner=self.user, created_by=self.user)
        Project.objects.create(name="Loose project")
        self.assertWrapped(reverse("crm:capital_index"), "Latest terms")
        self.assertWrapped(reverse("crm:project_capital", args=[self.project.pk]), "Committed")
        self.assertWrapped(reverse("crm:approval_list"), "Agency / case")
        self.assertWrapped(reverse("crm:project_list"), "Project in MEIH", "Part of")
        self.assertWrapped(reverse("crm:dropdown_edit", args=["Priority"]), "Built-in name")

    def test_weekly_email_keeps_its_markup(self):
        # The scroll frame belongs to the page; the emailed copy is the same partial and stays class-free.
        body = (Path(settings.BASE_DIR, "crm", "templates", "crm", "partials", "report_body.html")
                .read_text(encoding="utf-8"))
        self.assertNotIn("table-wrap", body)
        self.assertIsNone(re.search(r'class="', body))
