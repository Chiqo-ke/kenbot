from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Load .env file automatically (python-dotenv)
# ---------------------------------------------------------------------------
# .env lives at  backend/.env  — values are set into os.environ so all the
# os.environ[] reads below work without the caller having to set them first.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env", override=False)  # don't clobber real env vars
except ImportError:
    pass  # python-dotenv not installed — env vars must be set externally

# ---------------------------------------------------------------------------
# Security — never hardcode secrets
# ---------------------------------------------------------------------------

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False
ALLOWED_HOSTS: list[str] = []

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "channels",
    "corsheaders",
    # project apps
    "surveyor",
    "pilot",
    "maps",
    "vault",
    "admin_portal",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "kenbot.urls"

WSGI_APPLICATION = "kenbot.wsgi.application"
ASGI_APPLICATION = "kenbot.asgi.application"

# ---------------------------------------------------------------------------
# Templates (minimal — API-only project)
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

# ---------------------------------------------------------------------------
# Database — override per environment
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

# ---------------------------------------------------------------------------
# Django Channels — Redis channel layer
# ---------------------------------------------------------------------------

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [os.environ.get("REDIS_URL", "redis://localhost:6379")],
        },
    }
}

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------

CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
CELERY_RESULT_BACKEND = os.environ.get("REDIS_URL", "redis://localhost:6379")
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_RESULT_SERIALIZER = "json"

# ---------------------------------------------------------------------------
# LLM — GitHub Models via device-flow token or GITHUB_TOKEN env var
# ---------------------------------------------------------------------------

# Token is loaded gracefully so Django management commands (migrate, check,
# collectstatic) work before auth_github.py has been run.
# Actual LLM API calls will fail with a clear error if the token is empty.
try:
    from kenbot.github_auth import get_github_token  # noqa: E402
    GITHUB_TOKEN: str = get_github_token()
except Exception:
    GITHUB_TOKEN = ""  # No token yet — run:  python auth_github.py

# GitHub Models base URL — same endpoint used by both Pilot and Surveyor.
GITHUB_MODELS_BASE_URL: str = os.environ.get(
    "GITHUB_MODELS_BASE_URL", "https://models.inference.ai.azure.com"
)

# Which model each agent uses.  Override via env var to switch models at runtime
# without changing code. See auth_github.py for the full list of available models.
KENBOT_PILOT_MODEL: str = os.environ.get(
    "KENBOT_PILOT_MODEL", "openai/gpt-4o-mini"
)
KENBOT_SURVEYOR_MODEL: str = os.environ.get(
    "KENBOT_SURVEYOR_MODEL", "openai/gpt-4o"
)

# ---------------------------------------------------------------------------
# Vault encryption key — must be a valid Fernet key (32-byte URL-safe base64)
# ---------------------------------------------------------------------------

VAULT_ENCRYPTION_KEY = os.environ["VAULT_ENCRYPTION_KEY"]

# ---------------------------------------------------------------------------
# Map files root directory
# ---------------------------------------------------------------------------

MAP_FILES_ROOT = BASE_DIR / "map_files"

# ---------------------------------------------------------------------------
# Logging — mask any sensitive values that leak into log records
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "mask_vault": {
            "()": "kenbot.logging_filters.MaskVaultFilter",
        },
    },
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["mask_vault"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "pilot": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "maps": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        "vault": {
            "handlers": ["console"],
            "level": "WARNING",  # Never DEBUG — vault ops are sensitive
            "propagate": False,
        },
        "surveyor": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}

# ---------------------------------------------------------------------------
# Logging — mask vault values
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "mask_vault": {
            "()": "kenbot.logging_filters.MaskVaultFilter",
        }
    },
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["mask_vault"],
        }
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "pilot": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "surveyor": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
        "vault": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "maps": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
}
