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


def set_current_user(user: AbstractBaseUser) -> None:
    _current_user.set(user)


def get_current_user() -> AbstractBaseUser:
    user = _current_user.get()
    if user is None:
        raise RuntimeError(
            "No current user set in session context. "
            "PilotConsumer must call set_current_user() before running the agent."
        )
    return user
