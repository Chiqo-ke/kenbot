#!/usr/bin/env python3
"""
KenBot — GitHub Device Flow Authentication
==========================================

Run this script once to authenticate with GitHub and store a token that
KenBot will use to call GitHub Models (https://models.inference.ai.azure.com).

Usage
-----
    python auth_github.py

What it does
------------
1. Contacts GitHub's device-authorization endpoint.
2. Prints a short code + URL — you paste the code at https://github.com/login/device/
3. Polls GitHub until you authorize.
4. Saves the access token to  ~/.kenbot/github_token  (and optionally to
   backend/.github_token for the Django server).

Prerequisites
-------------
You need a GitHub OAuth App with the "Device flow" feature enabled.

HOW TO CREATE AN OAUTH APP (takes ~2 minutes):
  1.  https://github.com/settings/developers  → "OAuth Apps" → "New OAuth App"
  2.  Application name:  KenBot (or anything)
  3.  Homepage URL:      http://localhost
  4.  Authorization callback URL:  http://localhost   (required but unused for device flow)
  5.  ✅  Tick "Enable Device Flow"
  6.  Click "Register application"
  7.  Copy the "Client ID"
  8.  Set it before running this script — one of:
        export GITHUB_OAUTH_CLIENT_ID=<your_client_id>
      OR save it to ~/.kenbot/client_id

Available models after auth
---------------------------
  openai/gpt-4o              – high-capability, best for Surveyor
  openai/gpt-4o-mini         – fast/cheap, default for Pilot
  openai/o1-mini             – reasoning model
  anthropic/claude-3-5-sonnet – Anthropic via GitHub Models
  anthropic/claude-3-7-sonnet – latest Claude
  meta/llama-3.1-405b-instruct
  mistral/mistral-large
  cohere/command-r-plus

Configure which model each agent uses:
  export KENBOT_PILOT_MODEL=openai/gpt-4o-mini
  export KENBOT_SURVEYOR_MODEL=openai/gpt-4o
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

KENBOT_DIR = Path.home() / ".kenbot"
TOKEN_FILE = KENBOT_DIR / "github_token"
CLIENT_ID_FILE = KENBOT_DIR / "client_id"

# Also mirror token to the Django backend for local dev convenience
_script_dir = Path(__file__).resolve().parent
BACKEND_TOKEN_FILE = _script_dir / "backend" / ".github_token"

# ---------------------------------------------------------------------------
# GitHub endpoints
# ---------------------------------------------------------------------------

DEVICE_CODE_URL = "https://github.com/login/device/code"
POLL_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"

# Minimal scope — a valid GitHub identity token is all GitHub Models needs.
SCOPE = ""


# ---------------------------------------------------------------------------
# Client ID resolution
# ---------------------------------------------------------------------------

def _load_client_id() -> str:
    """
    Resolve the OAuth App Client ID in priority order:
      1. GITHUB_OAUTH_CLIENT_ID env var
      2. ~/.kenbot/client_id file
    Exits with a helpful message if neither is set.
    """
    cid = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "").strip()
    if cid:
        return cid

    if CLIENT_ID_FILE.exists():
        cid = CLIENT_ID_FILE.read_text().strip()
        if cid:
            return cid

    print("\n" + "=" * 60)
    print("  GitHub OAuth App Client ID not found")
    print("=" * 60)
    print("""
You need a GitHub OAuth App Client ID to use device flow.

Quick setup (2 minutes):
  1. Go to:  https://github.com/settings/developers
  2. Click "OAuth Apps" → "New OAuth App"
  3. Fill in:
       Application name:           KenBot
       Homepage URL:               http://localhost
       Authorization callback URL: http://localhost
  4. ✅  Check "Enable Device Flow"
  5. Click "Register application"
  6. Copy the displayed "Client ID"

Then either:
  (a) Set env var:   export GITHUB_OAUTH_CLIENT_ID=<client_id>
  (b) Or save to file: echo '<client_id>' > ~/.kenbot/client_id

