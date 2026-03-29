from __future__ import annotations

from django.apps import AppConfig


class SurveyorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "surveyor"
    verbose_name = "Surveyor"

    def ready(self) -> None:
        # Ensure Celery tasks are registered when the app starts.
        import surveyor.tasks  # noqa: F401
