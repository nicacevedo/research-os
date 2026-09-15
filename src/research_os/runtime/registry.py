"""The action dispatch table, and what each action promises about replay.

A closed mapping from :class:`~research_os.runtime.policy.ActionKind` to a
:class:`RegisteredAction`, with no default branch. An action with no entry is
refused when the plan is validated rather than discovered halfway through.

The interesting field is ``replay_safe``, and it exists because a failing crash
test showed the first design was wrong. LangGraph re-runs a node when a process
dies inside it, so every action is potentially executed more than once. The
original code handled that by giving every action a reconciler that said "the
effect did not happen" -- which is a *lie* whenever the outcome is unknown, and
duplicated the side effect exactly as the test demanded it must not.

The distinction that was missing: actions differ in whether they change anything
outside this process.

``replay_safe=True`` -- reading a repository, validating a capsule, recomputing
the frontier. Running it again produces the same answer and no additional
effect. Replay is free, so the ledger lets it re-run.

``replay_safe=False`` -- submitting a job, making a commit, downloading a file.
Running it again is a duplicate. These must supply a ``reconcile`` that can look
at the world and say whether the previous attempt took hold. Registration
without one is refused at import time, because the alternative -- discovering it
during a crash -- is the expensive way to find out.

Where reconciliation is genuinely impossible, the action is refused on resume
and escalated. Guessing is how a cluster runs the same experiment twice.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from research_os.runtime.actions.base import ActionOutcome, Handler
from research_os.runtime.actions.inspect import (
    assess_frontier,
    inspect_repository,
    rebuild_derived_index,
    validate_capsule,
)
from research_os.runtime.context import CycleContext
from research_os.runtime.policy import ACTIONS, ActionKind

__all__ = [
    "ACTION_HANDLERS",
    "ActionOutcome",
    "RegisteredAction",
    "register",
    "unimplemented_actions",
]

#: Establishes whether a previous attempt's visible effect took hold. Returns
#: the recovered result, or ``None`` to say it did not happen.
Reconciler = Callable[
    [Mapping[str, Any], CycleContext, Mapping[str, Any]], dict[str, Any] | None
]


@dataclass(frozen=True, slots=True)
class RegisteredAction:
    handler: Handler
    #: True when running the handler again produces no additional visible
    #: effect outside this process.
    replay_safe: bool
    #: Required when ``replay_safe`` is false.
    reconcile: Reconciler | None = None

    def __post_init__(self) -> None:
        if not self.replay_safe and self.reconcile is None:
            raise ValueError(
                "an action with a visible side effect must supply a reconciler, so "
                "that a crash between the effect and its acknowledgement can be "
                "resolved by looking at the world rather than by guessing"
            )


ACTION_HANDLERS: dict[ActionKind, RegisteredAction] = {
    # Every one of these reads. Running one twice costs a little time and
    # changes nothing, so replay is the cheapest correct answer.
    ActionKind.INSPECT_REPOSITORY: RegisteredAction(
        inspect_repository, replay_safe=True
    ),
    ActionKind.VALIDATE_CAPSULE: RegisteredAction(validate_capsule, replay_safe=True),
    ActionKind.ASSESS_FRONTIER: RegisteredAction(assess_frontier, replay_safe=True),
    # Rebuilding a derived index is idempotent by construction: it is a function
    # of the store it is derived from, and it holds the index lock while it runs.
    ActionKind.REBUILD_DERIVED_INDEX: RegisteredAction(
        rebuild_derived_index, replay_safe=True
    ),
}


def register(
    action: ActionKind,
    handler: Handler,
    *,
    replay_safe: bool,
    reconcile: Reconciler | None = None,
) -> None:
    """Register or replace one action's handler.

    Used by the capability modules at import time, and by tests that need an
    action with an observable effect. Goes through :class:`RegisteredAction` so
    the reconciler requirement cannot be bypassed.
    """

    ACTION_HANDLERS[action] = RegisteredAction(
        handler=handler, replay_safe=replay_safe, reconcile=reconcile
    )


def unimplemented_actions() -> tuple[ActionKind, ...]:
    """Actions whose authority is defined but whose handler is not written.

    Human-executed actions are excluded: they have no handler by design, because
    the person performs them. Surfaced by ``researchctl runtime doctor`` so the
    gap between "the policy knows about this" and "this build can do it" is
    visible rather than discovered when a planner picks one.
    """

    return tuple(
        action
        for action, policy in ACTIONS.items()
        if action not in ACTION_HANDLERS and not policy.human_executes
    )
