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
        "navigating",
        "executing",
        "awaiting_user_confirmation",
        "awaiting_captcha",
        "awaiting_healing",
        "awaiting_vault_key",
        "awaiting_human_input",
        "awaiting_user_input_on_portal",
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
    # Goal tree built by build_execution_plan; forwarded to the extension as set_plan
    plan: list[dict] = Field(default_factory=list)
    # step_ids of workflow steps that have already completed successfully
    completed_steps: list[str] = Field(default_factory=list)
    # Most recent heartbeat snapshot from the extension (in-memory, not persisted)
    last_heartbeat: dict = Field(default_factory=dict)
    # Per-step failure counters — reset when a step succeeds
    step_fail_counts: dict[str, int] = Field(default_factory=dict)
    # URL where the bot paused for the user to take manual portal action (login/form)
    awaiting_portal_url: str = ""
