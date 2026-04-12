from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from urllib.parse import urlparse

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from pilot.agent import build_pilot_agent
from pilot.state import ExecutionState

logger = logging.getLogger(__name__)

# Regex matching login / auth pages emitted by the heartbeat
_LOGIN_RE = re.compile(
    r'/(login|signin|sign-in|auth|sso)[/?#]?|/oauth',
    re.IGNORECASE,
)

# Sentinel prefixes that tools return to trigger consumer side-effects
_PAUSE_PREFIX = "PAUSE_FOR_CONFIRMATION"
_MISSING_KEY_PREFIX = "AWAIT_VAULT_KEY"

# ---------------------------------------------------------------------------
# Agent thinking progress labels
# ---------------------------------------------------------------------------

_TOOL_THINKING_LABELS: dict[str, tuple[str, str]] = {
    "load_service_map":      ("Loading service map…",       "Inapakia ramani ya huduma…"),
    "build_execution_plan":  ("Building plan…",             "Inaunda mpango…"),
    "execute_workflow_step": ("Executing step…",            "Inatekeleza hatua…"),
    "request_healing":       ("Requesting guidance…",       "Inaomba msaada…"),
    "trigger_survey":        ("Processing survey…",         "Inashughulikia dodoso…"),
    "confirm_submission":    ("Confirming submission…",     "Inathibitisha uwasilishaji…"),
    "explore_page":          ("Examining page…",            "Inachunguza ukurasa…"),
    "navigate_browser":      ("Navigating…",                "Inasafiri..."),
    "browser_click":         ("Clicking element…",          "Inabonyeza kipengele…"),
    "browser_fill":          ("Filling field…",             "Inajaza sehemu…"),
}


class _ThinkingCallback(AsyncCallbackHandler):
    """Sends lightweight progress messages to the extension during agent turns."""

    def __init__(self, send_fn) -> None:
        self._send = send_fn

    async def on_llm_start(self, serialized, messages, **kwargs) -> None:  # type: ignore[override]
        await self._send({"type": "agent_thinking", "text_en": "Thinking…", "text_sw": "Inafikiri…"})

    async def on_tool_start(self, serialized, input_str, **kwargs) -> None:  # type: ignore[override]
        name = serialized.get("name", "")
        en, sw = _TOOL_THINKING_LABELS.get(name, (f"Running {name}…", f"Inafanya {name}…"))
        await self._send({"type": "agent_thinking", "text_en": en, "text_sw": sw})


