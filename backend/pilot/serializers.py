from __future__ import annotations

from rest_framework import serializers

from pilot.models import ExecutionLog, PilotSession


class PilotSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PilotSession
        fields = [
            "session_id",
            "service_id",
            "status",
            "step_index",
            "total_steps",
            "started_at",
            "ended_at",
        ]
        read_only_fields = fields


class ExecutionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExecutionLog
        fields = ["role", "content", "created_at"]
        read_only_fields = fields
