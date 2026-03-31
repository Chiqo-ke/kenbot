from __future__ import annotations

"""
Thread/task-local storage for the authenticated Django user during a
WebSocket session.

The PilotConsumer sets this before invoking the agent so that tools
(running in the same async task context) can look up the user without
receiving it as a tool argument — which would expose it in LLM tool calls.
"""

import contextvars

from django.contrib.auth.models import AbstractBaseUser

_current_user: contextvars.ContextVar[AbstractBaseUser | None] = contextvars.ContextVar(
    "_current_user", default=None
)

# UUID from the browser extension (passed as ?vault_key=<UUID> in the WS URL).
# Scopes vault entries per-browser without requiring a user FK.
_current_anon_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_anon_key", default=None
)


def set_current_user(user: AbstractBaseUser) -> None:
    _current_user.set(user)


def get_current_user() -> AbstractBaseUser | None:
    return _current_user.get()


def set_current_anon_key(anon_key: str) -> None:
    _current_anon_key.set(anon_key)


def get_current_anon_key() -> str | None:
    return _current_anon_key.get()


# Latest heartbeat snapshot — set by the consumer before invoking the agent
# so that the explore_page tool can surface it without a WS round-trip.
_current_heartbeat: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "_current_heartbeat", default={}
)


def set_current_heartbeat(data: dict) -> None:
    _current_heartbeat.set(data)


def get_current_heartbeat() -> dict:
    return _current_heartbeat.get()
