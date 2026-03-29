from __future__ import annotations

import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# AES-GCM constants
_NONCE_BYTES = 12  # 96-bit nonce — NIST recommended for GCM
_KEY_BYTES = 32    # AES-256


def _load_key() -> bytes:
    """
    Derive the 32-byte AES key from settings.VAULT_ENCRYPTION_KEY.

    The env var must be a URL-safe base64-encoded 32-byte value.
    Generate with: python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
    """
    from django.conf import settings

    raw = settings.VAULT_ENCRYPTION_KEY
    key = base64.urlsafe_b64decode(raw.encode())
    if len(key) != _KEY_BYTES:
        raise ValueError(
            f"VAULT_ENCRYPTION_KEY must decode to exactly {_KEY_BYTES} bytes; "
            f"got {len(key)}."
        )
    return key


def encrypt(plaintext: str) -> str:
    """
    Encrypt *plaintext* with AES-256-GCM and return a URL-safe base64 string
    formatted as ``<nonce_b64>.<ciphertext_b64>``.

    Never logs the plaintext.
    """
    key = _load_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    encoded = (
        base64.urlsafe_b64encode(nonce).decode()
        + "."
        + base64.urlsafe_b64encode(ciphertext).decode()
    )
    return encoded


def decrypt(token: str) -> str:
    """
    Decrypt a token produced by :func:`encrypt`.

    Raises ``ValueError`` if the token is malformed or authentication fails.
    Never logs the plaintext.
    """
    key = _load_key()
    try:
        nonce_b64, cipher_b64 = token.split(".", 1)
        nonce = base64.urlsafe_b64decode(nonce_b64)
        ciphertext = base64.urlsafe_b64decode(cipher_b64)
    except (ValueError, Exception) as exc:
        raise ValueError("Malformed vault token.") from exc

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ValueError("Vault token authentication failed.") from exc

    return plaintext.decode()
