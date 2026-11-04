"""The phone layout (Kellen: "the interface on mobile is bad and needs to be fixed. Chat is blocking the view"): Chat's
list is a button in the top bar instead of a box pinned over the page, and the page keeps room below its end for any
folded boxes (app.css, dock.js)."""
from datetime import timedelta

from allauth.socialaccount.models import SocialAccount, SocialToken
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm.models import ChatConnection, PushSubscription

from .test_gmail_api import ME, ON

User = get_user_model()


@override_settings(**ON)
class PhoneTopBarTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("kellen", password="x", email=ME)
        account = SocialAccount.objects.create(user=self.user, provider="google", uid="1", extra_data={"email": ME})
        SocialToken.objects.create(account=account, token="a", token_secret="r",
                                   expires_at=timezone.now() + timedelta(hours=1))
        self.client.force_login(self.user)

    def test_the_top_bar_has_a_chat_button_only_when_chat_is_on(self):
        self.assertNotContains(self.client.get(reverse("crm:task_list")), "data-chat-toggle")
        ChatConnection.objects.create(user=self.user, enabled=True, google_email=ME)
        r = self.client.get(reverse("crm:task_list"))
        self.assertContains(r, 'class="appbar-icon chat-btn" data-chat-toggle')
        self.assertContains(r, 'data-chat-list-url=')  # the list it opens is on the page too

    def test_the_stylesheet_keeps_room_for_folded_boxes_on_a_phone(self):
        css = (settings.BASE_DIR / "crm" / "static" / "crm" / "app.css").read_text(encoding="utf-8")
        phone = css[css.index("the page keeps room below its end for any folded mail or chat boxes"):]
        self.assertIn("body.has-dock { padding-bottom: var(--dock-h, 0px); }", phone)

    def test_each_browser_on_the_notification_settings_carries_labels_for_the_stacked_phone_rows(self):
        PushSubscription.objects.create(user=self.user, endpoint="https://web.push.apple.com/x", p256dh="p", auth="a",
                                        device="Safari on iPhone")
        r = self.client.get(reverse("crm:notification_settings"))
        self.assertContains(r, '<td data-label="Since">')
        self.assertContains(r, '<td data-label="Last notification">')
