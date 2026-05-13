"""Django settings for jirrabit."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "JIRRABIT_SECRET_KEY",
    "django-insecure--hfi*bj3@3v10yvwos6o+31j*-73!_d573g5ido7vdbqk3_oml",
)

DEBUG = os.environ.get("JIRRABIT_DEBUG", "1") == "1"

ALLOWED_HOSTS = os.environ.get("JIRRABIT_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "daphne",
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
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
            "NAME": os.environ.get("JIRRABIT_DB_NAME", "jirrabit"),
            "USER": os.environ.get("JIRRABIT_DB_USER", "jirrabit"),
            "PASSWORD": os.environ.get("JIRRABIT_DB_PASSWORD", ""),
            "HOST": os.environ.get("JIRRABIT_DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("JIRRABIT_DB_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("JIRRABIT_DB_CONN_MAX_AGE", "60")),
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
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "accounts:login"

LANGUAGE_CODE = "es-es"
TIME_ZONE = "Europe/Madrid"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = "jirrabit@localhost"

JIRRABIT_HOOK_MODULES = [
    "core.hooks_builtin",
    "issues.hooks",
]
