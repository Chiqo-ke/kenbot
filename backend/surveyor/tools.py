from __future__ import annotations

import json
import logging
from typing import Any

from langchain.tools import tool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# browser-use integration
# ---------------------------------------------------------------------------
# browser-use is LangChain-native. Its Agent class accepts a LangChain LLM
# directly and uses the browser's accessibility tree (not screenshots),
# making it cheaper and more reliable for government portal form detection.
# ---------------------------------------------------------------------------


async def run_browser_exploration(
    task: str,
    start_url: str,
    llm: Any,
    system_prompt: str | None = None,
) -> dict:
    """
    Spin up a browser-use Agent, run the exploration task, and return the
    raw JSON dict that the agent produced in its final result.

    The agent is instructed to return a ServiceMap-shaped JSON object.
    The caller (agent.py) is responsible for Pydantic validation.

    Args:
        system_prompt: Optional text appended to browser-use's default system
            prompt via ``extend_system_message``, making the agent aware of
            its role as the KenBot Surveyor.
    """
    from browser_use import Agent, Browser, BrowserConfig

    config = BrowserConfig(
        headless=True,
        # Disable JavaScript-only extensions/popups that clutter gov portals.
        disable_security=False,
    )
    browser = Browser(config=config)

    try:
        agent = Agent(
            task=task,
            llm=llm,
            browser=browser,
            # Tell browser-use to prefer the accessibility tree approach.
            use_vision=False,
            # Extend the default system prompt with the Surveyor's purpose.
            extend_system_message=system_prompt,
        )
        result = await agent.run()
        raw_text: str = result.final_result() or "{}"
        logger.debug("Surveyor: browser-use raw result: %s", raw_text[:500])
        return json.loads(raw_text)
    finally:
        await browser.close()


# ---------------------------------------------------------------------------
# LangChain @tool wrappers used inside the LangGraph nodes
# (available for future Pilot/healing toolchain extensions)
# ---------------------------------------------------------------------------


class ExplorePortalInput(BaseModel):
    service_id: str = Field(description="Unique identifier for the service")
    service_name: str = Field(
        description="Human-readable service name e.g. 'Good Conduct Certificate'"
    )
    start_url: str = Field(description="Entry URL for the service portal")


@tool("explore_portal", args_schema=ExplorePortalInput)
async def explore_portal_tool(
    service_id: str,
    service_name: str,
    start_url: str,
) -> str:
    """
    Explore a Kenyan government portal service and return a ServiceMap JSON
    string. Uses browser-use with GPT-4o via GitHub Models.

    The LLM must NEVER receive real credentials. Placeholder values are used.
    """
    from django.conf import settings
    from langchain.chat_models import init_chat_model

    from surveyor.agent import SurveyState, build_surveyor_graph

    raw_model = settings.KENBOT_SURVEYOR_MODEL  # e.g. "openai/gpt-4o"
    model_provider = raw_model.split("/", 1)[0] if "/" in raw_model else "openai"
    llm = init_chat_model(  # noqa: F841 — stored in state, not used directly here
        model=raw_model,
        model_provider=model_provider,
        base_url=settings.GITHUB_MODELS_BASE_URL,
        api_key=settings.GITHUB_TOKEN,
    )

    graph = build_surveyor_graph()
    initial_state: SurveyState = {
        "service_id": service_id,
        "service_name": service_name,
        "start_url": start_url,
        "raw_exploration": None,
        "service_map": None,
        "validation_issues": [],
        "status": "exploring",
        "healing_target": None,
        "attempt": 0,
    }

    final_state = await graph.ainvoke(initial_state)
    service_map = final_state.get("service_map")

    if service_map is None:
        issues = final_state.get("validation_issues", [])
        return json.dumps(
            {"error": "Survey failed", "issues": issues}
        )

    return service_map.model_dump_json(indent=2)


class RequestHealingInput(BaseModel):
    service_id: str = Field(description="Service whose map needs re-survey")
    step_id: str = Field(description="Specific step_id with broken selectors")
    failed_selector: str = Field(
        description="The selector string that failed in the extension"
    )


@tool("request_healing", args_schema=RequestHealingInput)
def request_healing_tool(
    service_id: str,
    step_id: str,
    failed_selector: str,
) -> str:
    """
    Queue a Celery healing task to re-survey a specific step whose selector
    has broken in the browser extension. Returns the Celery task ID.

    This tool is called by the Pilot when it receives a step_failed message
    from the extension over WebSocket.
    """
    from surveyor.tasks import heal_step

    task = heal_step.delay(
        service_id=service_id,
        step_id=step_id,
        failed_selector=failed_selector,
    )
    logger.info(
        "Surveyor: healing task %s queued for service='%s' step='%s'",
        task.id,
        service_id,
        step_id,
    )
    return json.dumps({"task_id": task.id, "status": "queued"})
