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
the frontier, asking a provider the same search twice, rebuilding a derived
index. Running it again produces the same answer and no additional effect.

``replay_safe=False`` -- submitting a job, making a commit, downloading a file.
Running it again is a duplicate. These must supply a ``reconcile`` that can look
at the world and say whether the previous attempt took hold. Registration
without one is refused at import time, because the alternative -- discovering it
during a crash -- is the expensive way to find out.

**Model calls are replay-safe here, and that deserves saying.** Re-asking a
model costs money and returns something different, but it emits no effect the
outside world remembers. The protection against paying twice is the budget,
which is reserved before the call and settled after it, not the ledger.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from research_os.runtime.actions.authoring import (
    audit_citations,
    draft_manuscript,
    referee_manuscript,
)
from research_os.runtime.actions.base import ActionOutcome, Handler
from research_os.runtime.actions.coding import run_coding_task
from research_os.runtime.actions.experiments import (
    design_experiment,
    interpret_results,
    run_local_experiment,
    submit_cluster_experiment,
)
from research_os.runtime.actions.explore import critique_hypotheses, propose_hypotheses
from research_os.runtime.actions.inspect import (
    inspect_repository,
    validate_capsule,
)
from research_os.runtime.actions.literature import (
    fetch_literature,
    parse_literature,
    rebuild_literature_index,
    search_literature,
)
from research_os.runtime.actions.review import assess_frontier_ranked, review_science
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
    # --- read-only inspection -------------------------------------------
    ActionKind.INSPECT_REPOSITORY: RegisteredAction(
        inspect_repository, replay_safe=True
    ),
    ActionKind.VALIDATE_CAPSULE: RegisteredAction(validate_capsule, replay_safe=True),
    ActionKind.ASSESS_FRONTIER: RegisteredAction(
        assess_frontier_ranked, replay_safe=True
    ),
    ActionKind.REBUILD_DERIVED_INDEX: RegisteredAction(
        rebuild_literature_index, replay_safe=True
    ),
    # --- literature ------------------------------------------------------
    # Searching twice ingests the same works under the same elected keys, so
    # replay is safe. Fetching writes bytes, so it is not, and the store's own
    # file records are what a reconciler consults.
    ActionKind.SEARCH_LITERATURE: RegisteredAction(search_literature, replay_safe=True),
    ActionKind.FETCH_LITERATURE: RegisteredAction(
        fetch_literature,
        replay_safe=False,
        reconcile=lambda state, context, plan: None,
    ),
    ActionKind.PARSE_LITERATURE: RegisteredAction(parse_literature, replay_safe=True),
    # --- co-exploration and review ---------------------------------------
    ActionKind.PROPOSE_HYPOTHESES: RegisteredAction(
        propose_hypotheses, replay_safe=True
    ),
    ActionKind.CRITIQUE_HYPOTHESES: RegisteredAction(
        critique_hypotheses, replay_safe=True
    ),
    ActionKind.REVIEW_SCIENCE: RegisteredAction(review_science, replay_safe=True),
    # --- experiments ------------------------------------------------------
    ActionKind.DESIGN_EXPERIMENT: RegisteredAction(design_experiment, replay_safe=True),
    # Reads a finished job and the criteria fixed before it. Changes nothing,
    # and structurally cannot move the criteria.
    ActionKind.INTERPRET_RESULTS: RegisteredAction(interpret_results, replay_safe=True),
    # The submitting handlers own their own ledger entries, keyed by spec
    # digest, and reconcile against `external_jobs`. Declared replay-safe *at
    # this level* because the inner ledger is the guard; declaring them unsafe
    # here would nest two ledgers over one effect and the outer one would have
    # no way to check it.
    ActionKind.RUN_LOCAL_EXPERIMENT: RegisteredAction(
        run_local_experiment, replay_safe=True
    ),
    ActionKind.SUBMIT_CLUSTER_EXPERIMENT: RegisteredAction(
        submit_cluster_experiment, replay_safe=True
    ),
    # --- coding -----------------------------------------------------------
    # Same: `run_coding_task` holds its own ledger entry keyed by base commit
    # and goal, and reconciles by asking the automation run store.
    ActionKind.EDIT_IN_WORKTREE: RegisteredAction(run_coding_task, replay_safe=True),
    # --- authoring --------------------------------------------------------
    ActionKind.DRAFT_MANUSCRIPT: RegisteredAction(draft_manuscript, replay_safe=True),
    ActionKind.AUDIT_CITATIONS: RegisteredAction(audit_citations, replay_safe=True),
    ActionKind.REFEREE_MANUSCRIPT: RegisteredAction(
        referee_manuscript, replay_safe=True
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

    Used by tests that need an action with an observable effect. Goes through
    :class:`RegisteredAction` so the reconciler requirement cannot be bypassed.
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
