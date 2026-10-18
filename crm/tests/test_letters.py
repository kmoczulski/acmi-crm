"""Flyer intake (Claude reads a broker flyer into the listing) and landowner letters (merge, labels, CSV, the log)."""
import json
import tempfile
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.html import escape

from crm.contracts.ai import Unavailable
from crm.land import briefs, flyer, letters, tracker
from crm.models import LandCounty, LandownerLetter, LandSite, LandSiteEvent, LetterTemplate
from crm.tests.test_contracts import fake_claude, text_block
from crm.tests.test_land import make_parcel
from crm.tests.utils import make_manager

User = get_user_model()

FOUND = {
    "source": "", "location": "SWC Burris Rd & SR 84, Casa Grande", "size": "±52 acres", "price": "Call for price",
    "taxes": "", "zoning_claim": "I-2 Industrial", "parcel_numbers": "503-26-020E", "concept_plan": "",
    "access": ["2 mi to I-10", " "], "highlights": [], "market": [], "nearby": ["Lucid Motors"], "documents": [],
    "utilities": [{"label": "Electric", "value": "APS 69 kV line on site"}, {"label": "Water", "value": "Global Water"}],
    "contacts": [{"name": "Pat Broker", "firm": "Land Advisors", "phone": "602-555-0100", "email": "pat@example.com",
                  "address": ""}],
    "drive_times": [{"place": "Phoenix Sky Harbor", "value": "45 min"}],
    "claims_check": [
        {"claim": "±52 acres", "found": "The CRM has 50.0 acres across 1 parcel", "status": "partly confirmed"},
        {"claim": "Zoned I-2", "found": "Parcel zoning I-2", "status": "confirmed"},
    ],
}
CURRENT = {"size": "±50 acres", "utilities": {"Electric": "SRP"},
           "claims_check": [{"claim": "zoned i-2", "found": "", "status": "not checked"}]}


class FlyerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rae", first_name="Rae")
        self.client.force_login(self.user)
        county = LandCounty.objects.create(fips="04021", name="Pinal", state="AZ")
        self.site = LandSite.objects.create(name="Casa Grande 52", listing=dict(CURRENT))
        self.site.parcels.add(make_parcel(county, "503-26-020E", -111.8, 32.9, acres=50.0, zoning="I-2"))
        tracker.ensure_deals(self.site)

    def test_merge_fills_blank_fields_only(self):
        merged, filled = flyer.merge(CURRENT, FOUND)
        self.assertEqual(merged["size"], "±50 acres")  # typed in already: kept
        self.assertEqual(merged["location"], "SWC Burris Rd & SR 84, Casa Grande")
        self.assertEqual(merged["access"], ["2 mi to I-10"])
        self.assertEqual(merged["utilities"], {"Electric": "SRP", "Water": "Global Water"})
        self.assertEqual(merged["contacts"], [{"name": "Pat Broker", "firm": "Land Advisors", "phone": "602-555-0100",
                                               "email": "pat@example.com"}])
        self.assertEqual(merged["drive_times"], [["Phoenix Sky Harbor", "45 min"]])
        self.assertEqual([c["claim"] for c in merged["claims_check"]], ["zoned i-2", "±52 acres"])  # no duplicate
        self.assertNotIn("Size", filled)
        self.assertIn("Utilities", filled)
        self.assertIn("1 claim checked", filled)
        again, more = flyer.merge(merged, FOUND)
        self.assertEqual((again, more), (merged, []))  # reading the same flyer twice changes nothing

    def test_read_sends_the_pdf_and_crm_facts(self):
        patcher, stream = fake_claude([text_block(json.dumps(FOUND))])
        with patcher, mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}):
            filled = flyer.read(self.site, b"%PDF-1.4 flyer", "Casa Grande flyer.pdf", self.user)
        self.assertIn("Location", filled)
        kwargs = stream.call_args.kwargs
        doc, facts = kwargs["messages"][0]["content"]
        self.assertEqual(doc["source"]["media_type"], "application/pdf")
        self.assertIn("FACTS FROM THE CRM", facts["text"])
        self.assertIn("zoning I-2", facts["text"])
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.site.refresh_from_db()
        self.assertEqual(self.site.listing["price"], "Call for price")
        self.assertIsNotNone(self.site.flyer_read_at)

    def test_images_go_as_images_and_other_files_are_refused(self):
        patcher, stream = fake_claude([text_block(json.dumps(FOUND))])
        with patcher, mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}):
            flyer.read(self.site, b"\x89PNG", "flyer.PNG")
            self.assertEqual(stream.call_args.kwargs["messages"][0]["content"][0]["type"], "image")
            with self.assertRaises(Unavailable):
                flyer.read(self.site, b"PK", "flyer.docx")

    def test_cut_off_or_broken_answers_change_nothing(self):
        for content, stop in (([text_block("{")], "max_tokens"), ([text_block("not json")], "end_turn")):
            patcher, _ = fake_claude(content, stop_reason=stop)
            with patcher, mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}), self.assertRaises(Unavailable):
                flyer.read(self.site, b"%PDF-1.4", "flyer.pdf")
        self.site.refresh_from_db()
        self.assertEqual(self.site.listing, CURRENT)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_upload_keeps_the_file_and_reads_it(self):
        url = f"/land/sites/{self.site.pk}/listing/"
        patcher, _ = fake_claude([text_block(json.dumps(FOUND))])
        with patcher, mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}):
            resp = self.client.post(url, {"action": "flyer", "flyer": SimpleUploadedFile("flyer.pdf", b"%PDF-1.4 x")})
        self.assertRedirects(resp, url)
        self.site.refresh_from_db()
        self.assertTrue(self.site.flyer.name.endswith(".pdf"))
        self.assertEqual(self.site.listing["location"], "SWC Burris Rd & SR 84, Casa Grande")
        self.assertTrue(LandSiteEvent.objects.filter(site=self.site, text__contains="filled from the flyer").exists())
        page = self.client.get(url)
        self.assertContains(page, f"/land/sites/{self.site.pk}/flyer/")
        file = self.client.get(f"/land/sites/{self.site.pk}/flyer/")
        self.assertTrue(file["Content-Disposition"].startswith("inline"))
        self.assertEqual(b"".join(file.streaming_content), b"%PDF-1.4 x")  # read it out; closing ends the DB link

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_upload_without_claude_still_keeps_the_flyer(self):
        url = f"/land/sites/{self.site.pk}/listing/"
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            self.client.post(url, {"action": "flyer", "flyer": SimpleUploadedFile("flyer.jpg", b"\xff\xd8")})
            self.assertContains(self.client.get(url), "Add ANTHROPIC_API_KEY")
        self.site.refresh_from_db()
        self.assertTrue(self.site.flyer)
        self.assertEqual(self.site.listing, CURRENT)
        self.assertIsNone(self.site.flyer_read_at)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_only_pdfs_and_images_are_kept(self):
        url = f"/land/sites/{self.site.pk}/listing/"
        resp = self.client.post(url, {"action": "flyer", "flyer": SimpleUploadedFile("flyer.html", b"<script>")},
                                follow=True)
        self.assertContains(resp, "Upload the flyer as a PDF or an image")
        self.site.refresh_from_db()
        self.assertFalse(self.site.flyer)

    def test_no_flyer_is_a_404(self):
        self.assertEqual(self.client.get(f"/land/sites/{self.site.pk}/flyer/").status_code, 404)


class LetterBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kai", first_name="Kai", last_name="Ames", email="kai@acmigroup.com")
        self.client.force_login(self.user)
        county = LandCounty.objects.create(fips="48251", name="Johnson", state="TX")
        self.site = LandSite.objects.create(name="Mansfield 180", status="review")
        mail = dict(care_of="C/O JOHN SMITH", mail_city="MANSFIELD", mail_state="TX", mail_zip="760631234")
        self.p1 = make_parcel(county, "R100", -97.1, 32.5, owner_name="SMITH FAMILY TRUST LLC ETAL", acres=100.0,
                              legal_acres=101.5, mail_address="PO BOX 12", situs_address="1234 FM 1187",
                              situs_city="MANSFIELD", **mail)
        self.p2 = make_parcel(county, "R101", -97.08, 32.5, owner_name="SMITH FAMILY TRUST LLC ETAL", acres=20.0,
                              mail_address="P.O. Box 12", legal_desc="ABST 12 J SMITH SURVEY TR 4", **mail)
        self.p3 = make_parcel(county, "R200", -97.06, 32.5, owner_name="JONES MARY", acres=50.0,
                              mail_address="77 ELM ST", mail_city="FORT WORTH", mail_state="TX", mail_zip="76102",
                              situs_address="900 COUNTY ROAD 401")
        self.p4 = make_parcel(county, "R300", -97.04, 32.5, owner_name="DOE JOHN", acres=10.0)
        self.p5 = make_parcel(county, "R400", -97.02, 32.5, owner_name="LEFT OUT LP", acres=5.0,
                              mail_address="1 MAIN ST", mail_city="CLEBURNE", mail_state="TX", mail_zip="76031")
        self.site.parcels.add(self.p1, self.p2, self.p3, self.p4, self.p5)
        tracker.ensure_deals(self.site)
        self.site.parcel_deals.filter(parcel=self.p5).update(included=False)
        self.url = f"/land/sites/{self.site.pk}/letters/"
        self.d1 = timezone.localdate() - timedelta(days=20)
        self.sender = {"sender": "Kai Ames", "sender_title": "Associate", "sender_phone": "(817) 555-0100",
                       "sender_email": "kai@acmigroup.com"}

    def rows(self):
        return {r["name"]: r for r in letters.recipients(self.site)}

    def post(self, action, to, **extra):
        data = {"action": action, "body": letters.BUILTINS[0]["body"], "date": "2026-10-16", "to": to,
                **self.sender, **extra}
        return self.client.post(self.url, data)


