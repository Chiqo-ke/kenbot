# KenBot

A dual-agent system that automates Kenyan government portal interactions (eCitizen, NTSA, KRA) using natural language commands in English or Swahili.

---

## How It Works

1. **You type** a natural-language request ("renew my driving licence") into the Chrome extension.
2. The **Pilot agent** receives your message over WebSocket, looks up the relevant service map, and walks you through each step.
3. When a form field requires a sensitive credential (National ID, KRA PIN, password), the extension securely fetches it from your **encrypted vault** and injects it directly into the DOM — the AI never sees the actual value.
4. If a page selector stops working, the extension reports the failure; the Pilot queues a **Surveyor** task to re-crawl and heal the map automatically.

```
Browser Extension  ←──WebSocket──→  Pilot Agent (Django Channels)
                                         │
                                    Maps / Vault
                                         │
                                    Surveyor Agent  →  browser-use
```

---

## Repository Layout

```
kenbot/
├── auth_github.py          # One-time GitHub device-flow auth script
├── backend/                # Django project
│   ├── kenbot/             # Project package (settings, urls, asgi, celery)
│   ├── pilot/              # WebSocket agent — executes tasks
│   ├── surveyor/           # Crawler agent — builds & heals maps
│   ├── maps/               # ServiceMap storage & schema
│   ├── vault/              # AES-256-GCM credential vault
│   ├── manage.py
│   ├── start.ps1           # Windows launch script
│   └── .env.example        # Environment variables template
└── extension/              # Chrome extension (plain JS)
    ├── manifest.json
    ├── background.js
    ├── content.js
    ├── popup.html / popup.js
    └── ui/                 # Shadow DOM overlay
```

---

## Quick Start

### Prerequisites

| Tool | Min version | Notes |
|------|-------------|-------|
| Python | 3.12 | 3.13 works too |
| Redis | 7 | For Celery task queue |
| Google Chrome | any | Extension target |
| Git | — | — |

### 1 — Clone & create virtual environment

```powershell
git clone <repo-url> kenbot
cd kenbot

python -m venv .venv          # or: uv venv
.venv\Scripts\Activate.ps1
```

### 2 — Install dependencies

```powershell
cd backend
pip install -r requirements.txt
# or with uv:
uv pip install -r requirements.txt
```

### 3 — Authenticate with GitHub

KenBot uses [GitHub Models](https://github.com/marketplace/models) for all LLM calls. Run the device-flow script once:

```powershell
# from repo root
python auth_github.py
```

Follow the on-screen instructions — open the URL, enter the 8-character code, and approve. Your token is saved to `~/.kenbot/github_token` and mirrored to `backend/.github_token`. See [docs/setup.md](docs/setup.md) for details.

### 4 — Configure environment

```powershell
cd backend
Copy-Item .env.example .env
```

Edit `.env` — the three mandatory values are:

| Variable | Description |
|----------|-------------|
| `DJANGO_SECRET_KEY` | Run `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"` |
| `VAULT_ENCRYPTION_KEY` | Run `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `GITHUB_OAUTH_CLIENT_ID` | `Ov23lisYmz40fv6u0JxN` |

Redis defaults to `redis://localhost:6379` — change `REDIS_URL` if needed.

### 5 — Apply migrations & start

```powershell
cd backend
python manage.py migrate
.\start.ps1
```

The server listens on `http://localhost:8000`. See [docs/setup.md](docs/setup.md) for model overrides and Celery worker setup.

### 6 — Load the Chrome extension

1. Open `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked** → select the `extension/` folder
4. The KenBot icon appears in your toolbar — see [docs/extension.md](docs/extension.md)

---

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/setup.md](docs/setup.md) | Full installation, auth, running, Celery, production |
| [docs/architecture.md](docs/architecture.md) | System design, data flow, component responsibilities |
| [docs/api.md](docs/api.md) | REST endpoints + WebSocket message protocol |
| [docs/maps.md](docs/maps.md) | ServiceMap schema reference |
| [docs/models.md](docs/models.md) | GitHub Models list, switching models, cost notes |
| [docs/extension.md](docs/extension.md) | Chrome extension install, usage, vault management |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend framework | Django 5, Django REST Framework |
| Real-time | Django Channels 4, Daphne |
| Pilot agent | LangChain 1.2 (AgentExecutor) |
| Surveyor agent | LangGraph 1.0 |
| Browser automation | browser-use 0.12 (wraps Playwright) |
| Task queue | Celery 5 + Redis 7 |
| LLM API | GitHub Models via OpenAI-compatible endpoint |
| Credential vault | AES-256-GCM (Python `cryptography`) |
| Validation | Pydantic v2 |
| Extension | Plain JavaScript — no bundler |

---

## Privacy Model

Sensitive credentials (passwords, National IDs, KRA PINs) are **never** sent to any LLM. The vault stores only AES-256-GCM encrypted ciphertext. When a form step requires a credential:

1. The Pilot sends the extension a step with placeholder key e.g. `{{national_id}}`.
2. The extension calls `GET /api/vault/national_id/` with the user's JWT.
3. Django decrypts and returns the plaintext over HTTPS (never logged).
4. The extension writes it directly into the DOM input — the LLM sees only `{{national_id}}`.

---

## License

See [LICENSE](LICENSE).
