from __future__ import annotations

import os

from celery import Celery

# Default to development settings so `celery -A kenbot worker` just works locally.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kenbot.settings.development")

app = Celery("kenbot")

# Pull Celery configuration from Django settings (anything prefixed CELERY_).
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in every INSTALLED_APP's tasks.py module.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> None:  # pragma: no cover
    print(f"Request: {self.request!r}")
