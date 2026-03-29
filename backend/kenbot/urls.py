from __future__ import annotations

from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    # Auth
    path("api/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    # Apps
    path("api/pilot/", include("pilot.urls")),
    path("api/maps/", include("maps.urls")),
    path("api/vault/", include("vault.urls")),
    path("api/surveyor/", include("surveyor.urls")),
]
