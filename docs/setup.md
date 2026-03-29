# Setup Guide

Complete instructions for installing, authenticating, and running KenBot locally.

---

## Prerequisites

| Requirement | Min version | Install |
|-------------|-------------|---------|
| Python | 3.12 | [python.org](https://python.org) or `winget install Python.Python.3.13` |
| Redis | 7 | [redis.io](https://redis.io/docs/getting-started/) or Docker: `docker run -p 6379:6379 redis:7` |
| Google Chrome | any | Extension runs in Chrome only |

---

## 1 — Clone the Repository

```powershell
git clone <repo-url> kenbot
cd kenbot
```

---

## 2 — Python Environment

KenBot uses the virtual environment at `C:\Users\<you>\Documents\.venv`. Either create it there or adjust `start.ps1` to point to your venv.

```powershell
python -m venv C:\Users\$env:USERNAME\Documents\.venv
C:\Users\$env:USERNAME\Documents\.venv\Scripts\Activate.ps1

# Install all dependencies
cd backend
pip install -r requirements.txt
```

> **Using uv?** `uv pip sync requirements.txt` — faster, same result.

---

## 3 — GitHub Authentication (Device Flow)

KenBot calls [GitHub Models](https://github.com/marketplace/models), which requires a GitHub token. Run the one-time auth script from the **repo root** (not `backend/`):

```powershell
python auth_github.py
```

The script will:

1. Request a device code from GitHub.
2. Print an 8-character **user code** and a URL.
3. Open the URL in your browser, sign in to GitHub, and enter the code.
4. Poll until you approve — then save the token automatically.

**Token storage:**  
- Primary: `~/.kenbot/github_token` (`C:\Users\<you>\.kenbot\github_token`)  
- Mirror: `backend/.github_token`

Django settings load the token in priority order:

```
GITHUB_TOKEN env var  →  ~/.kenbot/github_token  →  backend/.github_token
```

**You only need to run this once.** Tokens from the GitHub device flow do not expire unless revoked.

### Verifying auth worked

```powershell
python -c "
from pathlib import Path
token = (Path.home() / '.kenbot' / 'github_token').read_text().strip()
print('Token present, length:', len(token))
"
```

---

## 4 — Environment Variables

```powershell
cd backend
Copy-Item .env.example .env
```

Open `.env` in your editor and fill in the three mandatory values:

### `DJANGO_SECRET_KEY`

```powershell
C:\Users\$env:USERNAME\Documents\.venv\Scripts\python.exe -c "
from django.core.management.utils import get_random_secret_key
print(get_random_secret_key())
"
```

Paste the output as the value.

### `VAULT_ENCRYPTION_KEY`

```powershell
C:\Users\$env:USERNAME\Documents\.venv\Scripts\python.exe -c "
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
"
```

> **Important:** Back this key up securely. Losing it makes all vault entries unreadable.

### `GITHUB_OAUTH_CLIENT_ID`

```
GITHUB_OAUTH_CLIENT_ID=Ov23lisYmz40fv6u0JxN
```

### Full `.env` example

```dotenv
DJANGO_SETTINGS_MODULE=kenbot.settings.development
DJANGO_SECRET_KEY=<generated-64-char-key>
GITHUB_TOKEN=                          # leave blank — loaded from ~/.kenbot/github_token
GITHUB_OAUTH_CLIENT_ID=Ov23lisYmz40fv6u0JxN
VAULT_ENCRYPTION_KEY=<generated-fernet-key>
REDIS_URL=redis://localhost:6379
KENBOT_PILOT_MODEL=openai/gpt-4o-mini  # optional override
KENBOT_SURVEYOR_MODEL=openai/gpt-4o    # optional override
```

---

## 5 — Database Migrations

```powershell
cd backend
python manage.py migrate
```

Creates `db.sqlite3` with tables for `PilotSession`, `ExecutionLog`, `ServiceMap`, `VaultEntry`, `SurveyJob`.

---

## 6 — Start the Server

```powershell
cd backend
.\start.ps1
```

The script:
- Activates the venv
- Verifies the GitHub token is present
- Prints the active model config
- Starts `daphne kenbot.asgi:application` on `0.0.0.0:8000`

### Model overrides via start.ps1

```powershell
.\start.ps1 -PilotModel "openai/gpt-4o" -SurveyorModel "openai/gpt-4o"
```

See [docs/models.md](models.md) for the full list of available models.

### Manually (without start.ps1)

```powershell
# PowerShell
$env:DJANGO_SETTINGS_MODULE = "kenbot.settings.development"
daphne -b 0.0.0.0 -p 8000 kenbot.asgi:application

# CMD
set DJANGO_SETTINGS_MODULE=kenbot.settings.development
daphne -b 0.0.0.0 -p 8000 kenbot.asgi:application
```

---

## 7 — Start Redis (if not already running)

```powershell
# Docker (simplest)
docker run -d --name kenbot-redis -p 6379:6379 redis:7

# Or start locally if Redis is installed
redis-server
```

---

## 8 — Start the Celery Worker

The Surveyor uses Celery for background crawl jobs. In a separate terminal:

```powershell
cd backend
C:\Users\$env:USERNAME\Documents\.venv\Scripts\Activate.ps1
celery -A kenbot worker -l info
```

> In development mode (`DJANGO_SETTINGS_MODULE=kenbot.settings.development`), `CELERY_TASK_ALWAYS_EAGER=True` runs tasks synchronously in-process — you don't need a separate worker.

---

## 9 — Create a Django User

```powershell
cd backend
python manage.py createsuperuser
```

This user account is what the extension logs in with to get a JWT.

---

## Verifying Everything Works

```powershell
# Django system check
python manage.py check

# Quick API test (returns 401 — expected without JWT)
Invoke-WebRequest -Uri http://localhost:8000/api/pilot/sessions/ -Method GET
```

Expected output for the API test: `StatusCode: 401`.

---

## Production Deployment

For production, switch to `kenbot.settings.production`:

```dotenv
DJANGO_SETTINGS_MODULE=kenbot.settings.production
ALLOWED_HOSTS=your-domain.com
```

Production settings enforce:
- `SECURE_SSL_REDIRECT = True`
- `SESSION_COOKIE_SECURE = True`
- `CSRF_COOKIE_SECURE = True`
- HSTS with 1 year `max-age`
- CORS restricted to `chrome-extension://[a-z]{32}` (your published extension ID)

Use a proper WSGI/ASGI host (e.g. nginx + daphne) and a production database (PostgreSQL recommended). Do **not** commit `.env` to git — it is in `.gitignore`.