class RecipientTests(LetterBase):
    def test_one_row_per_owner_and_address(self):
        rows = letters.recipients(self.site)
        self.assertEqual([r["name"] for r in rows], ["Smith Family Trust LLC et al", "Jones Mary", "Doe John"])
        smith = rows[0]
        self.assertEqual({p.pk for p in smith["parcels"]}, {self.p1.pk, self.p2.pk})  # "PO BOX" = "P.O. Box"
        self.assertEqual(smith["acres"], 121.5)  # deeded acres where the roll has them
        self.assertEqual((smith["care_of"], smith["zip"]), ("JOHN SMITH", "76063-1234"))
        self.assertFalse(rows[2]["mailable"])  # no mailing address on the roll
        self.assertNotIn("Left Out LP", [r["name"] for r in rows])  # excluded parcel

    def test_names_read_like_names(self):
        for raw, nice in (("SMITH FAMILY TRUST LLC ETAL", "Smith Family Trust LLC et al"),
                          ("O'NEIL MARY & JOHN ET AL", "O'Neil Mary & John et al"),
                          ("BANK OF AMERICA NA", "Bank of America NA"), ("THE SMITH TRUST", "The Smith Trust"),
                          ("123 N 1ST ST", "123 N 1st St"), ("RR 2 BOX 15", "RR 2 Box 15"),
                          ("MUÑOZ ROSA", "Muñoz Rosa"), ("Already Typed by Hand", "Already Typed by Hand"),
                          ("", "")):
            self.assertEqual(letters.nice(raw), nice)

    def test_address_blocks(self):
        smith = self.rows()["Smith Family Trust LLC et al"]
        self.assertEqual(letters.address_lines(smith), ["SMITH FAMILY TRUST LLC ETAL", "C/O JOHN SMITH", "PO BOX 12",
                                                        "MANSFIELD, TX 76063-1234"])
        self.assertEqual(letters.address_lines(smith, title=True)[1:], ["c/o John Smith", "PO Box 12",
                                                                        "Mansfield, TX 76063-1234"])

    def test_merge_fields(self):
        smith = self.rows()["Smith Family Trust LLC et al"]
        text = letters.build(self.site, [smith], letters.BUILTINS[0]["body"], self.sender, date(2026, 10, 16),
                             {smith["key"]: "Mr. Smith"})[0]["text"]
        self.assertTrue(text.startswith("Dear Mr. Smith,"))
        self.assertIn("your land in Johnson County, 1234 FM 1187, Mansfield and Abst 12 J Smith Survey Tr 4 "
                      "(121.5 acres, parcels R100 and R101)", text)
        self.assertIn("call me at (817) 555-0100 or email me at kai@acmigroup.com", text)
        self.assertNotIn("{", text)
        self.assertEqual(letters.merge("{oops} { x } {owner}", {"owner": "Ann"}), "{oops} { x } Ann")

    def test_problems_catch_typos_and_blank_sender_details(self):
        self.assertEqual(letters.problems(letters.BUILTINS[0]["body"], self.sender), [])
        self.assertIn("{ownr}", letters.problems("Dear {ownr},", self.sender)[0])
        blank = letters.problems("Call {sender_phone} or {sender_email}", {**self.sender, "sender_phone": ""})
        self.assertEqual(blank, ["The letter uses phone, but it is blank."])

    def test_labels_and_csv(self):
        rows = [r for r in letters.recipients(self.site) if r["mailable"]]
        sheets = letters.label_sheets(rows, start=3)
        self.assertEqual(sheets[0][:2], [None, None])  # the first two labels on the sheet were used already
        self.assertEqual(sheets[0][2]["lines"][0], "SMITH FAMILY TRUST LLC ETAL")
        self.assertEqual(len(letters.label_sheets(rows * 20)), 2)  # 40 labels: two sheets
        csv_text = letters.mail_csv(rows, {rows[1]["key"]: "Mrs. Jones"})
        self.assertTrue(csv_text.startswith("﻿Name,Care of,Address"))
        self.assertIn("SMITH FAMILY TRUST LLC ETAL,JOHN SMITH,PO BOX 12,MANSFIELD,TX,76063-1234", csv_text)
        self.assertIn("Mrs. Jones,R200,50,Johnson", csv_text)
        self.assertEqual(letters._cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")


class LetterPageTests(LetterBase):
    def test_page_lists_owners_and_built_in_letters(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Smith Family Trust LLC et al")
        self.assertContains(page, "No mailing address on the appraisal roll")
        self.assertContains(page, "(built in)")
        self.assertEqual(LetterTemplate.objects.exclude(key="").count(), len(letters.BUILTINS))
        self.client.get(self.url)
        self.assertEqual(LetterTemplate.objects.count(), len(letters.BUILTINS))  # kept in step, never duplicated

    def test_pdf_letters(self):
        keys = [r["key"] for r in letters.recipients(self.site)]
        with mock.patch.object(briefs, "html_to_pdf", return_value=b"%PDF-letters") as pdf:
            resp = self.post("pdf", keys)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn('filename="ACMI_Letters_Mansfield_180_', resp["Content-Disposition"])
        html = pdf.call_args.args[0]
        self.assertEqual(html.count('class="letter"'), 2)  # Doe John has no address
        self.assertIn("Dear Jones Mary,", html)
        self.assertIn("October 16, 2026", html)
        self.assertIn("data:image/png;base64,", html)  # the ACMI logo, inline

    def test_without_chrome_the_letters_come_as_a_page_to_print(self):
        keys = [r["key"] for r in letters.recipients(self.site)]
        with mock.patch.object(briefs, "html_to_pdf", side_effect=briefs.PdfUnavailable("no Chrome")):
            resp = self.post("pdf", keys)
        self.assertContains(resp, escape("Couldn't make the PDF (no Chrome)"))
        self.assertContains(resp, "Dear Smith Family Trust LLC et al,")
        printed = self.post("print", keys)
        self.assertContains(printed, 'class="letter"', count=2)

    def test_labels_and_csv_downloads(self):
        keys = [r["key"] for r in letters.recipients(self.site)]
        with mock.patch.object(briefs, "html_to_pdf", return_value=b"%PDF-labels") as pdf:
            resp = self.post("labels", keys, start="4")
        self.assertIn("ACMI_Labels_Mansfield_180_", resp["Content-Disposition"])
        self.assertEqual(pdf.call_args.args[0].count('class="label'), 5)  # three used labels skipped, then two
        resp = self.post("csv", keys)
        self.assertEqual(resp["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("JONES MARY", resp.content.decode("utf-8-sig"))

    def test_mistakes_come_back_with_the_edits_kept(self):
        resp = self.post("pdf", [], body="Dear {owner}, my own words")
        self.assertContains(resp, "Tick at least one owner")
        self.assertContains(resp, "my own words")
        resp = self.post("pdf", [self.rows()["Jones Mary"]["key"]], body="Dear {ownr},")
        self.assertContains(resp, escape("merge fields that don't exist: {ownr}"))

    def test_logging_dates_owner_contacted_and_moves_the_deal_on(self):
        rows = self.rows()
        keys = [rows["Smith Family Trust LLC et al"]["key"], rows["Jones Mary"]["key"]]
        resp = self.post("sent", keys, method="certified", sent_on=self.d1.isoformat(), notes="Receipts in the folder",
                         **{"dear_" + keys[1]: "Mrs. Jones"})
        self.assertRedirects(resp, self.url)
        sent = {x.owner_name: x for x in LandownerLetter.objects.filter(site=self.site)}
        self.assertEqual(set(sent), {"Smith Family Trust LLC et al", "Jones Mary"})
        jones = sent["Jones Mary"]
        self.assertTrue(jones.body.startswith("Dear Mrs. Jones,"))
        self.assertEqual((jones.method, jones.sent_on, jones.sent_by), ("certified", self.d1, self.user))
        self.assertEqual(jones.parcels, [self.p3.pk])
        self.assertIn("77 Elm St", jones.mailing)
        deals = {d.parcel_id: d for d in self.site.parcel_deals.all()}
        for p in (self.p1, self.p2, self.p3):
            self.assertEqual((deals[p.pk].milestones.get("contacted"), deals[p.pk].status),
                             (self.d1.isoformat(), "outreach"))
        self.assertNotIn("contacted", deals[self.p4.pk].milestones)  # no letter went to Doe
        self.site.refresh_from_db()
        self.assertEqual((self.site.milestones.get("contacted"), self.site.status), (self.d1.isoformat(), "outreach"))
        event = LandSiteEvent.objects.get(site=self.site, kind="letter")
        self.assertEqual(event.text, f"Letters sent (certified mail, {self.d1:%b} {self.d1.day}) to Smith Family Trust "
                                     "LLC et al and Jones Mary")

        self.post("sent", keys, method="certified", sent_on=self.d1.isoformat())  # the same batch twice: logged once
        self.assertEqual(LandownerLetter.objects.filter(site=self.site).count(), 2)
        earlier, later = self.d1 - timedelta(days=16), self.d1 + timedelta(days=9)
        self.post("sent", [keys[1]], method="mail", sent_on=earlier.isoformat())  # an earlier letter, logged late
        self.post("sent", [keys[1]], method="mail", sent_on=later.isoformat())
        deal = self.site.parcel_deals.get(parcel=self.p3)
        self.assertEqual(deal.milestones["contacted"], earlier.isoformat())  # the first contact counts
        self.site.refresh_from_db()
        self.assertEqual(self.site.milestones["contacted"], earlier.isoformat())
        self.assertEqual(self.rows()["Jones Mary"]["letters"][0].sent_on, later)

    def test_no_future_dates_and_a_real_method(self):
        key = self.rows()["Jones Mary"]["key"]
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertContains(self.post("sent", [key], method="mail", sent_on=tomorrow), escape("can't be in the future"))
        self.assertContains(self.post("sent", [key], method="pigeon"), "Pick how the letters went out")
        self.assertFalse(LandownerLetter.objects.exists())

    def test_taking_a_letter_out_of_the_log(self):
        self.post("sent", [self.rows()["Jones Mary"]["key"]], method="mail", sent_on=self.d1.isoformat())
        letter = LandownerLetter.objects.get()
        other = User.objects.create_user("lee")
        self.client.force_login(other)
        self.client.post(self.url, {"action": "delete_letter", "letter": letter.pk})
        self.assertTrue(LandownerLetter.objects.exists())  # not theirs, and not a manager
        junk = self.client.post(self.url, {"action": "delete_letter", "letter": "x"})
        self.assertEqual(junk.status_code, 404)  # not a crash
        self.client.force_login(make_manager(other))
        self.client.post(self.url, {"action": "delete_letter", "letter": letter.pk})
        self.assertFalse(LandownerLetter.objects.exists())
        self.assertEqual(self.site.parcel_deals.get(parcel=self.p3).milestones["contacted"], self.d1.isoformat())

    def test_save_a_template_then_reprint_a_logged_letter(self):
        resp = self.post("save_template", [], body="Dear {owner}, a letter of our own.", template_name="Our letter")
        mine = LetterTemplate.objects.get(key="", name="Our letter")
        self.assertRedirects(resp, f"{self.url}?template={mine.pk}")
        self.assertContains(self.client.get(f"{self.url}?template={mine.pk}"), "a letter of our own.")
        self.post("save_template", [], body="Dear {owner}, better words.", template_name="our letter")
        mine.refresh_from_db()
        self.assertEqual((LetterTemplate.objects.filter(key="").count(), mine.body), (1, "Dear {owner}, better words."))

        self.post("sent", [self.rows()["Jones Mary"]["key"]], method="hand", sent_on=self.d1.isoformat(),
                  template=str(mine.pk), body=mine.body)
        letter = LandownerLetter.objects.get()
        self.assertEqual(letter.template, mine)
        page = self.client.get(f"{self.url}?print={letter.pk}")
        self.assertContains(page, "Dear Jones Mary, better words.")
        self.assertContains(page, f"As logged: sent {letters.long_date(self.d1)} (hand-delivered).")

    def test_site_page_shows_the_letters(self):
        site_page = f"/land/sites/{self.site.pk}/manage/"
        self.assertContains(self.client.get(site_page), "Landowner letters")
        self.post("sent", [self.rows()["Jones Mary"]["key"]], method="mail", sent_on=self.d1.isoformat())
        self.assertContains(self.client.get(site_page),
                            f"1</b> letter sent · last {self.d1:%b} {self.d1.day} to Jones Mary")