class PilotConsumer(AsyncWebsocketConsumer):
    """
    Django Channels WebSocket consumer for the Pilot agent.

    One instance per user session.  The URL pattern is:
        ws/pilot/<session_id>/

    Message protocol (all JSON):

    Extension → server
        {"type": "user_message",       "content": "..."}
        {"type": "step_confirmed",     "step_id": "..."}
        {"type": "step_failed",        "selector": "...", "step_id": "...",
                                       "page_context": "..."}  ← values stripped
        {"type": "captcha_solved"}
        {"type": "captcha_detected"}
        {"type": "vault_key_added",    "vault_key": "..."}
        {"type": "user_form_filled"}   ← user finished filling portal form

    Server → extension
        {"type": "agent_message",      "content_en": "...",  "content_sw": "..."}
        {"type": "execute_step",       "step_id": "...",     "actions": [...]}
        {"type": "pause_confirmation", "step_label": "...",  "fields": "..."}
        {"type": "await_captcha"}
        {"type": "await_vault_key",    "missing_keys": [...]}
        {"type": "open_url",           "url": "...",         "missing_keys": "..."}
        {"type": "state_update",       "state": {...}}
        {"type": "error",              "message": "..."}
        {"type": "session_complete"}
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self.session_id: str = self.scope["url_route"]["kwargs"]["session_id"]
        self.user = self.scope.get("user", None)

        # Extract the browser extension's anon_key from the query string.
        # The extension passes it as ?vault_key=<UUID> alongside the JWT token.
        from urllib.parse import parse_qs
        qs = parse_qs(self.scope.get("query_string", b"").decode())
        self.anon_key: str | None = (qs.get("vault_key") or [None])[0]

        # Reject anonymous (unauthenticated) WebSocket connections.
        # We must accept() first so the browser receives a proper WS close
        # frame (code 4001) rather than a raw HTTP 403 — otherwise the
        # extension can't distinguish auth failure from network errors and
        # will retry indefinitely with the same expired token.
        if not self.user or not self.user.is_authenticated:
            logger.warning("WS rejected — unauthenticated connection attempt for session=%s", self.session_id)
            await self.accept()
            await self.close(code=4001)
            return

        await self.accept()

        self.state = ExecutionState()
        self.agent_executor = build_pilot_agent()

        # Create or restore a DB session record.
        # Returns stored chat_history and execution state if this is a reconnect.
        await self._connect_session_record()

        logger.info(
            "PilotConsumer connected session=%s",
            self.session_id,
        )
        await self._send({"type": "state_update", "state": self.state.model_dump()})

    async def disconnect(self, close_code: int) -> None:
        logger.info(
            "PilotConsumer disconnected session=%s code=%s",
            getattr(self, "session_id", "?"),
            close_code,
        )
        # Cancel any pending rate-limit retry so it doesn't fire into a dead socket.
        task = getattr(self, "_rate_limit_retry_task", None)
        if task and not task.done():
            task.cancel()
        if hasattr(self, "state"):
            if self.state.status not in ("completed", "failed"):
                await self._update_session_status("disconnected")
            # Always persist chat + execution progress so the agent has full
            # context when this session reconnects after a cross-origin navigation.
            await self._save_session_state()

    # ------------------------------------------------------------------
    # Message dispatcher
    # ------------------------------------------------------------------

    async def receive(self, text_data: str) -> None:
        try:
            data: dict = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error("Invalid JSON payload.")
            return

        msg_type: str = data.get("type", "")

        handlers = {
            "user_message": self._handle_user_message,
            "step_confirmed": self._handle_step_confirmed,
            "step_failed": self._handle_step_failed,
            "captcha_solved": self._handle_captcha_solved,
            "captcha_detected": self._handle_captcha_detected,
            "vault_key_added": self._handle_vault_key_added,
            "user_form_filled": self._handle_user_form_filled,
            "confirmation_response": self._handle_confirmation_response,
            "resume_workflow": self._handle_resume_workflow,
            "subgoal_selected": self._handle_subgoal_selected,
            "heartbeat": self._handle_heartbeat,
            "reset_session": self._handle_reset_session,
            # Autonomous navigation results
            "navigate_confirmed": self._handle_free_action_result,
            "free_action_result": self._handle_free_action_result,
        }

        handler = handlers.get(msg_type)
        if handler is None:
            logger.warning("Unknown message type '%s' in session %s", msg_type, self.session_id)
            await self._send_error(f"Unknown message type: {msg_type}")
            return

        try:
            await handler(data)
        except Exception as exc:
            logger.exception(
                "Unhandled error in session %s handling %s: %s",
                self.session_id,
                msg_type,
                exc,
            )
            await self._send_error("An internal error occurred. Please try again.")

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    async def _handle_user_message(self, data: dict) -> None:
        content: str = data.get("content", "").strip()
        if not content:
            return

        self.state.chat_history.append({"role": "human", "content": content})
        await self._log_interaction("user", content)

        # If the user replies while the bot is waiting for manual portal action,
        # treat it as confirmation and resume the workflow immediately.
        if self.state.status == "awaiting_user_input_on_portal":
            blocked_url = self.state.awaiting_portal_url
            step_id = self.state.current_step_id or ""
            service_id = self.state.service_id or ""
            self.state.status = "executing"
            self.state.awaiting_portal_url = ""
            await self._send({"type": "state_update", "state": self.state.model_dump()})
            prompt = (
                f"[User replied after completing manual portal action: '{content}'] "
                f"The browser was previously blocked at '{blocked_url}'. "
                f"Resume the workflow by calling execute_workflow_step with "
                f"service_id='{service_id}' and step_id='{step_id}'. "
                "Do not re-plan or call load_service_map."
            )
        else:
            prompt = content

        response_text = await self._run_agent(prompt)
        if response_text:
            self.state.chat_history.append({"role": "ai", "content": response_text})
            await self._dispatch_agent_output(response_text)

    async def _handle_step_confirmed(self, data: dict) -> None:
        step_id: str = data.get("step_id", "")
        logger.info("Step confirmed step_id=%s session=%s", step_id, self.session_id)

        # Track completion and update goal panel
        if step_id and step_id not in self.state.completed_steps:
            self.state.completed_steps.append(step_id)
        # Reset failure counter for this step on success
        self.state.step_fail_counts.pop(step_id, None)

        if self.state.plan and step_id:
            goal = self._find_goal_for_step(step_id)
            if goal:
                step_ids = goal.get("step_ids", [])
                if all(s in self.state.completed_steps for s in step_ids):
                    goal["status"] = "done"
                    await self._send({"type": "goal_update", "goal_id": goal["id"], "status": "done", "failure_subgoals": []})
                else:
                    # Goal is still in progress — mark it running if not already
                    if goal.get("status") != "running":
                        goal["status"] = "running"
                        await self._send({"type": "goal_update", "goal_id": goal["id"], "status": "running", "failure_subgoals": []})

        self.state.step_index += 1

        if self.state.step_index >= self.state.total_steps:
            self.state.status = "completed"
            await self._send({"type": "session_complete"})
            await self._send({"type": "state_update", "state": self.state.model_dump()})
            return

        self.state.status = "executing"
        await self._send({"type": "state_update", "state": self.state.model_dump()})

        # Continue the workflow loop — ask the agent to dispatch the next step
        response_text = await self._run_agent(
            f"Step '{step_id}' completed successfully. Continue to the next step in the workflow."
        )
        if response_text:
            await self._dispatch_agent_output(response_text)

    async def _handle_step_failed(self, data: dict) -> None:
        """
        Extension reports a broken selector.

        The page_context field MUST have all form value= attributes stripped
        before being sent — enforced at both ends (extension JS and tool guard).
        """
        step_id: str = data.get("step_id", "")
        selector: str = data.get("selector", "")
        page_context: str = data.get("page_context", "")

        logger.warning(
            "Step failed step_id=%s selector=%s session=%s",
            step_id,
            selector,
            self.session_id,
        )
        self.state.status = "awaiting_healing"
        await self._send({"type": "state_update", "state": self.state.model_dump()})

        # Update goal panel to show this goal as failed with subgoal options
        if self.state.plan and step_id:
            goal = self._find_goal_for_step(step_id)
            if goal:
                goal["status"] = "failed"
                await self._send(
                    {
                        "type": "goal_update",
                        "goal_id": goal["id"],
                        "status": "failed",
                        "failure_subgoals": goal.get("failure_subgoals", []),
                    }
                )

        # Increment per-step failure counter
        self.state.step_fail_counts[step_id] = self.state.step_fail_counts.get(step_id, 0) + 1
        fail_count = self.state.step_fail_counts[step_id]

        # Feed failure back into the agent for it to call request_healing
        hb = self.state.last_heartbeat
        current_url = hb.get('url', '') if hb else ''

        # Detect login / auth page patterns using module-level regex
        is_login_page = bool(_LOGIN_RE.search(current_url))

        if fail_count >= 3 or is_login_page:
            # Bot is stuck — ask the agent to pause and request user action
            self.state.status = "awaiting_human_input"
            await self._send({"type": "state_update", "state": self.state.model_dump()})
            page_desc = "a login / authentication page" if is_login_page else f"'{current_url}'"
            failure_msg = (
                f"Step '{step_id}' has now failed {fail_count} time(s). "
                f"The browser is currently on {page_desc}. "
                "YOU MUST STOP ALL RETRIES AND HEALING ATTEMPTS FOR THIS STEP. "
                "Your only allowed action is to send a friendly message to the user explaining "
                "that manual action is required on the current page (e.g. logging in, solving a form), "
                "and ask them to complete it and then reply 'Done' or 'Nimemaliza' so you can continue. "
                "Do NOT call execute_workflow_step or request_healing again until the user replies."
            )
        else:
            hb_context = (
                f" Current page URL: {hb.get('url', '?')}."
                f" Has error on page: {hb.get('has_error', False)}."
                f" User-modified fields: {hb.get('user_modified_fields', [])}."
                " Call explore_page to inspect the page, then retry or call request_healing."
            ) if hb else " Call request_healing to queue a Surveyor re-map for this step."
            failure_msg = (
                f"Step '{step_id}' failed because selector '{selector}' was not found."
                + hb_context
            )
        response_text = await self._run_agent(failure_msg)
        if response_text:
            await self._dispatch_agent_output(response_text)

    async def _handle_captcha_detected(self, data: dict) -> None:
        self.state.status = "awaiting_captcha"
        await self._send({"type": "await_captcha"})
        await self._send({"type": "state_update", "state": self.state.model_dump()})
        # Also inform the agent
        response_text = await self._run_agent(
            "A CAPTCHA has appeared on the page. I need the user to solve it."
        )
        if response_text:
            await self._dispatch_agent_output(response_text)

    async def _handle_captcha_solved(self, data: dict) -> None:
        logger.info("CAPTCHA solved session=%s", self.session_id)
        self.state.status = "executing"
        await self._send({"type": "state_update", "state": self.state.model_dump()})
        response_text = await self._run_agent("The user has solved the CAPTCHA. Continue.")
        if response_text:
            await self._dispatch_agent_output(response_text)

    async def _handle_vault_key_added(self, data: dict) -> None:
        vault_key: str = data.get("vault_key", "")
        logger.info(
            "Vault key added vault_key=%s session=%s", vault_key, self.session_id
        )
        self.state.status = "executing"
        await self._send({"type": "state_update", "state": self.state.model_dump()})
        response_text = await self._run_agent(
            f"The user has just added the vault key '{vault_key}'. Continue."
        )
        if response_text:
            await self._dispatch_agent_output(response_text)

    async def _handle_user_form_filled(self, data: dict) -> None:
        logger.info("User filled portal form session=%s", self.session_id)
        self.state.status = "executing"
        await self._send({"type": "state_update", "state": self.state.model_dump()})
        response_text = await self._run_agent(
            "The user has filled in their details on the portal. Continue guiding them through the remaining steps."
        )
        if response_text:
            await self._dispatch_agent_output(response_text)

    async def _handle_confirmation_response(self, data: dict) -> None:
        """
        Extension user confirmed or cancelled a submit/pay step.

        Forwards the decision back to the agent so it can either proceed
        with the next action or cancel the workflow gracefully.
        """
        confirmed: bool = bool(data.get("confirmed", False))
        step_label: str = data.get("step_label", "")
        logger.info(
            "Confirmation response confirmed=%s step=%s session=%s",
            confirmed,
            step_label,
            self.session_id,
        )
        self.state.status = "executing"
        await self._send({"type": "state_update", "state": self.state.model_dump()})

        if confirmed:
            feedback = (
                f"The user has confirmed the '{step_label}' step. "
                "Proceed with executing the submit/pay action."
            )
        else:
            feedback = (
                f"The user has CANCELLED the '{step_label}' step. "
                "Do not submit. Ask the user how they would like to proceed."
            )
            self.state.status = "idle"

        response_text = await self._run_agent(feedback)
        if response_text:
            await self._dispatch_agent_output(response_text)

    async def _handle_subgoal_selected(self, data: dict) -> None:
        """
        User clicked a contingency sub-goal button after a step failed.

        action='retry' — retry the failed step.
        action='sub_service' — load a different service map (e.g. password reset).
        """
        action: str = data.get("action", "")
        sub_service_id: str = data.get("service_id", "")
        logger.info(
            "Subgoal selected action=%s sub_service_id=%s session=%s",
            action,
            sub_service_id,
            self.session_id,
        )

        if action == "retry":
            feedback = (
                f"The user chose to retry the failed step '{self.state.current_step_id}'. "
                "Please call execute_workflow_step again for that step_id."
            )
        elif action == "sub_service" and sub_service_id:
            # Switch to the recovery service (e.g. forgot-password flow)
            self.state.service_id = sub_service_id
            feedback = (
                f"The user chose the '{sub_service_id}' recovery flow. "
                f"Call load_service_map with service_id='{sub_service_id}' immediately."
            )
        else:
            logger.warning(
                "Unknown subgoal action='%s' session=%s", action, self.session_id
            )
            return

        self.state.status = "executing"
        await self._send({"type": "state_update", "state": self.state.model_dump()})
        response_text = await self._run_agent(feedback)
        if response_text:
            await self._dispatch_agent_output(response_text)

    async def _handle_free_action_result(self, data: dict) -> None:
        """Handle the result of a free autonomous navigation action (navigate/click/fill).

        The extension reports success/failure; we feed the result back into the
        agent so it can verify and continue.
        """
        success: bool = data.get("success", False)
        url: str = data.get("url", "")
        label: str = data.get("label", "")
        error: str = data.get("error", "")

        if url:
            # navigate_confirmed path
            feedback = (
                f"Navigation to '{url}' {'complete' if success else 'failed'}. "
                + (f"Error: {error}" if error else "Call explore_page to verify the new page, then continue.")
            )
        elif label:
            # click_element / fill_field path
            feedback = (
                f"Action on element '{label}' {'succeeded' if success else 'failed'}. "
                + (f"Error: {error}" if error else "Call explore_page to verify the result, then continue.")
            )
        else:
            feedback = (
                f"Free action {'succeeded' if success else 'failed'}. "
                + (f"Error: {error}" if error else "Call explore_page to verify the result, then continue.")
            )

        logger.info(
            "Free action result success=%s label=%r url=%r session=%s",
            success, label, url, self.session_id,
        )
        try:
            response_text = await self._run_agent(feedback)
            if response_text:
                await self._dispatch_agent_output(response_text)
        except Exception as exc:
            # The WebSocket may have closed mid-navigation (1001 going away).
            # The new page will reconnect; state is already persisted via
            # _save_session_state so the agent can continue from the next message.
            exc_str = str(exc)
            if "ClientDisconnected" in type(exc).__name__ or "ClientDisconnected" in exc_str or "ConnectionClosed" in type(exc).__name__:
                logger.info(
                    "Connection closed during free-nav result — result saved for reconnect session=%s",
                    self.session_id,
                )
            else:
                raise

    async def _handle_heartbeat(self, data: dict) -> None:
        """
        Receive a 15-second page snapshot from the extension.

        Stores the snapshot in self.state.last_heartbeat so it is available
        to the explore_page tool during the next agent run (via ContextVar).
        Never triggers an agent run on its own — purely passive storage.

        SECURITY: page_text_preview may contain portal text but no credentials
        (those are vault-only and never appear in visible page text).
        """
        url: str = data.get("url", "")
        visible_fields: list = data.get("visible_fields", [])

        self.state.last_heartbeat = {
            "url": url,
            "title": data.get("title", ""),
            "page_text_preview": data.get("page_text_preview", ""),
            "visible_fields": visible_fields,
            "has_error": bool(data.get("has_error", False)),
            "has_success": bool(data.get("has_success", False)),
            "user_modified_fields": data.get("user_modified_fields", []),
        }
        logger.debug(
            "Heartbeat session=%s url=%s has_error=%s user_modified=%s",
            self.session_id,
            url,
            self.state.last_heartbeat["has_error"],
            self.state.last_heartbeat["user_modified_fields"],
        )
        await self._send({"type": "heartbeat_ack"})

        # ── Portal-blocked detection ────────────────────────────────────────
        # Fire when we're actively executing (or healing) and we haven't already
        # paused for this URL.  No LLM call — send the message directly.
        is_login_page = bool(_LOGIN_RE.search(url)) or any(
            str(f.get("type", "")).lower() == "password" for f in visible_fields
            if isinstance(f, dict)
        )
        if (
            is_login_page
            and self.state.status in ("executing", "awaiting_healing", "failed")
            and not self.state.awaiting_portal_url
        ):
            self.state.status = "awaiting_user_input_on_portal"
            self.state.awaiting_portal_url = url
            await self._send({"type": "state_update", "state": self.state.model_dump()})
            logger.info(
                "Portal blocked at %s — asking user for manual action session=%s",
                url, self.session_id,
            )
            # Extract portal name generically from URL hostname
            _host = urlparse(url).hostname or ""
            _parts = _host.split(".")
            portal = _parts[-3].capitalize() if len(_parts) >= 3 else (_host.capitalize() or "the portal")
            await self._send({
                "type": "agent_message",
                "content_en": (
                    f"The browser has reached a {portal} login / sign-in page. "
                    "Please log in manually — I'll detect when you're done and "
                    "continue the automation automatically. "
                    "Or type \"Done\" once you've finished."
                ),
                "content_sw": (
                    f"Kivinjari kimefika ukurasa wa kuingia wa {portal}. "
                    "Tafadhali ingia mwenyewe — nitaendelea moja kwa moja baada ya kutambua. "
                    "Au andika \"Nimemaliza\" unapokuwa tayari."
                ),
            })
            return

        # ── Auto-resume monitoring ──────────────────────────────────────────
        # Poll subsequent heartbeats: if URL has changed away from the blocked
        # page (and is no longer a login page), auto-resume the workflow.
        if self.state.status == "awaiting_user_input_on_portal" and self.state.awaiting_portal_url:
            blocked_base = self.state.awaiting_portal_url.split("?")[0]
            current_base = url.split("?")[0]
            if current_base != blocked_base and not _LOGIN_RE.search(url):
                logger.info(
                    "Portal unblocked (URL changed from %s to %s) — resuming session=%s",
                    blocked_base, current_base, self.session_id,
                )
                self.state.status = "executing"
                self.state.awaiting_portal_url = ""
                await self._send({"type": "state_update", "state": self.state.model_dump()})
                await self._send({
                    "type": "agent_message",
                    "content_en": "You're signed in! Continuing the automation…",
                    "content_sw": "Umeingia! Inaendelea...",
                })
                step_id = self.state.current_step_id or ""
                service_id = self.state.service_id or ""
                response_text = await self._run_agent(
                    f"The user has completed the manual portal action (login/form). "
                    f"The browser is now on '{url}'. "
                    f"Resume the workflow by calling execute_workflow_step with "
                    f"service_id='{service_id}' and step_id='{step_id}'."
                )
                if response_text:
                    await self._dispatch_agent_output(response_text)

    async def _handle_reset_session(self, data: dict) -> None:
        """Full session reset — wipes execution state and DB record."""
        logger.info("Session reset requested session=%s", self.session_id)
        self.state = ExecutionState()
        await self._save_session_state()
        await self._send({"type": "state_update", "state": self.state.model_dump()})

    async def _handle_resume_workflow(self, data: dict) -> None:
        """
        Extension sends this after a cross-origin page navigation so the
        server-side state is restored and the workflow continues from the
        correct step index.
        """
        self.state.service_id = data.get("service_id", self.state.service_id)
        self.state.step_index = data.get("step_index", self.state.step_index)
        self.state.total_steps = data.get("total_steps", self.state.total_steps)
        self.state.current_step_id = data.get("step_id", self.state.current_step_id)
        self.state.status = "executing"
        logger.info(
            "Workflow resumed service=%s step_index=%s session=%s",
            self.state.service_id,
            self.state.step_index,
            self.session_id,
        )
        await self._send({"type": "state_update", "state": self.state.model_dump()})

        # retry_current=True means the page was reloaded mid-step (not a navigate action).
        # Ask the agent to re-execute that specific step rather than move to the next one.
        if data.get("retry_current"):
            prompt = (
                f"The page was reloaded while executing step '{self.state.current_step_id}' "
                f"in workflow '{self.state.service_id}'. Please retry that step now by calling "
                f"execute_workflow_step with service_id='{self.state.service_id}' and "
                f"step_id='{self.state.current_step_id}'."
            )
        else:
            prompt = (
                f"Navigation completed. We are now on a new page. "
                f"The workflow is '{self.state.service_id}', currently at step index "
                f"{self.state.step_index} of {self.state.total_steps}. "
                f"Continue with the next execute_workflow_step call."
            )
        response_text = await self._run_agent(prompt)
        if response_text:
            await self._dispatch_agent_output(response_text)

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    # Sentinel prefixes that tools return to trigger consumer-side dispatch.
    # These must be intercepted from ToolMessages before the LLM prose response
    # is forwarded to the extension — the LLM may wrap them in natural language.
    # BUILD_PLAN is handled separately as a transparent side-effect before
    # checking for these prefixes.
    _SENTINEL_PREFIXES = (
        "EXECUTE_STEP:",
        "PAUSE_FOR_CONFIRMATION:",
        "NAVIGATE_TO:",
        "CLICK_ELEMENT:",
        "FILL_FIELD:",
    )

    async def _run_agent(self, user_input: str, *, silent: bool = False) -> str | None:
        """Invoke the LangGraph agent asynchronously and return the output string.

        silent=True suppresses the thinking bubble — used by the auto-retry path
        so retries don't leave a stuck spinner if they also 429.

        If any tool returned a sentinel string (EXECUTE_STEP, AWAIT_VAULT_KEY,
        OPEN_URL, PAUSE_FOR_CONFIRMATION) during this agent turn, that sentinel
        is returned directly instead of the final AI prose message. This ensures
        the consumer always dispatches the sentinel even when the LLM wraps it
        in natural-language text.
        """
        from pilot._session_context import set_current_anon_key
        if self.anon_key:
            set_current_anon_key(self.anon_key)
        # Make the latest heartbeat snapshot available to the explore_page tool.
        from pilot._session_context import set_current_heartbeat
        set_current_heartbeat(self.state.last_heartbeat)
        try:
            # Build messages list from chat history
            messages: list = []
            for msg in self.state.chat_history:
                if msg["role"] == "human":
                    messages.append(HumanMessage(content=msg["content"]))
                elif msg["role"] == "ai":
                    messages.append(AIMessage(content=msg["content"]))
            messages.append(HumanMessage(content=user_input))

            result = await self.agent_executor.ainvoke(
                {"messages": messages},
                config={"callbacks": [_ThinkingCallback(self._send)]} if not silent else {},
            )

            # Pass 1: process BUILD_PLAN as a transparent side-effect so the
            # goal panel is sent to the extension before we look for EXECUTE_STEP.
            for msg in result["messages"]:
                if isinstance(msg, ToolMessage):
                    content = msg.content if isinstance(msg.content, str) else ""
                    if content.startswith("BUILD_PLAN:"):
                        await self._process_build_plan(content)
                        break  # only one plan per agent turn

            # Pass 2: look for regular action sentinels — first match wins.
            # The LLM may wrap the sentinel in prose in its final AIMessage;
            # intercepting from the ToolMessage is more reliable.
            for msg in result["messages"]:
                if isinstance(msg, ToolMessage):
                    content = msg.content if isinstance(msg.content, str) else ""
                    if any(content.startswith(p) for p in self._SENTINEL_PREFIXES):
                        logger.debug(
                            "Sentinel intercepted from ToolMessage session=%s: %.80s",
                            self.session_id,
                            content,
                        )
                        return content

            # Doom-loop detection: if the same (tool_name, args) appears ≥3
            # times in the last 5 ToolMessages, inject an intervention message
            # so the LLM stops repeating itself instead of burning tokens.
            tool_calls: list[tuple[str, str]] = []
            for msg in result["messages"]:
                if isinstance(msg, ToolMessage):
                    tool_calls.append((msg.name or "", msg.content[:120] if isinstance(msg.content, str) else ""))
            for combo in set(tool_calls[-5:]):
                if tool_calls[-5:].count(combo) >= 3:
                    logger.warning(
                        "Doom-loop detected tool=%s session=%s — injecting intervention",
                        combo[0],
                        self.session_id,
                    )
                    intervention = (
                        "[AGENT LOOP DETECTED] You have called the same tool with the same arguments "
                        "3 times without progress. Stop repeating. Either: (a) use a different tool, "
                        "(b) call browser_click or navigate_browser to unblock yourself, or "
                        "(c) ask the user for help with ask_user."
                    )
                    self.state.chat_history.append({"role": "ai", "content": intervention})
                    asyncio.ensure_future(self._save_session_state())
                    return intervention

            # No sentinel — return the final AI prose message
            last_msg = result["messages"][-1]
            return last_msg.content if hasattr(last_msg, "content") else str(last_msg)
        except Exception as exc:
            logger.exception("Agent execution error session=%s: %s", self.session_id, exc)
            # Transient errors (rate-limit, network timeout) should not permanently fail the
            # workflow — leave status unchanged so heartbeat detection and user messages can
            # still fire.  The bot will recover on the next heartbeat or user message.
            exc_name = type(exc).__name__
            exc_str = str(exc)
            is_transient = (
                exc_name in ("RateLimitError", "APITimeoutError", "APIConnectionError")
                or "429" in exc_str
                or "rate" in exc_str.lower()
                or "Too many requests" in exc_str
            )
            if not is_transient:
                self.state.status = "failed"
                self.state.error_message = exc_str
                self.state.recoverable = False
                await self._send({"type": "state_update", "state": self.state.model_dump()})
            elif silent:
                # Called from _retry_after_rate_limit — don't spawn another retry.
                # The caller inspects the None return and re-schedules if still within cap.
                logger.warning(
                    "Retry also rate-limited session=%s: %s", self.session_id, exc_name
                )
            else:
                logger.warning(
                    "Transient API error — scheduling auto-retry session=%s: %s",
                    self.session_id, exc_name,
                )
                existing = getattr(self, "_rate_limit_retry_task", None)
                if not (existing and not existing.done()):
                    self._rate_limit_retry_task = asyncio.ensure_future(
                        self._retry_after_rate_limit(user_input)
                    )
                    await self._send({
                        "type": "agent_message",
                        "content_en": "I hit a rate limit — retrying automatically in 60 seconds. No action needed.",
                        "content_sw": "Nimepata kikomo — nitajaribu tena kwa sekunde 60. Hakuna hatua inayohitajika.",
                    })
            return None

    async def _retry_after_rate_limit(self, prompt: str, delay: int = 60, attempt: int = 1) -> None:
        """Silently retry the agent after a rate-limit delay. Up to 3 attempts with exponential backoff."""
        _MAX_RETRIES = 3
        try:
            await asyncio.sleep(delay)
            logger.info(
                "Auto-retry attempt %d/%d session=%s", attempt, _MAX_RETRIES, self.session_id
            )
            # silent=True: no thinking bubble; if it 429s again the elif-silent
            # branch in _run_agent logs it and returns None so we can re-schedule.
            response_text = await self._run_agent(prompt, silent=True)
            if response_text:
                await self._dispatch_agent_output(response_text)
            elif attempt < _MAX_RETRIES:
                next_delay = min(delay * 2, 300)  # 60 → 120 → 300 (5-min cap)
                mins = next_delay // 60
                await self._send({
                    "type": "agent_message",
                    "content_en": f"Service is still busy — retrying in {mins} minute(s)\u2026",
                    "content_sw": f"Huduma bado ina shughuli — nitajaribu tena baada ya dakika {mins}\u2026",
                })
                self._rate_limit_retry_task = asyncio.ensure_future(
                    self._retry_after_rate_limit(prompt, delay=next_delay, attempt=attempt + 1)
                )
            else:
                await self._send({
                    "type": "agent_message",
                    "content_en": "The service is still busy after several retries. Send any message when you\u2019re ready to try again.",
                    "content_sw": "Huduma bado ina shughuli baada ya majaribio mengi. Tuma ujumbe wowote unapokuwa tayari.",
                })
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Auto-retry failed session=%s: %s", self.session_id, exc)

    async def _process_build_plan(self, build_plan_output: str) -> None:
        """Store the goal tree in state and send set_plan to the extension."""
        try:
            payload = json.loads(build_plan_output[len("BUILD_PLAN:"):])
            self.state.plan = payload.get("goals", [])
            await self._send(
                {
                    "type": "set_plan",
                    "service_name": payload.get("service_name", ""),
                    "goals": self.state.plan,
                }
            )
            logger.info(
                "Plan built: service=%s goals=%d session=%s",
                payload.get("service_id", "?"),
                len(self.state.plan),
                self.session_id,
            )
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error(
                "Failed to parse BUILD_PLAN payload session=%s: %s", self.session_id, exc
            )

    def _find_goal_for_step(self, step_id: str) -> dict | None:
        """Return the first goal node whose step_ids contains step_id."""
        for goal in self.state.plan:
            if step_id in goal.get("step_ids", []):
                return goal
        return None

    async def _dispatch_agent_output(self, output: str) -> None:
        """
        Inspect the agent's output string for sentinel prefixes that should
        trigger side-effects (pause, await vault key, etc.).
        """
        if output.startswith("PAUSE_FOR_CONFIRMATION:"):
            _, step_label, fields = output.split(":", 2)
            self.state.status = "awaiting_user_confirmation"
            await self._send(
                {
                    "type": "pause_confirmation",
                    "step_label": step_label,
                    "fields": fields,
                }
            )
            await self._send({"type": "state_update", "state": self.state.model_dump()})
            return

        if output.startswith("EXECUTE_STEP:"):
            payload = json.loads(output[len("EXECUTE_STEP:"):])
            self.state.current_step_id = payload.get("step_id")
            self.state.total_steps = payload.get("total_steps", self.state.total_steps)
            self.state.service_id = payload.get("service_id", self.state.service_id)
            self.state.status = "executing"
            # Forward the step to the extension — keep service_id so the
            # extension can include it in pendingResume for cross-origin nav.
            ext_msg = dict(payload)
            ext_msg["type"] = "execute_step"
            await self._send(ext_msg)
            await self._send({"type": "state_update", "state": self.state.model_dump()})
            return

        if output.startswith("NAVIGATE_TO:"):
            try:
                payload = json.loads(output[len("NAVIGATE_TO:"):])
                await self._send({"type": "navigate_to", "url": payload.get("url", ""), "reason": payload.get("reason", "")})
            except (json.JSONDecodeError, KeyError) as exc:
                logger.error("Failed to parse NAVIGATE_TO payload session=%s: %s", self.session_id, exc)
            return

        if output.startswith("CLICK_ELEMENT:"):
            try:
                payload = json.loads(output[len("CLICK_ELEMENT:"):])
                await self._send({"type": "click_element", "label": payload.get("label", ""), "reason": payload.get("reason", "")})
            except (json.JSONDecodeError, KeyError) as exc:
                logger.error("Failed to parse CLICK_ELEMENT payload session=%s: %s", self.session_id, exc)
            return

        if output.startswith("FILL_FIELD:"):
            try:
                payload = json.loads(output[len("FILL_FIELD:"):])
                await self._send({"type": "fill_field", "label": payload.get("label", ""), "vault_key": payload.get("vault_key", ""), "reason": payload.get("reason", "")})
            except (json.JSONDecodeError, KeyError) as exc:
                logger.error("Failed to parse FILL_FIELD payload session=%s: %s", self.session_id, exc)
            return

        # Regular agent text — translate and forward to extension
        translation = await self._translate_to_swahili(output)
        await self._send(
            {
                "type": "agent_message",
                "content_en": output,
                "content_sw": translation,
            }
        )

    # ------------------------------------------------------------------
    # Translation helper (lightweight — uses LLM only if text is long)
    # ------------------------------------------------------------------

    async def _translate_to_swahili(self, text: str) -> str:
        """Best-effort Swahili translation via the LLM."""
        try:
            from django.conf import settings
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                base_url=settings.GITHUB_MODELS_BASE_URL,
                api_key=settings.GITHUB_TOKEN,
            )
            response = await client.chat.completions.create(
                model=settings.KENBOT_PILOT_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Translate the following text to Kenyan Swahili. "
                            "Return only the translated text, nothing else."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                max_tokens=512,
            )
            return response.choices[0].message.content or text
        except Exception as exc:
            logger.warning("Swahili translation failed: %s", exc)
            return text  # fall back to English

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _send(self, payload: dict) -> None:
        await self.send(text_data=json.dumps(payload))

    async def _send_error(self, message: str) -> None:
        await self._send({"type": "error", "message": message})

    @database_sync_to_async
    def _connect_session_record(self) -> None:
        """Create a new session or restore stored state when reconnecting."""
        from pilot.models import PilotSession

        session, created = PilotSession.objects.get_or_create(
            session_id=self.session_id,
            defaults={"status": "active"},
        )
        if not created and session.chat_history:
            # Reconnect — restore agent context so the LLM remembers the task
            self.state.chat_history = list(session.chat_history)
            if session.service_id:
                self.state.service_id = session.service_id
            if session.step_index:
                self.state.step_index = session.step_index
            if session.total_steps:
                self.state.total_steps = session.total_steps
            if session.plan:
                self.state.plan = list(session.plan)
                # Reconstruct completed_steps from goals already marked done
                self.state.completed_steps = [
                    step_id
                    for goal in session.plan
                    if goal.get("status") == "done"
                    for step_id in goal.get("step_ids", [])
                ]
            logger.info(
                "Session state restored: service=%s step=%d/%d history_len=%d plan_goals=%d session=%s",
                self.state.service_id,
                self.state.step_index,
                self.state.total_steps,
                len(self.state.chat_history),
                len(self.state.plan),
                self.session_id,
            )

    @database_sync_to_async
    def _save_session_state(self) -> None:
        """Persist chat history and execution progress to the DB."""
        from pilot.models import PilotSession

        PilotSession.objects.filter(session_id=self.session_id).update(
            chat_history=list(self.state.chat_history),
            service_id=self.state.service_id or "",
            step_index=self.state.step_index,
            total_steps=self.state.total_steps,
            plan=list(self.state.plan),
        )

    @database_sync_to_async
    def _update_session_status(self, new_status: str) -> None:
        from pilot.models import PilotSession

        PilotSession.objects.filter(session_id=self.session_id).update(
            status=new_status
        )

    @database_sync_to_async
    def _log_interaction(self, role: str, content: str) -> None:
        from pilot.models import ExecutionLog, PilotSession

        try:
            session = PilotSession.objects.get(session_id=self.session_id)
            ExecutionLog.objects.create(session=session, role=role, content=content)
        except PilotSession.DoesNotExist:
            logger.warning(
                "Cannot log interaction — no PilotSession for %s", self.session_id
            )