Then re-run: python auth_github.py
""")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Device flow
# ---------------------------------------------------------------------------

def _request_device_code(client_id: str) -> dict:
    """POST to GitHub to get device_code and user_code."""
    resp = requests.post(
        DEVICE_CODE_URL,
        headers={"Accept": "application/json"},
        data={"client_id": client_id, "scope": SCOPE},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        print(f"\n[ERROR] GitHub returned: {data.get('error_description', data['error'])}")
        sys.exit(1)
    return data


def _poll_for_token(client_id: str, device_code: str, interval: int) -> str:
    """Poll GitHub until the user authorizes (or it expires). Returns access token."""
    print("\nPolling GitHub for authorization", end="", flush=True)
    while True:
        time.sleep(interval)
        print(".", end="", flush=True)

        resp = requests.post(
            POLL_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        error = data.get("error")
        if not error:
            # Success
            return data["access_token"]

        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            interval += 5
            continue
        elif error == "expired_token":
            print("\n[ERROR] Device code expired. Please run the script again.")
            sys.exit(1)
        elif error == "access_denied":
            print("\n[ERROR] Authorization was denied.")
            sys.exit(1)
        else:
            print(f"\n[ERROR] {data.get('error_description', error)}")
            sys.exit(1)


def _verify_token(token: str) -> dict:
    """Call /user to confirm the token is valid and get the GitHub username."""
    resp = requests.get(
        USER_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

def _save_token(token: str) -> None:
    """Save token to ~/.kenbot/github_token and to backend/.github_token."""
    KENBOT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    print(f"\n[✓] Token saved to {TOKEN_FILE}")

    # Mirror to backend directory for local dev convenience
    try:
        BACKEND_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        BACKEND_TOKEN_FILE.write_text(token)
        BACKEND_TOKEN_FILE.chmod(0o600)
        print(f"[✓] Token mirrored to {BACKEND_TOKEN_FILE}")
    except Exception:
        pass  # Non-fatal if backend dir doesn't exist


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("  KenBot — GitHub Device Flow Authentication")
    print("=" * 60)

    # Check if already authenticated
    if TOKEN_FILE.exists():
        existing = TOKEN_FILE.read_text().strip()
        if existing:
            try:
                user = _verify_token(existing)
                print(f"\n[✓] Already authenticated as: {user['login']}")
                print(f"    Token file: {TOKEN_FILE}")
                print("\nRe-run with --reauth to get a new token.")
                if "--reauth" not in sys.argv:
                    _print_model_table()
                    return
            except Exception:
                print("[!] Existing token is invalid — starting fresh auth flow.\n")

    client_id = _load_client_id()

    # Step 1: Request device code
    code_data = _request_device_code(client_id)
    device_code = code_data["device_code"]
    user_code = code_data["user_code"]
    verification_uri = code_data.get("verification_uri", "https://github.com/login/device")
    expires_in = code_data.get("expires_in", 900)
    interval = code_data.get("interval", 5)

    # Step 2: Show user what to do
    print(f"""
┌─────────────────────────────────────────────────┐
│  Open this URL in your browser:                 │
│                                                 │
│    {verification_uri:<45} │
│                                                 │
│  Enter this code when prompted:                 │
│                                                 │
│    {user_code:<45} │
│                                                 │
│  Code expires in {expires_in // 60} minutes.                   │
└─────────────────────────────────────────────────┘
""")

    # Step 3: Poll
    token = _poll_for_token(client_id, device_code, interval)

    # Step 4: Verify
    user = _verify_token(token)
    print(f"\n\n[✓] Authenticated as: {user['login']} ({user.get('name', '')})")

    # Step 5: Save
    _save_token(token)

    _print_model_table()
    print("""
Next steps
----------
1. Start the KenBot backend (PowerShell):
     cd backend
     daphne kenbot.asgi:application

   Or use the helper script:
     .\start.ps1

2. (Optional) Override which model each agent uses:

   PowerShell:
     $env:KENBOT_PILOT_MODEL = "openai/gpt-4o-mini"
     $env:KENBOT_SURVEYOR_MODEL = "openai/gpt-4o"

   CMD:
     set KENBOT_PILOT_MODEL=openai/gpt-4o-mini
     set KENBOT_SURVEYOR_MODEL=openai/gpt-4o

3. Token auto-renews on next auth run. To re-authenticate:
     python auth_github.py --reauth
""")



def _print_model_table() -> None:
    """Print the list of available GitHub Models."""
    print("""
Available GitHub Models
-----------------------
  openai/gpt-4o                    (default: Surveyor)
  openai/gpt-4o-mini               (default: Pilot)
  openai/o1-mini
  anthropic/claude-3-5-sonnet
  anthropic/claude-3-7-sonnet
  meta/llama-3.1-405b-instruct
  mistral/mistral-large-2411
  cohere/command-r-plus-08-2024
""")


if __name__ == "__main__":
    main()
