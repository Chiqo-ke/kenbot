from __future__ import annotations

import logging

from django.db import models

logger = logging.getLogger(__name__)


class ServiceMapRecord(models.Model):
    """
    Lightweight index of every JSON map file on disk.

    The canonical schema lives in schemas.py (Pydantic). This model is used
    only for querying and listing — the actual map data is in the JSON file.
    """

    service_id = models.CharField(max_length=120, unique=True, db_index=True)
    service_name = models.CharField(max_length=255)
    portal = models.CharField(max_length=120)
    version = models.CharField(max_length=20)
    last_surveyed = models.DateTimeField()
    surveyor_confidence = models.FloatField()
    # Relative path from settings.MAP_FILES_ROOT
    file_path = models.CharField(max_length=512)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_surveyed"]
        verbose_name = "Service Map"
        verbose_name_plural = "Service Maps"

    def __str__(self) -> str:
        return f"{self.service_name} ({self.service_id}) v{self.version}"
