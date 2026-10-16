from contextvars import ContextVar

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden, HttpResponseNotFound

_current_user = ContextVar("current_user", default=None)

PUBLIC_PREFIXES = ("/accounts/", "/static/", "/admin/login/", "/healthz", "/internal/", "/g/")
# Password pages that only make sense while username / password sign-in is on.
LOCAL_LOGIN_PREFIXES = ("/accounts/password/", "/accounts/signup/")


def get_current_user():
    """The signed-in user for the current request, or None (imports, shell)."""
    return _current_user.get()


class CurrentUserMiddleware:
    """Remembers request.user so model saves can record who made a change."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user if request.user.is_authenticated else None
        token = _current_user.set(user)
        try:
            return self.get_response(request)
        finally:
            _current_user.reset(token)


class LoginRequiredMiddleware:
    """Every page except sign-in requires an authenticated user."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.LOCAL_LOGIN_ENABLED:
            # Google is the only way in: no password reset or sign-up pages, and the admin's password form
            # hands over to Google sign-in.
            if request.path.startswith(LOCAL_LOGIN_PREFIXES):
                return HttpResponseNotFound()
            if request.path == "/admin/login/":
                if request.user.is_authenticated:  # signed in but not staff: sending them to sign-in would loop
                    return HttpResponseForbidden("Your account doesn't have admin access.")
                return redirect_to_login(request.GET.get("next") or "/admin/")
        if not request.user.is_authenticated and not request.path.startswith(PUBLIC_PREFIXES):
            return redirect_to_login(request.get_full_path())
        return self.get_response(request)


class ContentSecurityPolicyMiddleware:
    """Adds the Content-Security-Policy header from settings (unless a view set its own)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = getattr(settings, "CONTENT_SECURITY_POLICY", "")
        if policy and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = policy
        return response
