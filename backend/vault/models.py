from __future__ import annotations

import logging

from django.contrib.auth.models import User
from django.db import models

logger = logging.getLogger(__name__)


class EncryptedVaultEntry(models.Model):
    """
    Stores a single encrypted credential keyed by (user, vault_key).

    The ``encrypted_value`` column contains a token produced by
    ``vault.encryption.encrypt()`` — never a plaintext value.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="vault_entries",
    )
    # Logical key — matches the required_user_data keys in ServiceMap
    # e.g. "national_id", "kra_pin", "nhif_number"
    vault_key = models.CharField(max_length=120)
    # AES-256-GCM ciphertext token (nonce.ciphertext in URL-safe base64)
    encrypted_value = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("user", "vault_key")]
        verbose_name = "Vault Entry"
        verbose_name_plural = "Vault Entries"

    def __str__(self) -> str:
        # Never include the encrypted_value in __str__
        return f"VaultEntry(user={self.user_id}, key={self.vault_key})"
