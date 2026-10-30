"""A name in an email opens who they are, and adds them to the directory (Kellen: "I want to be able to click an email
and [add] them to the [directory] directly. Anne is a land broker")."""
from datetime import timedelta
from unittest import mock

from allauth.socialaccount.models import SocialAccount, SocialToken
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.mail import gmail_api
from crm.models import Activity, Company, Firm, FirmEmail, MailConnection, Mailbox, Person, UnmatchedEmail

from .test_gmail_api import ME, ON, FakeGmail, api_message

User = get_user_model()
ANNE = "anne@landco.com"
FETCH = {"HTTP_X_REQUESTED_WITH": "fetch"}


class WhoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", email=ME)
        self.client.force_login(self.user)
        self.bank = Firm.objects.create(name="Shore United Bank", kind=Firm.Kind.BANK, domains=["shoreunitedbank.com"])
        self.company = Company.objects.create(name="Firehawk Aerospace", domains=["firehawkaerospace.com"])

    def card(self, email, name=""):
        return self.client.get(reverse("crm:who"), {"email": email, "name": name})

    def add(self, **fields):
        data = {"email": ANNE, "name": "Anne Hooper", **fields}
        return self.client.post(reverse("crm:who_add"), data, **FETCH)

    def test_unknown_person_gets_the_add_form_with_a_firm_named_after_the_domain(self):
        r = self.card(ANNE, "Anne Hooper")
        self.assertContains(r, "Add to the directory")
        self.assertContains(r, 'value="Landco"')
        self.assertContains(r, "A new firm gets landco.com")
        self.assertContains(r, '<option value="re_broker">Real estate broker</option>', html=True)
        self.assertContains(r, 'name="office_phone"')
        self.assertContains(r, 'name="cell_phone"')

    def test_adding_makes_the_firm_takes_the_domain_and_files_waiting_email(self):
        mailbox = Mailbox.objects.create(user=self.user, email=ME)
        UnmatchedEmail.objects.create(mailbox=mailbox, message_id="<1@x>", occurred_at=timezone.now(),
                                      kind=Activity.Kind.EMAIL_RECEIVED, address=ANNE, domain="landco.com",
                                      subject="Counter LOI")
        r = self.add(kind="re_broker", title="Land broker", office_phone="(410) 555-0100", cell_phone="(410) 555-0111")
        self.assertTrue(r.json()["ok"])
        firm = Firm.objects.get(name="Landco")
        self.assertEqual(firm.kind, Firm.Kind.RE_BROKER)
        self.assertEqual(firm.domains, ["landco.com"])
        self.assertEqual(firm.contacts, [{"name": "Anne Hooper", "title": "Land broker", "email": ANNE,
                                          "phone": "(410) 555-0100 (office), (410) 555-0111 (mobile)"}])
        self.assertEqual(firm.created_by, self.user)
        self.assertEqual(r.json()["url"], firm.get_absolute_url())
        self.assertIn("1 waiting email moved", r.json()["message"])
        self.assertFalse(UnmatchedEmail.objects.exists())
        self.assertEqual(FirmEmail.objects.get().provider, firm)
        # Now she's known: the card says where she is instead of offering the form again.
        r = self.card(ANNE)
        self.assertContains(r, "a contact at")
        self.assertNotContains(r, "Add to the directory")

    def test_a_named_firm_of_another_case_is_joined_not_duplicated(self):
        Firm.objects.create(name="Hooper Land Co", kind=Firm.Kind.OTHER)
        self.add(firm="hooper land co", kind="re_broker")
        firm = Firm.objects.get(name="Hooper Land Co")
        self.assertEqual(firm.kind, Firm.Kind.RE_BROKER)  # an "Other" firm takes the type picked
        self.assertEqual(len(firm.contacts), 1)
        self.assertEqual(Firm.objects.count(), 2)

    def test_a_firm_that_has_the_domain_gets_the_contact_and_keeps_its_type(self):
        r = self.card("pat@shoreunitedbank.com", "Pat Lee")
        self.assertContains(r, "is Shore United Bank")
        r = self.add(email="pat@shoreunitedbank.com", name="Pat Lee", kind="re_broker")
        self.assertTrue(r.json()["ok"])
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.kind, Firm.Kind.BANK)
        self.assertEqual(self.bank.domains, ["shoreunitedbank.com"])
        self.assertEqual([c["email"] for c in self.bank.contacts], ["pat@shoreunitedbank.com"])

    def test_adding_again_fills_blanks_only(self):
        self.add(firm="Landco", title="")
        self.add(firm="Landco", title="Land broker", office_phone="410")
        self.add(firm="Landco", title="Something else")
        contacts = Firm.objects.get(name="Landco").contacts
        self.assertEqual(len(contacts), 1)
        self.assertEqual((contacts[0]["title"], contacts[0]["phone"]), ("Land broker", "410 (office)"))

    def test_personal_email_needs_a_firm_name_and_never_ties_the_domain(self):
        r = self.add(email="anne.hooper@gmail.com")
        self.assertEqual(r.status_code, 400)
        self.assertIn("personal email", r.json()["error"])
        self.assertContains(self.card("anne.hooper@gmail.com"), "is personal email")
        self.add(email="anne.hooper@gmail.com", firm="Hooper Land")
        self.assertEqual(Firm.objects.get(name="Hooper Land").domains, [])

    def test_a_companys_domain_stays_with_the_company(self):
        self.assertContains(self.card("will@firehawkaerospace.com"), "the company Firehawk Aerospace")
        self.add(email="will@firehawkaerospace.com", name="Will Edwards", firm="Will's consulting")
        self.assertEqual(Firm.objects.get(name="Will's consulting").domains, [])
        self.company.refresh_from_db()
        self.assertEqual(self.company.domains, ["firehawkaerospace.com"])

    def test_acmi_addresses_are_not_offered_or_added(self):
        r = self.card("simon@acmigroup.com", "Simon")
        self.assertContains(r, "An ACMI address")
        self.assertNotContains(r, "Add to the directory")
        r = self.add(email="simon@acmigroup.com", firm="ACMI")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Firm.objects.filter(name="ACMI").exists())

    def test_a_person_in_the_crm_is_linked(self):
        p = Person.objects.create(full_name="Will Edwards", email="will@firehawkaerospace.com", company=self.company)
        r = self.card("will@firehawkaerospace.com")
        self.assertContains(r, "In the CRM")
        self.assertContains(r, p.get_absolute_url())

    def test_not_an_email_and_get_are_refused(self):
        self.assertEqual(self.add(email="not an address").status_code, 400)
        self.assertEqual(self.client.get(reverse("crm:who_add")).status_code, 405)
        self.assertFalse(Firm.objects.exclude(pk=self.bank.pk).exists())

    def test_without_javascript_the_form_redirects_back_with_a_message(self):
        r = self.client.post(reverse("crm:who_add"), {"email": ANNE, "name": "Anne Hooper", "next": "/inbox/?t=a1"})
        self.assertRedirects(r, "/inbox/?t=a1", fetch_redirect_response=False)
        r = self.client.post(reverse("crm:who_add"), {"email": ANNE, "name": "Anne", "next": "https://evil.example/"})
        self.assertRedirects(r, reverse("crm:directory"), fetch_redirect_response=False)


