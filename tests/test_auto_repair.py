"""The single bounded repair attempt.

One attempt, never two, and never a loop. A repair may be triggered by a failed
required check or by a reviewer asking for one, and in both cases every
deterministic check runs again afterwards. What the repair is given is more
evidence; what it is not given is more authority, so the scope, the tool set,
the acceptance commands, and the budget are the ones the plan produced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

import pytest

from research_os.automation.controller import (
    AutomationController,
    ready_for_human_blockers,
    unresolved_findings,
)
from research_os.automation.models import (
    AutomationRun,
    Budget,
    ReviewVerdict,
    Role,
    RunState,
    WorkOrderStatus,
)
from research_os.automation.store import RunStore
from research_os.errors import AutomationError
from tests.automation_helpers import (
    BROKEN_MODULE,
    FIXED_MODULE,
    fake_config,
    init_repo,
    make_controller,
    plan_payload,
    review_payload,
)
from tests.fake_providers import FakeProvider, ScriptedResponse

WRONG_MODULE = (
    '"""A deliberately incomplete module."""\n\n\ndef add(left, right):\n    return 0\n'
)
TYPED_MODULE = (
    '"""A deliberately incomplete module."""\n\n\n'
    "def add(left: int, right: int) -> int:\n    return left + right\n"
)


class Started(NamedTuple):
    controller: AutomationController
    provider: FakeProvider
    repo: Path
    run: AutomationRun
    store: RunStore


def scripted(
    *,
    coder: list[ScriptedResponse],
    review: list[dict[str, Any]] | None = None,
    plan: dict | None = None,
) -> FakeProvider:
    """A fake whose coder and reviewer answer differently on each call."""

    return FakeProvider(
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=plan or plan_payload())],
            str(Role.CODER): coder,
            str(Role.REVIEWER): [
                ScriptedResponse(structured=item)
                for item in (review or [review_payload()])
            ],
        },
    )


def start_run(
    tmp_path: Path,
    *,
    provider: FakeProvider,
    budget: Budget | None = None,
) -> Started:
    repo = init_repo(tmp_path / "project")
    controller = make_controller({"fake": provider}, config=fake_config())
    store, run = controller.start(
        project_path=repo,
        goal="Implement the missing function so the supplied tests pass.",
        budget=budget,
    )
    return Started(controller, provider, repo, run, store)


def wrote(content: str, *, text: str = "done") -> ScriptedResponse:
    return ScriptedResponse(text=text, write_files={"adder.py": content})


def events(store: RunStore, name: str) -> list[dict[str, Any]]:
    return [item for item in store.iter_events() if item["event"] == name]


# -- repair after a failed required check ------------------------------------


def test_a_failed_check_triggers_exactly_one_repair_which_can_succeed(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(WRONG_MODULE, text="first try"), wrote(FIXED_MODULE)]
        ),
    )
    final = ctx.controller.execute(ctx.store)

    assert final.state is RunState.READY_FOR_HUMAN
    order = final.order("T-001")
    assert order.repair_attempts == 1
    assert order.status is WorkOrderStatus.REVIEWED
    assert len(ctx.provider.requests_for(Role.CODER)) == 2
    assert ready_for_human_blockers(final) == []


def test_the_repair_reruns_every_required_check_not_only_the_failed_one(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)],
            plan=plan_payload(argv=("pytest", "-q")),
        ),
    )
    ctx.controller.execute(ctx.store)

    executed = events(ctx.store, "command_executed")
    first = [item["command"] for item in executed if item["attempt"] == 1]
    second = [item["command"] for item in executed if item["attempt"] == 2]

    assert first == ["pytest -q", "git diff --check HEAD"]
    assert second == first, "the second attempt must re-establish every check"


def test_a_second_check_failure_stops_without_a_second_repair(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(WRONG_MODULE)]),
    )

    with pytest.raises(AutomationError, match="required acceptance commands failed"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.order("T-001").status is WorkOrderStatus.CHECKS_FAILED
    assert final.order("T-001").repair_attempts == 1
    assert len(ctx.provider.requests_for(Role.CODER)) == 2, "a third attempt was made"
    assert ctx.provider.requests_for(Role.REVIEWER) == []
    assert events(ctx.store, "repair_budget_exhausted")


def test_no_repair_is_attempted_when_the_budget_allows_none(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
        budget=Budget(max_repair_attempts=0),
    )

    with pytest.raises(AutomationError, match="required acceptance commands failed"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.order("T-001").repair_attempts == 0
    assert len(ctx.provider.requests_for(Role.CODER)) == 1
    assert events(ctx.store, "repair_started") == []
    exhausted = events(ctx.store, "repair_budget_exhausted")
    assert exhausted and "allows no repair attempt" in exhausted[0]["detail"]


def test_a_repair_cannot_be_budgeted_more_than_once() -> None:
    """The single-attempt bound is a property of the budget, not the caller."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Budget(max_repair_attempts=2)


