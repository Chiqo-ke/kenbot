from __future__ import annotations

from rest_framework import serializers

from surveyor.models import SurveyJob, SurveyResult


class SurveyJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = SurveyJob
        fields = [
            "id",
            "service_id",
            "service_name",
            "start_url",
            "celery_task_id",
            "status",
            "validation_issues",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "celery_task_id",
            "status",
            "validation_issues",
            "created_at",
            "updated_at",
        ]


class TriggerSurveySerializer(serializers.Serializer):
    service_id = serializers.CharField(max_length=128)
    service_name = serializers.CharField(max_length=256)
    start_url = serializers.URLField(max_length=2048)


class SurveyResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = SurveyResult
        fields = [
            "id",
            "service_id",
            "map_version",
            "confidence",
            "needs_review",
            "created_at",
        ]
