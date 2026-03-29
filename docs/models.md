# GitHub Models

KenBot uses the [GitHub Models](https://github.com/marketplace/models) API — OpenAI-compatible, backed by Azure AI, accessible with any GitHub personal access token.

---

## Endpoint

```
https://models.inference.ai.azure.com
```

This is configured in `backend/kenbot/settings/base.py`:

```python
GITHUB_MODELS_BASE_URL = "https://models.inference.ai.azure.com"
KENBOT_PILOT_MODEL    = os.environ.get("KENBOT_PILOT_MODEL",    "openai/gpt-4o-mini")
KENBOT_SURVEYOR_MODEL = os.environ.get("KENBOT_SURVEYOR_MODEL", "openai/gpt-4o")
```

---

## Default Models

| Agent | Default Model | Rationale |
|-------|--------------|-----------|
| Pilot | `openai/gpt-4o-mini` | Lightweight, fast, low cost. Handles step-by-step task execution where reasoning demand is lower. |
| Surveyor | `openai/gpt-4o` | Stronger reasoning for complex portal crawls, understanding ambiguous page layouts. |

---

## Available Models

The following models are available via GitHub Models. Use the exact model ID string in the env vars.

### OpenAI

| Model ID | Context | Notes |
|----------|---------|-------|
| `openai/gpt-4o` | 128k | Best overall reasoning; default Surveyor |
| `openai/gpt-4o-mini` | 128k | Fast and cheap; default Pilot |
| `openai/o1` | 200k | Extended thinking; use for very complex crawls |
| `openai/o1-mini` | 128k | Compact reasoning model |
| `openai/o3-mini` | 200k | Latest compact reasoning |

### Mistral

| Model ID | Context | Notes |
|----------|---------|-------|
| `mistral-ai/mistral-large` | 128k | Strong open-weight alternative |
| `mistral-ai/mistral-nemo` | 128k | Smaller, fast |
| `mistral-ai/codestral-2501` | 256k | Code-focused |

### Meta Llama

| Model ID | Context | Notes |
|----------|---------|-------|
| `meta/llama-3.3-70b-instruct` | 128k | Strong open-weight model |
| `meta/llama-3.2-90b-vision-instruct` | 128k | Multimodal — can interpret page screenshots |
| `meta/llama-3.2-11b-vision-instruct` | 128k | Smaller vision model |
| `meta/llama-3.1-8b-instruct` | 128k | Very fast, lightweight |

### Anthropic

| Model ID | Context | Notes |
|----------|---------|-------|
| `anthropic/claude-3-5-sonnet` | 200k | Excellent reasoning and following complex instructions |
| `anthropic/claude-3-5-haiku` | 200k | Fast Claude; good Pilot candidate |
| `anthropic/claude-3-7-sonnet` | 200k | Extended thinking mode available |

### Cohere

| Model ID | Notes |
|----------|-------|
| `cohere/command-r-plus` | Strong at retrieval-augmented tasks |
| `cohere/command-r` | Lighter variant |

### DeepSeek

| Model ID | Notes |
|----------|-------|
| `deepseek/deepseek-r1` | Strong reasoning, open weights |
| `deepseek/deepseek-v3` | Fast general model |

> **Check current availability:** https://github.com/marketplace/models — model availability can change.

---

## Switching Models

### Option 1 — Environment variable (persistent)

Edit `backend/.env`:

```dotenv
KENBOT_PILOT_MODEL=anthropic/claude-3-5-haiku
KENBOT_SURVEYOR_MODEL=anthropic/claude-3-5-sonnet
```

Restart the server for changes to take effect.

### Option 2 — start.ps1 flags (one-off override)

```powershell
.\start.ps1 -PilotModel "openai/gpt-4o" -SurveyorModel "meta/llama-3.3-70b-instruct"
```

The flags set `$env:KENBOT_PILOT_MODEL` and `$env:KENBOT_SURVEYOR_MODEL` for the duration of that process.

### Option 3 — PowerShell session variable

```powershell
$env:KENBOT_PILOT_MODEL = "openai/gpt-4o"
python manage.py runserver   # or daphne
```

---

## Authentication

GitHub Models accepts any GitHub personal access token with the `models:read` scope (or any classic PAT). The device-flow token created by `auth_github.py` has this scope by default.

The token is loaded by Django at startup in this priority order:

1. `GITHUB_TOKEN` environment variable
2. `~/.kenbot/github_token` (written by `auth_github.py`)
3. `backend/.github_token` (mirror)

If no token is found, `settings.GITHUB_TOKEN` is set to `""` — Django management commands (like `migrate`) still work, but agent calls will fail.

---

## Rate Limits

GitHub Models imposes per-model rate limits (typically 60 requests/minute for free tier). The Surveyor's browser-use integration makes multiple LLM calls per crawl. For heavy usage:

- Use `openai/gpt-4o-mini` for the Pilot (cheaper, higher rate limits)
- Schedule Surveyor jobs during off-peak hours
- Monitor rate limit headers in Django logs

---

## Adding a New Model

No code changes are needed — just update the model ID string. The `init_chat_model()` call in `pilot/agent.py` and `surveyor/agent.py` passes the model name directly to the OpenAI-compatible SDK:

```python
from langchain.chat_models import init_chat_model

llm = init_chat_model(
    settings.KENBOT_PILOT_MODEL,
    base_url=settings.GITHUB_MODELS_BASE_URL,
    api_key=settings.GITHUB_TOKEN,
)
```

Any model available at the GitHub Models endpoint that is compatible with the OpenAI chat completions API schema will work.

---

## Vision / Multimodal Models

If you use a vision-capable model (e.g. `meta/llama-3.2-90b-vision-instruct`), the Surveyor can be extended to pass page screenshots to the LLM instead of raw HTML. The current implementation sends text-only page context. Vision support can be added to `surveyor/agent.py` by base64-encoding a Playwright screenshot and including it as an image message part.
