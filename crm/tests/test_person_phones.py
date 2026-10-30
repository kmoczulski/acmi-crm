"""Office and cell phone boxes on the person form. They save as "number (office)" / "number (mobile)", the labels
crm/mail/signature_phones.py already writes, so the card and the signature import keep working."""
from django.test import TestCase

from crm.forms import PersonForm
from crm.models import Person


class PersonPhoneFormTests(TestCase):
    def test_labelled_numbers_show_in_their_boxes(self):
        p = Person.objects.create(first_name="Will", phones=["301-555-0100 x204 (office)", "301-555-0142 (mobile)", "215-555-0199"])
        form = PersonForm(instance=p)
        self.assertEqual(form["office_phone"].value(), "301-555-0100 x204")
        self.assertEqual(form["cell_phone"].value(), "301-555-0142")
        self.assertEqual(form["phones"].value(), "215-555-0199")

    def test_boxes_save_with_labels_and_keep_other_numbers(self):
        p = Person.objects.create(first_name="Will", phones=["215-555-0199"])
        form = PersonForm(
            {"first_name": "Will", "office_phone": " 301-555-0100 x204 ", "cell_phone": "301-555-0142", "phones": "215-555-0199"},
            instance=p,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        p.refresh_from_db()
        self.assertEqual(p.phones, ["215-555-0199", "301-555-0100 x204 (office)", "301-555-0142 (mobile)"])

    def test_empty_boxes_add_nothing(self):
        p = Person.objects.create(first_name="Will", phones=[])
        form = PersonForm({"first_name": "Will", "office_phone": "", "cell_phone": "  "}, instance=p)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        p.refresh_from_db()
        self.assertEqual(p.phones, [])
