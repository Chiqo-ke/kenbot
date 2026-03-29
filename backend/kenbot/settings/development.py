from __future__ import annotations

from .base import *  # noqa: F401, F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# ---------------------------------------------------------------------------
# Development overrides
# ---------------------------------------------------------------------------

CORS_ALLOW_ALL_ORIGINS = True  # extension origin varies; tighten in production

# Use console email backend for local dev
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Celery runs tasks eagerly in development so you don't need a Redis worker
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
