"""
kenbot.github_auth
==================

Token resolution for GitHub Models API.

Priority order:
  1. GITHUB_TOKEN environment variable  (CI/CD, Docker, VPS deploy)
  2. ~/.kenbot/github_token             (device-flow token from auth_github.py)
  3. backend/.github_token              (local dev mirror written by auth_github.py)

Raise ImproperlyConfigured with a clear message if none found, so Django
startup fails early with an actionable error instead of a cryptic KeyError.
"""

from __future__ import annotations

from pathlib import Path

_TOKEN_LOCATIONS = [
    Path.home() / ".kenbot" / "github_token",
    # Relative to this file: backend/kenbot/github_auth.py → backend/.github_token
    Path(__file__).resolve().parent.parent / ".github_token",
]


def get_github_token() -> str:
    """
    Return the GitHub access token, or raise ImproperlyConfigured.
    Imported by settings/base.py — keep this import-time-safe.
    """
    import os

    # 1. Env var (highest priority — used in CI, Docker, production)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    # 2. Token files written by auth_github.py
    for path in _TOKEN_LOCATIONS:
        if path.exists():
            token = path.read_text().strip()
            if token:
                return token

    # Nothing found — give a clear, actionable error
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "GitHub token not found. Run the authentication script first:\n\n"
        "    python auth_github.py\n\n"
        "Or set the GITHUB_TOKEN environment variable for server deployments."
    )