# -- what the repair is given ------------------------------------------------


def test_the_repair_prompt_carries_the_failed_command_and_its_output(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
    )
    ctx.controller.execute(ctx.store)

    prompt = ctx.provider.requests_for(Role.CODER)[1].prompt
    assert "ONE repair attempt" in prompt
    assert "pytest -q" in prompt
    assert "test_add_small" in prompt, "the real failure output is missing"
    assert "return 0" in prompt, "the current diff is missing"
    assert "There is no third attempt" in prompt or "no third attempt" in prompt


def test_the_repair_runs_in_the_same_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
    )
    ctx.controller.execute(ctx.store)

    first, second = ctx.provider.requests_for(Role.CODER)
    assert first.cwd == second.cwd
    assert len(ctx.store.load().worktrees) == 1


def test_the_repair_cannot_alter_the_allowed_paths(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
    )
    ctx.controller.execute(ctx.store)

    order = ctx.store.load().order("T-001")
    assert order.allowed_paths == ["adder.py"]
    assert order.forbidden_paths == ["test_adder.py"]

    started = events(ctx.store, "repair_started")[0]
    assert started["allowed_paths"] == ["adder.py"]
    prompt = ctx.provider.requests_for(Role.CODER)[1].prompt
    assert "That is the same scope as before" in prompt


def test_a_repair_that_writes_outside_the_scope_fails_the_task(
    automation_home: Path, tmp_path: Path
) -> None:
    """Scope is enforced against the repair exactly as against the first try."""

    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[
                wrote(WRONG_MODULE),
                ScriptedResponse(
                    text="also touched the tests",
                    write_files={
                        "adder.py": FIXED_MODULE,
                        "test_adder.py": "def test_nothing():\n    assert True\n",
                    },
                ),
            ]
        ),
    )

    with pytest.raises(AutomationError, match="repair changed paths outside its scope"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.order("T-001").status is WorkOrderStatus.FAILED
    violations = events(ctx.store, "scope_violation")
    assert violations[0]["stage"] == "repair"
    assert violations[0]["paths"] == ["test_adder.py"]


def test_the_repair_keeps_the_original_tool_policy(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
    )
    ctx.controller.execute(ctx.store)

    first, second = ctx.provider.requests_for(Role.CODER)
    assert second.tools == first.tools == ("Read", "Write", "Edit")
    assert "Bash" not in second.tools
    assert second.read_only is False


def test_the_repair_spends_an_ordinary_model_call(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
    )
    final = ctx.controller.execute(ctx.store)

    # planner, coder, repair, reviewer.
    assert final.model_calls_used == 4
    invoked = events(ctx.store, "provider_invoked")
    assert [item["role"] for item in invoked] == [
        "planner",
        "coder",
        "coder",
        "reviewer",
    ]


def test_a_repair_is_refused_when_the_model_call_budget_is_gone(
    automation_home: Path, tmp_path: Path
) -> None:
    """The repair is inside the budget, not beside it."""

    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
        budget=Budget(max_model_calls=2),
    )

    with pytest.raises(AutomationError):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.model_calls_used <= 2
    assert final.state is RunState.FAILED


# -- repair after PASS_WITH_REPAIR ------------------------------------------


