"""Tests for scope enforcement and the worker prompts."""

from __future__ import annotations

import pytest

from research_os.automation.executor import build_coder_prompt, scope_violations
from research_os.automation.models import (
    AcceptanceCommand,
    CommandResult,
    Independence,
    ReviewVerdict,
    utc_now,
)
from research_os.automation.reviewer import build_reviewer_prompt, parse_review
from research_os.errors import ProviderInvocationError
from tests.test_auto_models import make_order


def test_a_file_inside_the_scope_is_allowed() -> None:
    assert scope_violations(("adder.py",), allowed=("adder.py",), forbidden=()) == ()


def test_a_directory_scope_covers_its_contents() -> None:
    assert (
        scope_violations(("src/pkg/module.py",), allowed=("src",), forbidden=()) == ()
    )
    assert (
        scope_violations(("src/pkg/module.py",), allowed=("src/pkg/",), forbidden=())
        == ()
    )


def test_a_sibling_with_a_shared_prefix_is_not_covered() -> None:
    assert scope_violations(
        ("source_other.py",), allowed=("source",), forbidden=()
    ) == ("source_other.py",)


def test_a_path_outside_the_scope_is_a_violation() -> None:
    assert scope_violations(
        ("adder.py", "test_adder.py"), allowed=("adder.py",), forbidden=()
    ) == ("test_adder.py",)


def test_a_forbidden_path_wins_over_an_allowed_one() -> None:
    assert scope_violations(
        ("src/secret.py",), allowed=("src",), forbidden=("src/secret.py",)
    ) == ("src/secret.py",)


@pytest.mark.parametrize(
    "path",
    [".research", ".research/claims/CLAIM-0001.yaml", ".research/project.yaml"],
)
def test_science_is_out_of_scope_even_when_a_plan_allows_it(path: str) -> None:
    assert scope_violations((path,), allowed=(".research",), forbidden=()) == (path,)


def test_the_coder_prompt_states_the_enforced_scope() -> None:
    order = make_order(
        allowed_paths=["adder.py"],
        forbidden_paths=["test_adder.py"],
        acceptance_commands=[
            AcceptanceCommand(argv=["pytest", "-q"], description="tests pass")
        ],
        expected_artifacts=["adder.py"],
    )
    prompt = build_coder_prompt(order, context_text="# context")

    assert "isolated Git worktree" in prompt
    assert "adder.py" in prompt
    assert "test_adder.py" in prompt
    assert "pytest -q" in prompt
    assert "Do not create a commit" in prompt
    assert '".research/"' in prompt
    assert "# context" in prompt


def test_the_reviewer_prompt_freezes_the_observed_evidence() -> None:
    now = utc_now()
    order = make_order(changed_paths=["adder.py"])
    checks = [
        CommandResult(
            argv=["pytest", "-q"],
            cwd="/tmp/worktree",
            required=True,
            exit_code=0,
            timed_out=False,
            timeout_seconds=60,
            started_at=now,
            ended_at=now,
            duration_ms=12,
        )
    ]
    prompt = build_reviewer_prompt(
        order,
        diff="--- a/adder.py\n+++ b/adder.py\n",
        check_results=checks,
        worker_report="I implemented it.",
        context_text="# context",
    )

    assert "You cannot change code" in prompt
    assert "already executed by the controller itself" in prompt
    assert "exit_code: 0" in prompt
    assert "an unverified claim, not evidence" in prompt
    assert "PASS_WITH_REPAIR" in prompt
    assert order.base_commit in prompt


def test_a_structured_verdict_is_parsed() -> None:
    outcome = parse_review(
        structured={
            "verdict": "PASS_WITH_REPAIR",
            "summary": "works, but",
            "findings": [
                {"severity": "minor", "message": "no docstring", "path": "adder.py"},
                {"severity": "note", "message": "   ", "path": None},
            ],
        },
        text=None,
        task_id="T-001",
        provider="fake",
        model="fake-reviewer",
        independence=Independence.DEGRADED_SAME_PROVIDER_FAMILY,
        independence_note="one family only",
        invocation_id="INV-0003",
    )

    assert outcome.verdict is ReviewVerdict.PASS_WITH_REPAIR
    assert [item.message for item in outcome.findings] == ["no docstring"]
    assert outcome.independence is Independence.DEGRADED_SAME_PROVIDER_FAMILY


def test_a_missing_summary_is_recorded_rather_than_invented() -> None:
    outcome = parse_review(
        structured={"verdict": "PASS", "summary": "", "findings": []},
        text=None,
        task_id="T-001",
        provider="fake",
        model=None,
        independence=Independence.DEGRADED_SAME_MODEL,
        independence_note="same model",
        invocation_id="INV-0003",
    )

    assert outcome.summary == "(the reviewer returned no summary)"


@pytest.mark.parametrize(
    "payload",
    [
        {"verdict": "APPROVED", "summary": "s", "findings": []},
        {"summary": "s", "findings": []},
        {"verdict": "approve", "summary": "s", "findings": []},
    ],
)
def test_an_unknown_verdict_is_refused(payload: dict) -> None:
    with pytest.raises(ProviderInvocationError, match="unknown verdict"):
        parse_review(
            structured=payload,
            text=None,
            task_id="T-001",
            provider="fake",
            model=None,
            independence=Independence.DEGRADED_SAME_MODEL,
            independence_note="n",
            invocation_id="INV-0003",
        )
