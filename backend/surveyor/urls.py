from __future__ import annotations

from django.urls import path

from surveyor import views

app_name = "surveyor"

urlpatterns = [
    path("trigger/", views.TriggerSurveyView.as_view(), name="trigger"),
    path("jobs/", views.SurveyJobListView.as_view(), name="job-list"),
    path(
        "jobs/<str:service_id>/",
        views.SurveyJobDetailView.as_view(),
        name="job-detail",
    ),
]
