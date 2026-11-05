"""Mojibake in email text ("weâ€™re") is repaired; normal text is left alone (crm/mail/textfix.py)."""
import base64
from unittest import mock

from django.test import SimpleTestCase

from crm.mail import render, textfix
from crm.mail.gmail_api import parse_api_message


def garble(text):
    """What a mislabelled message looks like: UTF-8 bytes read as Windows-1252 (the five bytes it leaves undefined, such as
    0x9d in a closing quote, come through as the matching control character, as Windows itself does)."""
    out = []
    for b in text.encode("utf-8"):
        try:
            out.append(bytes([b]).decode("cp1252"))
        except UnicodeDecodeError:
            out.append(chr(b))
    return "".join(out)


class RepairTests(SimpleTestCase):
    def test_the_pasted_email_comes_out_right(self):
        bad = ("Thanks for reaching out. Mark and I will be traveling, but weâ€™re available Monday, November 30, "
               "or Tuesday, December 1, if youâ€™ll be in town.")
        good = textfix.repair(bad)
        self.assertEqual(good, "Thanks for reaching out. Mark and I will be traveling, but we’re available Monday, "
                               "November 30, or Tuesday, December 1, if you’ll be in town.")

    def test_quotes_dashes_ellipsis_accents_and_non_breaking_spaces(self):
        for original in ("“quoted” and ‘single’", "a – b — c", "wait… what", "café and Zoë and São Paulo", "5 PM",
                         "price €5 • done ™"):
            self.assertEqual(textfix.repair(garble(original)), original, original)

    def test_clean_text_is_untouched(self):
        for text in ("we’re fine", "café, São Paulo, Zoë, naïve", "Ã is a letter", "Â", "plain ascii", "", None,
                     "Smart quotes “ok” already right", "a€b"):
            self.assertEqual(textfix.repair(text), text)

    def test_a_run_that_is_not_valid_utf8_is_left_alone(self):
        odd = "â€ x"  # a three-byte lead with only one continuation: not valid UTF-8, so it stays as it is
        self.assertEqual(textfix.repair(odd), odd)

    def test_mixed_good_and_bad_in_one_message(self):
        mixed = "Good: we’re. Bad: " + garble("you’ll") + "."
        self.assertEqual(textfix.repair(mixed), "Good: we’re. Bad: you’ll.")


class ReadingPaneTests(SimpleTestCase):
    def part(self, text, charset):
        data = base64.urlsafe_b64encode(text.encode("utf-8")).decode()
        return data, {"headers": [{"name": "Content-Type", "value": f'text/plain; charset="{charset}"'}]}

    def test_a_message_labelled_windows_1252_but_written_in_utf8_reads_correctly(self):
        data, part = self.part("we’re available", "windows-1252")  # the sender's label is wrong
        self.assertEqual(render._decode(data, part), "we’re available")

    def test_a_correctly_labelled_message_is_unchanged(self):
        data, part = self.part("we’re available", "utf-8")
        self.assertEqual(render._decode(data, part), "we’re available")

    def test_the_synced_body_is_repaired_too(self):
        data = base64.urlsafe_b64encode("weâ€™re free Monday".encode("utf-8")).decode()  # already garbled upstream
        msg = {"id": "ab01", "threadId": "ab01", "labelIds": ["INBOX"], "internalDate": "1793800000000",
               "payload": {"mimeType": "text/plain", "body": {"data": data},
                           "headers": [{"name": "From", "value": "Luke <luke@x.com>"}, {"name": "To", "value": "d@y.com"},
                                       {"name": "Subject", "value": "Re: dates"}, {"name": "Message-ID", "value": "<a@x>"}]}}
        self.assertIn("we’re free Monday", parse_api_message(msg).body)
