"""The capital partners' data room (crm/dataroom/): partner accounts reach only their room, see only the allowed
fields of the deals switched on, download only approved files, and never count as part of the team."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import notify
from crm.dataroom import logic
from crm.forms import active_users
from crm.models import (
    Campus, Company, DataRoom, DataRoomFile, DataRoomLoginAttempt, DataRoomMember, DataRoomView, Document, Firm,
    Opportunity,
)
from crm.permissions import team_users

from .utils import make_manager

User = get_user_model()
PASSWORD = "harbor-lantern-quietly-71"


class DataRoomTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.kellen = make_manager(User.objects.create_user("Kellen", "kmoczulski@acmigroup.com", "x",
                                                            first_name="Kellen", last_name="Moczulski"))
        self.meih = Campus.objects.create(code="MEIH", name="MEIH")
        self.room = DataRoom.objects.create(name="Fortress", partner=Firm.objects.create(name="Fortress", kind="investor"))

        def deal(name, **kw):
            fields = {"campus": self.meih, "stage": "loi_signed", "investor_report": True, "value": 25_000_000,
                      "sq_ft_need": 50_000, "probability": 80, "notes": f"INTERNAL NOTE {name}",
                      "next_step": f"INTERNAL NEXT STEP {name}", "lead_source": f"INTERNAL LEAD {name}",
                      "owner": self.kellen, **kw}
            return Opportunity.objects.create(name=f"{name} · MEIH", company=Company.objects.create(
                name=name, notes=f"INTERNAL COMPANY NOTE {name}", website="ara.com", sector="Defense"), **fields)

        self.ara = deal("ARA", partner_note="A 25-year lease under negotiation.")
        self.spinner = deal("Spinner")
        self.off_report = deal("Hidden Co", investor_report=False)
        self.cold = deal("Cold Co", stage="lost")
        member = DataRoomMember.objects.create(room=self.room, name="Pat Investor", email="pat@fortress.com")
        link = logic.issue_invite(member, self.kellen)
        token = link.rstrip("/").rsplit("/", 1)[-1]
        r = self.client.post(link, {"first_name": "Pat", "last_name": "Investor", "password1": PASSWORD,
                                    "password2": PASSWORD})
        self.assertRedirects(r, reverse("crm:room_home"))
        self.member = DataRoomMember.objects.get(pk=member.pk)
        self.partner = self.member.user
        self.token = token


class PartnerAccountTests(DataRoomTestCase):
    def test_invite_makes_a_partner_account_and_works_once(self):
        self.assertEqual(self.partner.username, "pat@fortress.com")
        self.assertTrue(self.partner.groups.filter(name="Partners").exists())
        self.assertFalse(self.partner.is_staff)
        self.assertFalse(self.member.invite_hash)
        self.client.logout()
        r = self.client.get(reverse("crm:room_invite", args=[self.token]))
        self.assertEqual(r.status_code, 404)

    def test_expired_or_weak_invites_are_refused(self):
        m = DataRoomMember.objects.create(room=self.room, name="Lee", email="lee@fortress.com")
        link = logic.issue_invite(m, self.kellen)
        self.client.logout()
        r = self.client.post(link, {"first_name": "Lee", "last_name": "L", "password1": "short", "password2": "short"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(DataRoomMember.objects.get(pk=m.pk).user)
        DataRoomMember.objects.filter(pk=m.pk).update(invite_expires=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(link).status_code, 404)

    def test_partners_are_never_part_of_the_team(self):
        self.assertNotIn(self.partner, team_users())
        self.assertNotIn(self.partner, active_users())
        self.assertEqual(set(notify.mentioned_users("@pat please look")), set())
        from crm.mail.sync import Recorder

        self.assertIsNone(Recorder().teammate("pat@fortress.com"))  # their mail is a partner's, not the team's

    def test_team_sign_in_refuses_partners_and_partner_sign_in_refuses_the_team(self):
        from crm.auth import PartnerBackend, PasswordBackend

        self.assertIsNone(PasswordBackend().authenticate(None, username="pat@fortress.com", password=PASSWORD))
        self.assertEqual(PartnerBackend().authenticate(None, username="pat@fortress.com", password=PASSWORD), self.partner)
        self.assertIsNone(PartnerBackend().authenticate(None, username="Kellen", password="x"))
        self.client.logout()
        r = self.client.post(reverse("crm:room_login"), {"email": "Kellen", "password": "x"})
        self.assertContains(r, "match an active data room account")

    @override_settings(LOCAL_LOGIN_ENABLED=False)
    def test_partners_sign_in_even_with_team_passwords_off(self):
        self.client.logout()
        r = self.client.post(reverse("crm:room_login"), {"email": "pat@fortress.com", "password": PASSWORD})
        self.assertRedirects(r, reverse("crm:room_home"))

    def test_wrong_passwords_lock_the_account_for_a_while(self):
        self.client.logout()
        for _ in range(logic.MAX_FAILURES):
            self.client.post(reverse("crm:room_login"), {"email": "pat@fortress.com", "password": "nope"})
        r = self.client.post(reverse("crm:room_login"), {"email": "pat@fortress.com", "password": PASSWORD})
        self.assertContains(r, "Too many attempts")

    def test_the_lockout_is_kept_in_the_database_not_a_process_cache(self):
        """Cloud Run runs several worker processes and instances; a strike counted in one must count in all."""
        self.client.logout()
        url = reverse("crm:room_login")
        for _ in range(logic.MAX_FAILURES):
            self.client.post(url, {"email": "pat@fortress.com", "password": "nope"})
        self.assertEqual(DataRoomLoginAttempt.objects.filter(username="pat@fortress.com").count(), logic.MAX_FAILURES)
        cache.clear()  # another process, or a restart: its cache knows nothing
        self.assertTrue(logic.locked("pat@fortress.com", "10.9.9.9"))  # the account is locked from anywhere
        self.assertContains(self.client.post(url, {"email": "pat@fortress.com", "password": PASSWORD}), "Too many attempts")
        # A right password once the window has passed clears the strikes.
        DataRoomLoginAttempt.objects.update(at=timezone.now() - timedelta(seconds=logic.FAILURE_WINDOW + 1))
        r = self.client.post(url, {"email": "pat@fortress.com", "password": PASSWORD})
        self.assertRedirects(r, reverse("crm:room_home"))
        self.assertEqual(DataRoomLoginAttempt.objects.count(), 0)

    @override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
    def test_behind_the_proxy_the_address_lock_follows_the_client_not_the_proxy(self):
        """In production every request reaches the app from Cloud Run's front end, so REMOTE_ADDR is the same for
        everyone; counting strikes on it would let five wrong passwords from anyone lock every partner out."""
        self.client.logout()
        url = reverse("crm:room_login")
        for n in range(logic.MAX_FAILURES):  # one address guessing at many accounts
            self.client.post(url, {"email": f"guess{n}@fortress.com", "password": "nope"}, HTTP_X_FORWARDED_FOR="198.51.100.7")
        self.assertTrue(logic.locked("", "198.51.100.7"))
        self.assertFalse(logic.locked("", "127.0.0.1"))  # the proxy's own address isn't what was counted
        r = self.client.post(url, {"email": "pat@fortress.com", "password": PASSWORD}, HTTP_X_FORWARDED_FOR="198.51.100.7")
        self.assertContains(r, "Too many attempts")
        r = self.client.post(url, {"email": "pat@fortress.com", "password": PASSWORD}, HTTP_X_FORWARDED_FOR="198.51.100.8")
        self.assertRedirects(r, reverse("crm:room_home"))  # everyone else still gets in

    def test_switching_a_member_off_ends_their_access_at_once(self):
        logic.switch_member(self.member, False)
        r = self.client.get(reverse("crm:room_home"))
        self.assertNotEqual(r.status_code, 200)
        self.client.logout()
        r = self.client.post(reverse("crm:room_login"), {"email": "pat@fortress.com", "password": PASSWORD})
        self.assertContains(r, "match an active data room account")


class PartnerConfinementTests(DataRoomTestCase):
    def test_every_internal_page_sends_a_partner_back_to_the_room(self):
        doc = Document.objects.create(opportunity=self.ara, title="Internal memo",
                                      file=SimpleUploadedFile("memo.pdf", b"%PDF secret"))
        for url in ["/", "/companies/", reverse("crm:opportunity_board"), self.ara.get_absolute_url(),
                    self.ara.company.get_absolute_url(), "/search/?q=ARA", "/boards/", "/admin/",
                    reverse("crm:document_file", args=[doc.pk]), reverse("crm:data_rooms"),
                    reverse("crm:data_room_manage", args=[self.room.pk])]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertEqual(r["Location"], "/room/", url)

    def test_signed_out_room_pages_go_to_the_room_sign_in(self):
        self.client.logout()
        r = self.client.get(reverse("crm:room_home"))
        self.assertTrue(r["Location"].startswith("/room/login/"))


class PartnerViewTests(DataRoomTestCase):
    def test_pipeline_shows_only_switched_on_report_deals_and_allowed_columns(self):
        r = self.client.get(reverse("crm:room_home"))
        self.assertContains(r, "ARA")
        self.assertContains(r, "Spinner")
        self.assertNotContains(r, "Hidden Co")  # not on the investor report
        self.assertNotContains(r, "Cold Co")  # cold / lost never shows
        self.assertContains(r, "$25,000,000")  # costs on by default
        self.assertNotContains(r, "Win %")  # win % off
        self.assertNotContains(r, "80%")
        for secret in ("INTERNAL", "Kellen", "Lead / source", "Next step"):
            self.assertNotContains(r, secret)

    def test_switching_a_deal_off_hides_it_and_its_page(self):
        logic.set_shown(self.room, self.spinner, False, self.kellen)
        self.assertNotContains(self.client.get(reverse("crm:room_home")), "Spinner")
        self.assertEqual(self.client.get(reverse("crm:room_deal", args=[self.spinner.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("crm:room_deal", args=[self.off_report.pk])).status_code, 404)

    def test_company_card_has_the_partner_note_and_nothing_internal(self):
        r = self.client.get(reverse("crm:room_deal", args=[self.ara.pk]))
        self.assertContains(r, "A 25-year lease under negotiation.")
        self.assertContains(r, "https://ara.com")
        for secret in ("INTERNAL", "Kellen", "/companies/", "/opportunities/"):
            self.assertNotContains(r, secret)
        self.assertTrue(DataRoomView.objects.filter(member=self.member, page="deal", opportunity=self.ara).exists())

    def test_only_approved_files_download_and_each_is_logged(self):
        doc = Document.objects.create(opportunity=self.ara, title="Site plan",
                                      file=SimpleUploadedFile("plan.pdf", b"%PDF plan"))
        other = Document.objects.create(opportunity=self.ara, title="Internal memo",
                                        file=SimpleUploadedFile("memo.pdf", b"%PDF secret"))
        self.assertNotContains(self.client.get(reverse("crm:room_deal", args=[self.ara.pk])), "Site plan")
        shared = DataRoomFile.objects.create(room=self.room, document=doc, opportunity=self.ara, title=doc.title)
        r = self.client.get(reverse("crm:room_deal", args=[self.ara.pk]))
        self.assertContains(r, "Site plan")
        self.assertNotContains(r, "Internal memo")
        r = self.client.get(reverse("crm:room_file", args=[shared.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"%PDF plan")
        self.assertTrue(DataRoomView.objects.filter(member=self.member, page="file", file=shared).exists())
        other_room = DataRoom.objects.create(name="Other")
        stranger = DataRoomFile.objects.create(room=other_room, document=other, title="x")
        self.assertEqual(self.client.get(reverse("crm:room_file", args=[stranger.pk])).status_code, 404)
        logic.set_shown(self.room, self.ara, False, self.kellen)
        self.assertEqual(self.client.get(reverse("crm:room_file", args=[shared.pk])).status_code, 404)


class ManagerTests(DataRoomTestCase):
    def setUp(self):
        super().setUp()
        self.client.logout()
        self.client.force_login(self.kellen)

    def test_preview_shows_the_partners_view_and_isnt_logged(self):
        before = DataRoomView.objects.count()
        r = self.client.get(reverse("crm:room_home") + f"?room={self.room.pk}")
        self.assertContains(r, "Preview: this is exactly what Fortress sees")
        self.assertEqual(DataRoomView.objects.count(), before)

    def test_a_non_manager_cant_run_or_preview_the_room(self):
        simon = User.objects.create_user("Simon", password="x")
        self.client.force_login(simon)
        self.assertNotEqual(self.client.get(reverse("crm:data_room_manage", args=[self.room.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("crm:room_home")).status_code, 404)

    def test_manage_page_switches_deals_approves_documents_and_invites(self):
        url = reverse("crm:data_room_manage", args=[self.room.pk])
        self.assertContains(self.client.get(url), "Spinner")
        self.client.post(url, {"action": "deal", "opportunity": self.spinner.pk, "shown": "0"})
        self.assertFalse(logic.visible_deals(self.room).filter(pk=self.spinner.pk).exists())
        doc = Document.objects.create(opportunity=self.ara, title="Site plan", url="https://drive.google.com/x")
        self.client.post(url, {"action": "approve_document", "document": doc.pk})
        self.assertTrue(DataRoomFile.objects.filter(room=self.room, document=doc).exists())
        hidden_doc = Document.objects.create(opportunity=self.off_report, title="Nope", url="https://drive.google.com/y")
        self.assertEqual(self.client.post(url, {"action": "approve_document", "document": hidden_doc.pk}).status_code, 404)
        r = self.client.post(url, {"action": "invite", "name": "Sam", "email": "sam@fortress.com"})
        self.assertContains(r, "/room/invite/")
        r = self.client.post(url, {"action": "invite", "name": "Me", "email": "kmoczulski@acmigroup.com"}, follow=True)
        self.assertContains(r, "belongs to someone on the ACMI team")
