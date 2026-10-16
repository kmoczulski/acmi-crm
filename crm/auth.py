"""Sign-in rules: Google accounts on ACMI's domain only; no self sign-up."""
from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.http import HttpResponseForbidden


def email_allowed(email):
    domain = (email or "").rsplit("@", 1)[-1].lower()
    return domain in [d.lower() for d in settings.ALLOWED_EMAIL_DOMAINS]


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # Username accounts are created by an admin, never by sign-up.
        return False


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        email = sociallogin.user.email or ""
        verified = any(e.verified for e in sociallogin.email_addresses) or (
            sociallogin.account.extra_data.get("email_verified") is True
        )
        if not (email_allowed(email) and verified):
            raise ImmediateHttpResponse(
                HttpResponseForbidden("Sign in with your ACMI Google account.")
            )

    def is_open_for_signup(self, request, sociallogin):
        # First Google sign-in by an ACMI account creates the user.
        return email_allowed(sociallogin.user.email)

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        if not user.username:
            user.username = (data.get("email") or "").split("@")[0]
        return user
