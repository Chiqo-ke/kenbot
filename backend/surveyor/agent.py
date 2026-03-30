from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, TypedDict

from langchain.chat_models import init_chat_model
from langgraph.graph import END, StateGraph

from maps.schemas import ServiceMap, WorkflowStep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Surveyor system prompt
# ---------------------------------------------------------------------------

SURVEYOR_SYSTEM_PROMPT = """\
You are the KenBot Surveyor — an autonomous browser agent specialised in \
mapping Kenyan government portal workflows so that the KenBot Pilot can later \
automate them on behalf of citizens.

Your sole purpose is to EXPLORE and DOCUMENT — never to perform real \
transactions on a user's behalf.

CORE RULES:
1. NEVER submit real personal data. Use only placeholder values such as \
TEST_USER, TEST_IDNO_12345678, TEST_PIN_123456, TEST_EMAIL@test.com.
2. For every page / step in the workflow, record:
   a. All form fields — ARIA labels, name attributes, data-testid / data-cy.
   b. Submit / Next button — ARIA role or stable data attribute.
   c. Success indicator — text string, element, or URL change that confirms \
the step completed successfully.
   d. Error messages — their text and CSS/XPath selectors.
   e. Any CAPTCHA, MFA prompt, or human-review gate.
3. Prefer ARIA-based and data-attribute selectors over fragile XPath indices.
4. Return your complete findings as a single JSON object that matches the \
ServiceMap schema. Do NOT wrap it in markdown fences.
5. If a page requires authentication, document the login step as the first \
workflow step using the known eCitizen login selectors.
"""

# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------


class SurveyState(TypedDict):
    service_id: str
    start_url: str
    service_name: str
    raw_exploration: dict | None
    service_map: ServiceMap | None
    validation_issues: list[str]
    status: Literal[
        "exploring", "validating", "healing", "complete", "failed"
    ]
    healing_target: str | None  # step_id that needs re-survey
    attempt: int  # guard against infinite re-explore loops


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_HEALING_ATTEMPTS = 3


def _get_llm():
    """Return the configured Surveyor LLM via the GitHub Models endpoint."""
    from django.conf import settings

    raw_model = settings.KENBOT_SURVEYOR_MODEL  # e.g. "openai/claude-sonnet-4-6"
    if "/" in raw_model:
        model_provider, model_name = raw_model.split("/", 1)
    else:
        model_provider, model_name = "openai", raw_model

    return init_chat_model(
        model=model_name,
        model_provider=model_provider,
        base_url=settings.GITHUB_MODELS_BASE_URL,
        api_key=settings.GITHUB_TOKEN,
    )


# ---------------------------------------------------------------------------
# Node: explore_portal
# ---------------------------------------------------------------------------


async def explore_portal(state: SurveyState) -> SurveyState:
    """Run browser-use Agent to explore the portal and produce raw JSON."""
    from surveyor.tools import run_browser_exploration

    logger.info(
        "Surveyor: starting exploration of '%s' at %s",
        state["service_name"],
        state["start_url"],
    )

    healing_context = ""
    if state.get("healing_target"):
        healing_context = (
            f"\nFocus re-exploration on step '{state['healing_target']}' "
            "which previously produced low-confidence selectors."
        )

    task = (
        f"Explore the '{state['service_name']}' government portal workflow "
        f"starting at {state['start_url']}.\n"
        "For every page/step in the workflow:\n"
        "  1. List all form fields with their ARIA labels, name attributes, "
        "     and any data-testid/data-cy attributes.\n"
        "  2. Identify the submit/next button using ARIA roles or stable attributes.\n"
        "  3. Document success indicators (text, element, or URL change confirming "
        "     the step completed).\n"
        "  4. Document visible error messages and their selectors.\n"
        "  5. Note any captchas, timeouts, or MFA prompts.\n"
        "IMPORTANT: Do NOT submit real personal data. Use placeholder values such as "
        "'TEST_USER', 'TEST_IDNO', 'TEST_PIN'.\n"
        "Return your complete findings as a single JSON object matching the "
        "ServiceMap schema.\n"
        f"{healing_context}"
    )

    try:
        raw = await run_browser_exploration(
            task=task,
            start_url=state["start_url"],
            llm=_get_llm(),
            system_prompt=SURVEYOR_SYSTEM_PROMPT,
        )
        return {
            **state,
            "raw_exploration": raw,
            "status": "validating",
            "attempt": state.get("attempt", 0) + 1,
        }
    except Exception as exc:
        logger.exception(
            "Surveyor: exploration failed for '%s': %s",
            state["service_name"],
            exc,
        )
        return {**state, "status": "failed", "validation_issues": [str(exc)]}


# ---------------------------------------------------------------------------
# Node: validate_map
# ---------------------------------------------------------------------------


