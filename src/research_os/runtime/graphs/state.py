"""What a bounded cycle carries between its nodes, and what it must not.

Everything here is small and structured. Large things are
:class:`~research_os.runtime.interfaces.ArtifactRef` values -- a hash, a media
type, a role -- because LangGraph persists state at every superstep and state
that carries bytes is a checkpoint table that grows by megabytes per literature
fetch. ``tests/test_runtime_graph.py`` measures a real checkpoint to assert it.

The services a node needs -- the database, the ledger, the router, the kernel
adapter -- travel in :class:`research_os.runtime.context.CycleContext`, which
LangGraph passes as runtime context and does **not** checkpoint. That is what
allows a live connection pool to reach a node without being serialised. It is
re-exported here because every node imports both from one place.
"""

from __future__ import annotations

from typing import Any, TypedDict

from research_os.runtime.context import CycleContext

__all__ = ["MAX_NOTES", "CycleContext", "CycleState", "note", "with_artifact"]


class CycleState(TypedDict, total=False):
    """One bounded cycle's persisted state.

    ``total=False`` because a resumed thread is restored key by key and a node
    that only sets what it learned is easier to reason about than one that must
    restate everything.
    """

    # --- identity, set once at the start ---------------------------------
    run_id: str
    project_id: str
    repo_path: str
    objective: str
    autonomy: str
    cycle_index: int

    # --- hydrate_project_state -------------------------------------------
    frontier: dict[str, Any]
    capsule_valid: bool

    # --- plan_one_action / validate_plan ---------------------------------
    plan: dict[str, Any]
    plan_refusal: str

    # --- perform_action ---------------------------------------------------
    action_result: dict[str, Any]
    action_reused: bool

    # --- deterministic_check ----------------------------------------------
    check_result: dict[str, Any]

    # --- review -----------------------------------------------------------
    review: dict[str, Any]

    # --- the human gate: prepare / interrupt / apply ----------------------
    decision_packet: dict[str, Any]
    approval_id: str
    decision: dict[str, Any]
    decision_applied: bool
    #: What the researcher must run themselves, when the approved action is one
    #: only they can perform.
    follow_up: str

    # --- termination -------------------------------------------------------
    terminal_state: str
    next_recommendation: str

    # --- accumulating record ----------------------------------------------
    #: Artifact references produced by this cycle, as plain dicts so the
    #: serialiser has nothing clever to do.
    artifacts: list[dict[str, Any]]
    #: A short human-readable trail. Bounded by `MAX_NOTES`.
    notes: list[str]


#: Notes are for a person reading `runtime run show`, not an audit log -- the
#: event table is the audit log. Capped so a long cycle cannot grow its own
#: checkpoints without bound.
MAX_NOTES = 200


def note(state: CycleState, message: str) -> list[str]:
    """Append to the trail, bounded."""

    existing = list(state.get("notes", []))
    existing.append(message)
    return existing[-MAX_NOTES:]


def with_artifact(state: CycleState, ref: dict[str, Any]) -> list[dict[str, Any]]:
    existing = list(state.get("artifacts", []))
    existing.append(ref)
    return existing
