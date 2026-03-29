from __future__ import annotations

from django.urls import path

from maps.views import ServiceMapDetailView, ServiceMapListView

urlpatterns = [
    path("", ServiceMapListView.as_view(), name="map-list"),
    path("<str:service_id>/", ServiceMapDetailView.as_view(), name="map-detail"),
]
