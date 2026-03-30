from __future__ import annotations

from django.urls import path

from admin_portal import views

app_name = "admin_portal"

urlpatterns = [
    # SPA entry point
    path("", views.dashboard_view, name="dashboard"),
    # REST API — all require is_staff=True
    path("api/admin/stats/", views.DashboardStatsView.as_view(), name="stats"),
    path("api/admin/maps/", views.AdminMapListView.as_view(), name="maps"),
    path(
        "api/admin/maps/<int:pk>/toggle/",
        views.AdminMapToggleView.as_view(),
        name="map-toggle",
    ),
    path("api/admin/jobs/", views.AdminSurveyJobListView.as_view(), name="jobs"),
    path(
        "api/admin/trigger/",
        views.AdminTriggerSurveyView.as_view(),
        name="trigger",
    ),
]
