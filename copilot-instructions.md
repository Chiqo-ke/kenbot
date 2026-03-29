# KenBot — GitHub Copilot Project Instructions

## What This Project Is
KenBot is a dual-agent system that automates Kenyan government portal interactions (eCitizen, NTSA, KRA) using natural language (English/Swahili). It has two agents:
- **The Surveyor** (`backend/surveyor/`) — explores portals, builds JSON state machine maps
- **The Pilot** (`backend/pilot/`) — executes user tasks via those maps over WebSocket

Both run as Python Django services. A plain JS browser extension is the thin client in the browser.

## Python Stack (do not suggest alternatives)
- Python 3.12+, Django 5.x, Django REST Framework, Django Channels
- LangGraph for Surveyor workflows, LangChain agents for Pilot
- `browser-use` for Surveyor browser automation (NOT raw Playwright)
- Pydantic v2 for all schema validation (NOT dataclasses or marshmallow)
- Celery + Redis for background tasks (Surveyor jobs)
- `cryptography` (Fernet) for vault encryption
- `uv` as package manager

## LLM / API
- Use `openai` Python SDK with `base_url="https://models.inference.ai.azure.com"` and `api_key=settings.GITHUB_TOKEN`
- Use `init_chat_model("openai/gpt-4o")` for Surveyor (heavy reasoning)
- Use `init_chat_model("openai/gpt-4o-mini")` for Pilot (lighter, cheaper)
- Never hardcode tokens. Always use `settings.GITHUB_TOKEN` from environment.

## Browser Extension
- Plain JavaScript only — NO TypeScript, NO React, NO bundler
- The extension is a thin client: it mounts Shadow DOM UI, manages WebSocket to Django Channels, and injects vault data into the DOM
- Zero AI logic in the extension. All reasoning stays in Django.

## Privacy Rules (non-negotiable)
- The LLM must NEVER see actual vault values (passwords, National IDs, KRA PINs)
- The Pilot agent sees only placeholder keys like `{{national_id}}`
- Actual decryption happens in `vault/views.py` — the extension fetches decrypted values and injects directly into the DOM
- Strip all form `value` attributes from any DOM snapshot before sending to an LLM

## Code Style
- Type hints everywhere. Use `from __future__ import annotations` for forward refs.
- Pydantic v2 models for all tool inputs/outputs (use `BaseModel`, not `TypedDict`)
- LangChain tools defined with `@tool` decorator — always include a clear docstring
- Django views: use class-based views (APIView or ViewSet) via DRF
- Async consumers: use `AsyncWebsocketConsumer` in Django Channels
- Never use `print()` — use Django's logging (`import logging; logger = logging.getLogger(__name__)`)

## Map Schema
The canonical map format is `ServiceMap` from `backend/maps/schemas.py`. Every map must pass `ServiceMap.model_validate(data)` before being written to disk. Do not define map structure in any other file.

## Selector Priority
Always instruct the LLM to prefer (in order): ARIA labels/roles > data-testid/data-cy > name attribute > XPath > CSS ID. Never position-based or nth-child selectors.

## Healing Protocol
If a selector fails in the extension, it sends `{"type": "step_failed", "selector": "..."}` to the Pilot via WebSocket. The Pilot calls the `request_healing` tool which queues a Celery task in the Surveyor. The extension never improvises selectors on its own.
