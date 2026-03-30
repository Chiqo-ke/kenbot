from __future__ import annotations

import logging
import re

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from vault.encryption import decrypt, encrypt
from vault.models import EncryptedVaultEntry
from vault.serializers import VaultKeyListSerializer, VaultStoreSerializer

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _get_anon_key(request: Request) -> str | None:
    """Extract and validate the X-Vault-Key header (UUID from the extension)."""
    key = request.headers.get("X-Vault-Key", "").strip()
    if _UUID_RE.match(key):
        return key
    return None


class VaultEntryView(APIView):
    """
    POST   /api/vault/  — store or update a single vault entry.
    GET    /api/vault/  — list stored vault *keys* for this browser (no values).

    Callers must include the header:
        X-Vault-Key: <UUID>   (generated once by the extension, persisted locally)
    """

    def get(self, request: Request) -> Response:
        anon_key = _get_anon_key(request)
        if not anon_key:
            return Response({"detail": "X-Vault-Key header missing or invalid."}, status=status.HTTP_400_BAD_REQUEST)
        entries = EncryptedVaultEntry.objects.filter(anon_key=anon_key).values(
            "vault_key", "updated_at"
        )
        serializer = VaultKeyListSerializer(entries, many=True)
        return Response(serializer.data)

    def post(self, request: Request) -> Response:
        anon_key = _get_anon_key(request)
        if not anon_key:
            return Response({"detail": "X-Vault-Key header missing or invalid."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = VaultStoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        vault_key = serializer.validated_data["vault_key"]
        plaintext = serializer.validated_data["value"]

        token = encrypt(plaintext)
        # Overwrite immediately so plaintext doesn't linger in memory longer
        del plaintext

        EncryptedVaultEntry.objects.update_or_create(
            anon_key=anon_key,
            vault_key=vault_key,
            defaults={"encrypted_value": token},
        )
        return Response({"vault_key": vault_key}, status=status.HTTP_201_CREATED)


class VaultRetrieveView(APIView):
    """
    GET    /api/vault/<vault_key>/  — decrypt and return a single vault value.
    DELETE /api/vault/<vault_key>/  — remove a vault entry.

    This endpoint is called by the browser extension at runtime so it can
    inject the value directly into the DOM.  The LLM (Pilot agent) never
    calls this endpoint and never sees the response.
    """

    def get(self, request: Request, vault_key: str) -> Response:
        anon_key = _get_anon_key(request)
        if not anon_key:
            return Response({"detail": "X-Vault-Key header missing or invalid."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            entry = EncryptedVaultEntry.objects.get(
                anon_key=anon_key, vault_key=vault_key
            )
        except EncryptedVaultEntry.DoesNotExist:
            return Response(
                {"detail": f"No vault entry for key '{vault_key}'."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            plaintext = decrypt(entry.encrypted_value)
        except ValueError:
            logger.error(
                "Vault decryption failed for anon_key=%s key=%s",
                anon_key[:8],
                vault_key,
            )
            return Response(
                {"detail": "Vault entry could not be decrypted."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"vault_key": vault_key, "value": plaintext})

    def delete(self, request: Request, vault_key: str) -> Response:
        anon_key = _get_anon_key(request)
        if not anon_key:
            return Response({"detail": "X-Vault-Key header missing or invalid."}, status=status.HTTP_400_BAD_REQUEST)
        deleted, _ = EncryptedVaultEntry.objects.filter(
            anon_key=anon_key, vault_key=vault_key
        ).delete()
        if not deleted:
            return Response(
                {"detail": f"No vault entry for key '{vault_key}'."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
