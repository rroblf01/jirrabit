"""Django settings for jirrabit.

Two modes selected by ``JIRRABIT_DEBUG``:

- ``JIRRABIT_DEBUG=1`` (default): development. SQLite, console email,
  permissive cookies, in-memory channels.
- ``JIRRABIT_DEBUG=0``: production. Hard-fails if ``JIRRABIT_SECRET_KEY``
  is not set, forces secure cookies / HSTS / SSL redirect, expects
  Postgres + Redis + SMTP.
"""

import os
from pathlib import Path

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
    h.strip()
    for h in os.environ.get("JIRRABIT_ALLOWED_HOSTS", "*" if DEBUG else "").split(",")
    if h.strip()
]
if not DEBUG and (not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS):
    raise RuntimeError(
        "JIRRABIT_ALLOWED_HOSTS must be an explicit list when JIRRABIT_DEBUG=0."
    )

INSTALLED_APPS = [
    "daphne",
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
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.login_throttle_middleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "core.middleware.nav_context_middleware",
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
            ],
        },
    },
]

WSGI_APPLICATION = "jirrabit.wsgi.application"
ASGI_APPLICATION = "jirrabit.asgi.application"

if os.environ.get("JIRRABIT_DB_ENGINE", "sqlite") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "jirrabit"),
            "USER": os.environ.get("POSTGRES_USER", "jirrabit"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            # CONN_MAX_AGE=0 when fronted by PgBouncer (transaction pooling),
            # otherwise long-lived connections amortise the TCP handshake.
            "CONN_MAX_AGE": int(os.environ.get("JIRRABIT_DB_CONN_MAX_AGE", "600")),
            "OPTIONS": {
                "application_name": "jirrabit",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "accounts:login"

LANGUAGE_CODE = os.environ.get("JIRRABIT_LANGUAGE", "es")
TIME_ZONE = os.environ.get("JIRRABIT_TIMEZONE", "Europe/Madrid")
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGES = [("es", "Español"), ("en", "English")]

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

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
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("JIRRABIT_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
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

# --- registration -------------------------------------------------------
# When True, ``/accounts/register/`` requires a valid invite token.
JIRRABIT_INVITE_ONLY = (
    os.environ.get("JIRRABIT_INVITE_ONLY", "0" if DEBUG else "1") == "1"
)
