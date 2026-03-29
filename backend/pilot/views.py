from __future__ import annotations

import logging
import uuid

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from pilot.models import ExecutionLog, PilotSession
from pilot.serializers import ExecutionLogSerializer, PilotSessionSerializer

logger = logging.getLogger(__name__)


class PilotSessionListView(APIView):
    """
    GET  /api/pilot/sessions/  — list the current user's sessions.
    POST /api/pilot/sessions/  — create a new session ID for the client to
                                  open a WebSocket connection with.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        sessions = PilotSession.objects.filter(user=request.user)
        serializer = PilotSessionSerializer(sessions, many=True)
        return Response(serializer.data)

    def post(self, request: Request) -> Response:
        session_id = uuid.uuid4()
        session = PilotSession.objects.create(
            session_id=session_id,
            user=request.user,
            status="active",
        )
        return Response(
            PilotSessionSerializer(session).data,
            status=status.HTTP_201_CREATED,
        )


class PilotSessionDetailView(APIView):
    """GET /api/pilot/sessions/<session_id>/ — fetch session state."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, session_id: str) -> Response:
        try:
            session = PilotSession.objects.get(
                session_id=session_id, user=request.user
            )
        except PilotSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = PilotSessionSerializer(session)
        return Response(serializer.data)


class PilotSessionLogsView(APIView):
    """GET /api/pilot/sessions/<session_id>/logs/ — fetch conversation logs."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, session_id: str) -> Response:
        try:
            session = PilotSession.objects.get(
                session_id=session_id, user=request.user
            )
        except PilotSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        logs = ExecutionLog.objects.filter(session=session)
        serializer = ExecutionLogSerializer(logs, many=True)
        return Response(serializer.data)
