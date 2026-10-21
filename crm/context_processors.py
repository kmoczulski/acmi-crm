from pathlib import Path

from django.conf import settings

from .permissions import is_manager

STATIC_DIR = Path(__file__).resolve().parent / "static" / "crm"


def _asset_version():
    """Changes whenever the stylesheet or logo changes, so browsers don't keep stale copies."""
    stamps = [f.stat().st_mtime_ns for f in STATIC_DIR.iterdir() if f.is_file()]
    return str(max(stamps) // 1_000_000_000) if stamps else "0"


def _logo():
    for name in ("logo.svg", "logo.png", "logo.jpg", "logo.jpeg"):
        if (STATIC_DIR / name).exists():
            return f"crm/{name}"
    return ""


def app_context(request):
    from django.utils import timezone

    from . import notify
    from .models import NewsScan, Task, UnmatchedEmail

    from . import prefs, search

    queue = news_open = unread = due = 0
    user_prefs, pages, new_items = {}, [], []
    if getattr(request, "user", None) and request.user.is_authenticated:
        queue = UnmatchedEmail.objects.values("domain").distinct().count()
        news_open = NewsScan.objects.filter(status=NewsScan.Status.OPEN).count()
        today = str(timezone.localdate())
        if hasattr(request, "session") and request.session.get("due_sweep") != today:
            notify.due_sweep(request.user)  # once a day per person: due / overdue tasks
            request.session["due_sweep"] = today
        unread = notify.unread_count(request.user)
        due = Task.objects.filter(assignee=request.user, done=False, due_date__lte=timezone.localdate()).count()
        user_prefs = prefs.get_prefs(request.user)
        pages, new_items = search.page_links(), search.new_links()
    return {
        "unread_notifications": unread,
        "my_due_count": due,
        "mail_queue_count": queue,
        "news_open_count": news_open,
        "PREFS": user_prefs,
        "NAV_PAGES": pages,
        "NEW_ITEMS": new_items,
        "LOGO": _logo(),
        "ASSET_VERSION": _asset_version(),
        "GOOGLE_SIGNIN_ENABLED": settings.GOOGLE_SIGNIN_ENABLED,
        "LOCAL_LOGIN_ENABLED": settings.LOCAL_LOGIN_ENABLED,
        # Managers-only buttons are hidden for everyone else (the views refuse them too).
        "IS_MANAGER": is_manager(getattr(request, "user", None)),
    }