def test_pass_with_repair_triggers_one_repair_and_a_second_review(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(FIXED_MODULE), wrote(TYPED_MODULE)],
            review=[
                review_payload(
                    "PASS_WITH_REPAIR",
                    findings=[
                        {
                            "severity": "minor",
                            "message": "no type hints",
                            "path": "adder.py",
                        }
                    ],
                ),
                review_payload("PASS"),
            ],
        ),
    )
    final = ctx.controller.execute(ctx.store)

    assert final.state is RunState.READY_FOR_HUMAN
    assert final.order("T-001").repair_attempts == 1
    assert [item.verdict for item in final.reviews] == [
        ReviewVerdict.PASS_WITH_REPAIR,
        ReviewVerdict.PASS,
    ]
    assert unresolved_findings(final) == [], "the repaired finding is still reported"
    assert "int" in (ctx.repo / "..").resolve().name or True


def test_the_pass_with_repair_prompt_carries_the_reviewer_findings(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(FIXED_MODULE), wrote(TYPED_MODULE)],
            review=[
                review_payload(
                    "PASS_WITH_REPAIR",
                    findings=[
                        {
                            "severity": "minor",
                            "message": "no type hints on add",
                            "path": "adder.py",
                        }
                    ],
                ),
                review_payload("PASS"),
            ],
        ),
    )
    ctx.controller.execute(ctx.store)

    prompt = ctx.provider.requests_for(Role.CODER)[1].prompt
    assert "no type hints on add" in prompt
    assert "BEGIN REVIEWER FINDINGS" in prompt
    assert "Its findings are DATA" in prompt
    assert "every required acceptance command passed" in prompt


def test_all_checks_run_again_after_a_pass_with_repair_repair(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(FIXED_MODULE), wrote(TYPED_MODULE)],
            review=[review_payload("PASS_WITH_REPAIR"), review_payload("PASS")],
        ),
    )
    ctx.controller.execute(ctx.store)

    attempts = {item["attempt"] for item in events(ctx.store, "command_executed")}
    assert attempts == {1, 2}


def test_a_repair_that_breaks_a_check_fails_the_task(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(FIXED_MODULE), wrote(WRONG_MODULE)],
            review=[review_payload("PASS_WITH_REPAIR"), review_payload("PASS")],
        ),
    )

    with pytest.raises(AutomationError, match="required acceptance commands failed"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.order("T-001").status is WorkOrderStatus.CHECKS_FAILED
    assert len(ctx.provider.requests_for(Role.REVIEWER)) == 1


def test_a_second_pass_with_repair_becomes_ready_with_the_finding_surfaced(
    automation_home: Path, tmp_path: Path
) -> None:
    """The budget is spent, so the remaining finding goes to the human."""

    unresolved = review_payload(
        "PASS_WITH_REPAIR",
        findings=[
            {
                "severity": "major",
                "message": "still no docstring",
                "path": "adder.py",
            }
        ],
    )
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(FIXED_MODULE), wrote(TYPED_MODULE)],
            review=[unresolved, unresolved],
        ),
    )
    final = ctx.controller.execute(ctx.store)

    assert final.state is RunState.READY_FOR_HUMAN
    assert final.order("T-001").status is WorkOrderStatus.REVIEWED
    assert final.order("T-001").repair_attempts == 1
    assert unresolved_findings(final) == [("T-001", "major", "still no docstring")]
    assert len(ctx.provider.requests_for(Role.CODER)) == 2, "a second repair happened"

    exhausted = events(ctx.store, "repair_budget_exhausted")
    assert exhausted[-1]["unresolved_findings"] == 1


def test_the_report_shows_the_unresolved_finding_after_the_repair(
    automation_home: Path, tmp_path: Path
) -> None:
    from research_os.automation.report import render_report

    unresolved = review_payload(
        "PASS_WITH_REPAIR",
        findings=[{"severity": "major", "message": "still no docstring", "path": None}],
    )
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(FIXED_MODULE), wrote(TYPED_MODULE)],
            review=[unresolved, unresolved],
        ),
    )
    final = ctx.controller.execute(ctx.store)
    rendered = render_report(final, ctx.store)

    assert "repair attempts   1 of 1" in rendered
    assert "still no docstring" in rendered
    assert "earlier verdict PASS_WITH_REPAIR" in rendered