@override_settings(**ON)
class WhoInTheReaderTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("kellen", password="x", email=ME)
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1", extra_data={"email": ME})
        SocialToken.objects.create(account=account, token="a", token_secret="r",
                                   expires_at=timezone.now() + timedelta(hours=1))
        MailConnection.objects.create(user=self.user, enabled=True, google_email=ME)
        Mailbox.objects.create(user=self.user, email=ME, backend=Mailbox.Backend.GMAIL_API)
        self.fake = FakeGmail([
            api_message("m1", "a1", f"Kellen <{ME}>", f"Anne Hooper <{ANNE}>", "Rosewick Road: Counter LOI",
                        extra_headers=[{"name": "Cc", "value": "Dave Dowell <dave@acmigroup.com>"}]),
            api_message("m2", "a1", f"Anne Hooper <{ANNE}>", ME, "Re: Rosewick Road: Counter LOI",
                        when=timezone.now() - timedelta(minutes=5)),
        ])
        self.client.force_login(self.user)
        patcher = mock.patch.object(gmail_api, "requests", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_names_in_the_reading_pane_open_who_they_are(self):
        r = self.client.get(reverse("crm:inbox"), {"t": "a1"})
        self.assertContains(r, f'data-who="{ANNE}" data-name="Anne Hooper"', count=2)  # her message's From, and To
        self.assertContains(r, 'data-who="dave@acmigroup.com"')
        self.assertNotContains(r, f'data-who="{ME}"')  # yourself is "me"
        self.assertContains(r, reverse("crm:who"))
        self.assertContains(r, "crm/who.js")

    def test_the_inbox_list_shows_the_time_with_the_date(self):
        # Kellen: "The time of the email should be displayed along with the date."
        from crm.templatetags.crm_tags import mail_when

        r = self.client.get(reverse("crm:inbox"))
        row = r.context["threads"][0]
        self.assertContains(r, f'{mail_when(row.last_message_at)}</span>')
        self.assertRegex(mail_when(row.last_message_at), r"^[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [ap]\.m\.$")


class MailWhenTests(TestCase):
    def test_date_and_time_in_the_hubs_zone_with_the_year_only_when_it_differs(self):
        from datetime import datetime, timezone as dt_timezone

        from crm.templatetags.crm_tags import mail_when

        now = timezone.now()
        this_year = datetime(now.year, 3, 4, 19, 5, tzinfo=dt_timezone.utc)  # 1:05 p.m. in Chicago (CST)
        self.assertEqual(mail_when(this_year), "Mar 4, 1:05 p.m.")
        self.assertEqual(mail_when(this_year.replace(year=now.year - 1)), f"Mar 4, {now.year - 1}, 1:05 p.m.")
        self.assertEqual(mail_when(None), "")
