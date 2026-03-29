from __future__ import annotations

from django.urls import path

from pilot.views import PilotSessionDetailView, PilotSessionListView, PilotSessionLogsView

urlpatterns = [
    path("sessions/", PilotSessionListView.as_view(), name="pilot-session-list"),
    path("sessions/<str:session_id>/", PilotSessionDetailView.as_view(), name="pilot-session-detail"),
    path("sessions/<str:session_id>/logs/", PilotSessionLogsView.as_view(), name="pilot-session-logs"),
]
