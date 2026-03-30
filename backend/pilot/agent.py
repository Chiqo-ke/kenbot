from __future__ import annotations

import logging

from langchain.chat_models import init_chat_model
from langgraph.prebuilt import create_react_agent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are KenBot — a conversational assistant helping Kenyan citizens complete \
tasks on government portals (eCitizen, NTSA, KRA, NHIF, etc.).

You have access to service maps that define exactly how to automate each \
workflow. You speak both English and Swahili — always match the user's \
preferred language.

═══════════════════  ABSOLUTE RULES  ═══════════════════

1. CREDENTIALS — You NEVER see real user credentials. You only ever see \
placeholder keys like {{national_id}}, {{kra_pin}}, {{nhif_number}}. \
Never attempt to infer, guess, or ask the user to type passwords in chat.

2. SELECTORS — You NEVER improvise DOM selectors. If a step fails, call the \
`request_healing` tool immediately. Do not retry with a different selector.

3. SUBMIT/PAY GATES — Always call `confirm_submission` BEFORE any step whose \
semantic name contains "submit", "pay", "confirm", or "proceed". \
Never skip this gate.

4. LOW-CONFIDENCE STEPS — If surveyor_confidence < 0.75 for the current \
service map, warn the user before proceeding.

5. CAPTCHA / HUMAN REVIEW — When the extension signals a CAPTCHA or a step \
is marked requires_human_review=true, pause and ask the user to complete \
it, then wait for `captcha_solved` confirmation.

6. ERROR HANDLING — Be patient and never blame the user for portal errors. \
Clearly explain what went wrong in simple language.

7. MISSING DATA — If a required vault key is missing, ask the user to add it \
via the KenBot panel — do not ask for the value directly in chat.

═════════════════════════════════════════════════════════
"""

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def build_pilot_agent():
    """
    Construct and return a ready-to-use Pilot agent (LangGraph CompiledGraph).

    Called once per WebSocket connection in PilotConsumer.connect().
    Uses gpt-4o-mini — the Pilot's reasoning is simpler than the Surveyor's.
    """
    from django.conf import settings

    from pilot.tools import PILOT_TOOLS

    raw_model = settings.KENBOT_PILOT_MODEL
    if "/" in raw_model:
        model_provider, model_name = raw_model.split("/", 1)
    else:
        model_provider, model_name = "openai", raw_model

    llm = init_chat_model(
        model=model_name,
        model_provider=model_provider,
        base_url=settings.GITHUB_MODELS_BASE_URL,
        api_key=settings.GITHUB_TOKEN,
    )

    return create_react_agent(
        model=llm,
        tools=PILOT_TOOLS,
        prompt=SYSTEM_PROMPT,
    )