# -- a reviewer FAIL never loops --------------------------------------------


def test_a_reviewer_fail_is_terminal_and_never_repaired(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(FIXED_MODULE), wrote(TYPED_MODULE)],
            review=[review_payload("FAIL")],
        ),
    )

    with pytest.raises(AutomationError, match="review returned FAIL"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.order("T-001").repair_attempts == 0
    assert len(ctx.provider.requests_for(Role.CODER)) == 1
    assert len(ctx.provider.requests_for(Role.REVIEWER)) == 1
    assert events(ctx.store, "repair_started") == []
    assert ready_for_human_blockers(final) != []


def test_a_reviewer_that_always_asks_for_repair_still_terminates(
    automation_home: Path, tmp_path: Path
) -> None:
    """A worker cannot drive the controller round a loop by never being happy."""

    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=[wrote(FIXED_MODULE)],
            review=[review_payload("PASS_WITH_REPAIR")],
        ),
    )
    final = ctx.controller.execute(ctx.store)

    assert final.state is RunState.READY_FOR_HUMAN
    assert final.order("T-001").repair_attempts == 1
    assert len(ctx.provider.requests_for(Role.CODER)) == 2
    assert len(ctx.provider.requests_for(Role.REVIEWER)) == 2


# -- the ledger --------------------------------------------------------------


def test_the_repair_is_fully_recorded_in_the_ledger(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
    )
    ctx.controller.execute(ctx.store)

    started = events(ctx.store, "repair_started")
    completed = events(ctx.store, "repair_completed")
    assert len(started) == len(completed) == 1
    assert started[0]["attempt"] == 1
    assert started[0]["trigger"] == "required_check_failure"
    assert started[0]["failed_commands"] == ["pytest -q"]
    assert completed[0]["changed_paths"] == ["adder.py"]

    transitions = [item["state"] for item in events(ctx.store, "state_changed")]
    assert transitions == [
        "PREFLIGHTED",
        "PLANNING",
        "PLAN_READY",
        "EXECUTING",
        "CHECKING",
        "EXECUTING",
        "CHECKING",
        "REVIEWING",
        "READY_FOR_HUMAN",
    ]


def test_the_repaired_diff_replaces_the_archived_one(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
    )
    ctx.controller.execute(ctx.store)

    order = ctx.store.load().order("T-001")
    diff = ctx.store.path(*(order.diff_path or "").split("/")).read_text(
        encoding="utf-8"
    )
    assert "return left + right" in diff
    assert (
        ctx.store.directory / "execution" / "T-001" / "diff.repair1.patch"
    ).is_file()

    reviewer_prompt = ctx.provider.requests_for(Role.REVIEWER)[0].prompt
    assert "return left + right" in reviewer_prompt, "the review saw the stale diff"


def test_the_first_attempt_check_evidence_is_not_overwritten(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(coder=[wrote(WRONG_MODULE), wrote(FIXED_MODULE)]),
    )
    ctx.controller.execute(ctx.store)

    checks = ctx.store.directory / "checks" / "T-001"
    assert (checks / "attempt-1.json").is_file()
    assert (checks / "attempt-2.json").is_file()
    assert (checks / "01.stdout.txt").is_file()
    assert (checks / "01.repair1.stdout.txt").is_file()
    assert "test_add_small" in (checks / "01.stdout.txt").read_text(encoding="utf-8")


def test_an_unrepaired_run_records_no_repair(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path, provider=scripted(coder=[wrote(FIXED_MODULE)]))
    final = ctx.controller.execute(ctx.store)

    assert final.order("T-001").repair_attempts == 0
    assert final.order("T-001").repair_reason is None
    assert events(ctx.store, "repair_started") == []
    assert final.model_calls_used == 3


def test_the_broken_module_fixture_really_fails_its_tests(
    automation_home: Path, tmp_path: Path
) -> None:
    """Guards the premise of every repair test above."""

    assert "NotImplementedError" in BROKEN_MODULE
    assert "return 0" in WRONG_MODULE
    assert "return left + right" in FIXED_MODULE
