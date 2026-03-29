from __future__ import annotations

from rest_framework import serializers


class VaultStoreSerializer(serializers.Serializer):
    """Input schema for storing/updating a vault entry."""

    vault_key = serializers.RegexField(
        r"^[a-z][a-z0-9_]{0,119}$",
        help_text="Lowercase snake_case key, e.g. 'national_id'.",
    )
    value = serializers.CharField(
        max_length=2048,
        write_only=True,  # never serialised back to the client
        help_text="Plaintext credential — encrypted server-side immediately.",
    )


class VaultKeyListSerializer(serializers.Serializer):
    """Represents a single stored vault key (value is never returned)."""

    vault_key = serializers.CharField()
    updated_at = serializers.DateTimeField()
