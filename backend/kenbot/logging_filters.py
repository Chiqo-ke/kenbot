from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Patterns that look like vault placeholder values injected anywhere (extra safety net)
_VAULT_PATTERNS = [
    re.compile(r"\{\{[a-z_]+\}\}"),  # {{national_id}} — these are safe (placeholders)
]

# Actual sensitive patterns: sequences that look like Kenyan IDs, PINs, passwords
_SENSITIVE_PATTERNS = [
    re.compile(r"\b[A-Z]{1,2}\d{6,8}\b"),  # National ID formats
    re.compile(r"\bA\d{9}[A-Z]\b"),         # KRA PIN pattern
]


class MaskVaultFilter(logging.Filter):
    """Strip any token that matches known sensitive value patterns from log records."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if isinstance(record.msg, str):
            for pattern in _SENSITIVE_PATTERNS:
                record.msg = pattern.sub("[REDACTED]", record.msg)
        if record.args:
            try:
                sanitised = tuple(
                    re.sub(p.pattern, "[REDACTED]", str(a)) if isinstance(a, str) else a
                    for a in record.args  # type: ignore[union-attr]
                    for p in _SENSITIVE_PATTERNS
                )
                record.args = sanitised
            except TypeError:
                pass
        return True