def validate_map(state: SurveyState) -> SurveyState:
    """Parse raw exploration output into a ServiceMap and evaluate confidence."""
    raw = state.get("raw_exploration")
    if not raw:
        return {
            **state,
            "status": "failed",
            "validation_issues": ["No raw exploration data to validate."],
        }

    issues: list[str] = []

    try:
        # Inject metadata that browser-use cannot reliably infer.
        raw.setdefault("service_id", state["service_id"])
        raw.setdefault("service_name", state["service_name"])
        raw.setdefault(
            "last_surveyed",
            datetime.now(tz=timezone.utc).isoformat(),
        )
        raw.setdefault("version", "1.0.0")
        raw.setdefault("surveyor_confidence", 0.5)

        service_map = ServiceMap.model_validate(raw)

        # Mark low-confidence steps for human review.
        if service_map.surveyor_confidence < 0.7:
            issues.append(
                f"Overall confidence {service_map.surveyor_confidence:.2f} < 0.7 — "
                "flagging affected steps for human review."
            )
            steps_with_review: list[WorkflowStep] = []
            for step in service_map.workflow:
                steps_with_review.append(step.model_copy(update={"requires_human_review": True}))
            service_map = service_map.model_copy(
                update={"workflow": steps_with_review}
            )
            logger.warning(
                "Surveyor: low confidence (%.2f) for service '%s' — all steps flagged.",
                service_map.surveyor_confidence,
                state["service_id"],
            )

        # Check for steps missing ARIA-based selectors.
        for step in service_map.workflow:
            for action in step.actions:
                if action.selector.strategy.value not in ("aria", "data-attr"):
                    issues.append(
                        f"Step '{step.step_id}' action '{action.semantic_name}' "
                        f"uses '{action.selector.strategy.value}' — prefer ARIA/data-attr."
                    )

        return {
            **state,
            "service_map": service_map,
            "validation_issues": issues,
            "status": "validating",
        }

    except Exception as exc:
        logger.exception(
            "Surveyor: map validation failed for '%s': %s",
            state["service_id"],
            exc,
        )
        return {
            **state,
            "status": "failed",
            "validation_issues": [f"Pydantic validation error: {exc}"],
        }


# ---------------------------------------------------------------------------
# Node: persist_map
# ---------------------------------------------------------------------------


def persist_map(state: SurveyState) -> SurveyState:
    """Write the validated ServiceMap to disk and Django DB via MapRepository."""
    from maps.repository import MapRepository

    service_map = state["service_map"]
    if service_map is None:
        return {**state, "status": "failed"}

    # Derive a stable relative path: <portal>/<service_id>.json
    relative_path = f"{service_map.portal}/{service_map.service_id}.json"

    try:
        repo = MapRepository()
        repo.save_map(service_map, relative_path)
        logger.info(
            "Surveyor: map persisted for service '%s' v%s at %s",
            service_map.service_id,
            service_map.version,
            relative_path,
        )
        return {**state, "status": "complete"}
    except Exception as exc:
        logger.exception(
            "Surveyor: failed to persist map for '%s': %s",
            state["service_id"],
            exc,
        )
        return {**state, "status": "failed", "validation_issues": [str(exc)]}


# ---------------------------------------------------------------------------
# Node: flag_for_human_review
# ---------------------------------------------------------------------------


def flag_for_human_review(state: SurveyState) -> SurveyState:
    """Persist the map but mark it as requiring human review in the DB."""
    from maps.repository import MapRepository

    service_map = state.get("service_map")
    issues = state.get("validation_issues", [])

    logger.warning(
        "Surveyor: flagging map for '%s' for human review. Issues: %s",
        state["service_id"],
        issues,
    )

    if service_map is not None:
        try:
            repo = MapRepository()
            relative_path = f"{service_map.portal}/{service_map.service_id}.json"
            repo.save_map(service_map, relative_path)
        except Exception as exc:
            logger.exception(
                "Surveyor: could not persist flagged map for '%s': %s",
                state["service_id"],
                exc,
            )

    return {**state, "status": "complete"}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route_after_validation(
    state: SurveyState,
) -> Literal["persist", "flag", "re_explore", "failed"]:
    """Decide what to do after the validate_map node."""
    if state["status"] == "failed":
        return "failed"

    service_map = state.get("service_map")
    if service_map is None:
        return "failed"

    attempt = state.get("attempt", 1)
    confidence = service_map.surveyor_confidence

    # If confidence is fine, persist immediately.
    if confidence >= 0.7:
        return "persist"

    # If confidence is low but we haven't exceeded healing attempts, re-explore.
    if confidence < 0.7 and attempt < _MAX_HEALING_ATTEMPTS:
        logger.info(
            "Surveyor: confidence %.2f < 0.7 for '%s', attempt %d — re-exploring.",
            confidence,
            state["service_id"],
            attempt,
        )
        return "re_explore"

    # Max attempts reached — flag for human review.
    return "flag"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def build_surveyor_graph():
    """Compile and return the Surveyor LangGraph state machine."""
    graph = StateGraph(SurveyState)

    graph.add_node("explore", explore_portal)
    graph.add_node("validate", validate_map)
    graph.add_node("persist", persist_map)
    graph.add_node("flag_review", flag_for_human_review)

    graph.set_entry_point("explore")
    graph.add_edge("explore", "validate")

    graph.add_conditional_edges(
        "validate",
        route_after_validation,
        {
            "persist": "persist",
            "flag": "flag_review",
            "re_explore": "explore",
            "failed": END,
        },
    )

    graph.add_edge("persist", END)
    graph.add_edge("flag_review", END)

    return graph.compile()
