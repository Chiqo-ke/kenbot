from __future__ import annotations

from .base import *  # noqa: F401, F403

# ---------------------------------------------------------------------------
# Production overrides
# ---------------------------------------------------------------------------

ALLOWED_HOSTS = [host for host in __import__("os").environ.get("ALLOWED_HOSTS", "").split(",") if host]

# Only allow the extension's chrome-extension:// origin explicitly
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^chrome-extension://[a-z]{32}$",
]

SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
