# Architecture

KenBot is a dual-agent system with a clear separation between the browser-side thin client and the server-side intelligence.

---

## High-Level Diagram

```
┌─────────────────────────────────────────────────────────┐
│                   Chrome Extension                       │
│                                                         │
│  popup.html/js ──► content.js ──► ui/overlay.js        │
│                        │                               │
│                  WebSocket client                       │
└────────────────────────│────────────────────────────────┘
                         │  wss://localhost:8000/ws/pilot/<session_id>/
                         │
┌────────────────────────▼────────────────────────────────┐
│                 Django (Daphne / ASGI)                   │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │              PilotConsumer                       │   │
│  │  (channels.generic.websocket.AsyncWebsocketConsumer) │
│  │                     │                            │   │
│  │            build_pilot_agent()                   │   │
│  │         LangChain AgentExecutor                  │   │
│  │                     │                            │   │
│  │   ┌─────────────────┴──────────────────────┐    │   │
│  │   │           LangChain Tools               │    │   │
│  │   │  load_service_map  confirm_submission    │    │   │
│  │   │  get_required_vault_keys  request_healing│    │   │
│  │   │  check_missing_vault_keys               │    │   │
│  │   └──────────────────────────────────────── ┘    │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  ┌──────────────┐   ┌─────────────┐   ┌─────────────┐  │
│  │ REST API      │   │   Maps DB   │   │  Vault DB   │  │
│  │ /api/pilot/  │   │ map_files/  │   │ AES-256-GCM │  │
│  │ /api/maps/   │   │ + models    │   │ + models    │  │
│  │ /api/vault/  │   └──────┬──────┘   └─────────────┘  │
│  │ /api/surveyor│          │                            │
│  └──────────────┘          │                            │
│                             │ Celery task                │
│  ┌──────────────────────────▼──────────────────────┐   │
│  │                 Surveyor Agent                   │   │
│  │              LangGraph workflow                  │   │
│  │                     │                            │   │
│  │            browser-use (Playwright)              │   │
│  │         crawls eCitizen / NTSA / KRA             │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  GitHub Models API (https://models.inference.ai.azure.com) │
│  openai/gpt-4o-mini (Pilot)   openai/gpt-4o (Surveyor)  │
└─────────────────────────────────────────────────────────┘
```

---

## Component Responsibilities

### Chrome Extension

| File | Role |
|------|------|
| `manifest.json` | Permissions, background service worker declaration |
| `background.js` | Service worker — manages auth state, communicates with popup |
| `content.js` | Injected into every page — mounts Shadow DOM overlay, owns the WebSocket connection |
| `popup.html/js` | Toolbar UI — login form, session controls |
| `ui/overlay.css` | Shadow DOM styles (isolated from page styles) |
| `ui/overlay.js` | Chat overlay logic — renders agent messages, step confirmations, vault prompts |

**Key rule:** Zero AI logic lives in the extension. It is a thin client that:
1. Opens a WebSocket to the Pilot
2. Executes `execute_step` instructions (clicking, typing, submitting)
3. Reports failures back
4. Injects vault values directly into DOM inputs

### Pilot Agent (`backend/pilot/`)

The Pilot is a **LangChain AgentExecutor** running inside a **Django Channels WebSocket consumer** (`AsyncWebsocketConsumer`). One consumer instance = one user session.

**State machine** (`pilot/state.py`) tracks a session through these statuses:

```
idle
 └─► loading_map
      └─► executing
           ├─► awaiting_user_confirmation  (user must approve before submitting)
           ├─► awaiting_captcha            (extension detected CAPTCHA)
           ├─► awaiting_healing            (selector failed, Surveyor queued)
           ├─► awaiting_vault_key          (credential not yet in vault)
           └─► completed / failed
```

**LangChain tools** (`pilot/tools.py`):

| Tool | What it does |
|------|-------------|
| `load_service_map` | Fetches the JSON ServiceMap for a service ID from `MapRepository` |
| `get_required_vault_keys` | Returns the list of credential keys a map needs |
| `check_missing_vault_keys` | Checks which required keys the user hasn't stored yet |
| `confirm_submission` | Pauses execution; sends `pause_confirmation` to extension; resumes when user confirms |
| `request_healing` | Queues a Celery Surveyor task to re-crawl and update a broken step |

### Surveyor Agent (`backend/surveyor/`)

