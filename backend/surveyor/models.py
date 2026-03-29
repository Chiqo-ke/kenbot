from __future__ import annotations

from django.db import models


class SurveyJob(models.Model):
    """Tracks a single Celery Surveyor job for admin visibility."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    service_id = models.CharField(max_length=128, db_index=True)
    service_name = models.CharField(max_length=256)
    start_url = models.URLField(max_length=2048)
    celery_task_id = models.CharField(max_length=255, unique=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    # JSON list of validation issue strings — stored as text for simplicity.
    validation_issues = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Survey Job"
        verbose_name_plural = "Survey Jobs"

    def __str__(self) -> str:
        return f"{self.service_id} [{self.status}] — {self.celery_task_id}"


class SurveyResult(models.Model):
    """
    Links a completed SurveyJob to the resulting ServiceMap on disk.

    The full map JSON is also cached here so the admin can inspect it without
    reading the filesystem.
    """

    job = models.OneToOneField(
        SurveyJob, on_delete=models.CASCADE, related_name="result"
    )
    service_id = models.CharField(max_length=128, db_index=True)
    map_version = models.CharField(max_length=32)
    confidence = models.FloatField()
    map_json = models.JSONField()
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Survey Result"
        verbose_name_plural = "Survey Results"

    def __str__(self) -> str:
        return f"{self.service_id} v{self.map_version} (confidence={self.confidence:.2f})"
