from contextvars import ContextVar

from django.conf import settings
from django.shortcuts import redirect

_current_user = ContextVar("current_user", default=None)

PUBLIC_PREFIXES = ("/accounts/", "/static/", "/admin/login/", "/healthz", "/internal/")


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
        if not request.user.is_authenticated and not request.path.startswith(PUBLIC_PREFIXES):
            return redirect(f"{settings.LOGIN_URL}?next={request.path}")
        return self.get_response(request)
