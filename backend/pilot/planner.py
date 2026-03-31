"""
pilot/planner.py — Goal-tree builder for the Pilot agent.

Given a ServiceMap, build_goal_tree() returns a flat list of goal nodes that
the consumer sends to the extension as the `set_plan` message.

Design rules (all portal knowledge stays in JSON maps — zero here):
  • Steps that share the same `phase` are folded under one goal node.
  • Steps with no `phase` each become their own goal node labelled step_label.
  • When a map has `requires_auth`, the auth service's goal tree is
    recursively prepended (marked is_prerequisite=True) so login is never
    inlined in individual maps.
  • Failure subgoals from step.failure_subgoals are attached to the goal node
    whose step_ids list contains that step.
"""
from __future__ import annotations

import logging
import uuid
from itertools import groupby

logger = logging.getLogger(__name__)

# Maximum recursion depth for requires_auth chains (prevents infinite loops)
_MAX_AUTH_DEPTH = 4


def build_goal_tree(service_map, depth: int = 0) -> list[dict]:
    """
    Return a list of goal-node dicts for `service_map`.

    Each node has the shape:
    {
        "id":              str,        # stable UUID for this goal
        "label":           str,        # human-readable phase / step label
        "status":          "pending",  # pending | running | done | failed
        "step_ids":        [str, ...], # workflow step_ids covered by this goal
        "failure_subgoals": [...],     # from WorkflowStep.failure_subgoals
        "is_prerequisite": bool,       # true for auto-prepended auth goals
    }

    Args:
        service_map: A validated ServiceMap instance (maps.schemas.ServiceMap).
        depth:       Internal recursion counter — do not pass from callers.
    """
    # Defensive: avoid runaway recursion
    if depth >= _MAX_AUTH_DEPTH:
        logger.warning(
            "planner: requires_auth chain too deep (depth=%d) for service_id=%s — stopping",
            depth,
            getattr(service_map, "service_id", "?"),
        )
        return []

    nodes: list[dict] = []

    # ── 1. Prepend auth goal tree when requires_auth is set ───────────────────
    if service_map.requires_auth:
        auth_nodes = _load_auth_goals(service_map.requires_auth, depth + 1)
        for node in auth_nodes:
            node["is_prerequisite"] = True
        nodes.extend(auth_nodes)

    # ── 2. Group remaining workflow steps by phase ────────────────────────────
    for phase_label, steps in _group_by_phase(service_map.workflow):
        steps = list(steps)
        step_ids = [s.step_id for s in steps]

        # Collect failure_subgoals from all steps in this phase.
        # In practice each phase usually has at most one step with subgoals.
        all_subgoals: list[dict] = []
        for step in steps:
            for sg in (step.failure_subgoals or []):
                all_subgoals.append(sg.model_dump())

        nodes.append(
            {
                "id": str(uuid.uuid4()),
                "label": phase_label,
                "status": "pending",
                "step_ids": step_ids,
                "failure_subgoals": all_subgoals,
                "is_prerequisite": False,
            }
        )

    return nodes


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _group_by_phase(workflow):
    """
    Yield (phase_label, steps) pairs preserving insertion order.

    Steps without a `phase` each become their own single-step group labelled
    by their step_label.
    """
    # We can't use itertools.groupby directly because None-phase steps must
    # each be their own group.  Walk manually.
    current_phase: str | None = None
    current_steps: list = []

    for step in workflow:
        effective_phase = step.phase if step.phase else step.step_label

        if effective_phase != current_phase:
            if current_steps:
                yield current_phase, iter(current_steps)
            current_phase = effective_phase
            current_steps = [step]
        else:
            current_steps.append(step)

    if current_steps:
        yield current_phase, iter(current_steps)


def _load_auth_goals(auth_service_id: str, depth: int) -> list[dict]:
    """Load an auth ServiceMap and build its goal tree."""
    from maps.repository import MapRepository

    repo = MapRepository()
    auth_map = repo.get_map(auth_service_id)
    if auth_map is None:
        logger.warning(
            "planner: requires_auth service_id=%s not found — skipping auth goals",
            auth_service_id,
        )
        return []
    return build_goal_tree(auth_map, depth=depth)
