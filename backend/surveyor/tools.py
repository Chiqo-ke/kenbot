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


def _patch_llm_for_browser_use(llm: Any) -> Any:
    """
    browser-use sets arbitrary attributes on the LLM object during Agent
    construction (e.g. ``provider``, ``ainvoke``).
    LangChain's Pydantic v2 ChatOpenAI blocks those with:
        ValueError: "ChatOpenAI" object has no field "<name>"
    We wrap the LLM in a thin proxy that stores all such overrides in a plain
    dict and forwards everything else to the real LLM, so browser-use can
    augment it freely without hitting Pydantic.
    """

    class _LLMProxy:
        """Proxy that absorbs arbitrary setattr calls from browser-use."""

        def __init__(self, wrapped: Any) -> None:
            # Use object.__setattr__ so our own __setattr__ is not triggered.
            object.__setattr__(self, "_w", wrapped)
            # Pre-seed the provider attribute browser-use checks on init.
            object.__setattr__(self, "_overrides", {"provider": "openai"})

        def __setattr__(self, name: str, value: Any) -> None:
            # Store ANY attribute browser-use (or any caller) tries to set
            # in a plain dict — completely bypasses Pydantic validation.
            object.__getattribute__(self, "_overrides")[name] = value

        def __getattr__(self, name: str) -> Any:
            overrides = object.__getattribute__(self, "_overrides")
            if name in overrides:
                return overrides[name]
            return getattr(object.__getattribute__(self, "_w"), name)

    return _LLMProxy(llm)


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
    # browser-use API changed across versions.  Try the modern path first
    # (BrowserProfile passed directly to Agent), then fall back to the legacy
    # Browser(config=BrowserConfig(...)) pattern.
    try:
        from browser_use import Agent, BrowserProfile

        profile = BrowserProfile(
            headless=True,
            disable_security=False,
        )
        llm = _patch_llm_for_browser_use(llm)
        agent = Agent(
            task=task,
            llm=llm,
            browser_profile=profile,
            use_vision=False,
            extend_system_message=system_prompt,
        )
        result = await agent.run()
        raw_text: str = result.final_result() or "{}"
        logger.debug("Surveyor: browser-use raw result: %s", raw_text[:500])
        return json.loads(raw_text)
    except (ImportError, TypeError):
        # Legacy API: Browser wraps a BrowserConfig/BrowserProfile object
        try:
            from browser_use import Agent, Browser, BrowserProfile as _BrowserConfig
        except ImportError:
            from browser_use import Agent, Browser, BrowserConfig as _BrowserConfig  # type: ignore[no-redef]

        _config = _BrowserConfig(
            headless=True,
            disable_security=False,
        )
        browser = Browser(config=_config)
        try:
            llm = _patch_llm_for_browser_use(llm)
            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                use_vision=False,
                extend_system_message=system_prompt,
            )
            result = await agent.run()
            raw_text = result.final_result() or "{}"
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
    string. Uses browser-use with GPT-4o mini via GitHub Models.

    The LLM must NEVER receive real credentials. Placeholder values are used.
    """
    from django.conf import settings
    from langchain.chat_models import init_chat_model

    from surveyor.agent import SurveyState, build_surveyor_graph

    raw_model = settings.KENBOT_SURVEYOR_MODEL  # e.g. "openai/gpt-4o-mini"
    # Strip the vendor prefix if present — GitHub Models API is always OpenAI-compatible.
    model_name = raw_model.split("/", 1)[-1] if "/" in raw_model else raw_model
    llm = init_chat_model(  # noqa: F841 — stored in state, not used directly here
        model=model_name,
        model_provider="openai",  # GitHub Models endpoint is OpenAI-compatible
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
