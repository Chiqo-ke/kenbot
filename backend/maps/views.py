from __future__ import annotations

import logging

from pydantic import ValidationError
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from maps.models import ServiceMapRecord
from maps.repository import MapRepository
from maps.schemas import ServiceMap
from maps.serializers import ServiceMapRecordSerializer, ServiceMapWriteSerializer

logger = logging.getLogger(__name__)


class ServiceMapListView(APIView):
    """
    GET  /api/maps/ — list all active map index records.
    POST /api/maps/ — create or update a service map (Surveyor-only).
    """

    def get(self, request: Request) -> Response:
        records = ServiceMapRecord.objects.filter(is_active=True)
        serializer = ServiceMapRecordSerializer(records, many=True)
        return Response(serializer.data)

    def post(self, request: Request) -> Response:
        """
        Accept a full ServiceMap JSON payload, validate it through Pydantic,
        persist to disk, and update the DB index.

        Expected body::

            {
                "service_id": "ecitizen_good_conduct",
                "portal": "ecitizen",
                "map_data": { <full ServiceMap object> }
            }
        """
        serializer = ServiceMapWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        map_data: dict = serializer.validated_data["map_data"]

        try:
            service_map = ServiceMap.model_validate(map_data)
        except ValidationError as exc:
            return Response(
                {"detail": "Map schema validation failed.", "errors": exc.errors()},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Derive a stable relative path from portal + service_id
        relative_path = f"{service_map.portal}/{service_map.service_id}.json"

        repo = MapRepository()
        try:
            record = repo.save_map(service_map, relative_path)
        except Exception as exc:
            logger.exception("Failed to save map %s: %s", service_map.service_id, exc)
            return Response(
                {"detail": "Failed to persist map."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            ServiceMapRecordSerializer(record).data,
            status=status.HTTP_201_CREATED,
        )


class ServiceMapDetailView(APIView):
    """
    GET  /api/maps/<service_id>/ — fetch full validated ServiceMap JSON.
    DELETE /api/maps/<service_id>/ — soft-delete (deactivate) the map.
    """

    def get(self, request: Request, service_id: str) -> Response:
        repo = MapRepository()
        service_map = repo.get_map(service_id)
        if service_map is None:
            return Response(
                {"detail": f"No active map for service_id '{service_id}'."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(service_map.model_dump())

    def delete(self, request: Request, service_id: str) -> Response:
        repo = MapRepository()
        deactivated = repo.deactivate_map(service_id)
        if not deactivated:
            return Response(
                {"detail": f"No map found for service_id '{service_id}'."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
