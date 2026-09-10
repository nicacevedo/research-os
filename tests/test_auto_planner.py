"""Tests for planner structured output and plan admission."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_os.automation.config import DEFAULT_ALLOWED_CHECK_PROGRAMS
from research_os.automation.models import Budget, RiskClass, Role
from research_os.automation.planner import (
    build_planner_prompt,
    parse_plan,
    plan_to_work_orders,
    validate_plan,
)
from research_os.errors import PlanValidationError
from tests.automation_helpers import fake_config, plan_payload

COMMIT = "c" * 40
PROGRAMS = DEFAULT_ALLOWED_CHECK_PROGRAMS


def test_structured_output_is_used_directly() -> None:
    plan = parse_plan(structured=plan_payload(), text=None)
    assert plan.summary == "Implement the missing function."
    assert [task.id for task in plan.tasks] == ["T-001"]
    assert plan.tasks[0].acceptance_commands[0].argv == ["python", "-m", "pytest", "-q"]


def test_a_fenced_json_body_is_recovered() -> None:
    body = "```json\n" + json.dumps(plan_payload()) + "\n```"
    plan = parse_plan(structured=None, text=body)
    assert plan.tasks[0].title == "Implement add"


def test_prose_without_json_is_rejected() -> None:
    with pytest.raises(PlanValidationError, match="no JSON object"):
        parse_plan(structured=None, text="I would start by reading the tests.")


def test_no_output_at_all_is_rejected() -> None:
    with pytest.raises(PlanValidationError, match="no JSON object"):
        parse_plan(structured=None, text=None)


def test_malformed_json_is_not_repaired() -> None:
    with pytest.raises(PlanValidationError, match="no JSON object"):
        parse_plan(structured=None, text='{"summary": "x", "tasks": [},')


def test_unknown_keys_are_rejected() -> None:
    payload = plan_payload()
    payload["tasks"][0]["shell"] = "rm -rf /"
    with pytest.raises(PlanValidationError, match="not a valid plan"):
        parse_plan(structured=payload, text=None)


def test_a_plan_with_no_tasks_is_rejected() -> None:
    with pytest.raises(PlanValidationError, match="not a valid plan"):
        parse_plan(structured={"summary": "nothing to do", "tasks": []}, text=None)


def test_a_conforming_plan_is_admitted() -> None:
    plan = parse_plan(structured=plan_payload(), text=None)
    validate_plan(plan, budget=Budget(), allowed_programs=PROGRAMS)


def test_too_many_tasks_for_the_budget_is_rejected() -> None:
    tasks = []
    for index in range(1, 4):
        entry = plan_payload(task_id=f"T-{index:03d}")["tasks"][0]
        tasks.append(entry)
    plan = parse_plan(structured=plan_payload(tasks=tasks), text=None)
    with pytest.raises(PlanValidationError, match="budget allows"):
        validate_plan(
            plan,
            budget=Budget(max_work_orders=2, max_write_work_orders=2),
            allowed_programs=PROGRAMS,
        )


def test_more_write_tasks_than_the_budget_is_rejected() -> None:
    tasks = [
        plan_payload(task_id="T-001")["tasks"][0],
        plan_payload(task_id="T-002")["tasks"][0],
    ]
    plan = parse_plan(structured=plan_payload(tasks=tasks), text=None)
    with pytest.raises(PlanValidationError, match="write tasks"):
        validate_plan(
            plan,
            budget=Budget(max_work_orders=4, max_write_work_orders=1),
            allowed_programs=PROGRAMS,
        )


def test_out_of_sequence_task_ids_are_rejected() -> None:
    plan = parse_plan(structured=plan_payload(task_id="T-002"), text=None)
    with pytest.raises(PlanValidationError, match="sequential from T-001"):
        validate_plan(plan, budget=Budget(), allowed_programs=PROGRAMS)


def test_a_forward_dependency_is_rejected() -> None:
    first = plan_payload(task_id="T-001")["tasks"][0]
    first["dependencies"] = ["T-002"]
    second = plan_payload(task_id="T-002")["tasks"][0]
    plan = parse_plan(structured=plan_payload(tasks=[first, second]), text=None)
    with pytest.raises(PlanValidationError, match="not an earlier task"):
        validate_plan(plan, budget=Budget(), allowed_programs=PROGRAMS)


def test_a_read_only_task_is_rejected_by_this_mvp() -> None:
    plan = parse_plan(structured=plan_payload(read_only=True), text=None)
    with pytest.raises(PlanValidationError, match="read-only"):
        validate_plan(plan, budget=Budget(), allowed_programs=PROGRAMS)


def test_a_task_without_a_scope_is_rejected() -> None:
    plan = parse_plan(structured=plan_payload(allowed=()), text=None)
    with pytest.raises(PlanValidationError, match="empty allowed_paths"):
        validate_plan(plan, budget=Budget(), allowed_programs=PROGRAMS)


def test_a_task_that_would_write_science_is_rejected() -> None:
    plan = parse_plan(
        structured=plan_payload(allowed=(".research/claims",)),
        text=None,
    )
    with pytest.raises(PlanValidationError, match=r"\.research/"):
        validate_plan(plan, budget=Budget(), allowed_programs=PROGRAMS)


def test_a_task_without_acceptance_commands_is_rejected() -> None:
    payload = plan_payload()
    payload["tasks"][0]["acceptance_commands"] = []
    plan = parse_plan(structured=payload, text=None)
    with pytest.raises(PlanValidationError, match="no acceptance command"):
        validate_plan(plan, budget=Budget(), allowed_programs=PROGRAMS)


@pytest.mark.parametrize(
    "argv",
    [
        ("curl", "https://example.invalid"),
        ("bash", "-c", "rm -rf /"),
        ("sudo", "apt", "install", "anything"),
        ("/bin/sh", "-c", "echo hi"),
    ],
)
def test_commands_outside_the_allowlist_are_rejected(argv: tuple[str, ...]) -> None:
    plan = parse_plan(structured=plan_payload(argv=argv), text=None)
    with pytest.raises(PlanValidationError):
        validate_plan(plan, budget=Budget(), allowed_programs=PROGRAMS)


def test_admitted_plans_become_work_orders() -> None:
    plan = parse_plan(structured=plan_payload(), text=None)
    validate_plan(plan, budget=Budget(), allowed_programs=PROGRAMS)
    orders = plan_to_work_orders(
        plan,
        project_path=Path("/tmp/project"),
        base_commit=COMMIT,
        coder=fake_config().role("coder"),
        timeout_seconds=120,
    )

    assert len(orders) == 1
    order = orders[0]
    assert order.task_id == "T-001"
    assert order.role is Role.CODER
    assert order.risk_class is RiskClass.WRITE_ISOLATED
    assert order.read_only is False
    assert order.allowed_paths == ["adder.py"]
    assert order.forbidden_paths == ["test_adder.py"]
    assert order.provider == "fake"
    assert order.timeout_seconds == 120
    assert order.base_commit == COMMIT


def test_an_unsafe_scope_path_cannot_become_a_work_order() -> None:
    plan = parse_plan(structured=plan_payload(allowed=("../elsewhere",)), text=None)
    with pytest.raises(PlanValidationError, match="not a valid work order"):
        plan_to_work_orders(
            plan,
            project_path=Path("/tmp/project"),
            base_commit=COMMIT,
            coder=fake_config().role("coder"),
            timeout_seconds=120,
        )


def test_the_planner_prompt_states_the_enforced_limits() -> None:
    prompt = build_planner_prompt(
        goal="implement add",
        context_text="# context",
        budget=Budget(max_work_orders=3, max_write_work_orders=1),
        allowed_programs=PROGRAMS,
        project_path=Path("/tmp/project"),
    )

    assert "implement add" in prompt
    assert "At most 3 tasks" in prompt
    assert "at most\n  1 of them may write" in prompt
    assert "git, pytest, python, python3, ruff, uv" in prompt
    assert ".research/" in prompt
    assert "# context" in prompt
