"""Tests for automation runtime models and the run-state machine."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from research_os.automation.models import (
    TERMINAL_RUN_STATES,
    AcceptanceCommand,
    AutomationRun,
    Budget,
    CommandResult,
    ModelInvocation,
    RiskClass,
    Role,
    RunState,
    WorkOrder,
    allowed_transitions,
    assert_transition,
    utc_now,
)
from research_os.errors import RunStateError

COMMIT = "a" * 40


def make_order(**updates: object) -> WorkOrder:
    data: dict[str, object] = {
        "task_id": "T-001",
        "title": "Implement add",
        "goal": "Make add return a sum.",
        "role": Role.CODER,
        "risk_class": RiskClass.WRITE_ISOLATED,
        "project_path": "/tmp/project",
        "base_commit": COMMIT,
        "read_only": False,
        "allowed_paths": ["adder.py"],
        "completion_condition": "pytest exits 0",
        "timeout_seconds": 60,
        "provider": "fake",
        "expected_output": "diff",
    }
    data.update(updates)
    return WorkOrder.model_validate(data)


def make_run(**updates: object) -> AutomationRun:
    now = utc_now()
    data: dict[str, object] = {
        "run_id": "RUN-20260909T101500Z-0a1b2c3d",
        "project_path": "/tmp/project",
        "goal": "do the thing",
        "base_commit": COMMIT,
        "created_at": now,
        "updated_at": now,
    }
    data.update(updates)
    return AutomationRun.model_validate(data)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.CREATED, RunState.PREFLIGHTED),
        (RunState.PREFLIGHTED, RunState.PLANNING),
        (RunState.PLANNING, RunState.PLAN_READY),
        (RunState.PLAN_READY, RunState.EXECUTING),
        (RunState.EXECUTING, RunState.CHECKING),
        (RunState.CHECKING, RunState.REVIEWING),
        (RunState.REVIEWING, RunState.READY_FOR_HUMAN),
    ],
)
def test_forward_transitions_are_allowed(current: RunState, target: RunState) -> None:
    assert_transition(current, target)


@pytest.mark.parametrize(
    "state",
    [state for state in RunState if state not in TERMINAL_RUN_STATES],
)
def test_any_live_run_may_fail_or_be_cancelled(state: RunState) -> None:
    assert_transition(state, RunState.FAILED)
    assert_transition(state, RunState.CANCELLED)


@pytest.mark.parametrize("state", sorted(TERMINAL_RUN_STATES))
def test_terminal_states_have_no_successor(state: RunState) -> None:
    assert allowed_transitions(state) == frozenset()


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.CREATED, RunState.READY_FOR_HUMAN),
        (RunState.CREATED, RunState.EXECUTING),
        (RunState.PLAN_READY, RunState.REVIEWING),
        (RunState.CHECKING, RunState.READY_FOR_HUMAN),
        (RunState.READY_FOR_HUMAN, RunState.EXECUTING),
        (RunState.FAILED, RunState.READY_FOR_HUMAN),
        (RunState.CANCELLED, RunState.PLANNING),
    ],
)
def test_undeclared_transitions_are_refused(
    current: RunState, target: RunState
) -> None:
    with pytest.raises(RunStateError):
        assert_transition(current, target)


def test_failed_run_cannot_be_walked_back_to_ready() -> None:
    with pytest.raises(RunStateError):
        assert_transition(RunState.FAILED, RunState.REVIEWING)


def test_read_only_order_must_use_read_only_risk_class() -> None:
    with pytest.raises(ValidationError, match="read_only risk class"):
        make_order(read_only=True)


def test_write_order_requires_a_scope() -> None:
    with pytest.raises(ValidationError, match="allowed_paths"):
        make_order(allowed_paths=[])


def test_order_cannot_depend_on_itself() -> None:
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        make_order(dependencies=["T-001"])


def test_order_rejects_duplicate_dependencies() -> None:
    with pytest.raises(ValidationError, match="must not repeat"):
        make_order(dependencies=["T-002", "T-002"])


@pytest.mark.parametrize("task_id", ["T-1", "T-0001", "001", "t-001", ""])
def test_order_rejects_malformed_task_ids(task_id: str) -> None:
    with pytest.raises(ValidationError):
        make_order(task_id=task_id)


@pytest.mark.parametrize("commit", ["abc", "A" * 40, "a" * 39, ""])
def test_order_requires_a_full_commit_sha(commit: str) -> None:
    with pytest.raises(ValidationError):
        make_order(base_commit=commit)


@pytest.mark.parametrize(
    "scope",
    ["/etc/passwd", "~/secrets", "../outside", "a/../b", "a\\b", "", "."],
)
def test_order_rejects_unsafe_scope_paths(scope: str) -> None:
    with pytest.raises(ValidationError):
        make_order(allowed_paths=[scope])


def test_acceptance_command_requires_a_bare_program() -> None:
    with pytest.raises(ValidationError, match="bare program name"):
        AcceptanceCommand(argv=["/usr/bin/rm", "-rf", "/"])
    with pytest.raises(ValidationError):
        AcceptanceCommand(argv=[])


def test_acceptance_command_display_is_the_argument_vector() -> None:
    command = AcceptanceCommand(argv=["uv", "run", "pytest", "-q"])
    assert command.display == "uv run pytest -q"
    assert command.required is True


def test_run_rejects_a_malformed_run_id() -> None:
    with pytest.raises(ValidationError, match="run_id"):
        make_run(run_id="RUN-nope")


def test_run_rejects_a_budget_it_has_already_exceeded() -> None:
    with pytest.raises(ValidationError, match="model-call budget"):
        make_run(model_calls_used=9, budget=Budget(max_model_calls=8))


def test_unreported_cost_stays_unknown_rather_than_zero() -> None:
    now = utc_now()
    invocation = ModelInvocation(
        invocation_id="INV-0001",
        run_id="RUN-20260909T101500Z-0a1b2c3d",
        role=Role.PLANNER,
        provider="fake",
        read_only=True,
        cwd="/tmp/project",
        started_at=now,
        ended_at=now,
        duration_ms=5,
        timeout_seconds=60,
        exit_code=0,
    )
    run = make_run(invocations=[invocation])
    assert invocation.total_cost_usd is None
    assert invocation.input_tokens is None
    assert run.total_cost_usd() is None


def test_reported_costs_are_summed() -> None:
    now = utc_now()

    def invocation(index: int, cost: float) -> ModelInvocation:
        return ModelInvocation(
            invocation_id=f"INV-{index:04d}",
            run_id="RUN-20260909T101500Z-0a1b2c3d",
            role=Role.PLANNER,
            provider="fake",
            read_only=True,
            cwd="/tmp/project",
            started_at=now,
            ended_at=now,
            duration_ms=5,
            timeout_seconds=60,
            exit_code=0,
            total_cost_usd=cost,
        )

    run = make_run(invocations=[invocation(1, 0.25), invocation(2, 0.75)])
    assert run.total_cost_usd() == pytest.approx(1.0)


def test_invocation_id_shape_is_enforced() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="INV-0001"):
        ModelInvocation(
            invocation_id="1",
            run_id="RUN-20260909T101500Z-0a1b2c3d",
            role=Role.PLANNER,
            provider="fake",
            read_only=True,
            cwd="/tmp",
            started_at=now,
            ended_at=now,
            duration_ms=1,
            timeout_seconds=60,
        )


def test_required_checks_passed_is_false_without_any_check() -> None:
    """An unverified work order must never read as a verified one.

    ``all([])`` is True, so a naive implementation reports that every required
    check passed on an order where nothing ran at all.
    """

    order = make_order()
    assert order.check_results == []
    assert order.required_checks_passed is False


# -- audit regression: nothing checked is not the same as checks passed ------


def command_result(*, required: bool, exit_code: int) -> CommandResult:
    now = utc_now()
    return CommandResult(
        argv=["pytest", "-q"],
        cwd="/tmp/worktree",
        required=required,
        exit_code=exit_code,
        timed_out=False,
        timeout_seconds=60,
        started_at=now,
        ended_at=now,
        duration_ms=1,
    )


def test_an_order_with_only_optional_checks_has_not_passed_its_required_ones() -> None:
    """``all([])`` would call this verified; no required check ever ran."""

    order = make_order(check_results=[command_result(required=False, exit_code=0)])

    assert order.check_results != []
    assert order.required_checks_passed is False


def test_required_checks_passed_is_true_only_when_a_required_check_succeeded() -> None:
    passing = make_order(check_results=[command_result(required=True, exit_code=0)])
    failing = make_order(check_results=[command_result(required=True, exit_code=1)])

    assert passing.required_checks_passed is True
    assert failing.required_checks_passed is False


def test_a_failing_optional_check_does_not_sink_a_passing_required_one() -> None:
    order = make_order(
        check_results=[
            command_result(required=True, exit_code=0),
            command_result(required=False, exit_code=1),
        ]
    )

    assert order.required_checks_passed is True
