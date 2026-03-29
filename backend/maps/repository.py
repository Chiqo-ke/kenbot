from __future__ import annotations

import json
import logging
from pathlib import Path

from django.conf import settings
from pydantic import ValidationError

from maps.models import ServiceMapRecord
from maps.schemas import ServiceMap

logger = logging.getLogger(__name__)


class MapRepository:
    """
    Read/write/validate ServiceMap JSON files from disk and keep the
    ServiceMapRecord index in the database in sync.

    All writes pass through Pydantic validation — an invalid map is never
    persisted to disk.
    """

    def __init__(self) -> None:
        self._root: Path = Path(settings.MAP_FILES_ROOT)

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get_map(self, service_id: str) -> ServiceMap | None:
        """Return a validated ServiceMap for *service_id*, or None if not found."""
        try:
            record = ServiceMapRecord.objects.get(service_id=service_id, is_active=True)
        except ServiceMapRecord.DoesNotExist:
            logger.warning("No active ServiceMapRecord for service_id=%s", service_id)
            return None

        file_path = self._root / record.file_path
        if not file_path.exists():
            logger.error("Map file missing on disk: %s", file_path)
            return None

        raw = json.loads(file_path.read_text(encoding="utf-8"))
        try:
            return ServiceMap.model_validate(raw)
        except ValidationError as exc:
            logger.error(
                "Map file %s failed Pydantic validation: %s", file_path, exc
            )
            return None

    def list_maps(self) -> list[ServiceMap]:
        """Return all active, valid ServiceMaps."""
        results: list[ServiceMap] = []
        for record in ServiceMapRecord.objects.filter(is_active=True):
            service_map = self.get_map(record.service_id)
            if service_map:
                results.append(service_map)
        return results

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def save_map(self, service_map: ServiceMap, relative_path: str) -> ServiceMapRecord:
        """
        Validate *service_map* and persist it to disk + update the DB index.

        *relative_path* is relative to settings.MAP_FILES_ROOT, e.g.
        ``"ecitizen/good_conduct.json"``.
        """
        # Validate first — raises ValidationError on bad data
        validated = ServiceMap.model_validate(service_map.model_dump())

        file_path = self._root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            validated.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.info("Wrote map file %s", file_path)

        record, created = ServiceMapRecord.objects.update_or_create(
            service_id=validated.service_id,
            defaults={
                "service_name": validated.service_name,
                "portal": validated.portal,
                "version": validated.version,
                "last_surveyed": validated.last_surveyed,
                "surveyor_confidence": validated.surveyor_confidence,
                "file_path": relative_path,
                "is_active": True,
            },
        )
        action = "Created" if created else "Updated"
        logger.info("%s ServiceMapRecord for service_id=%s", action, validated.service_id)
        return record

    def deactivate_map(self, service_id: str) -> bool:
        """Soft-delete a map by marking its record inactive (file stays on disk)."""
        updated = ServiceMapRecord.objects.filter(service_id=service_id).update(
            is_active=False
        )
        if updated:
            logger.info("Deactivated map for service_id=%s", service_id)
        return bool(updated)

    def get_map_age_hours(self, service_id: str) -> float:
        """
        Return how many hours have elapsed since the map was last surveyed.

        Returns ``float('inf')`` when no record exists for *service_id* so
        callers can treat an unknown map as infinitely stale and trigger a
        fresh Surveyor job.
        """
        from django.utils import timezone

        try:
            record = ServiceMapRecord.objects.get(service_id=service_id)
        except ServiceMapRecord.DoesNotExist:
            return float("inf")

        delta = timezone.now() - record.last_surveyed
        return delta.total_seconds() / 3600
