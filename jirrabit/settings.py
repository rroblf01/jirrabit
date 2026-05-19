"""Django settings for jirrabit.

Two modes selected by ``JIRRABIT_DEBUG``:

- ``JIRRABIT_DEBUG=1`` (default): development. SQLite, console email,
  permissive cookies, in-memory channels.
- ``JIRRABIT_DEBUG=0``: production. Hard-fails if ``JIRRABIT_SECRET_KEY``
  is not set, forces secure cookies / HSTS / SSL redirect, expects
  Postgres + Redis + SMTP.
"""

import os
import warnings
from pathlib import Path
from urllib.parse import unquote, urlparse

# WhiteNoise serves static files via ``FileResponse`` (a subclass of
# ``StreamingHttpResponse`` with a sync iterator); Django's ASGI handler
# wraps it with ``sync_to_async`` and emits a warning on every static
# request. The behaviour is correct, the warning is noise.
warnings.filterwarnings(
    "ignore",
    message="StreamingHttpResponse must consume synchronous iterators.*",
    module=r"django\.core\.handlers\.asgi",
)

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("JIRRABIT_DEBUG", "1") == "1"

# SECRET_KEY: dev fallback only when DEBUG. Prod must inject via env.
SECRET_KEY = os.environ.get("JIRRABIT_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-insecure-dev-only-do-not-use-in-prod"
    else:
        raise RuntimeError("JIRRABIT_SECRET_KEY must be set when JIRRABIT_DEBUG=0.")

ALLOWED_HOSTS = [
    "jirrabit.ricardorobles.es",
    "localhost",
    "127.0.0.1",
]


INSTALLED_APPS = [
    "channels",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "accounts.apps.AccountsConfig",
    "core.apps.CoreConfig",
    "projects.apps.ProjectsConfig",
    "issues.apps.IssuesConfig",
    "board.apps.BoardConfig",
    "search.apps.SearchConfig",
    "realtime.apps.RealtimeConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.login_throttle_middleware",
    "core.middleware.api_rate_limit_middleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "core.middleware.nav_context_middleware",
    "core.middleware.csp_middleware",
]

ROOT_URLCONF = "jirrabit.urls"

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
                "django.template.context_processors.i18n",
                "core.context_processors.nav",
                "core.context_processors.palette",
                "core.context_processors.notifications_count",
            ],
        },
    },
]

WSGI_APPLICATION = "jirrabit.wsgi.application"
ASGI_APPLICATION = "jirrabit.asgi.application"

if os.environ.get("JIRRABIT_DB_ENGINE", "postgres") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

else:
    _url = urlparse(os.environ.get("JIRRABIT_DATABASE_URI", ""))
    if _url.scheme not in ("postgres", "postgresql"):
        raise ValueError(
            f"JIRRABIT_DATABASE_URI must use postgres:// or postgresql:// scheme, got: {_url.scheme}"
        )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": (_url.path or "/").lstrip("/") or "jirrabit",
            "USER": unquote(_url.username) if _url.username else "",
            "PASSWORD": unquote(_url.password) if _url.password else "",
            "HOST": _url.hostname or "127.0.0.1",
            "PORT": str(_url.port) if _url.port else "5432",
            "CONN_MAX_AGE": int(os.environ.get("JIRRABIT_DB_CONN_MAX_AGE", "20")),
            "OPTIONS": {
                "application_name": "jirrabit",
            },
        }
    }

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "accounts:login"

LANGUAGE_CODE = os.environ.get("JIRRABIT_LANGUAGE", "es-es")
TIME_ZONE = os.environ.get("JIRRABIT_TIMEZONE", "Europe/Madrid")
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGES = [("es", "Español"), ("en", "English")]

# --- logging --------------------------------------------------------------
# Pipe ``jirrabit.*`` loggers to stdout at INFO level so debug prints from
# middleware, signals, consumers and notifications are visible in
# ``docker compose logs``. Without this, Django's default config drops
# INFO records from custom loggers on the floor.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(name)s %(levelname)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "jirrabit": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}

STATIC_URL = "static/"
# STATIC_ROOT lives outside the source tree so a bind-mounted /app in dev
# does not shadow the collected files baked into the image at build time.
STATIC_ROOT = Path(os.environ.get("JIRRABIT_STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- email ----------------------------------------------------------------
EMAIL_BACKEND = os.environ.get(
    "JIRRABIT_EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = os.environ.get("JIRRABIT_EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("JIRRABIT_EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("JIRRABIT_EMAIL_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("JIRRABIT_EMAIL_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("JIRRABIT_EMAIL_TLS", "1") == "1"
DEFAULT_FROM_EMAIL = os.environ.get("JIRRABIT_FROM_EMAIL", "jirrabit@localhost")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# --- channels (websockets) ------------------------------------------------
_REDIS_URL = os.environ.get("REDIS_URL")
if _REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [_REDIS_URL]},
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }

# --- security headers (prod only) ----------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

if not DEBUG:
    # SSL redirect/HSTS/secure cookies can be disabled via env when running
    # prod-like locally without a TLS-terminating proxy in front.
    SECURE_SSL_REDIRECT = os.environ.get("JIRRABIT_SECURE_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = os.environ.get("JIRRABIT_SECURE_COOKIES", "1") == "1"
    CSRF_COOKIE_SECURE = os.environ.get("JIRRABIT_SECURE_COOKIES", "1") == "1"
    SECURE_HSTS_SECONDS = int(os.environ.get("JIRRABIT_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
    SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = False  # HTMX needs the token in JS
else:
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0

# --- login throttling ----------------------------------------------------
JIRRABIT_LOGIN_THROTTLE_LIMIT = int(os.environ.get("JIRRABIT_LOGIN_LIMIT", "8"))
JIRRABIT_LOGIN_THROTTLE_WINDOW = int(os.environ.get("JIRRABIT_LOGIN_WINDOW", "300"))
JIRRABIT_LOGIN_THROTTLE_BAN = int(os.environ.get("JIRRABIT_LOGIN_BAN", "900"))

# --- API rate limit ------------------------------------------------------
JIRRABIT_API_RATE_LIMIT = int(os.environ.get("JIRRABIT_API_RATE_LIMIT", "120"))
JIRRABIT_API_RATE_WINDOW = int(os.environ.get("JIRRABIT_API_RATE_WINDOW", "60"))

# --- registration -------------------------------------------------------
# When True, ``/accounts/register/`` requires a valid invite token.
JIRRABIT_INVITE_ONLY = os.environ.get("JIRRABIT_INVITE_ONLY", "0" if DEBUG else "1") == "1"
