from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from surveyor.models import SurveyJob
from surveyor.serializers import (
    SurveyJobSerializer,
    TriggerSurveySerializer,
)

logger = logging.getLogger(__name__)


class TriggerSurveyView(APIView):
    """
    POST /api/surveyor/trigger/

    Queue a new portal exploration job for the given service.
    Internal/admin-only endpoint — not exposed to extension users.
    """

    def post(self, request: Request) -> Response:
        serializer = TriggerSurveySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from surveyor.tasks import survey_service

        task = survey_service.delay(
            service_id=data["service_id"],
            service_name=data["service_name"],
            start_url=data["start_url"],
        )

        logger.info(
            "TriggerSurveyView: queued task %s for service_id=%s",
            task.id,
            data["service_id"],
        )

        return Response(
            {"task_id": task.id, "status": "queued"},
            status=status.HTTP_202_ACCEPTED,
        )


class SurveyJobListView(APIView):
    """
    GET /api/surveyor/jobs/

    Return all SurveyJob records (newest first) for admin monitoring.
    """

    def get(self, request: Request) -> Response:
        jobs = SurveyJob.objects.all()
        serializer = SurveyJobSerializer(jobs, many=True)
        return Response(serializer.data)


class SurveyJobDetailView(APIView):
    """
    GET /api/surveyor/jobs/<service_id>/

    Return the most recent SurveyJob for the given service_id.
    """

    def get(self, request: Request, service_id: str) -> Response:
        try:
            job = SurveyJob.objects.filter(service_id=service_id).latest(
                "created_at"
            )
        except SurveyJob.DoesNotExist:
            return Response(
                {"detail": "No survey job found for this service."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = SurveyJobSerializer(job)
        return Response(serializer.data)
