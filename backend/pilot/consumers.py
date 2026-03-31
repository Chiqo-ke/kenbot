from __future__ import annotations

import json
import logging
import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from pilot.agent import build_pilot_agent
from pilot.state import ExecutionState

logger = logging.getLogger(__name__)

# Sentinel prefixes that tools return to trigger consumer side-effects
_PAUSE_PREFIX = "PAUSE_FOR_CONFIRMATION"
_MISSING_KEY_PREFIX = "AWAIT_VAULT_KEY"


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

        response_text = await self._run_agent(content)
        if response_text:
            self.state.chat_history.append({"role": "ai", "content": response_text})
            await self._dispatch_agent_output(response_text)

    async def _handle_step_confirmed(self, data: dict) -> None:
        step_id: str = data.get("step_id", "")
        logger.info("Step confirmed step_id=%s session=%s", step_id, self.session_id)

        # Track completion and update goal panel
        if step_id and step_id not in self.state.completed_steps:
            self.state.completed_steps.append(step_id)

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

        # Feed failure back into the agent for it to call request_healing
        hb = self.state.last_heartbeat
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

    async def _handle_heartbeat(self, data: dict) -> None:
        """
        Receive a 15-second page snapshot from the extension.

        Stores the snapshot in self.state.last_heartbeat so it is available
        to the explore_page tool during the next agent run (via ContextVar).
        Never triggers an agent run on its own — purely passive storage.

        SECURITY: page_text_preview may contain portal text but no credentials
        (those are vault-only and never appear in visible page text).
        """
        self.state.last_heartbeat = {
            "url": data.get("url", ""),
            "title": data.get("title", ""),
            "page_text_preview": data.get("page_text_preview", ""),
            "visible_fields": data.get("visible_fields", []),
            "has_error": bool(data.get("has_error", False)),
            "has_success": bool(data.get("has_success", False)),
            "user_modified_fields": data.get("user_modified_fields", []),
        }
        logger.debug(
            "Heartbeat session=%s url=%s has_error=%s user_modified=%s",
            self.session_id,
            self.state.last_heartbeat["url"],
            self.state.last_heartbeat["has_error"],
            self.state.last_heartbeat["user_modified_fields"],
        )
        await self._send({"type": "heartbeat_ack"})

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
    )

    async def _run_agent(self, user_input: str) -> str | None:
        """Invoke the LangGraph agent asynchronously and return the output string.

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

            result = await self.agent_executor.ainvoke({"messages": messages})

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

            # No sentinel — return the final AI prose message
            last_msg = result["messages"][-1]
            return last_msg.content if hasattr(last_msg, "content") else str(last_msg)
        except Exception as exc:
            logger.exception("Agent execution error session=%s: %s", self.session_id, exc)
            self.state.status = "failed"
            self.state.error_message = str(exc)
            self.state.recoverable = False
            await self._send({"type": "state_update", "state": self.state.model_dump()})
            return None

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
