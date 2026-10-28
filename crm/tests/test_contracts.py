"""Contracts: the agreement file, Notessa-style reading (defined terms, cross-references, amendments, cited answers,
abstracts), redlines, critical dates and reminders, and the land / campus / dashboard hooks."""
import io
import json
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import campus as campus_mod
from crm import notify
from crm.contracts import ai, compare, dates, methods
from crm.contracts import text as textmod
from crm.models import (
    Campus, Contract, ContractAnswer, ContractDate, ContractDocument, ContractTemplate, LandCounty, LandSite,
    LandSiteEvent, Notification, SiteParcelDeal,
)
from crm.tests.test_land import make_parcel
from crm.tests.utils import make_manager

User = get_user_model()

ORIGINAL = (
    "PURCHASE AND SALE AGREEMENT\n"
    'This Purchase and Sale Agreement is made as of March 2, 2026 (the "Effective Date") by and between Wamsley Farm '
    'LLC ("Seller") and ACMI Properties MEIH LLC ("Buyer").\n'
    '1. PURCHASE AND SALE. Seller agrees to sell and Buyer agrees to buy the land in Exhibit A (the "Property").\n'
    "2. DEPOSIT. Within three business days after the Effective Date, Buyer shall deposit $100,000 "
    '(the "Deposit") with the Escrow Agent.\n'
)
ORIGINAL_PAGE_2 = (
    '5.2 Feasibility Period. Buyer shall have ninety (90) days after the Effective Date (the "Feasibility Period") '
    "to inspect the Property. The Deposit becomes non-refundable at the end of the Feasibility Period, except as "
    "provided in Section 7.1.\n"
    "7.1 Termination. Buyer may terminate this Agreement by notice before the end of the Feasibility Period, as "
    "described in Section 5.2.\n"
    '"Closing Date" means the date that is thirty (30) days after the end of the Feasibility Period.\n'
)
AMENDMENT = (
    "FIRST AMENDMENT TO PURCHASE AND SALE AGREEMENT\n"
    '1. Section 5.2 of the Agreement is hereby amended by deleting "ninety (90) days" and replacing it with '
    '"one hundred twenty (120) days".\n'
    "2. Except as modified by this Amendment, the Agreement remains in full force.\n"
)


