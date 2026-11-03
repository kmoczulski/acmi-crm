"""Contract review: the closing timeline (rules, what-if effective dates, versions side by side, exercising an
extension), redlines with who made each change, Word tracked changes, comments pinned to the text with @mentions and
Claude's notes, clickable summaries, and the dashboard's critical dates calendar."""
import io
import json
import tempfile
import zipfile
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from crm.contracts import ai, compare, dates, timeline
from crm.contracts import text as textmod
from crm.models import Comment, Contract, ContractDate, ContractDocument, LandSite, Notification, Task
from crm.tests.test_contracts import ORIGINAL, ORIGINAL_PAGE_2, fake_claude, text_block
from crm.tests.utils import make_manager

User = get_user_model()
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def tracked_docx():
    """A .docx whose second paragraph has a tracked insertion by Venable and a deletion by Kellen."""
    body = (f'<w:document xmlns:w="{W}"><w:body>'
            '<w:p><w:r><w:t>2. DEPOSIT.</w:t></w:r></w:p>'
            '<w:p><w:r><w:t xml:space="preserve">Buyer shall deposit </w:t></w:r>'
            '<w:del w:id="1" w:author="Kellen Moczulski" w:date="2026-10-01T10:00:00Z"><w:r><w:delText>$100,000</w:delText></w:r></w:del>'
            '<w:ins w:id="2" w:author="Venable" w:date="2026-10-02T09:00:00Z"><w:r><w:t>$250,000</w:t></w:r></w:ins>'
            '<w:r><w:t xml:space="preserve"> with the Escrow Agent.</w:t></w:r></w:p>'
            '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", body)
    return buf.getvalue()


class RuleTests(TestCase):
    def test_reading_rules(self):
        self.assertEqual(timeline.parse_rule("Within three business days after the Effective Date"),
                         {"offset": 3, "unit": "business", "anchor": "effective"})
        self.assertEqual(timeline.parse_rule("one hundred twenty (120) days after the Effective Date"),
                         {"offset": 120, "unit": "days", "anchor": "effective"})
        self.assertEqual(timeline.parse_rule("thirty (30) days after the end of the Feasibility Period"),
                         {"offset": 30, "unit": "days", "anchor": "feasibility_end"})
        self.assertEqual(timeline.parse_rule("15 days prior to the expiration of the Feasibility Period")["offset"], -15)
        self.assertIsNone(timeline.parse_rule("tenant 30 days"))
        self.assertEqual(timeline.parse_extension("Buyer may extend the Feasibility Period by thirty (30) days"),
                         {"extends": "feasibility_end", "extend_by": 30, "extend_unit": "days"})

    def test_adding(self):
        self.assertEqual(timeline.add(date(2026, 3, 6), 3, "business"), date(2026, 3, 11))  # Fri + 3 skips the weekend
        self.assertEqual(timeline.add(date(2026, 1, 31), 1, "months"), date(2026, 2, 28))
        self.assertEqual(timeline.add(date(2026, 3, 2), -15), date(2026, 2, 15))

    def test_compute_and_compare(self):
        terms = [
            {"key": "deposit", "label": "Deposit", "kind": "deposit", "anchor": "effective", "offset": 3, "unit": "business"},
            {"key": "feasibility_end", "label": "Feasibility ends", "kind": "feasibility", "anchor": "effective",
             "offset": 90, "unit": "days"},
            {"key": "closing", "label": "Closing", "kind": "closing", "anchor": "feasibility_end", "offset": 30,
             "unit": "days"},
            {"key": "outside", "label": "Outside date", "kind": "closing", "date": date(2027, 3, 1)},
            {"key": "loop", "label": "Loop", "kind": "other", "anchor": "loop", "offset": 1, "unit": "days"},
        ]
        rows = {r["key"]: r for r in timeline.compute(terms, date(2026, 3, 2), today=date(2026, 3, 1))}
        self.assertEqual(rows["deposit"]["when"], date(2026, 3, 5))
        self.assertEqual(rows["feasibility_end"]["when"], date(2026, 5, 31))
        self.assertEqual(rows["closing"]["when"], date(2026, 6, 30))
        self.assertEqual(rows["outside"]["when"], date(2027, 3, 1))
        self.assertIsNone(rows["loop"]["when"])  # counts from itself: no date, no hang
        self.assertTrue(rows["closing"]["computed"])
        self.assertEqual(rows["closing"]["how"], "30 days after feasibility end")
        later = [dict(t) for t in terms]
        later[1]["offset"] = 120
        v1 = {"rows": timeline.compute(terms, date(2026, 3, 2))}
        v2 = {"rows": timeline.compute(later, date(2026, 3, 2))}
        table, span = timeline.compare([v1, v2])
        changed = {r["key"]: r["changed"] for r in table}
        self.assertEqual((changed["deposit"], changed["feasibility_end"], changed["closing"]), (False, True, True))
        self.assertEqual(span[1], date(2027, 3, 1))
        lanes, ticks = timeline.lanes([v1, v2], span, today=date(2026, 3, 1))
        self.assertEqual(len(lanes), 2)
        self.assertTrue(any(t.get("today") for t in ticks))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class TimelineTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen", last_name="Moczulski"))
        self.client.force_login(self.user)
        self.site = LandSite.objects.create(name="Outside the Gate", status="contract")
        self.c = Contract.objects.create(title="Wamsley PSA", kind="psa", status="signed", site=self.site,
                                         owner=self.user, effective_date=date(2026, 3, 2))

    def add(self, **kw):
        d = ContractDate(contract=self.c, created_by=self.user, **kw)
        timeline.fill_rule(d)
        d.save()
        return d

    def test_fill_rule_from_typed_rule(self):
        f = self.add(kind="feasibility", label="Feasibility ends", date=date(2026, 5, 31),
                     rule="ninety (90) days after the Effective Date")
        self.assertEqual((f.key, f.anchor, f.offset, f.unit), ("feasibility_end", "effective", 90, "days"))
        ext = self.add(kind="extension", label="Feasibility extension", date=date(2026, 5, 16),
                       rule="Buyer may extend the Feasibility Period by thirty (30) days on 15 days notice")
        self.assertEqual((ext.extends, ext.extend_by), ("feasibility_end", 30))

    def test_exercise_extension_pushes_dates(self):
        feas = self.add(kind="feasibility", label="Feasibility ends", date=date(2026, 5, 31),
                        rule="ninety (90) days after the Effective Date")
        hard = self.add(kind="hard", label="Deposit goes hard", date=date(2026, 5, 31))  # same day, no rule
        closing = self.add(kind="closing", label="Closing", date=date(2026, 6, 30),
                           rule="thirty (30) days after the end of the Feasibility Period")
        ext = self.add(kind="extension", label="Feasibility extension", date=date(2026, 5, 16), amount=25000,
                       rule="Buyer may extend the Feasibility Period by thirty (30) days")
        page = self.client.get(f"/contracts/{self.c.pk}/timeline/")
        self.assertContains(page, "Confirm the extension")
        self.assertContains(page, "Deposit goes hard")
        r = self.client.post(f"/contracts/{self.c.pk}/timeline/", {
            "action": "exercise", "date_id": ext.pk, "notice_on": "2026-05-10", "new_date": "2026-06-30",
            "also": [hard.pk], "note": "Fee wired"})
        self.assertEqual(r.status_code, 302)
        for d in (feas, hard, closing, ext):
            d.refresh_from_db()
        self.assertEqual(feas.date, date(2026, 6, 30))
        self.assertEqual(closing.date, date(2026, 7, 30))  # counts from the feasibility end
        self.assertEqual(hard.date, date(2026, 6, 30))  # ticked to move with it
        self.assertEqual(feas.history[0]["from"], "2026-05-31")
        self.assertIn("Feasibility extension exercised", feas.history[0]["why"])
        self.assertTrue(ext.done)
        self.c.refresh_from_db()
        self.assertEqual((self.c.decision, self.c.decision_on), ("extend", date(2026, 5, 10)))
        self.site.refresh_from_db()
        self.assertEqual(self.site.milestones["feasibility_end"], "2026-06-30")
        page = self.client.get(self.c.get_absolute_url())  # the history shows on the contract page
        self.assertContains(page, "2026-05-31 → 2026-06-30")
        self.assertContains(page, "exercised 2026-05-10")
        self.assertContains(self.client.get(f"/contracts/{self.c.pk}/timeline/"), "Moved 1×")

    def test_push_by_hand_and_what_if(self):
        feas = self.add(kind="feasibility", label="Feasibility ends", date=date(2026, 5, 31),
                        rule="ninety (90) days after the Effective Date")
        self.add(kind="closing", label="Closing", date=date(2026, 6, 30),
                 rule="thirty (30) days after the end of the Feasibility Period")
        page = self.client.get(f"/contracts/{self.c.pk}/timeline/?eff=2026-04-01&v=kept")
        self.assertContains(page, "What-if")
        self.assertContains(page, "Jun 30, 2026")  # Apr 1 + 90 days, worked out from the rule
        self.client.post(f"/contracts/{self.c.pk}/timeline/", {"action": "push", "date_id": feas.pk,
                                                                "new_date": "2026-06-15", "note": "Seller agreed"})
        self.assertEqual(self.c.dates.get(kind="closing").date, date(2026, 7, 15))

    def test_versions_side_by_side_and_keep(self):
        d1 = ContractDocument.objects.create(contract=self.c, kind="draft", title="Round 1", sent_by="us",
                                             text=ORIGINAL, timeline=[
            {"key": "feasibility_end", "label": "Feasibility ends", "kind": "feasibility", "anchor": "effective",
             "offset": 90, "unit": "days", "date": None, "amount": None}])
        d2 = ContractDocument.objects.create(contract=self.c, kind="draft", title="Round 2", sent_by="them",
                                             text=ORIGINAL, timeline=[
            {"key": "feasibility_end", "label": "Feasibility ends", "kind": "feasibility", "anchor": "effective",
             "offset": 60, "unit": "days", "date": None, "amount": "100000"}])
        page = self.client.get(f"/contracts/{self.c.pk}/timeline/?eff=2026-03-02&v={d1.pk}&v={d2.pk}")
        self.assertContains(page, "May 31, 2026")
        self.assertContains(page, "May 1, 2026")
        self.assertContains(page, "changed")
        r = self.client.post(f"/contracts/{self.c.pk}/timeline/", {"action": "keep", "doc": d2.pk, "eff": "2026-03-02"})
        self.assertRedirects(r, self.c.get_absolute_url() + "#dates", fetch_redirect_response=False)
        kept = self.c.dates.get(confirmed=False)
        self.assertEqual((kept.date, kept.anchor, kept.offset), (date(2026, 5, 1), "effective", 60))

    def test_contract_page_shows_timeline(self):
        self.add(kind="feasibility", label="Feasibility ends", date=date(2026, 5, 31))
        page = self.client.get(self.c.get_absolute_url())
        self.assertContains(page, 'class="tl-dot tl-k-feasibility')
        self.assertContains(page, f"/contracts/{self.c.pk}/timeline/")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class RedlineAndCommentTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen", last_name="Moczulski"))
        self.simon = User.objects.create_user("simon", first_name="Simon", last_name="Shewmaker")
        self.client.force_login(self.user)
        self.c = Contract.objects.create(title="Wamsley PSA", kind="psa", counterparty="Wamsley Farm LLC",
                                         owner=self.simon)

    def draft(self, title, text, sent_by, **kw):
        return ContractDocument.objects.create(contract=self.c, kind="draft", title=title, text=text,
                                               page_starts=[0], sent_by=sent_by, added_by=self.user, text_status="ok", **kw)

    def test_blame_credits_the_round_that_made_each_change(self):
        v0 = "The Deposit is $100,000 and closing is in 30 days."
        v1 = "The Deposit is $250,000 and closing is in 30 days."
        v2 = "The Deposit is $250,000 and closing is in 45 days."
        origin, removed = compare.blame([v0, v1, v2])
        _, stats = compare.redline(v0, v2, origin=origin, removed=removed, who=lambda r, s, e: (f"round {r}", f"rl-a{r}"))
        self.assertEqual([(i["inserted"], i["round"]) for i in stats["items"]], [("$250,000", 1), ("45", 2)])
        self.assertIn('class="rl-chg rl-a1"', compare.redline(v0, v2, origin=origin, removed=removed,
                                                              who=lambda r, s, e: ("x", f"rl-a{r}"))[0])

    def test_redline_page_lists_who_made_each_change(self):
        a = self.draft("Round 1", "The Deposit is $100,000 and closing is in 30 days.", "us")
        b = self.draft("Round 2", "The Deposit is $250,000 and closing is in 30 days.", "them",
                       author="Venable (seller's counsel)")
        page = self.client.get(f"/contracts/{self.c.pk}/compare/?a={a.pk}&b={b.pk}")
        self.assertContains(page, "Redlined by")
        self.assertContains(page, "Venable (seller&#x27;s counsel)")
        self.assertContains(page, 'id="chg-1"')
        self.assertEqual(b.made_by, "Venable (seller's counsel)")
        self.assertEqual(a.made_by, "Kellen Moczulski")
        b.author = ""
        self.assertEqual(b.made_by, "Wamsley Farm LLC")  # their round, nobody named: the other side

    def test_word_tracked_changes(self):
        text, starts, status, tracked = textmod.extract_full(tracked_docx(), "Round 3.docx")
        self.assertEqual(status, "ok")
        self.assertIn("Buyer shall deposit $250,000 with the Escrow Agent.", text)
        self.assertNotIn("$100,000", text)
        ins = next(c for c in tracked if c["type"] == "ins")
        dele = next(c for c in tracked if c["type"] == "del")
        self.assertEqual((text[ins["start"]:ins["end"]], ins["author"], ins["date"]), ("$250,000", "Venable", "2026-10-02"))
        self.assertEqual((dele["text"], dele["author"], dele["start"]), ("$100,000", "Kellen Moczulski", ins["start"]))
        html = textmod.reader_html(text, starts, tracked=tracked)
        self.assertIn('data-author="Kellen Moczulski" title="Deleted by Kellen Moczulski, 2026-10-01">$100,000</del>', html)
        self.assertIn('data-author="Venable" title="Added by Venable, 2026-10-02">$250,000</ins>', html)
        # Uploaded through the contract page, the reader shows them
        self.client.post(self.c.get_absolute_url(), {"action": "upload", "kind": "draft", "dated": "2026-10-02",
                                                     "title": "Round 3", "sent_by": "them",
                                                     "file": SimpleUploadedFile("Round 3.docx", tracked_docx())})
        doc = ContractDocument.objects.get(title="Round 3")
        self.assertEqual(len(doc.tracked), 2)
        page = self.client.get(f"/contracts/{self.c.pk}/read/{doc.pk}/")
        self.assertContains(page, "Tracked changes in this file")

    def test_comment_on_selection_with_mention(self):
        doc = self.draft("Round 1", ORIGINAL, "us")
        start = ORIGINAL.index("$100,000")
        reader = f"/contracts/{self.c.pk}/read/{doc.pk}/"
        r = self.client.post(f"/contracts/{self.c.pk}/comments/", {
            "action": "add", "doc": doc.pk, "start": start, "end": start + 8, "quote": "$100,000",
            "body": "@simon can we get this down?", "next": reader})
        c = Comment.objects.get()
        self.assertRedirects(r, f"{reader}#c-{c.pk}", fetch_redirect_response=False)
        self.assertEqual((c.start, c.end, c.quote, c.contract, c.document), (start, start + 8, "$100,000", self.c, doc))
        n = Notification.objects.get(user=self.simon)
        self.assertEqual(n.kind, "mention")
        self.assertEqual(n.url, f"{reader}#c-{c.pk}")
        page = self.client.get(reader)
        self.assertContains(page, f'<mark class="cm-mark" data-c="{c.pk}">$100,000</mark>')
        self.assertContains(page, "can we get this down?")
        # A quote whose offsets drifted is found again; a reply notifies the commenter's thread; resolve hides the mark
        self.client.post(f"/contracts/{self.c.pk}/comments/", {
            "action": "add", "doc": doc.pk, "start": 0, "end": 3, "quote": "Escrow Agent", "body": "Which bank?"})
        moved = Comment.objects.get(body="Which bank?")
        self.assertEqual(ORIGINAL[moved.start:moved.end], "Escrow Agent")
        self.client.force_login(self.simon)
        self.client.post(f"/contracts/{self.c.pk}/comments/", {"action": "reply", "parent": c.pk, "body": "Trying"})
        self.assertTrue(Notification.objects.filter(user=self.user, kind="comment").exists())
        self.client.post(f"/contracts/{self.c.pk}/comments/", {"action": "resolve", "comment": c.pk})
        c.refresh_from_db()
        self.assertTrue(c.resolved)
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reader), f'data-c="{c.pk}">$100,000')

    def test_only_author_or_manager_deletes(self):
        doc = self.draft("Round 1", ORIGINAL, "us")
        c = Comment.objects.create(body="mine", author=self.user, contract=self.c, document=doc, start=0, end=4)
        self.client.force_login(self.simon)
        r = self.client.post(f"/contracts/{self.c.pk}/comments/", {"action": "delete", "comment": c.pk})
        self.assertTrue(Comment.objects.filter(pk=c.pk).exists())
        self.assertNotEqual(r.status_code, 500)

    def test_claude_notes_on_changes(self):
        a = self.draft("Round 1", "The Deposit is $100,000 and closing is in 30 days.", "us")
        b = self.draft("Round 2", "The Deposit is $250,000 and closing is in 45 days.", "them")
        notes = json.dumps({"notes": [{"n": 1, "comment": "Raises the deposit 2.5x; more at risk once it goes hard.",
                                       "flag": True}, {"n": 2, "comment": "Gives the seller 15 more days.",
                                                       "flag": False}]})
        patch, stream = fake_claude([text_block(notes)])
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), patch:
            r = self.client.post(f"/contracts/{self.c.pk}/compare/", {"action": "annotate", "a": a.pk, "b": b.pk})
        self.assertEqual(r.status_code, 302)
        prompt = stream.call_args.kwargs["messages"][0]["content"][-1]["text"]
        self.assertIn('[1] near', prompt)
        self.assertIn('ADDED: "$250,000"', prompt)
        made = list(Comment.objects.filter(by_claude=True).order_by("start"))
        self.assertEqual(len(made), 2)
        self.assertTrue(made[0].body.startswith("Change 1. Flag: Raises the deposit"))
        self.assertEqual(b.text[made[0].start:made[0].end], "$250,000")
        page = self.client.get(f"/contracts/{self.c.pk}/compare/?a={a.pk}&b={b.pk}")
        self.assertContains(page, "Gives the seller 15 more days.")
        self.assertContains(page, "cm-claude")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ClaudeTimelineTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.c = Contract.objects.create(title="Wamsley PSA", kind="psa", effective_date=date(2026, 3, 2))
        self.doc = ContractDocument.objects.create(contract=self.c, kind="draft", title="Round 1",
                                                   text=ORIGINAL + ORIGINAL_PAGE_2, text_status="ok", page_starts=[0])

    def item(self, **kw):
        base = {"kind": "feasibility", "label": "Feasibility ends", "date": "", "rule": "90 days after the Effective Date",
                "amount": "", "document": 1, "quote": "ninety (90) days after the Effective Date", "key": "feasibility_end",
                "anchor": "effective", "offset": 90, "unit": "days", "extends": "", "extend_by": None, "extend_unit": ""}
        return {**base, **kw}

    def test_read_timeline(self):
        found = json.dumps({"dates": [self.item(), self.item(kind="closing", label="Closing", key="closing",
                                                              anchor="feasibility_end", offset=30,
                                                              quote="thirty (30) days after the end")]})
        patch, stream = fake_claude([text_block(found)])
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), patch:
            r = self.client.post(f"/contracts/{self.c.pk}/timeline/", {"action": "read", "doc": self.doc.pk})
        self.assertEqual(r.status_code, 302)
        self.doc.refresh_from_db()
        self.assertEqual([t["key"] for t in self.doc.timeline], ["feasibility_end", "closing"])
        self.assertIsNotNone(self.doc.timeline[0]["start"])
        fmt = stream.call_args.kwargs["output_config"]["format"]["schema"]
        self.assertIn("anchor", fmt["properties"]["dates"]["items"]["required"])
        page = self.client.get(f"/contracts/{self.c.pk}/timeline/?v={self.doc.pk}")
        self.assertContains(page, "Jun 30, 2026")  # Mar 2 + 90 + 30

    def test_find_dates_keeps_rules(self):
        found = json.dumps({"dates": [self.item(date="2026-05-31")]})
        patch, _ = fake_claude([text_block(found)])
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), patch:
            made = ai.find_dates(self.c, self.user)
        self.assertEqual((made[0].key, made[0].anchor, made[0].offset, made[0].critical),
                         ("feasibility_end", "effective", 90, True))


class CalendarTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def test_month_calendar(self):
        psa = Contract.objects.create(title="Wamsley PSA", kind="psa", status="signed")
        pda = Contract.objects.create(title="Charles County PDA", kind="pda", status="signed")
        gone = Contract.objects.create(title="Dead deal", kind="psa", status="terminated")
        draft = Contract.objects.create(title="Lake Acton PSA", kind="psa", status="negotiating")
        day = self.today.replace(day=10)
        ContractDate.objects.create(contract=psa, kind="feasibility", label="Feasibility ends", date=day)
        ContractDate.objects.create(contract=psa, kind="deposit", label="First deposit", date=day, done=True)
        ContractDate.objects.create(contract=psa, kind="payment", label="Monthly rent", date=day, critical=False)
        ContractDate.objects.create(contract=pda, kind="approval", label="Site plan approval deadline", date=day)
        ContractDate.objects.create(contract=gone, kind="closing", label="Closing", date=day)
        ContractDate.objects.create(contract=draft, kind="feasibility", label="Draft feasibility", date=day)
        Task.objects.create(title="Send extension notice", label="critical", due_date=day, created_by=self.user)
        cal = dates.month_calendar(None, self.today)
        items = [i for w in cal["weeks"] for d in w for i in d["items"]]
        labels = {(i.tag, i.label, i.done) for i in items}
        self.assertIn(("PSA", "Feasibility ends", False), labels)
        self.assertIn(("PSA", "First deposit", True), labels)
        self.assertIn(("PDA", "Site plan approval deadline", False), labels)
        self.assertNotIn("Send extension notice", {i.label for i in items})  # only executed agreements
        self.assertNotIn("Draft feasibility", {i.label for i in items})
        self.assertNotIn("Monthly rent", {i.label for i in items})
        self.assertNotIn("Closing", {i.label for i in items})
        page = self.client.get("/")
        self.assertContains(page, "Critical dates · " + self.today.strftime("%B %Y"))
        tab = self.client.get("/critical-dates/?view=week")
        self.assertContains(tab, "week of")
        self.assertContains(tab, "My work calendar")
        listed = self.client.get("/contracts/")
        self.assertContains(listed, "Land")
        self.assertContains(page, "Site plan approval deadline")
        nxt = (self.today.replace(day=1) + timedelta(days=32)).strftime("%Y-%m")
        self.assertContains(self.client.get(f"/?crit={nxt}"), "Critical dates · ")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ExecutedExtensionTests(TestCase):
    """The ETC PSA as it was entered: dates from Trello with rules in words and no keys, and an extension whose
    rule says "extends it 120 days" without naming what it extends in the usual way."""

    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen", last_name="Moczulski"))
        self.client.force_login(self.user)
        self.site = LandSite.objects.create(name="4390 Indian Head Hwy", status="contract")
        self.c = Contract.objects.create(title="4390 Indian Head Hwy PSA", kind="psa", status="signed", site=self.site,
                                         effective_date=date(2026, 6, 24), owner=self.user)

        def mk(**kw):
            return ContractDate.objects.create(contract=self.c, **kw)

        self.feas = mk(kind="feasibility", label="Feasibility Period Expiration", date=date(2026, 10, 22),
                       rule="120 days after the Effective Date, 5:00 p.m. Eastern")
        mk(kind="title", label="Notice of Title Defects Deadline", date=date(2026, 10, 22),
           rule="On or before the end of the Feasibility Period")
        self.ext = mk(kind="extension", label="Extension Notice Deadline", date=date(2026, 10, 22), amount=10000,
                      rule="Written notice by the end of the Feasibility Period extends it 120 days "
                           "($10,000 extension deposit)")
        mk(kind="title", label="Seller's Response to Title Defects", date=date(2026, 11, 1),
           rule="10 days after the Notice of Title Defects (assuming notice on Oct 22)")
        mk(kind="notice", label="Notice of Closing Date", date=date(2026, 12, 11),
           rule="10 days' advance notice of the Closing Date to the Seller")
        mk(kind="closing", label="Closing Date Deadline", date=date(2026, 12, 21),
           rule="No more than 60 days after the Feasibility Period ends")
        ContractDocument.objects.create(contract=self.c, kind="original", title="FE PSA - executed", text=ORIGINAL)

    def test_works_out_the_extension_and_pushes_the_chain(self):
        page = self.client.get(f"/contracts/{self.c.pk}/timeline/")
        self.assertContains(page, "Feb 19, 2027")  # Oct 22 + 120 days, worked out, not typed
        self.assertContains(page, "Confirm the extension")
        self.assertContains(page, "is the final executed agreement")
        r = self.client.post(f"/contracts/{self.c.pk}/timeline/", {"action": "exercise", "date_id": self.ext.pk,
                                                                    "target": self.feas.pk, "notice_on": "2026-10-19"})
        self.assertEqual(r.status_code, 302)
        got = {d.label: (d.date, d.done) for d in self.c.dates.all()}
        self.assertEqual(got["Feasibility Period Expiration"], (date(2027, 2, 19), False))
        self.assertEqual(got["Notice of Title Defects Deadline"][0], date(2027, 2, 19))
        self.assertEqual(got["Seller's Response to Title Defects"][0], date(2027, 3, 1))
        self.assertEqual(got["Closing Date Deadline"][0], date(2027, 4, 20))
        self.assertEqual(got["Notice of Closing Date"][0], date(2027, 4, 10))
        self.assertEqual(got["Extension Notice Deadline"], (date(2026, 10, 22), True))  # exercised, not moved
        self.c.refresh_from_db()
        self.assertEqual(self.c.decision, "extend")
        self.site.refresh_from_db()
        self.assertEqual(self.site.milestones["feasibility_end"], "2027-02-19")
        self.assertContains(self.client.get(f"/contracts/{self.c.pk}/timeline/"), "Exercised Oct 19, 2026")

    def test_contract_page_marks_the_executed_agreement(self):
        page = self.client.get(self.c.get_absolute_url())
        self.assertContains(page, "Final executed")
        self.assertContains(page, "is the final executed agreement")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DropUploadTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen", last_name="Moczulski"))
        self.client.force_login(self.user)

    def test_guess_from_the_file_name(self):
        from crm.contracts import intake

        self.assertEqual(intake.guess("ACMI PDA - Long Wall - LW Redline 2026.10.19.docx"),
                         {"kind": "draft", "title": "ACMI PDA - Long Wall - LW Redline 2026.10.19",
                          "dated": date(2026, 10, 19)})
        self.assertEqual(intake.guess("First Amendment to PSA.pdf")["kind"], "amendment")
        self.assertEqual(intake.guess("FE PSA - executed - 20260622.pdf")["kind"], "original")

    def test_drop_on_a_contract_without_claude(self):
        c = Contract.objects.create(title="Long Wall PDA", kind="pda", owner=self.user)
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            r = self.client.post(f"/contracts/{c.pk}/drop/", {
                "file": SimpleUploadedFile("LW Redline 2026.10.19.docx", tracked_docx())})
            again = self.client.post(f"/contracts/{c.pk}/drop/", {"file": SimpleUploadedFile("copy.docx", tracked_docx())})
        self.assertTrue(r.json()["ok"])
        doc = ContractDocument.objects.get()
        self.assertEqual((doc.kind, doc.dated, len(doc.tracked)), ("draft", date(2026, 10, 19), 2))
        self.assertIn("Already in the file", again.json()["error"])
        self.assertEqual(ContractDocument.objects.count(), 1)

    def test_drop_a_new_agreement_with_claude(self):
        found = json.dumps({"document": {"kind": "original", "title": "Wamsley PSA (executed)", "dated": "2026-03-02",
                                         "sent_by": "", "author": "", "summary": "The signed purchase agreement."},
                            "contract": {"title": "Wamsley PSA", "kind": "psa", "counterparty": "Wamsley Farm LLC",
                                         "our_entity": "ACMI Properties MEIH LLC", "price": "4,200,000",
                                         "acres": "212.5", "effective_date": "2026-03-02", "status": "signed"}})
        patch, stream = fake_claude([text_block(found)])
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), patch:
            r = self.client.post("/contracts/new/drop/", {"file": SimpleUploadedFile("scan_0042.txt", ORIGINAL.encode())})
        data = r.json()
        c = Contract.objects.get()
        self.assertEqual(data["url"], c.get_absolute_url())
        self.assertEqual((c.title, c.kind, c.status, c.counterparty, str(c.price), c.effective_date),
                         ("Wamsley PSA", "psa", "signed", "Wamsley Farm LLC", "4200000.00", date(2026, 3, 2)))
        doc = c.documents.get()
        self.assertEqual((doc.kind, doc.title, doc.summary),
                         ("original", "Wamsley PSA (executed)", "The signed purchase agreement."))
        self.assertIn("contract's", " ".join(data["filled"]))
        self.assertEqual(stream.call_args.kwargs["output_config"]["effort"], "low")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class WordRedlineTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.c = Contract.objects.create(title="Long Wall PDA", kind="pda")

    def test_tracked_view_and_reread(self):
        from django.core.files.base import ContentFile
        from django.core.management import call_command

        v1 = ContractDocument.objects.create(
            contract=self.c, kind="original", title="PDA 2026.06.23", dated=date(2026, 6, 23), page_starts=[0],
            text="2. DEPOSIT.\nBuyer shall deposit $100,000 with the Escrow Agent.", text_status="ok")
        v2 = ContractDocument(contract=self.c, kind="draft", title="LW Redline", dated=date(2026, 10, 19),
                              text="2. DEPOSIT.\nBuyer shall deposit $250,000 with the Escrow Agent.", page_starts=[0],
                              text_status="ok")
        v2.file.save("LW Redline.docx", ContentFile(tracked_docx()), save=True)  # uploaded before tracking: none kept
        call_command("reread_contract_files", stdout=io.StringIO())
        v2.refresh_from_db()
        self.assertEqual(len(v2.tracked), 2)
        page = self.client.get(f"/contracts/{self.c.pk}/compare/?b={v2.pk}&view=tracked")
        self.assertContains(page, "Tracked in the Word file (2)")
        self.assertContains(page, "Venable")
        self.assertContains(page, "Kellen Moczulski")
        self.assertContains(page, 'data-tc="1"')
        self.assertContains(page, f"Compared with {v1.title}")
        detail = self.client.get(self.c.get_absolute_url())
        self.assertContains(detail, "(2 tracked)")
        self.assertContains(detail, "Compare with previous")
