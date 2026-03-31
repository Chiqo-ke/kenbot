from __future__ import annotations

import logging
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class SelectorStrategy(str, Enum):
    ARIA = "aria"
    DATA_ATTR = "data-attr"
    XPATH = "xpath"
    CSS = "css"
    TEXT_CONTENT = "text-content"


class Selector(BaseModel):
    primary: str = Field(description="Best selector — prefer ARIA labels/roles")
    fallbacks: list[str] = Field(
        default=[], description="Ordered fallback selectors"
    )
    strategy: SelectorStrategy = Field(
        description="Strategy used for primary selector"
    )


class ActionType(str, Enum):
    TEXT = "text"
    PASSWORD = "password"
    CLICK = "click"
    SELECT = "select"
    CHECKBOX = "checkbox"
    FILE_UPLOAD = "file-upload"
    WAIT = "wait"
    NAVIGATE = "navigate"
    SCROLL = "scroll"


class Action(BaseModel):
    semantic_name: str = Field(
        description="Human-readable name e.g. 'national_id_field'"
    )
    selector: Selector | None = Field(
        default=None,
        description="Target element selector. Not required for navigate/scroll.",
    )
    type: ActionType
    required_data_key: str | None = Field(
        None,
        description="Vault key referenced as placeholder e.g. 'national_id'",
    )
    placeholder_label: str | None = Field(
        None, description="Shown to user: 'Enter your National ID'"
    )
    validation_hint: str | None = Field(
        None, description="e.g. 'Must be 8 digits'"
    )
    # navigate action
    url: str | None = Field(None, description="URL for navigate action type.")
    # scroll action
    scroll_amount: int | None = Field(None, description="Pixels to scroll down for scroll type.")


class RecoveryAction(str, Enum):
    RETRY = "retry"
    RESTART_WORKFLOW = "restart_workflow"
    ESCALATE_TO_USER = "escalate_to_user"
    HEALING_REQUEST = "healing_request"


class ErrorState(BaseModel):
    condition: str = Field(
        description="e.g. 'Invalid KRA PIN message visible'"
    )
    selector: Selector
    recovery_action: RecoveryAction


class SubGoalSpec(BaseModel):
    """A contingency sub-goal shown to the user when a step fails."""

    label: str = Field(description="Human-readable label shown in the goal panel.")
    action: Literal["retry", "sub_service"] = Field(
        description="'retry' retries the failed step; 'sub_service' loads another service map."
    )
    service_id: str | None = Field(
        None,
        description="For action='sub_service': the service_id to load (e.g. 'ecitizen_forgot_password').",
    )


class WorkflowStep(BaseModel):
    step_id: str
    step_label: str
    url_match: str
    url_match_strategy: Literal["exact", "starts-with", "contains", "regex"] = (
        "contains"
    )
    # --------------- goal-planning fields ---------------
    phase: str | None = Field(
        None,
        description=(
            "Optional human-readable goal group label (e.g. 'Sign In', 'Fill Details'). "
            "Steps sharing the same phase are folded under one goal node in the goal panel. "
            "Falls back to step_label when absent."
        ),
    )
    failure_subgoals: list[SubGoalSpec] = Field(
        default=[],
        description=(
            "Contingency sub-goals shown when this step fails. "
            "Typically includes a 'retry' option and, for credential steps, "
            "a 'sub_service' option pointing at a recovery flow."
        ),
    )
    # ----------------------------------------------------
    actions: list[Action]
    next_trigger: Selector | None = None
    success_indicator: Selector
    error_states: list[ErrorState] = []
    requires_human_review: bool = False
    human_instruction: str | None = Field(
        None,
        description="Instruction shown to user when requires_human_review=true.",
    )
    requires_otp_input: bool = False
    otp_selector: str | None = Field(
        None,
        description="CSS selector for the OTP input field on the portal page.",
    )
    otp_submit_selector: str | None = Field(
        None,
        description="CSS selector for the submit button to click after filling OTP.",
    )
    estimated_wait_ms: int | None = None


class ServiceMap(BaseModel):
    service_id: str
    service_name: str
    portal: str
    version: str = Field(description="Semantic version string e.g. '1.0.0'")
    last_surveyed: str = Field(description="ISO 8601 datetime string")
    surveyor_confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0"
    )
    required_user_data: list[str] = Field(
        description="Vault keys required to execute this workflow"
    )
    # --------------- goal-planning fields ---------------
    requires_auth: str | None = Field(
        None,
        description=(
            "service_id of an auth flow that must run before this service. "
            "The planner auto-prepends the auth goal tree so maps never inline login steps."
        ),
    )
    # ----------------------------------------------------
    workflow: list[WorkflowStep]
    known_downtimes: list[str] = []

    @field_validator("version")
    @classmethod
    def validate_semver(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(
                f"version must be semver (e.g. '1.0.0'), got: {v!r}"
            )
        return v

    @field_validator("last_surveyed")
    @classmethod
    def validate_iso8601(cls, v: str) -> str:
        from datetime import datetime

        try:
            datetime.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(
                f"last_surveyed must be ISO 8601, got: {v!r}"
            ) from exc
        return v

    @field_validator("workflow")
    @classmethod
    def workflow_not_empty(cls, v: list[WorkflowStep]) -> list[WorkflowStep]:
        if not v:
            raise ValueError("workflow must contain at least one step")
        return v
