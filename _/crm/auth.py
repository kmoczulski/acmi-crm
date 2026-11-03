"""Sign-in rules: Google accounts on ACMI's domain only; no self sign-up."""
from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.auth_backends import AuthenticationBackend
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.http import HttpResponseForbidden


def email_allowed(email):
    domain = (email or "").rsplit("@", 1)[-1].lower()
    return domain in [d.lower() for d in settings.ALLOWED_EMAIL_DOMAINS]


# Passwords are checked only by these two backends, so ACMI_LOCAL_LOGIN=false switches password sign-in off
# everywhere (the sign-in page and /admin/login/), not just the form. Google sign-in never calls authenticate()
# with a password, so it is unaffected.

class PasswordBackend(ModelBackend):
    """Django's username / password check (used by /admin/login/)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not settings.LOCAL_LOGIN_ENABLED:
            return None
        return _not_partner(super().authenticate(request, username=username, password=password, **kwargs))


class AllauthBackend(AuthenticationBackend):
    """allauth's backend: password sign-in on /accounts/login/, and the backend Google sessions are tied to."""

    def authenticate(self, request, **credentials):
        if not settings.LOCAL_LOGIN_ENABLED:
            return None
        return _not_partner(super().authenticate(request, **credentials))


class PartnerBackend(ModelBackend):
    """Capital partners' passwords (crm/dataroom/). Works whether or not the team's password sign-in is on, and only
    for a partner account whose membership and room are both switched on."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        from .permissions import is_partner

        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user is None or not is_partner(user):
            return None
        member = getattr(user, "data_room_member", None)
        if member is None or not member.active or not member.room.active:
            return None
        return user


def _not_partner(user):
    """The team's sign-in never lets a partner in; partners use the data room's own page."""
    from .permissions import is_partner

    return None if user is not None and is_partner(user) else user


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
        if (sociallogin.state or {}).get("process") == "connect" and request.user.is_authenticated:
            from .permissions import is_partner

            # Connecting Google (calendar, Mail) attaches it to the signed-in account: never a partner's, and never
            # someone else's ACMI address, so a token always belongs to the mailbox owner.
            if is_partner(request.user):
                raise ImmediateHttpResponse(HttpResponseForbidden("Not available for this account."))
            own = (request.user.email or "").lower()
            if own and email.lower() != own:
                raise ImmediateHttpResponse(HttpResponseForbidden(
                    f"That Google account is {email}. Connect your own account, {request.user.email}."))

    def is_open_for_signup(self, request, sociallogin):
        # First Google sign-in by an ACMI account creates the user.
        return email_allowed(sociallogin.user.email)

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        if not user.username:
            user.username = (data.get("email") or "").split("@")[0]
        return user
