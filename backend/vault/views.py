from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from vault.encryption import decrypt, encrypt
from vault.models import EncryptedVaultEntry
from vault.serializers import VaultKeyListSerializer, VaultStoreSerializer

logger = logging.getLogger(__name__)


class VaultEntryView(APIView):
    """
    POST   /api/vault/  — store or update a single vault entry.
    GET    /api/vault/  — list stored vault *keys* for the current user (no values).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        entries = EncryptedVaultEntry.objects.filter(user=request.user).values(
            "vault_key", "updated_at"
        )
        serializer = VaultKeyListSerializer(entries, many=True)
        return Response(serializer.data)

    def post(self, request: Request) -> Response:
        serializer = VaultStoreSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        vault_key = serializer.validated_data["vault_key"]
        plaintext = serializer.validated_data["value"]

        token = encrypt(plaintext)
        # Overwrite immediately so plaintext doesn't linger in memory longer
        del plaintext

        EncryptedVaultEntry.objects.update_or_create(
            user=request.user,
            vault_key=vault_key,
            defaults={"encrypted_value": token},
        )
        return Response({"vault_key": vault_key}, status=status.HTTP_201_CREATED)


class VaultRetrieveView(APIView):
    """
    GET  /api/vault/<vault_key>/  — decrypt and return a single vault value.

    This endpoint is called by the browser extension at runtime so it can
    inject the value directly into the DOM.  The LLM (Pilot agent) never
    calls this endpoint and never sees the response.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request, vault_key: str) -> Response:
        try:
            entry = EncryptedVaultEntry.objects.get(
                user=request.user, vault_key=vault_key
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
                "Vault decryption failed for user=%s key=%s",
                request.user.pk,
                vault_key,
            )
            return Response(
                {"detail": "Vault entry could not be decrypted."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Return value only — response is HTTPS only in production
        return Response({"vault_key": vault_key, "value": plaintext})

    def delete(self, request: Request, vault_key: str) -> Response:
        deleted, _ = EncryptedVaultEntry.objects.filter(
            user=request.user, vault_key=vault_key
        ).delete()
        if not deleted:
            return Response(
                {"detail": f"No vault entry for key '{vault_key}'."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
