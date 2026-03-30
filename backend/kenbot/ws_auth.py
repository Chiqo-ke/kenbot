"""
JWT WebSocket authentication middleware for Django Channels.

The Chrome extension cannot set HTTP headers on a WebSocket connection, so
it passes the JWT access token as a query-string parameter:

    ws://127.0.0.1:8000/ws/pilot/<session_id>/?token=<jwt>

This middleware decodes that token and populates scope["user"] before the
consumer runs, exactly like AuthMiddlewareStack does for session cookies.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken

logger = logging.getLogger(__name__)


@database_sync_to_async
def _get_user_from_token(raw_token: str):
    """Validate JWT and return the associated User or AnonymousUser."""
    try:
        token = AccessToken(raw_token)
        user_id = token["user_id"]

        from django.contrib.auth import get_user_model
        User = get_user_model()
        return User.objects.get(pk=user_id)
    except (TokenError, InvalidToken, KeyError) as exc:
        logger.debug("WS JWT invalid: %s", exc)
        return AnonymousUser()
    except Exception as exc:
        logger.warning("WS JWT auth error: %s", exc)
        return AnonymousUser()


class JWTAuthMiddleware:
    """
    ASGI middleware — wraps a Channels application and authenticates
    WebSocket connections using a JWT in the query string.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            # Parse ?token= from the query string
            qs = parse_qs(scope.get("query_string", b"").decode())
            token_list = qs.get("token", [])
            if token_list:
                scope["user"] = await _get_user_from_token(token_list[0])
            else:
                scope["user"] = AnonymousUser()
        return await self.inner(scope, receive, send)


def JWTAuthMiddlewareStack(inner):
    """Convenience wrapper — drop-in replacement for AuthMiddlewareStack."""
    return JWTAuthMiddleware(inner)
