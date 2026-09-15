"""The action registry: closed, consistent with policy, and honest about replay.

The ``replay_safe`` flag is the field a crash test forced into existence. Its
invariant -- an effectful action must be able to establish what a previous
attempt did -- is enforced at registration rather than discovered during a
crash, which is the expensive way to find out.
"""

from __future__ import annotations

import pytest

from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.failures import FailureClass
from research_os.runtime.policy import ACTIONS, AutonomyLevel
from research_os.runtime.registry import (
    ACTION_HANDLERS,
    RegisteredAction,
    unimplemented_actions,
)


def test_every_registered_action_has_a_policy_entry() -> None:
    """An action that can be performed but whose authority is undefined."""

    missing = sorted(str(a) for a in ACTION_HANDLERS if a not in ACTIONS)
    assert missing == []


def test_every_registered_handler_is_callable() -> None:
    for action, registered in ACTION_HANDLERS.items():
        assert callable(registered.handler), f"{action} has a non-callable handler"


def test_no_a2_action_has_a_handler() -> None:
    """The runtime performs no scientific-authority action, even once approved.

    Every `A2` action in this build writes canonical scientific state, merges to
    a canonical branch, or publishes -- and the runtime has no method for any of
    those. Approval unlocks a recorded decision and an instruction.
    """

    gated_with_handlers = sorted(
        str(action)
        for action, registered in ACTION_HANDLERS.items()
        if ACTIONS[action].level is AutonomyLevel.A2 and registered.handler is not None
    )
    assert gated_with_handlers == []


def test_every_gated_action_tells_the_researcher_what_to_run() -> None:
    """A gate that says "approved" and nothing else is a dead end."""

    silent = sorted(
        str(action)
        for action, policy in ACTIONS.items()
        if policy.human_executes and not policy.follow_up.strip()
    )
    assert silent == []


def test_an_effectful_action_cannot_be_registered_without_a_reconciler() -> None:
    """Enforced at registration, not discovered during a crash."""

    def handler(state, context, plan):  # pragma: no cover - never called
        return ActionOutcome.succeeded("x")

    with pytest.raises(ValueError, match="must supply a reconciler"):
        RegisteredAction(handler=handler, replay_safe=False)


def test_a_replay_safe_action_needs_no_reconciler() -> None:
    def handler(state, context, plan):  # pragma: no cover - never called
        return ActionOutcome.succeeded("x")

    assert RegisteredAction(handler=handler, replay_safe=True).reconcile is None


def test_unimplemented_actions_excludes_the_human_executed_ones() -> None:
    """Those have no handler by design; listing them as gaps would be noise."""

    gaps = set(unimplemented_actions())
    human = {action for action, policy in ACTIONS.items() if policy.human_executes}
    assert gaps & human == set()


def test_a_successful_outcome_cannot_carry_a_failure_class() -> None:
    """A refuted hypothesis is a success. This is where that could go wrong."""

    with pytest.raises(ValueError, match="negative result is a success"):
        ActionOutcome(ok=True, failure_class=FailureClass.CODE_EXCEPTION)


def test_a_failed_outcome_must_name_its_class() -> None:
    """Otherwise the retry policy has nothing to decide from."""

    with pytest.raises(ValueError, match="must name its failure class"):
        ActionOutcome(ok=False, detail="something went wrong")
