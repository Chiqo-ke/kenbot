from __future__ import annotations

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application
from django.urls import path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kenbot.settings.development")

# Initialise Django before importing consumers that reference Django models.
django_asgi_app = get_asgi_application()

from kenbot.ws_auth import JWTAuthMiddlewareStack  # noqa: E402
from pilot.consumers import PilotConsumer  # noqa: E402 — must be after Django init

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": JWTAuthMiddlewareStack(
            URLRouter(
                [
                    path(
                        "ws/pilot/<str:session_id>/",
                        PilotConsumer.as_asgi(),
                    ),
                ]
            )
        ),
    }
)
