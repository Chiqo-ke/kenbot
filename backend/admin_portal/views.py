from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import render

from maps.models import ServiceMapRecord
from surveyor.models import SurveyJob, SurveyResult

User = get_user_model()
logger = logging.getLogger(__name__)


def dashboard_view(request):
    """Serve the admin portal SPA."""
    return render(request, "admin_portal/dashboard.html")


class DashboardStatsView(APIView):
    """
    GET /api/admin/stats/

    Summary counts for the admin dashboard — staff-only.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request: Request) -> Response:
        total_maps = ServiceMapRecord.objects.filter(is_active=True).count()
        total_portals = (
            ServiceMapRecord.objects.filter(is_active=True)
            .values("portal")
            .distinct()
            .count()
        )
        jobs_pending = SurveyJob.objects.filter(status=SurveyJob.Status.PENDING).count()
        jobs_running = SurveyJob.objects.filter(status=SurveyJob.Status.RUNNING).count()
        jobs_complete = SurveyJob.objects.filter(
            status=SurveyJob.Status.COMPLETE
        ).count()
        jobs_failed = SurveyJob.objects.filter(status=SurveyJob.Status.FAILED).count()

        # Maps broken down by portal
        portals = list(
            ServiceMapRecord.objects.filter(is_active=True)
            .values("portal")
            .distinct()
            .values_list("portal", flat=True)
        )

        return Response(
            {
                "total_maps": total_maps,
                "total_portals": total_portals,
                "portals": portals,
                "survey_jobs": {
                    "pending": jobs_pending,
                    "running": jobs_running,
                    "complete": jobs_complete,
                    "failed": jobs_failed,
                    "total": jobs_pending + jobs_running + jobs_complete + jobs_failed,
                },
            }
        )


class AdminMapListView(APIView):
    """
    GET /api/admin/maps/

    All maps (including inactive) with full detail — staff-only.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request: Request) -> Response:
        records = ServiceMapRecord.objects.all().order_by("-last_surveyed")
        data = [
            {
                "id": r.id,
                "service_id": r.service_id,
                "service_name": r.service_name,
                "portal": r.portal,
                "version": r.version,
                "last_surveyed": r.last_surveyed.isoformat(),
                "surveyor_confidence": r.surveyor_confidence,
                "is_active": r.is_active,
                "file_path": r.file_path,
                "updated_at": r.updated_at.isoformat(),
            }
            for r in records
        ]
        return Response(data)


class AdminMapToggleView(APIView):
    """
    POST /api/admin/maps/<int:pk>/toggle/

    Activate or deactivate a map — staff-only.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request: Request, pk: int) -> Response:
        try:
            record = ServiceMapRecord.objects.get(pk=pk)
        except ServiceMapRecord.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        record.is_active = not record.is_active
        record.save(update_fields=["is_active", "updated_at"])
        logger.info(
            "Admin %s toggled map %s → is_active=%s",
            request.user.username,
            record.service_id,
            record.is_active,
        )
        return Response({"service_id": record.service_id, "is_active": record.is_active})


class AdminSurveyJobListView(APIView):
    """
    GET /api/admin/jobs/

    All survey jobs with nested result — staff-only.
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request: Request) -> Response:
        jobs = SurveyJob.objects.select_related("result").order_by("-created_at")[:100]
        data = []
        for j in jobs:
            entry = {
                "id": j.id,
                "service_id": j.service_id,
                "service_name": j.service_name,
                "start_url": j.start_url,
                "celery_task_id": j.celery_task_id,
                "status": j.status,
                "validation_issues": j.validation_issues,
                "created_at": j.created_at.isoformat(),
                "updated_at": j.updated_at.isoformat(),
            }
            if hasattr(j, "result"):
                entry["result"] = {
                    "map_version": j.result.map_version,
                    "confidence": j.result.confidence,
                    "needs_review": j.result.needs_review,
                }
            data.append(entry)
        return Response(data)


class AdminTriggerSurveyView(APIView):
    """
    POST /api/admin/trigger/

    Queue a new surveyor job — staff-only.

    Body: { service_id, service_name, start_url }
    """

    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request: Request) -> Response:
        service_id = (request.data.get("service_id") or "").strip()
        service_name = (request.data.get("service_name") or "").strip()
        start_url = (request.data.get("start_url") or "").strip()

        if not service_id or not service_name or not start_url:
            return Response(
                {"detail": "service_id, service_name, and start_url are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from surveyor.tasks import survey_service

        task = survey_service.delay(
            service_id=service_id,
            service_name=service_name,
            start_url=start_url,
        )

        logger.info(
            "Admin %s triggered survey task %s for service_id=%s",
            request.user.username,
            task.id,
            service_id,
        )

        return Response(
            {"task_id": task.id, "status": "queued"},
            status=status.HTTP_202_ACCEPTED,
        )
