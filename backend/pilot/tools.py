from __future__ import annotations

import logging
import re

from langchain.tools import tool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool input schemas (Pydantic v2)
# ---------------------------------------------------------------------------


class LoadServiceMapInput(BaseModel):
    service_id: str = Field(description="The service_id string from the ServiceMap.")


class GetRequiredVaultKeysInput(BaseModel):
    service_id: str = Field(description="The service_id string from the ServiceMap.")


class HealingRequestInput(BaseModel):
    service_id: str = Field(description="Service whose step needs re-mapping.")
    step_id: str = Field(description="The step_id that failed.")
    failed_selector: str = Field(description="The selector string that failed.")


class ConfirmSubmissionInput(BaseModel):
    step_label: str = Field(description="Human-readable label of the step about to submit.")
    fields_summary: str = Field(
        description=(
            "Comma-separated list of FIELD NAMES only (e.g. 'national_id, full_name'). "
            "Must NEVER include actual credential values."
        )
    )


class ExecuteStepInput(BaseModel):
    service_id: str
    step_id: str
    step_label: str
    actions_json: str = Field(
        description="JSON array of Action objects from the ServiceMap step."
    )


# ---------------------------------------------------------------------------
# Sentinel prefix the consumer reads to pause execution
# ---------------------------------------------------------------------------

_PAUSE_PREFIX = "PAUSE_FOR_CONFIRMATION"
_MISSING_KEY_PREFIX = "AWAIT_VAULT_KEY"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(args_schema=LoadServiceMapInput)
def load_service_map(service_id: str) -> dict:
    """Load the validated JSON service map for a given government service."""
    from maps.repository import MapRepository

    repo = MapRepository()
    service_map = repo.get_map(service_id)
    if service_map is None:
        return {"error": f"No active map found for service_id: {service_id}"}
    return service_map.model_dump()


@tool(args_schema=GetRequiredVaultKeysInput)
def get_required_vault_keys(service_id: str) -> list[str]:
    """Return the list of vault data keys required to complete a service workflow."""
    from maps.repository import MapRepository

    repo = MapRepository()
    service_map = repo.get_map(service_id)
    if service_map is None:
        logger.warning("get_required_vault_keys: no map for service_id=%s", service_id)
        return []
    return service_map.required_user_data


@tool(args_schema=HealingRequestInput)
def request_healing(
    service_id: str,
    step_id: str,
    failed_selector: str,
) -> str:
    """
    Request the Surveyor to re-map a specific step whose selector has broken.

    The Surveyor will re-explore the portal to find a working selector for the
    given step, then update the ServiceMap on disk.
    """
    from surveyor.tasks import heal_step  # imported lazily to avoid circular deps

    task = heal_step.delay(service_id, step_id, failed_selector)
    logger.info(
        "Healing task queued service_id=%s step_id=%s task_id=%s",
        service_id,
        step_id,
        task.id,
    )
    return f"Healing request queued. Task ID: {task.id}. Waiting for Surveyor."


@tool(args_schema=ConfirmSubmissionInput)
def confirm_submission(step_label: str, fields_summary: str) -> str:
    """
    Pause the workflow and ask the user to confirm before any submit/pay action.

    Always call this before steps whose semantic_name contains 'submit', 'pay',
    'confirm', or 'proceed'. fields_summary must contain ONLY field NAMES,
    never actual credential values.
    """
    # Privacy guard: reject if the summary looks like it contains real values
    _SENSITIVE = re.compile(
        r"\b(?:[A-Z]{1,2}\d{6,8}|A\d{9}[A-Z]|\d{10,16})\b"  # IDs / card numbers
    )
    if _SENSITIVE.search(fields_summary):
        logger.error(
            "confirm_submission received fields_summary that may contain "
            "real credential values. Sanitising."
        )
        fields_summary = _SENSITIVE.sub("[REDACTED]", fields_summary)

    return f"{_PAUSE_PREFIX}:{step_label}:{fields_summary}"


@tool(args_schema=GetRequiredVaultKeysInput)
def check_missing_vault_keys(service_id: str) -> str:
    """
    Check which required vault keys the current user has NOT yet stored.

    Returns a AWAIT_VAULT_KEY sentinel the consumer reads to prompt the user,
    or 'ALL_KEYS_PRESENT' if nothing is missing.
    """
    # NOTE: The consumer resolves the actual user from the WebSocket scope and
    # passes it via a thread-local set before calling the agent. We read it here.
    from pilot._session_context import get_current_user  # noqa: PLC0415

    from maps.repository import MapRepository
    from vault.models import EncryptedVaultEntry

    user = get_current_user()
    repo = MapRepository()
    service_map = repo.get_map(service_id)
    if service_map is None:
        return f"ERROR: no map for service_id={service_id}"

    required = set(service_map.required_user_data)
    stored = set(
        EncryptedVaultEntry.objects.filter(
            user=user, vault_key__in=list(required)
        ).values_list("vault_key", flat=True)
    )
    missing = sorted(required - stored)
    if not missing:
        return "ALL_KEYS_PRESENT"
    return f"{_MISSING_KEY_PREFIX}:{','.join(missing)}"


# ---------------------------------------------------------------------------
# Exported tool list — consumed by agent.py
# ---------------------------------------------------------------------------

PILOT_TOOLS = [
    load_service_map,
    get_required_vault_keys,
    request_healing,
    confirm_submission,
    check_missing_vault_keys,
]
