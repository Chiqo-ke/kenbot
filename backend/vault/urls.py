from __future__ import annotations

from django.urls import path

from vault.views import VaultEntryView, VaultRetrieveView

urlpatterns = [
    path("", VaultEntryView.as_view(), name="vault-list-store"),
    path("<str:vault_key>/", VaultRetrieveView.as_view(), name="vault-retrieve-delete"),
]
