"""ACMI CRM settings.

Everything environment-specific comes from environment variables so the same
file runs on the laptop (SQLite, username login) and on Cloud Run (Postgres,
Google sign-in).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path):
    """Read KEY=VALUE lines from .env into the environment (real env vars win)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, str(default)).lower() in ("1", "true", "yes", "on")


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "crm",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "crm.middleware.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "crm.middleware.LoginRequiredMiddleware",
    "crm.middleware.CurrentUserMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "crm.context_processors.app_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Postgres when POSTGRES_DB is set (Cloud SQL), SQLite otherwise (laptop).
if env("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB"),
            "USER": env("POSTGRES_USER", "postgres"),
            "PASSWORD": env("POSTGRES_PASSWORD", ""),
            # Cloud Run reaches Cloud SQL through a unix socket: POSTGRES_HOST=/cloudsql/<connection name>
            "HOST": env("POSTGRES_HOST", "localhost"),
            "PORT": env("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": env("ACMI_SQLITE_PATH", BASE_DIR / "db.sqlite3"),
            # The mail sync and the web app write at the same time. WAL lets reads and a write
            # overlap; IMMEDIATE transactions queue writers instead of failing with "locked".
            "OPTIONS": {
                "timeout": 30,
                "transaction_mode": "IMMEDIATE",
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

AUTHENTICATION_BACKENDS = [
    # Django's and allauth's backends, with passwords refused when ACMI_LOCAL_LOGIN is false (crm/auth.py).
    "crm.auth.PasswordBackend",
    "crm.auth.AllauthBackend",
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", "America/Chicago")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
MEDIA_URL = "media/"
MEDIA_ROOT = Path(env("ACMI_MEDIA_ROOT", BASE_DIR / "media"))
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/accounts/login/"

# --- Sign-in ---------------------------------------------------------------
# Only accounts on these domains may sign in with Google.
ALLOWED_EMAIL_DOMAINS = [d for d in env("ACMI_ALLOWED_EMAIL_DOMAINS", "acmigroup.com").split(",") if d]
# Local username/password login. Turn off in production so Google is the only way in.
LOCAL_LOGIN_ENABLED = env_bool("ACMI_LOCAL_LOGIN", True)

ACCOUNT_LOGIN_METHODS = {"username"}
ACCOUNT_SIGNUP_FIELDS = ["username*", "password1*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_ADAPTER = "crm.auth.AccountAdapter"
SOCIALACCOUNT_ADAPTER = "crm.auth.SocialAccountAdapter"
SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
SOCIALACCOUNT_PROVIDERS = {}
GOOGLE_SIGNIN_ENABLED = bool(env("GOOGLE_CLIENT_ID"))
if GOOGLE_SIGNIN_ENABLED:
    SOCIALACCOUNT_PROVIDERS["google"] = {
        "APP": {"client_id": env("GOOGLE_CLIENT_ID"), "secret": env("GOOGLE_CLIENT_SECRET", "")},
        # Calendar read access lets the CRM put everyone's meetings on the Meetings calendar.
        "SCOPE": ["profile", "email", "https://www.googleapis.com/auth/calendar.readonly"],
        # offline = Google returns a refresh token, so the scheduled sync works while nobody is signed in.
        "AUTH_PARAMS": {"hd": ALLOWED_EMAIL_DOMAINS[0] if ALLOWED_EMAIL_DOMAINS else "", "access_type": "offline"},
    }
# Keep each person's Google token (encrypted at rest by the database host in production).
SOCIALACCOUNT_STORE_TOKENS = True
# Calendar sync window: this many days back and ahead of today.
CALENDAR_DAYS_BACK = int(env("ACMI_CALENDAR_DAYS_BACK", "90"))
CALENDAR_DAYS_AHEAD = int(env("ACMI_CALENDAR_DAYS_AHEAD", "60"))

# --- Email sync (crm/mail) -------------------------------------------------
# Mail between ACMI people is never logged.
INTERNAL_EMAIL_DOMAINS = [
    d for d in env("ACMI_INTERNAL_DOMAINS", "acmigroup.com,acmiproperties.com").split(",") if d
]
IMAP_HOST = env("ACMI_IMAP_HOST", "imap.gmail.com")
# Weekly news review: where the scan .pdf/.docx files land.
NEWS_WATCH_FOLDER = env("ACMI_NEWS_WATCH_FOLDER", r"C:/Users/Kemoc/Desktop/Prospecting/Weekly News Search")
# Shared secret for the scheduler that calls /internal/sync-mail/ in production.
CRON_SECRET = env("ACMI_CRON_SECRET", "")

# --- Security headers ------------------------------------------------------
# Scripts may only come from this site and the pinned unpkg files (which also carry integrity hashes). Inline scripts
# are still allowed because the templates use them. Images, tiles and map-service requests are left open.
CONTENT_SECURITY_POLICY = env(
    "ACMI_CSP",
    "script-src 'self' 'unsafe-inline' https://unpkg.com; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
)

# --- Logging ---------------------------------------------------------------
# Django's defaults print nothing when DEBUG is off, so a crash in production would leave no trace. Send errors to
# the console, which Cloud Run collects. 4xx responses aren't logged here (the platform's request log has them).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "{levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("ACMI_LOG_LEVEL", "WARNING")},
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", True)
    # Cloud Run's front end sets X-Forwarded-Proto; the health check is left alone for plain-HTTP probes.
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)
    SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
    if SECRET_KEY.startswith("dev-only") or len(SECRET_KEY) < 50:
        raise RuntimeError("Set DJANGO_SECRET_KEY (50+ random characters) before running with DJANGO_DEBUG=false")
