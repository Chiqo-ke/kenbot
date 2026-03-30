from __future__ import annotations

import logging
import uuid

from django.contrib.auth.models import User
from django.db import models

logger = logging.getLogger(__name__)


class PilotSession(models.Model):
    """Tracks a single user automation session opened over WebSocket."""

    STATUS_CHOICES = [
        ("active", "Active"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("disconnected", "Disconnected"),
    ]

    # Matches the <session_id> URL argument in the WebSocket URL
    session_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pilot_sessions",
    )
    service_id = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="active"
    )
    step_index = models.PositiveIntegerField(default=0)
    total_steps = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    chat_history = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Pilot Session"
        verbose_name_plural = "Pilot Sessions"

    def __str__(self) -> str:
        return f"PilotSession({self.session_id}, user={self.user_id}, {self.status})"


class ExecutionLog(models.Model):
    """
    Append-only log of every message exchanged in a session.

    Vault values are NEVER stored here — only placeholder keys and
    agent messages.
    """

    ROLE_CHOICES = [
        ("user", "User"),
        ("ai", "AI"),
        ("system", "System"),
    ]

    session = models.ForeignKey(
        PilotSession,
        on_delete=models.CASCADE,
        related_name="logs",
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    # Content stores chat text only — never credential values
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Execution Log Entry"
        verbose_name_plural = "Execution Log Entries"

    def __str__(self) -> str:
        return f"ExecutionLog({self.session_id} [{self.role}] {self.created_at})"
