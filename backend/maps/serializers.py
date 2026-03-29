from __future__ import annotations

from rest_framework import serializers

from maps.models import ServiceMapRecord


class ServiceMapRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceMapRecord
        fields = [
            "service_id",
            "service_name",
            "portal",
            "version",
            "last_surveyed",
            "surveyor_confidence",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class ServiceMapWriteSerializer(serializers.Serializer):
    """
    Input schema for POST /api/maps/.

    Accepts the full ServiceMap JSON body.  Actual Pydantic validation
    happens inside MapRepository.save_map() — this layer only checks
    that the minimum required top-level keys are present.
    """

    service_id = serializers.RegexField(
        r"^[a-z][a-z0-9_]{0,119}$",
        help_text="Unique snake_case service identifier.",
    )
    portal = serializers.RegexField(
        r"^[a-z][a-z0-9_]{0,119}$",
        help_text="Portal slug, e.g. 'ecitizen' or 'kra'.",
    )
    # Remaining ServiceMap fields are validated as-is by Pydantic.
    # We only surface service_id and portal here so DRF can produce
    # meaningful early errors without duplicating the whole schema.
    map_data = serializers.JSONField(
        help_text="Full ServiceMap payload (all fields including service_id and portal).",
    )