def make_pdf(pages):
    """A small text PDF, one Helvetica text block per page, so extraction runs on a real PDF."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    page_ids = [4 + 2 * i for i in range(len(pages))]
    objs.append(f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        ops = ["BT", "/F1 9 Tf", "12 TL", "36 760 Td"]
        for line in text.split("\n"):
            esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) Tj T*")
        stream = "\n".join(ops + ["ET"]).encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {page_ids[i] + 1} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def make_docx(paragraphs, break_after=None):
    import docx

    d = docx.Document()
    for i, p in enumerate(paragraphs):
        d.add_paragraph(p)
        if i == break_after:
            d.add_page_break()
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def fake_claude(content, stop_reason="end_turn"):
    message = SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5", content=content,
                              usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=0,
                                                    cache_creation_input_tokens=5000))
    stream = mock.Mock(return_value=FakeStream(message))
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return mock.patch("anthropic.Anthropic", return_value=client), stream


def text_block(text, cites=None):
    return SimpleNamespace(type="text", text=text, citations=cites)


def char_cite(doc_index, text, quote, start=None, end=None):
    start = text.index(quote) if start is None else start
    return SimpleNamespace(type="char_location", cited_text=quote, document_index=doc_index, document_title="x",
                           start_char_index=start, end_char_index=start + len(quote) if end is None else end)


class ExtractionTests(TestCase):
    def test_pdf_pages_and_offsets(self):
        text, starts, status = textmod.extract(make_pdf([ORIGINAL, ORIGINAL_PAGE_2]), "PSA.pdf")
        self.assertEqual(status, "ok")
        self.assertEqual(len(starts), 2)
        self.assertIn("Wamsley Farm", text[:starts[1]])
        self.assertIn("Feasibility Period", text[starts[1]:])
        self.assertEqual(textmod.page_of(starts, starts[1] + 5), 2)

    def test_docx_splits_at_page_breaks(self):
        data = make_docx(["PURCHASE AGREEMENT", "1. PRICE. The price is $2,000,000.", "5.2 Closing. Closing occurs."],
                         break_after=1)
        text, starts, status = textmod.extract(data, "PSA.docx")
        self.assertEqual((status, len(starts)), ("ok", 2))
        self.assertTrue(text[starts[1]:].startswith("5.2 Closing"))

    def test_scans_unsupported_and_broken_files(self):
        self.assertEqual(textmod.extract(make_pdf(["", ""]), "scan.pdf")[2], "scan")
        self.assertEqual(textmod.extract(b"x", "rent roll.xlsx")[2], "unsupported")
        self.assertEqual(textmod.extract(b"not a pdf", "broken.pdf")[2], "error")


class StructureTests(TestCase):
    def setUp(self):
        self.text = ORIGINAL + "\n\n" + ORIGINAL_PAGE_2

    def test_defined_terms(self):
        terms = {t: self.text[s:e] for t, s, e in textmod.definitions(self.text)}
        for t in ("Effective Date", "Seller", "Buyer", "Property", "Deposit", "Feasibility Period", "Closing Date"):
            self.assertIn(t, terms)
        self.assertTrue(terms["Closing Date"].startswith('"Closing Date" means the date'))
        self.assertIn('ninety (90) days after the Effective Date (the "Feasibility Period")', terms["Feasibility Period"])
        self.assertEqual(textmod.definitions('Attach the survey (see "Exhibit A" and the title report).'), [])

    def test_sections_skip_the_table_of_contents(self):
        toc = "TABLE OF CONTENTS\n5.2 Feasibility Period ........ 2\n7.1 Termination ........ 3\n\n"
        text = toc + self.text
        secs = textmod.sections(text)
        self.assertEqual(set(secs) >= {"1", "2", "5.2", "7.1"}, True)
        self.assertTrue(text[secs["5.2"][0]:].startswith("5.2 Feasibility Period. Buyer shall have"))
        self.assertIn("Section 7.1", text[slice(*secs["5.2"])])

    def test_cross_references(self):
        refs = [(self.text[s:e], num) for s, e, num, _ in textmod.xrefs(self.text)]
        self.assertEqual(refs, [("Section 7.1", "7.1"), ("Section 5.2", "5.2")])
        more = textmod.xrefs("as set out in Sections 5.2(a) and 5.3, and Section 1031 of the Code; Section 5.2, 10 days")
        self.assertEqual([n for *_, n, _ in more], ["5.2", "5.3", "5.2"])
        self.assertTrue(textmod.xrefs(AMENDMENT)[0][3])  # "…of the Agreement" points at the original

    def test_amended_sections(self):
        changed = textmod.amended_sections(AMENDMENT)
        self.assertEqual(list(changed), ["5.2"])
        s, e = changed["5.2"]
        self.assertIn("one hundred twenty (120) days", AMENDMENT[s:e])
        self.assertEqual(textmod.amended_sections("Except as modified by this Amendment and Section 3, nothing "
                                                  "changes."), {})

    def test_reader_html(self):
        text = self.text.replace("Wamsley", "<script>Wamsley")
        starts = [0, text.index("5.2 Feasibility")]
        quote = "Buyer shall deposit $100,000"
        s = text.index(quote)
        ref = text.index("Section 7.1")
        out = textmod.reader_html(text, starts, terms=["Buyer", "Deposit"], highlight=(s + 2, s + len(quote)),
                                  links={ref: (ref + len("Section 7.1"), "/x?hl=1-2#hl", {"num": "7.1"})},
                                  anchors={starts[1]: "s-5.2"}, badges={starts[1]: [("§ 5.2 changed", "/y")]})
        self.assertEqual(out.count('class="cr-page"'), 2)
        self.assertIn("&lt;script&gt;Wamsley", out)
        self.assertNotIn("<script>", out)
        self.assertIn('<mark class="cr-hl" id="hl"><button type="button" class="cr-term" data-term="Buyer">Buyer'
                      '</button> shall deposit $100,000</mark>', out)  # widened to cover the whole term
        self.assertIn('<a class="cr-xref" href="/x?hl=1-2#hl" data-num="7.1">Section 7.1</a>', out)
        self.assertIn('<span class="cr-anchor" id="s-5.2"></span><a class="cr-badge" data-x href="/y">§ 5.2 changed</a>5.2 '
                      'Feasibility', out)


class RedlineTests(TestCase):
    def test_word_changes_and_rewrapping(self):
        html, stats = compare.redline("The Deposit is $100,000\npayable at signing.", "The Deposit is $250,000 "
                                      "payable at signing.")
        self.assertIn("<del data-x>$100,000</del>", html)
        self.assertIn("<ins>$250,000</ins>", html)
        self.assertEqual((stats["changes"], stats["inserted"], stats["deleted"]), (1, 1, 1))
        _, same = compare.redline("one two\nthree four", "one two three\nfour")
        self.assertEqual(same["changes"], 0)  # only the line breaks moved

    def test_long_unchanged_stretches_fold(self):
        words = " ".join(f"w{i}" for i in range(500))
        html, _ = compare.redline(words + " old", words + " new")
        self.assertIn('<details class="rl-same"><summary>… 460 unchanged words …', html)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ContractPagesTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        self.site = LandSite.objects.create(name="Outside the Gate", status="loi", campus=self.campus)

    def make_contract(self, **kw):
        return Contract.objects.create(**{"title": "Wamsley PSA", "kind": "psa", "site": self.site,
                                          "campus": self.campus, "owner": self.user, **kw})

    def upload(self, contract, name, data, kind="original", dated="2026-03-02", **extra):
        return self.client.post(contract.get_absolute_url(), {
            "action": "upload", "kind": kind, "dated": dated, "title": extra.pop("title", ""),
            "file": SimpleUploadedFile(name, data), **extra})

    def test_create_from_a_site_and_list(self):
        r = self.client.get(f"/contracts/new/?site={self.site.pk}")
        self.assertContains(r, "Outside the Gate PSA")
        r = self.client.post(f"/contracts/new/?site={self.site.pk}", {
            "title": "Wamsley PSA", "kind": "psa", "status": "negotiating", "site": self.site.pk,
            "counterparty": "Wamsley Farm LLC", "owner": self.user.pk, "waiting_on": "them",
            "waiting_since": "2026-10-01"})
        c = Contract.objects.get()
        self.assertRedirects(r, c.get_absolute_url())
        self.assertEqual((c.campus, c.created_by), (self.campus, self.user))  # campus follows the site
        page = self.client.get("/contracts/")
        self.assertContains(page, "Wamsley PSA")
        self.assertContains(page, "Waiting on them")
        self.assertContains(self.client.get(f"/land/sites/{self.site.pk}/manage/"), "Wamsley PSA")

    def test_upload_read_and_reader(self):
        c = self.make_contract()
        r = self.upload(c, "Wamsley PSA.pdf", make_pdf([ORIGINAL, ORIGINAL_PAGE_2]))
        self.assertRedirects(r, c.get_absolute_url() + "#documents")
        doc = ContractDocument.objects.get()
        self.assertEqual((doc.title, doc.pages, doc.text_status, doc.added_by), ("Wamsley PSA", 2, "ok", self.user))
        self.upload(c, "First Amendment.txt", AMENDMENT.encode(), kind="amendment", dated="2026-06-01")
        amendment = ContractDocument.objects.latest("id")
        page = self.client.get(c.get_absolute_url())
        self.assertContains(page, "First Amendment")
        self.assertContains(page, "2 pages")

        reader = self.client.get(f"/contracts/{c.pk}/read/{doc.pk}/")
        self.assertContains(reader, 'data-term="Feasibility Period"')
        self.assertContains(reader, 'id="s-5.2"')
        self.assertContains(reader, "§ 5.2 changed by First Amendment")  # the amendment is linked back
        glossary = json.loads(reader.content.decode().split('id="cr-glossary" type="application/json">')[1]
                              .split("</script>")[0])
        self.assertIn("Closing Date", glossary)
        # In the amendment, "Section 5.2 of the Agreement" opens the original's section 5.2.
        amended = self.client.get(f"/contracts/{c.pk}/read/{amendment.pk}/")
        self.assertContains(amended, f"/contracts/{c.pk}/read/{doc.pk}/?hl=")
        self.assertContains(amended, 'data-num="5.2"')
        secs = textmod.sections(doc.text)
        span = self.client.get(f"/contracts/{c.pk}/read/{doc.pk}/span/?start={secs['5.2'][0]}&end={secs['5.2'][1]}")
        self.assertTrue(span.json()["text"].startswith("5.2 Feasibility Period"))
        self.assertEqual(span.json()["page"], 2)
        hl = self.client.get(f"/contracts/{c.pk}/read/{doc.pk}/?hl=10-40")
        self.assertContains(hl, 'id="hl"')
        f = self.client.get(f"/contracts/files/{doc.pk}/")
        self.assertEqual(f["Content-Type"], "application/pdf")
        self.assertTrue(b"".join(f.streaming_content).startswith(b"%PDF"))  # read it out; closing ends the DB link

    def test_drafts_redline(self):
        c = self.make_contract()
        self.upload(c, "PSA draft 1.txt", b"The Deposit is $100,000.", kind="draft", dated="2026-01-10", sent_by="us")
        self.upload(c, "PSA draft 2.txt", b"The Deposit is $250,000.", kind="draft", dated="2026-01-20",
                    sent_by="them")
        page = self.client.get(c.get_absolute_url())
        self.assertContains(page, "Compare with previous")
        a, b = ContractDocument.objects.order_by("dated")
        r = self.client.get(f"/contracts/{c.pk}/compare/?a={a.pk}&b={b.pk}")
        self.assertContains(r, "<del data-x>$100,000.</del>")
        self.assertContains(r, "<ins>$250,000.</ins>")

    def test_dates_by_hand_and_permissions(self):
        c = self.make_contract()
        self.client.post(c.get_absolute_url(), {"action": "date_add", "kind": "deposit", "label": "",
                                                "date": "2026-11-01", "amount": "100000"})
        d = ContractDate.objects.get()
        self.assertEqual((d.label, d.amount, d.confirmed, d.created_by), ("Deposit due", Decimal("100000"), True,
                                                                         self.user))
        self.client.post(c.get_absolute_url(), {"action": "date_done", "date_id": d.pk})
        d.refresh_from_db()
        self.assertTrue(d.done)
        joe = User.objects.create_user("joe")
        self.client.force_login(joe)
        self.assertEqual(self.client.post(c.get_absolute_url(), {"action": "date_delete", "date_id": d.pk}).status_code,
                         302)
        self.assertTrue(ContractDate.objects.filter(pk=d.pk).exists())  # not theirs, not a manager
        doc = ContractDocument.objects.create(contract=c, title="PSA", text="x", text_status="ok")
        self.client.post(c.get_absolute_url(), {"action": "doc_delete", "doc": doc.pk})
        self.assertTrue(ContractDocument.objects.filter(pk=doc.pk).exists())
        self.assertEqual(self.client.post(f"/contracts/{c.pk}/delete/").status_code, 302)
        self.assertTrue(Contract.objects.filter(pk=c.pk).exists())

    def test_deadlines_on_the_dashboard_and_list(self):
        c = self.make_contract()
        today = timezone.localdate()
        ContractDate.objects.create(contract=c, kind="hard", label="Deposit goes hard", date=today + timedelta(days=3))
        ContractDate.objects.create(contract=c, kind="title", label="Title objections", date=today - timedelta(days=2))
        ContractDate.objects.create(contract=c, kind="closing", label="Closing (suggested)",
                                    date=today + timedelta(days=5), confirmed=False)
        home = self.client.get("/")
        self.assertContains(home, "<h3>Deadlines</h3>")
        self.assertContains(home, "Deposit goes hard")
        self.assertContains(home, "2 days late")
        self.assertNotContains(home, "Closing (suggested)")  # not kept yet
        listing = self.client.get("/contracts/")
        self.assertContains(listing, "Title objections")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
@mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"})
class ClaudeTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("kellen", first_name="Kellen"))
        self.client.force_login(self.user)
        self.c = Contract.objects.create(title="Wamsley PSA", kind="psa", owner=self.user,
                                         effective_date=date(2026, 3, 2))
        self.text = ORIGINAL + "\n\n" + ORIGINAL_PAGE_2
        self.original = ContractDocument.objects.create(
            contract=self.c, kind="original", title="PSA", dated=date(2026, 3, 2), text=self.text,
            page_starts=[0, len(ORIGINAL) + 2], pages=2, text_status="ok")
        self.amendment = ContractDocument.objects.create(
            contract=self.c, kind="amendment", title="First Amendment", dated=date(2026, 6, 1), text=AMENDMENT,
            page_starts=[0], pages=1, text_status="ok")
        ContractDocument.objects.create(contract=self.c, kind="draft", title="Old draft", text="draft",
                                        text_status="ok", dated=date(2026, 2, 1))

    def test_ask_cites_the_exact_language(self):
        new = "one hundred twenty (120) days"
        old = "Buyer shall have ninety (90) days after the Effective Date"
        patcher, stream = fake_claude([
            SimpleNamespace(type="thinking", thinking=""),
            text_block("## Feasibility\nThe Feasibility Period is now "),
            text_block("120 days after the Effective Date", [char_cite(1, AMENDMENT, new)]),
            text_block(", up from "),
            text_block("90 days", [char_cite(0, self.text, old, start=3, end=9)]),  # drifted indexes
            text_block("."),
        ])
        with patcher:
            r = self.client.post(self.c.get_absolute_url(), {"action": "ask",
                                                             "question": "How long is the feasibility period?"})
        answer = ContractAnswer.objects.get()
        self.assertRedirects(r, self.c.get_absolute_url() + f"#answer-{answer.pk}", fetch_redirect_response=False)
        kwargs = stream.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["fallbacks"]), ("claude-opus-5-5", "default"))
        content = kwargs["messages"][0]["content"]
        docs, ask = content[:-1], content[-1]["text"]
        self.assertEqual([d["title"] for d in docs], ["1. PSA", "2. First Amendment"])  # signed only, in order
        self.assertEqual(docs[0]["source"]["data"], self.text)
        self.assertTrue(all(d["citations"] == {"enabled": True} for d in docs))
        self.assertEqual(docs[-1]["cache_control"], {"type": "ephemeral"})
        self.assertIn("Amendment; dated Jun 1, 2026", docs[1]["context"])
        self.assertIn("Today is", ask)
        self.assertTrue(ask.endswith("QUESTION\nHow long is the feasibility period?"))

        self.assertEqual(answer.blocks[0], {"text": "## Feasibility\nThe Feasibility Period is now ", "cites": []})
        cite = answer.blocks[1]["cites"][0]
        self.assertEqual((cite["doc"], AMENDMENT[cite["start"]:cite["end"]], cite["page"]),
                         (self.amendment.pk, new, 1))
        moved = answer.blocks[3]["cites"][0]
        self.assertEqual(self.text[moved["start"]:moved["end"]], old)  # re-found by its words
        self.assertEqual(moved["page"], 2)
        self.assertEqual((answer.input_tokens, answer.output_tokens), (6200, 300))

        html, sources = ai.render(answer, lambda c: f"/read/{c['doc']}?hl={c['start']}-{c['end']}#hl")
        self.assertIn("<h4>Feasibility</h4>", html)
        self.assertIn('class="cite"', html)
        self.assertEqual([s["n"] for s in sources], [1, 2])
        page = self.client.get(self.c.get_absolute_url())
        self.assertContains(page, "How long is the feasibility period?")
        self.assertContains(page, f'data-quote="{new}"')

    def test_restated_agreement_supersedes_and_compare_question(self):
        restated = ContractDocument.objects.create(contract=self.c, kind="restated", title="A&R PSA",
                                                   dated=date(2026, 9, 1), text="Restated.", text_status="ok")
        blocks = ai.doc_blocks(ai.default_docs(self.c))
        self.assertIn("superseded by document 3 (amended and restated)", blocks[0]["context"])
        self.assertNotIn("superseded", blocks[2]["context"])
        patcher, stream = fake_claude([text_block("Price went up.")])
        with patcher:
            self.client.post(self.c.get_absolute_url(), {"action": "ask", "question": "What changed?",
                                                         "docs": [self.original.pk, restated.pk]})
        titles = [d["title"] for d in stream.call_args.kwargs["messages"][0]["content"][:-1]]
        self.assertEqual(titles, ["1. PSA", "2. A&R PSA"])

    def test_abstract_from_a_template(self):
        template = methods.for_kind("psa")[0]
        self.assertEqual(template.key, "psa")
        patcher, stream = fake_claude([text_block("## Deposits\n- $100,000 ", [char_cite(0, self.text, "deposit $100,000")]),
                                       text_block("\n## Flags\n- None.")])
        with patcher:
            r = self.client.post(self.c.get_absolute_url(), {"action": "template", "template": template.pk})
        answer = ContractAnswer.objects.get()
        self.assertRedirects(r, f"/contracts/{self.c.pk}/answers/{answer.pk}/")
        prompt = stream.call_args.kwargs["messages"][0]["content"][-1]["text"]
        self.assertIn("Land purchase (PSA) abstract", prompt)
        self.assertIn('"## <label>"', prompt)
        self.assertIn("3. Deposits: Each deposit", prompt)
        self.assertEqual(stream.call_args.kwargs["max_tokens"], 32000)
        page = self.client.get(r.url)
        self.assertContains(page, "report-logo")
        self.assertContains(page, "deposit $100,000")  # the source list quotes it
        self.assertTrue(answer.is_abstract)

    def test_find_dates_then_keep(self):
        ContractDate.objects.create(contract=self.c, kind="hard", label="Deposit goes hard", date=date(2026, 6, 30))
        found = {"dates": [
            {"kind": "feasibility", "label": "Feasibility Period ends", "date": "2026-06-30",
             "rule": "120 days after the Effective Date (First Amendment)", "amount": "", "document": 2,
             "quote": 'replacing it with "one hundred twenty (120) days".'},
            {"kind": "deposit", "label": "Deposit due", "date": "2026-03-05", "rule": "3 business days after the "
             "Effective Date", "amount": "$100,000", "document": 1,
             "quote": "Within three business days after the Effective Date, Buyer shall deposit $100,000"},
            {"kind": "closing", "label": "Closing", "date": "", "rule": "30 days after the Feasibility Period ends",
             "amount": "", "document": 1, "quote": "Not in the text at all"},
            {"kind": "hard", "label": "Deposit goes hard", "date": "2026-06-30", "rule": "", "amount": "",
             "document": 1, "quote": ""},  # already on the contract
        ]}
        patcher, stream = fake_claude([text_block(json.dumps(found))])
        with patcher:
            self.client.post(self.c.get_absolute_url(), {"action": "find_dates"})
        kwargs = stream.call_args.kwargs
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("citations", kwargs["messages"][0]["content"][0])  # incompatible with structured output
        proposed = list(ContractDate.objects.filter(confirmed=False).order_by("id"))
        self.assertEqual([p.label for p in proposed], ["Feasibility Period ends", "Deposit due", "Closing"])
        deposit = proposed[1]
        self.assertEqual((deposit.amount, deposit.source, deposit.from_claude), (Decimal("100000"), self.original, True))
        self.assertTrue(self.text[deposit.source_start:].startswith("Within three business days"))
        self.assertIsNone(proposed[2].date)
        self.assertIsNone(proposed[2].source_start)  # quote not found: shown as unverified
        page = self.client.get(self.c.get_absolute_url())
        self.assertContains(page, "Claude found these")
        self.assertContains(page, "Quote (not found in the text)")

        self.client.post(self.c.get_absolute_url(), {"action": "date_keep", "date_id": [proposed[0].pk, proposed[1].pk]})
        self.assertEqual(ContractDate.objects.filter(confirmed=False).count(), 1)
        self.client.post(self.c.get_absolute_url(), {"action": "date_discard"})
        self.assertEqual(ContractDate.objects.count(), 3)

    def test_failures_are_reported_not_raised(self):
        patcher, _ = fake_claude([], stop_reason="refusal")
        with patcher:
            r = self.client.post(self.c.get_absolute_url(), {"action": "ask", "question": "Anything?"}, follow=True)
        self.assertContains(r, "Claude declined")
        patcher, _ = fake_claude([text_block("not json")])
        with patcher:
            r = self.client.post(self.c.get_absolute_url(), {"action": "find_dates"}, follow=True)
        self.assertContains(r, "couldn&#x27;t be read")
        empty = Contract.objects.create(title="Nothing yet")
        with self.assertRaises(ai.Unavailable):
            ai.ask(empty, "Anything?")
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaises(ai.Unavailable):
                ai.ask(self.c, "Anything?")


class RemindersTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("simon")
        self.c = Contract.objects.create(title="Kroc PSA", owner=self.user, status="signed")
        self.today = timezone.localdate()

    def add(self, days, contract=None, **kw):
        return ContractDate.objects.create(contract=contract or self.c, label=f"D{days}",
                                           date=self.today + timedelta(days=days), **kw)

    def test_thresholds_once_each_and_moved_dates(self):
        for days in (30, 10, 7, 1, 0, -3, 45):
            self.add(days)
        self.add(7, confirmed=False)
        self.add(7, done=True)
        self.add(7, contract=Contract.objects.create(title="Closed one", owner=self.user, status="closed"))
        notify.due_sweep(self.user)

        def notes():
            return Notification.objects.filter(user=self.user, kind="deadline")

        self.assertEqual(notes().count(), 6)
        texts = sorted(n.text for n in notes())
        self.assertIn("Kroc PSA: D0 today", texts)
        self.assertTrue(any(t.startswith("Kroc PSA: D-3 was due") and "(3 days ago)" in t for t in texts))
        self.assertTrue(any(t.startswith("Kroc PSA: D10 in 10 days") for t in texts))
        notify.due_sweep(self.user)
        self.assertEqual(notes().count(), 6)  # nothing twice
        moved = ContractDate.objects.get(label="D10")
        moved.date += timedelta(days=1)
        moved.save()
        dates.reminders(self.user)
        self.assertEqual(notes().count(), 7)  # a moved date is reminded again

    def test_unowned_contracts_remind_their_creator(self):
        joe = User.objects.create_user("joe")
        c = Contract.objects.create(title="Option", created_by=joe)
        self.add(1, contract=c)
        dates.reminders(joe)
        dates.reminders(self.user)
        self.assertEqual(Notification.objects.filter(kind="deadline").values_list("user__username", flat=True)
                         .get(), "joe")


class SiteTimelineTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("dave"))
        self.client.force_login(self.user)
        county = LandCounty.objects.create(fips="24017", name="Charles", state="MD")
        self.site = LandSite.objects.create(name="Outside the Gate", status="loi")
        self.p1 = make_parcel(county, "1", -77.0, 38.4, owner_name="WAMSLEY", acres=2.0)
        self.p2 = make_parcel(county, "2", -76.98, 38.4, owner_name="KROC DAVID", acres=1.0)
        self.site.parcels.add(self.p1, self.p2)
        from crm.land import tracker

        tracker.ensure_deals(self.site)
        self.c = Contract.objects.create(title="Assemblage PSA", kind="psa", site=self.site, status="negotiating")

    def post(self, **data):
        base = {"action": "details", "title": self.c.title, "kind": "psa", "status": self.c.status,
                "site": self.site.pk}
        return self.client.post(self.c.get_absolute_url(), {**base, **data})

    def test_signing_feasibility_and_closing_fill_the_site_timeline(self):
        ContractDocument.objects.create(contract=self.c, kind="draft", sent_by="us", title="Draft 1",
                                        dated=date(2026, 2, 1))
        self.post(status="signed", effective_date="2026-03-02")
        self.site.refresh_from_db()
        self.assertEqual(self.site.milestones, {"psa_sent": "2026-02-01", "psa_signed": "2026-03-02"})
        self.assertEqual(self.site.status, "contract")
        self.assertTrue(LandSiteEvent.objects.filter(site=self.site, text__contains="(from Assemblage PSA)").exists())
        self.client.post(self.c.get_absolute_url(), {"action": "date_add", "kind": "feasibility",
                                                     "date": "2026-06-30"})
        self.client.post(self.c.get_absolute_url(), {"action": "date_add", "kind": "closing", "date": "2026-07-30"})
        self.site.refresh_from_db()
        self.assertEqual(self.site.milestones["feasibility_end"], "2026-06-30")
        self.assertNotIn("closed", self.site.milestones)  # not closed yet
        self.c.refresh_from_db()
        self.post(status="closed", effective_date="2026-03-02")
        self.site.refresh_from_db()
        self.assertEqual((self.site.milestones["closed"], self.site.status), ("2026-07-30", "acquired"))

    def test_one_owners_contract_goes_on_their_parcel_deals(self):
        wamsley = SiteParcelDeal.objects.get(parcel=self.p1)
        page = self.client.get(self.c.get_absolute_url())
        self.assertContains(page, "Owners it covers")
        self.client.post(self.c.get_absolute_url(), {"action": "parcels", "deals": [str(wamsley.pk)]})
        self.post(status="signed", effective_date="2026-03-02")
        wamsley.refresh_from_db()
        self.site.refresh_from_db()
        self.assertEqual((wamsley.milestones.get("psa_signed"), wamsley.status), ("2026-03-02", "contract"))
        self.assertEqual(self.site.milestones, {})  # the site's own timeline is left alone
        self.assertEqual(SiteParcelDeal.objects.get(parcel=self.p2).milestones, {})
        other = LandSite.objects.create(name="Lake Acton", status="loi")
        self.post(status="signed", effective_date="2026-03-02", site=other.pk)
        self.assertFalse(self.c.parcel_deals.exists())  # moved to another site: the old owners drop off
        other.refresh_from_db()
        self.assertEqual(other.milestones.get("psa_signed"), "2026-03-02")

    def test_dropped_site_keeps_its_status(self):
        self.site.status = "dropped"
        self.site.save()
        self.post(status="signed", effective_date="2026-03-02")
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, "dropped")


class CampusAndTemplatesTests(TestCase):
    def setUp(self):
        self.user = make_manager(User.objects.create_user("john"))
        self.client.force_login(self.user)

    def test_campus_summary_lines(self):
        campus = Campus.objects.create(code="MEIH", name="Maryland Energetics Innovation Hub")
        site = LandSite.objects.create(name="Lake Acton", status="contract", campus=campus)
        c = Contract.objects.create(title="Acton PSA", kind="psa", site=site, status="signed", waiting_on="them")
        today = timezone.localdate()
        ContractDate.objects.create(contract=c, kind="feasibility", label="Feasibility ends",
                                    date=today + timedelta(days=5))
        item = campus_mod.summary(campus, today=today)["land"][0]
        self.assertTrue(item["facts"][0].startswith("Acton PSA (signed, ball with them); next date: Feasibility ends"))
        self.assertIn("Feasibility ends in 5 days (Acton PSA)", item["flags"])

    def test_builtins_copy_edit_delete(self):
        methods.ensure_builtins()
        methods.ensure_builtins()
        self.assertEqual(ContractTemplate.objects.filter(key="psa").count(), 1)
        builtin = ContractTemplate.objects.get(key="lease")
        page = self.client.get("/contracts/templates/")
        self.assertContains(page, "Lease abstract")
        r = self.client.post(f"/contracts/templates/{builtin.pk}/", {
            "name": "ACMI lease review", "description": "", "kinds": ["lease"], "instructions": "Short.",
            "points_text": "Rent: base rent and bumps\n- Delivery: dates and penalties\n\n"})
        self.assertRedirects(r, "/contracts/templates/")
        mine = ContractTemplate.objects.get(name="ACMI lease review")
        self.assertEqual((mine.key, mine.kinds, mine.points[1]), ("", ["lease"], {"label": "Delivery",
                                                                                  "ask": "dates and penalties"}))
        self.assertEqual(ContractTemplate.objects.get(pk=builtin.pk).name, "Lease abstract")  # built-in untouched
        self.client.force_login(User.objects.create_user("joe"))
        self.client.post(f"/contracts/templates/{mine.pk}/", {"action": "delete"})
        self.assertTrue(ContractTemplate.objects.filter(pk=mine.pk).exists())
        self.client.force_login(self.user)
        self.client.post(f"/contracts/templates/{mine.pk}/", {"action": "delete"})
        self.assertFalse(ContractTemplate.objects.filter(pk=mine.pk).exists())
