from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Execution state — Pydantic v2 model (not a dataclass, per project style)
# ---------------------------------------------------------------------------


class ExecutionState(BaseModel):
    """
    Tracks the lifecycle of a single user automation session.

    Passed between the WebSocket consumer and the Pilot tools so that
    every component has a consistent, validated view of where the session is.
    """

    status: Literal[
        "idle",
        "loading_map",
        "executing",
        "awaiting_user_confirmation",
        "awaiting_captcha",
        "awaiting_healing",
        "awaiting_vault_key",
        "completed",
        "failed",
    ] = "idle"
    service_id: str | None = None
    current_step_id: str | None = None
    step_index: int = 0
    total_steps: int = 0
    error_message: str | None = None
    recoverable: bool = True
    # Chat history kept in state for the LLM's context window
    chat_history: list[dict] = Field(default_factory=list)