The Surveyor is a **LangGraph** workflow that uses **browser-use** to autonomously crawl a government portal URL and output a validated `ServiceMap` JSON.

It is triggered:
- Manually via `POST /api/surveyor/trigger/` (authenticated)
- Automatically when the Pilot calls `request_healing`

The Surveyor runs as an async Celery task (`surveyor/tasks.py`), writes the map to `backend/map_files/`, and updates the `SurveyJob` model with status + result.

### Maps App (`backend/maps/`)

Stores and validates `ServiceMap` objects.

- **Schema** (`maps/schemas.py`) — Pydantic v2: `ServiceMap`, `WorkflowStep`, `Action`, `Selector`, `ErrorState`
- **Repository** (`maps/repository.py`) — file-backed store in `map_files/`, with DB model as index
- **REST API** — `GET /api/maps/` (list), `GET /api/maps/<service_id>/` (detail), `POST` (admin create)

### Vault App (`backend/vault/`)

Stores encrypted credentials per user.

- **Encryption** (`vault/encryption.py`) — AES-256-GCM via `cryptography` library
- Token format: `<nonce_b64>.<ciphertext_b64>` — nonce is unique per encryption call
- **REST API** — `POST /api/vault/` (store key+value), `GET /api/vault/<key>/` (retrieve decrypted), `DELETE /api/vault/<key>/`
- The Pilot agent **never** calls vault endpoints — only the browser extension calls them

---

## Data Flow: Executing a Task

```
User: "renew my driving licence"
  │
  ▼ WebSocket {"type": "user_message", "content": "renew my driving licence"}
  │
  ▼ PilotConsumer → AgentExecutor
  │   Tool: load_service_map("ntsa_driving_licence_renewal")
  │   Tool: check_missing_vault_keys([...])
  │     → if missing: send {"type": "await_vault_key", "missing_keys": [...]}
  │       user adds credentials via extension → vault stores encrypted
  │       WebSocket {"type": "vault_key_added"} → agent resumes
  │
  ▼ Agent decides next step → sends {"type": "execute_step", "actions": [...]}
  │   actions contain selectors + placeholder keys e.g. {{national_id}}
  │
  ▼ Extension executes step:
  │   - For {{national_id}}: GET /api/vault/national_id/ → inject value into DOM
  │   - Click, fill, submit
  │   - On success: WebSocket {"type": "step_confirmed"}
  │   - On selector failure: WebSocket {"type": "step_failed", "selector": "..."}
  │       → Pilot calls request_healing → Celery → Surveyor re-crawls → map updated
  │
  ▼ Before any form submission:
  │   Agent calls confirm_submission → pauses
  │   Extension shows confirmation dialog → user approves
  │   WebSocket {"type": "confirmation_response", "confirmed": true}
  │   Agent resumes → submits
  │
  ▼ Task complete → {"type": "session_complete"}
```

---

## Security Design

| Concern | Approach |
|---------|----------|
| LLM never sees credentials | Vault values replaced with `{{placeholder_key}}` in every prompt |
| DOM snapshots sanitized | All form `value` attributes stripped before sending page context to LLM |
| Extension auth | JWT (`Authorization: Bearer <token>`) on all REST API calls |
| WebSocket auth | `AuthMiddlewareStack` verifies JWT before `connect()` completes |
| Vault encryption | AES-256-GCM, unique nonce per write, key stored only in env var |
| Token isolation | GitHub token never in DB; loaded from filesystem/env at startup |
| Production TLS | `SECURE_SSL_REDIRECT`, HSTS, secure cookies enforced in production settings |

---

## Django App Graph

```
kenbot/           ← project package
 ├── settings/
 │    ├── base.py        loads .env via dotenv, GitHub token, model config
 │    ├── development.py DEBUG=True, CELERY_TASK_ALWAYS_EAGER
 │    └── production.py  HTTPS enforced, CORS restricted
 ├── asgi.py             ProtocolTypeRouter → HTTP + WebSocket
 ├── celery.py           Celery app, autodiscover_tasks
 ├── github_auth.py      token loader utility
 └── urls.py             /api/auth/ + app includes

pilot/            ← LangChain WebSocket agent
surveyor/         ← LangGraph crawl agent (Celery tasks)
maps/             ← ServiceMap storage
vault/            ← AES-256-GCM credential store
```
