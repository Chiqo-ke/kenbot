# Ensure the Celery app is always imported when Django starts so that
# @shared_task decorators in every app are registered correctly.
from kenbot.celery import app as celery_app  # noqa: F401

__all__ = ("celery_app",)
