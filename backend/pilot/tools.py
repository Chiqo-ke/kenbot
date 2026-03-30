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


class TriggerSurveyInput(BaseModel):
    service_id: str = Field(description="snake_case identifier for the service, e.g. 'renew_driving_license'.")
    service_name: str = Field(description="Human-readable service name, e.g. 'Driving Licence Renewal'.")
    start_url: str = Field(description="The portal URL where the Surveyor should begin exploration.")


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


class OpenPortalInput(BaseModel):
    url: str = Field(description="The portal URL to open for the user, e.g. 'https://ecitizen.go.ke/'.")
    missing_keys: str = Field(
        description=(
            "Comma-separated human-readable names of the fields the user must fill "
            "on the portal, e.g. 'National ID, Phone Number, Driving Licence Number'."
        )
    )


class ExecuteStepInput(BaseModel):
    service_id: str
    step_id: str
    step_label: str
    actions_json: str = Field(
        description="JSON array of Action objects from the ServiceMap step."
    )


class ExecuteWorkflowStepInput(BaseModel):
    service_id: str = Field(description="The service_id string from the ServiceMap.")
    step_id: str = Field(description="The step_id of the specific workflow step to execute.")


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
    """Load the validated JSON service map for a given government service.

    If the map is missing, returns {"error": ..., "needs_survey": true}.
    When you receive that response, immediately call trigger_survey to queue
    the Surveyor — do not attempt to automate without a map.
    """
    from maps.repository import MapRepository

    repo = MapRepository()
    service_map = repo.get_map(service_id)
    if service_map is None:
        return {
            "error": f"No active map found for service_id: {service_id}",
            "needs_survey": True,
        }
    return service_map.model_dump()


@tool(args_schema=TriggerSurveyInput)
def trigger_survey(service_id: str, service_name: str, start_url: str) -> str:
    """Queue the Surveyor to crawl a government portal and build a ServiceMap.

    Call this whenever load_service_map returns needs_survey=true.
    The survey runs asynchronously; tell the user it may take 1-3 minutes
    and that they should ask again once it completes.
    """
    from surveyor.tasks import survey_service

    task = survey_service.delay(
        service_id=service_id,
        service_name=service_name,
        start_url=start_url,
    )
    logger.info(
        "Pilot queued survey task=%s service_id=%s url=%s",
        task.id,
        service_id,
        start_url,
    )
    return (
        f"Survey job queued (task_id={task.id}). "
        f"The '{service_name}' service is not yet available — "
        "the Surveyor is building its automation map right now. "
        "This usually takes 2–5 minutes. "
        "Please tell the user: 'This service is not available yet. "
        "Please try again in a few minutes.'"
    )


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
    from pilot._session_context import get_current_anon_key  # noqa: PLC0415

    from maps.repository import MapRepository
    from vault.models import EncryptedVaultEntry

    anon_key = get_current_anon_key()
    repo = MapRepository()
    service_map = repo.get_map(service_id)
    if service_map is None:
        return f"ERROR: no map for service_id={service_id}"

    required = set(service_map.required_user_data)
    # If no anon_key is present the extension hasn't connected yet — treat all
    # keys as missing so the agent prompts the user to open the extension.
    if not anon_key:
        missing = sorted(required)
        return f"{_MISSING_KEY_PREFIX}:{','.join(missing)}"

    stored = set(
        EncryptedVaultEntry.objects.filter(
            anon_key=anon_key, vault_key__in=list(required)
        ).values_list("vault_key", flat=True)
    )
    missing = sorted(required - stored)
    if not missing:
        return "ALL_KEYS_PRESENT"
    return f"{_MISSING_KEY_PREFIX}:{','.join(missing)}"


@tool(args_schema=OpenPortalInput)
def open_portal_for_user(url: str, missing_keys: str) -> str:
    """Open a government portal URL in the user's browser so they can fill in
    their personal details directly on the site.

    Call this when check_missing_vault_keys returns missing keys AND the user
    has not stored their credentials in the vault. The extension will open the
    portal in the active tab. Instruct the user which fields to fill, then
    wait for them to confirm they are done before continuing.
    """
    return f"OPEN_URL:{url}:{missing_keys}"


@tool(args_schema=ExecuteWorkflowStepInput)
def execute_workflow_step(service_id: str, step_id: str) -> str:
    """Dispatch a single workflow step to the browser extension for execution.

    The extension will fill forms, click buttons, and navigate pages as defined
    in the step's actions. Call this immediately when check_missing_vault_keys
    returns ALL_KEYS_PRESENT, starting with the first step_id in the workflow.
    After receiving step_confirmed for each step, call this tool again with the
    next step_id. Never skip steps or improvise selectors.
    """
    import json as _json

    from maps.repository import MapRepository

    repo = MapRepository()
    service_map = repo.get_map(service_id)
    if service_map is None:
        return f"ERROR: no map found for service_id={service_id}"

    workflow = service_map.workflow
    step = None
    step_index = 0
    for i, s in enumerate(workflow):
        if s.step_id == step_id:
            step = s
            step_index = i
            break

    if step is None:
        available = [s.step_id for s in workflow]
        return (
            f"ERROR: step_id='{step_id}' not found in workflow for "
            f"service_id={service_id}. Available step IDs: {available}"
        )

    payload = {
        **step.model_dump(),
        "step_index": step_index,
        "total_steps": len(workflow),
        "service_id": service_id,
    }
    logger.info(
        "execute_workflow_step dispatching step_id=%s index=%d/%d service_id=%s",
        step_id,
        step_index,
        len(workflow),
        service_id,
    )
    return f"EXECUTE_STEP:{_json.dumps(payload)}"


# ---------------------------------------------------------------------------
# Exported tool list — consumed by agent.py
# ---------------------------------------------------------------------------

PILOT_TOOLS = [
    load_service_map,
    trigger_survey,
    request_healing,
    confirm_submission,
    execute_workflow_step,
]
