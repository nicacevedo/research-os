"""End-to-end controller tests driven entirely by fake providers.

Nothing here requires a real agent CLI. The Git repositories, worktrees,
subprocesses, and run directories are real, so what is exercised is the actual
control flow, isolation, and gating.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest

from research_os.automation.config import DEFAULT_ALLOWED_CHECK_PROGRAMS
from research_os.automation.controller import (
    AutomationController,
    ready_for_human_blockers,
    unresolved_findings,
)
from research_os.automation.gitutil import branch_exists, porcelain_status
from research_os.automation.models import (
    AcceptanceCommand,
    AutomationRun,
    Budget,
    Independence,
    ReviewVerdict,
    Role,
    RunState,
    WorkOrderStatus,
)
from research_os.automation.store import RunStore, worktrees_root
from research_os.automation.worktree import worktree_path
from research_os.capsule import validate_project
from research_os.errors import (
    AutomationError,
    BudgetExceededError,
    PlanValidationError,
    PreflightError,
    ProviderInvocationError,
    ProviderUnavailableError,
)
from tests.automation_helpers import (
    BROKEN_MODULE,
    FIXED_MODULE,
    commit_all,
    fake_config,
    head,
    init_repo,
    make_controller,
    plan_payload,
    review_payload,
)
from tests.fake_providers import FakeProvider, ScriptedResponse, UnavailableProvider
from tests.fs_helpers import make_git_repo, snapshot_files, write_reviewable_capsule

TINY_TEST = "from adder import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def working_snapshot(repo: Path) -> dict[str, str]:
    """Snapshot the working tree, skipping Git's own binary metadata."""

    return {
        path.relative_to(repo).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


class Started(NamedTuple):
    """One started run plus everything a test needs to inspect it."""

    controller: AutomationController
    provider: FakeProvider
    repo: Path
    run: AutomationRun
    store: RunStore


def scripted(
    *,
    plan: dict | None = None,
    coder: ScriptedResponse | None = None,
    review: dict | None = None,
    name: str = "fake",
    family: str = "fake-family",
) -> FakeProvider:
    """Return a fake provider scripted for a whole successful run."""

    return FakeProvider(
        name=name,
        family=family,
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=plan or plan_payload())],
            str(Role.CODER): [
                coder
                or ScriptedResponse(
                    text="Implemented add in adder.py.",
                    write_files={"adder.py": FIXED_MODULE},
                )
            ],
            str(Role.REVIEWER): [
                ScriptedResponse(structured=review or review_payload())
            ],
        },
    )


def start_run(
    tmp_path: Path,
    *,
    provider: FakeProvider | None = None,
    providers: dict | None = None,
    config: object | None = None,
    goal: str = "Implement the missing function so the supplied tests pass.",
    budget: Budget | None = None,
    dry_run: bool = False,
    skip_planner: bool = False,
) -> Started:
    repo = init_repo(tmp_path / "project")
    fake = provider or scripted()
    controller = make_controller(providers or {"fake": fake}, config=config)  # type: ignore[arg-type]
    store, run = controller.start(
        project_path=repo,
        goal=goal,
        budget=budget,
        dry_run=dry_run,
        skip_planner=skip_planner,
    )
    return Started(controller, fake, repo, run, store)


def science_repo(tmp_path: Path) -> Path:
    """A capsule project that also holds a small software task."""

    repo = make_git_repo(tmp_path / "science")
    write_reviewable_capsule(repo)
    (repo / "adder.py").write_text(BROKEN_MODULE, encoding="utf-8")
    (repo / "test_adder.py").write_text(TINY_TEST, encoding="utf-8")
    commit_all(repo)
    return repo


# -- the happy path ---------------------------------------------------------


def test_a_successful_run_reaches_ready_for_human(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    assert ctx.run.state is RunState.PLAN_READY

    final = ctx.controller.execute(ctx.store)

    assert final.state is RunState.READY_FOR_HUMAN
    assert ready_for_human_blockers(final) == []
    order = final.work_orders[0]
    assert order.status is WorkOrderStatus.REVIEWED
    assert order.changed_paths == ["adder.py"]
    assert order.required_checks_passed is True
    assert final.reviews[0].verdict is ReviewVerdict.PASS
    assert final.model_calls_used == 3
    assert final.finished_at is not None


def test_the_project_worktree_is_never_modified(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    before = working_snapshot(ctx.repo)
    base = head(ctx.repo)

    ctx.controller.execute(ctx.store)

    assert (ctx.repo / "adder.py").read_text(encoding="utf-8") == BROKEN_MODULE
    assert porcelain_status(ctx.repo) == ()
    assert head(ctx.repo) == base
    assert working_snapshot(ctx.repo) == before


def test_the_change_lands_on_its_own_branch_in_its_own_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    final = ctx.controller.execute(ctx.store)
    order = final.work_orders[0]

    tree = Path(order.worktree_path or "")
    assert tree == worktree_path(final.run_id, "T-001")
    assert worktrees_root() in tree.parents
    assert (tree / "adder.py").read_text(encoding="utf-8") == FIXED_MODULE
    assert branch_exists(ctx.repo, order.branch or "")
    assert order.diff_path is not None
    diff = ctx.store.path(*order.diff_path.split("/")).read_text(encoding="utf-8")
    assert "return left + right" in diff


def test_the_write_worker_only_ever_runs_in_its_own_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    final = ctx.controller.execute(ctx.store)

    coder_calls = ctx.provider.requests_for(Role.CODER)
    assert len(coder_calls) == 1
    call = coder_calls[0]
    assert call.read_only is False
    assert call.cwd == worktree_path(final.run_id, "T-001")
    assert call.cwd != ctx.repo
    assert ctx.repo not in call.cwd.parents

    for invocation in final.invocations:
        if not invocation.read_only:
            assert Path(invocation.cwd) != ctx.repo


def test_the_planner_and_reviewer_get_no_tools_at_all(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    ctx.controller.execute(ctx.store)

    for role in (Role.PLANNER, Role.REVIEWER):
        calls = ctx.provider.requests_for(role)
        assert calls, f"{role} was never invoked"
        for call in calls:
            assert call.read_only is True
            assert call.tools == ()
            assert call.json_schema is not None


def test_the_reviewer_sees_the_diff_and_the_observed_exit_codes(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    ctx.controller.execute(ctx.store)
    prompt = ctx.provider.requests_for(Role.REVIEWER)[0].prompt

    assert "return left + right" in prompt
    assert "pytest -q" in prompt
    assert "exit_code: 0" in prompt
    assert "an unverified claim, not evidence" in prompt


def test_every_meaningful_action_is_in_the_ledger(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    ctx.controller.execute(ctx.store)
    events = list(ctx.store.iter_events())
    kinds = [item["event"] for item in events]

    for expected in (
        "run_created",
        "independence_assessed",
        "state_changed",
        "context_built",
        "provider_invoked",
        "plan_accepted",
        "worktree_created",
        "execution_observed",
        "command_executed",
        "review_recorded",
        "ready_for_human",
    ):
        assert expected in kinds, expected
    assert [item["seq"] for item in events] == list(range(1, len(events) + 1))

    invoked = [item for item in events if item["event"] == "provider_invoked"]
    assert [item["role"] for item in invoked] == ["planner", "coder", "reviewer"]
    assert invoked[-1]["model_calls_used"] == 3
    assert invoked[-1]["model_call_budget"] == Budget().max_model_calls

    transitions = [item["state"] for item in events if item["event"] == "state_changed"]
    assert transitions == [
        "PREFLIGHTED",
        "PLANNING",
        "PLAN_READY",
        "EXECUTING",
        "CHECKING",
        "REVIEWING",
        "READY_FOR_HUMAN",
    ]


def test_prompts_and_model_output_are_archived(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    final = ctx.controller.execute(ctx.store)
    directory = ctx.store.directory

    assert (directory / "context" / "context.json").is_file()
    assert (directory / "context" / "context.md").is_file()
    assert (directory / "plan" / "plan.json").is_file()
    assert (directory / "reviews" / "T-001.json").is_file()
    assert (directory / "checks" / "T-001" / "results.json").is_file()
    assert (directory / "model_outputs" / "INV-0001.json").is_file()
    for invocation in final.invocations:
        assert invocation.prompt_path is not None
        assert ctx.store.path(*invocation.prompt_path.split("/")).is_file()
        assert invocation.raw_output_path is not None
        assert ctx.store.path(*invocation.raw_output_path.split("/")).is_file()


def test_reported_usage_is_recorded_and_absent_usage_stays_unknown(
    automation_home: Path, tmp_path: Path
) -> None:
    provider = scripted()
    provider.responses[str(Role.REVIEWER)] = [
        ScriptedResponse(
            structured=review_payload(),
            total_cost_usd=None,
            input_tokens=None,
            output_tokens=None,
        )
    ]
    ctx = start_run(tmp_path, provider=provider)
    final = ctx.controller.execute(ctx.store)

    reviewer = next(item for item in final.invocations if item.role is Role.REVIEWER)
    assert reviewer.total_cost_usd is None
    assert reviewer.input_tokens is None
    planner = next(item for item in final.invocations if item.role is Role.PLANNER)
    assert planner.total_cost_usd == pytest.approx(0.01)
    assert final.total_cost_usd() == pytest.approx(0.02)


# -- failure paths ----------------------------------------------------------


def test_failing_acceptance_checks_stop_the_run(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=ScriptedResponse(
                text="I edited the file.",
                write_files={"adder.py": "def add(left, right):\n    return 0\n"},
            )
        ),
    )

    with pytest.raises(AutomationError, match="required acceptance commands failed"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.work_orders[0].status is WorkOrderStatus.CHECKS_FAILED
    assert ready_for_human_blockers(final) != []
    assert final.reviews == []
    assert ctx.provider.requests_for(Role.REVIEWER) == []


def test_a_failed_run_can_never_become_ready(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=ScriptedResponse(
                text="broken",
                write_files={"adder.py": "def add(a, b):\n    return 0\n"},
            )
        ),
    )
    with pytest.raises(AutomationError):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    with pytest.raises(AutomationError, match="already FAILED"):
        ctx.controller.execute(ctx.store)
    assert "already failed" in " ".join(ready_for_human_blockers(final))


def test_a_reviewer_fail_stops_the_run(automation_home: Path, tmp_path: Path) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            review=review_payload(
                "FAIL",
                findings=[
                    {
                        "severity": "blocker",
                        "message": "the implementation special-cases the tests",
                        "path": "adder.py",
                    }
                ],
            )
        ),
    )

    with pytest.raises(AutomationError, match="review returned FAIL"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.reviews[0].verdict is ReviewVerdict.FAIL
    assert final.work_orders[0].status is WorkOrderStatus.FAILED
    assert "FAIL" in " ".join(ready_for_human_blockers(final))


def test_pass_with_repair_is_ready_but_keeps_its_findings(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            review=review_payload(
                "PASS_WITH_REPAIR",
                findings=[
                    {
                        "severity": "minor",
                        "message": "no type hints",
                        "path": "adder.py",
                    }
                ],
            )
        ),
    )
    final = ctx.controller.execute(ctx.store)

    assert final.state is RunState.READY_FOR_HUMAN
    assert final.reviews[0].verdict is ReviewVerdict.PASS_WITH_REPAIR
    assert unresolved_findings(final) == [("T-001", "minor", "no type hints")]


def test_changes_outside_the_scope_fail_the_task(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=ScriptedResponse(
                text="I also relaxed the tests.",
                write_files={
                    "adder.py": FIXED_MODULE,
                    "test_adder.py": "def test_nothing():\n    assert True\n",
                },
            )
        ),
    )

    with pytest.raises(AutomationError, match="outside its scope"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.work_orders[0].status is WorkOrderStatus.FAILED
    assert "test_adder.py" in (final.work_orders[0].failure_reason or "")
    assert ctx.provider.requests_for(Role.REVIEWER) == []


def test_a_worker_that_changes_nothing_fails_the_task(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path, provider=scripted(coder=ScriptedResponse(text="Looks fine to me."))
    )

    with pytest.raises(AutomationError, match="produced no changes"):
        ctx.controller.execute(ctx.store)

    assert ctx.store.load().state is RunState.FAILED


def test_a_worker_timeout_fails_the_task(automation_home: Path, tmp_path: Path) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            coder=ScriptedResponse(
                timed_out=True,
                exit_code=None,
                error="provider timed out after 1800s",
            )
        ),
    )

    with pytest.raises(ProviderInvocationError, match="timed out"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.invocations[-1].timed_out is True
    assert final.work_orders[0].status is WorkOrderStatus.FAILED


def test_a_failed_worktree_is_left_in_place_for_inspection(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path, provider=scripted(coder=ScriptedResponse(text="nothing")))
    with pytest.raises(AutomationError):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert len(final.worktrees) == 1
    assert Path(final.worktrees[0].path).is_dir()
    assert final.worktrees[0].removed_at is None


def test_malformed_planner_output_fails_the_run_before_any_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    provider = FakeProvider(
        responses={str(Role.PLANNER): [ScriptedResponse(text="Sure, I'd start by...")]}
    )
    controller = make_controller({"fake": provider})

    with pytest.raises(PlanValidationError):
        controller.start(project_path=repo, goal="do something")

    run = RunStore.open(RunStore.list_run_ids()[0]).load()
    assert run.state is RunState.FAILED
    assert run.work_orders == []
    assert run.worktrees == []
    assert not worktrees_root().exists()


def test_a_plan_that_would_write_science_is_refused(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    controller = make_controller(
        {"fake": scripted(plan=plan_payload(allowed=(".research/claims",)))}
    )

    with pytest.raises(PlanValidationError, match=r"\.research/"):
        controller.start(project_path=repo, goal="edit the science")


def test_an_unparseable_review_fails_the_run(
    automation_home: Path, tmp_path: Path
) -> None:
    provider = scripted()
    provider.responses[str(Role.REVIEWER)] = [ScriptedResponse(text="looks good to me")]
    ctx = start_run(tmp_path, provider=provider)

    with pytest.raises(ProviderInvocationError, match="no JSON object"):
        ctx.controller.execute(ctx.store)

    assert ctx.store.load().state is RunState.FAILED


def test_an_unknown_review_verdict_fails_the_run(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        provider=scripted(
            review={"verdict": "LOOKS_FINE", "summary": "ok", "findings": []}
        ),
    )

    with pytest.raises(ProviderInvocationError, match="unknown verdict"):
        ctx.controller.execute(ctx.store)

    assert ctx.store.load().state is RunState.FAILED


# -- budgets ----------------------------------------------------------------


def test_a_budget_that_cannot_cover_the_plan_is_refused_up_front(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path, budget=Budget(max_model_calls=2))

    with pytest.raises(BudgetExceededError, match="cannot cover this plan"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.worktrees == []
    assert ctx.provider.requests_for(Role.CODER) == []


def test_an_exhausted_model_call_budget_refuses_the_next_call(
    automation_home: Path, tmp_path: Path
) -> None:
    """The budget is re-checked immediately before every invocation.

    The budget is tightened after planning rather than started at one call,
    because a plan needing two calls inside a one-call budget is now refused as
    impossible before it is ever dispatched. What is under test here is the
    per-invocation guard, not plan validation, so the run is taken to an
    exhausted budget the same way the wall-clock case below is.
    """

    ctx = start_run(tmp_path, budget=Budget(max_model_calls=2))

    assert ctx.run.model_calls_used == 1
    spent = ctx.store.load().model_copy(update={"budget": Budget(max_model_calls=1)})
    with pytest.raises(BudgetExceededError, match="model-call budget exhausted"):
        ctx.controller.assert_budget(spent)


def test_an_exhausted_wall_clock_budget_refuses_the_next_call(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    stale = ctx.run.model_copy(
        update={
            "created_at": "2020-01-01T00:00:00Z",
            "budget": Budget(max_wall_clock_seconds=60),
        }
    )

    with pytest.raises(BudgetExceededError, match="wall-clock"):
        ctx.controller.assert_budget(stale)


def test_the_configured_budget_applies_when_no_override_is_given(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path, config=fake_config(budget=Budget(max_model_calls=3)))

    assert ctx.run.model_calls_used == 1
    assert ctx.run.budget.max_model_calls == 3


# -- preflight and providers ------------------------------------------------


def test_a_dirty_project_cannot_start_a_run(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    (repo / "adder.py").write_text("# edited\n", encoding="utf-8")
    controller = make_controller({"fake": scripted()})

    with pytest.raises(PreflightError, match="uncommitted changes"):
        controller.start(project_path=repo, goal="implement add")


def test_a_repository_without_commits_cannot_start_a_run(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = make_git_repo(tmp_path / "empty")
    controller = make_controller({"fake": scripted()})

    with pytest.raises(PreflightError, match="no commits"):
        controller.start(project_path=repo, goal="implement add")


def test_a_directory_outside_git_cannot_start_a_run(
    automation_home: Path, tmp_path: Path
) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    controller = make_controller({"fake": scripted()})

    with pytest.raises(PreflightError, match="not inside a Git repository"):
        controller.start(project_path=plain, goal="implement add")


def test_an_empty_goal_is_refused(automation_home: Path, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "project")
    controller = make_controller({"fake": scripted()})

    with pytest.raises(PreflightError, match="at least one character"):
        controller.start(project_path=repo, goal="   ")


def test_no_available_provider_stops_before_any_run_directory(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    controller = make_controller(
        {"absent": UnavailableProvider()},
        config=fake_config(planner="absent", coder="absent", reviewer="absent"),
    )

    with pytest.raises(ProviderUnavailableError, match="no locally verified"):
        controller.start(project_path=repo, goal="implement add")

    assert RunStore.list_run_ids() == ()


def test_an_unavailable_configured_provider_is_substituted(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        providers={"fake": scripted(), "absent": UnavailableProvider()},
        config=fake_config(planner="absent"),
    )

    assert ctx.run.roles["planner"].provider == "fake"
    events = [
        item
        for item in ctx.store.iter_events()
        if item["event"] == "provider_substituted"
    ]
    assert any("absent is unavailable" in item["detail"] for item in events)


def test_one_provider_family_is_reported_as_degraded_independence(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)

    assert ctx.run.independence is Independence.DEGRADED_SAME_PROVIDER_FAMILY
    assert "NOT an independent review" in (ctx.run.independence_note or "")


def test_a_second_family_is_preferred_for_the_reviewer(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        providers={
            "fake": scripted(),
            "other": scripted(name="other", family="other-family"),
        },
    )

    assert ctx.run.roles["reviewer"].provider == "other"
    assert ctx.run.independence is Independence.INDEPENDENT_PROVIDER_FAMILY
    assert "different model family" in (ctx.run.independence_note or "")


def test_an_explicit_reviewer_choice_is_respected(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(
        tmp_path,
        providers={
            "fake": scripted(),
            "other": scripted(name="other", family="other-family"),
        },
        config=fake_config(explicit_roles=frozenset({"reviewer"})),
    )

    assert ctx.run.roles["reviewer"].provider == "fake"
    assert ctx.run.independence is Independence.DEGRADED_SAME_PROVIDER_FAMILY


def test_the_same_model_for_both_roles_is_the_weakest_independence(
    automation_home: Path, tmp_path: Path
) -> None:
    config = fake_config()
    config.roles["reviewer"] = config.roles["reviewer"].model_copy(
        update={"model": config.roles["coder"].model}
    )
    ctx = start_run(tmp_path, config=config)

    assert ctx.run.independence is Independence.DEGRADED_SAME_MODEL


# -- dry run, cancel, cleanup ----------------------------------------------


def test_a_dry_run_plans_but_never_dispatches(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path, dry_run=True)
    before = working_snapshot(ctx.repo)

    assert ctx.run.dry_run is True
    assert ctx.run.state is RunState.PLAN_READY
    assert ctx.run.work_orders[0].task_id == "T-001"
    assert ctx.run.worktrees == []
    assert ctx.provider.requests_for(Role.CODER) == []
    assert working_snapshot(ctx.repo) == before
    assert porcelain_status(ctx.repo) == ()

    with pytest.raises(AutomationError, match="--dry-run"):
        ctx.controller.execute(ctx.store)
    assert not worktrees_root().exists()


def test_skipping_the_planner_spends_nothing(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path, skip_planner=True)

    assert ctx.run.state is RunState.PREFLIGHTED
    assert ctx.run.model_calls_used == 0
    assert ctx.provider.calls == []
    assert (ctx.store.directory / "context" / "context.json").is_file()
    with pytest.raises(AutomationError, match="only a PLAN_READY run"):
        ctx.controller.execute(ctx.store)


def test_a_run_can_be_cancelled(automation_home: Path, tmp_path: Path) -> None:
    ctx = start_run(tmp_path)
    cancelled = ctx.controller.cancel(ctx.store, reason="changed my mind")

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.failure_reason == "changed my mind"
    with pytest.raises(AutomationError, match="already CANCELLED"):
        ctx.controller.cancel(ctx.store, reason="again")
    with pytest.raises(AutomationError, match="already CANCELLED"):
        ctx.controller.execute(ctx.store)


def test_cleanup_removes_worktrees_and_keeps_branches(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    final = ctx.controller.execute(ctx.store)
    order = final.work_orders[0]

    cleaned, removed = ctx.controller.cleanup(ctx.store)

    assert removed == (order.worktree_path,)
    assert not Path(order.worktree_path or "").exists()
    assert branch_exists(ctx.repo, order.branch or "")
    assert cleaned.worktrees[0].removed_at is not None
    assert cleaned.state is RunState.READY_FOR_HUMAN
    assert ctx.store.run_file.is_file()
    assert list(ctx.store.iter_events())


def test_cleanup_is_idempotent(automation_home: Path, tmp_path: Path) -> None:
    ctx = start_run(tmp_path)
    ctx.controller.execute(ctx.store)
    ctx.controller.cleanup(ctx.store)
    _, removed = ctx.controller.cleanup(ctx.store)

    assert removed == ()


# -- scientific state is untouched -----------------------------------------


def test_a_run_on_a_capsule_project_mutates_no_science(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = science_repo(tmp_path)
    before_capsule = snapshot_files(repo / ".research")
    before_report = validate_project(repo)
    controller = make_controller({"fake": scripted()})
    store, run = controller.start(project_path=repo, goal="implement add")
    final = controller.execute(store)

    after_report = validate_project(repo)
    assert final.state is RunState.READY_FOR_HUMAN
    assert before_report.project is not None
    assert run.project_id == before_report.project.id
    assert snapshot_files(repo / ".research") == before_capsule
    assert after_report.ok == before_report.ok
    assert after_report.findings == before_report.findings

    claims = [item for item in after_report.objects if str(item.type) == "claim"]
    assert [str(item.status) for item in claims] == ["evidence_linked"]


def test_the_worktree_of_a_capsule_project_keeps_science_out_of_scope(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = science_repo(tmp_path)
    before_capsule = snapshot_files(repo / ".research")
    controller = make_controller(
        {
            "fake": scripted(
                coder=ScriptedResponse(
                    text="I edited a claim as well.",
                    write_files={
                        "adder.py": FIXED_MODULE,
                        ".research/claims/CLAIM-0001.yaml": "id: CLAIM-0001\n",
                    },
                )
            )
        }
    )
    store, _ = controller.start(project_path=repo, goal="implement add")

    with pytest.raises(AutomationError, match="outside its scope"):
        controller.execute(store)

    final = store.load()
    assert final.state is RunState.FAILED
    assert ".research/claims/CLAIM-0001.yaml" in (
        final.work_orders[0].failure_reason or ""
    )
    assert snapshot_files(repo / ".research") == before_capsule


def test_the_controller_never_records_a_scientific_review(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    final = ctx.controller.execute(ctx.store)
    payload = final.reviews[0].model_dump()

    assert payload["verdict"] in {"PASS", "PASS_WITH_REPAIR", "FAIL"}
    assert "reviewer_kind" not in payload
    assert "subject_digest" not in payload
    assert "evidence_digests" not in payload


def test_the_allowlist_comes_from_configuration(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    config = fake_config()
    narrowed = type(config)(
        roles=config.roles,
        budget=config.budget,
        allowed_check_programs=("ruff",),
        source=None,
        explicit_roles=frozenset(),
    )
    controller = make_controller({"fake": scripted()}, config=narrowed)

    with pytest.raises(PlanValidationError, match="allowed check programs"):
        controller.start(project_path=repo, goal="implement add")

    assert DEFAULT_ALLOWED_CHECK_PROGRAMS != ("ruff",)


# -- audit regression: outbound symlink under write scope --------------------


def symlink_escape_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repository whose in-scope path is a symlink to a file outside it.

    This is the shape the security audit exercised: the link is committed, so it
    arrives in every worktree created from the base commit, and the file it
    points at belongs to nobody in the run.
    """

    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("canonical content\n", encoding="utf-8")

    repo = init_repo(tmp_path / "project")
    (repo / "src").mkdir()
    (repo / "src" / "real.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "src" / "notes.txt").symlink_to(target)
    commit_all(repo, "add an outbound symlink")
    return repo, target


def test_an_outbound_symlink_stops_the_run_before_the_writer_is_invoked(
    automation_home: Path, tmp_path: Path
) -> None:
    """The audit's escape: an in-scope path that is a link out of the worktree.

    A writer told it may change ``src/`` would write straight through the link.
    The file outside changes, the link does not, so ``git diff`` reports a clean
    in-scope run and scope enforcement -- which reads Git -- sees nothing.
    """

    repo, target = symlink_escape_repo(tmp_path)
    before = target.read_bytes()
    provider = scripted(
        plan=plan_payload(allowed=("src",), argv=("pytest", "-q")),
        coder=ScriptedResponse(
            text="updated the notes",
            write_files={"src/notes.txt": "written through the symlink\n"},
        ),
    )
    controller = make_controller({"fake": provider})
    store, _ = controller.start(project_path=repo, goal="update the notes")

    with pytest.raises(AutomationError, match="resolving outside"):
        controller.execute(store)

    assert provider.requests_for(Role.CODER) == [], "the writer was invoked"
    assert target.read_bytes() == before, "the file outside the worktree changed"

    final = store.load()
    assert final.state is RunState.FAILED
    assert final.state is not RunState.READY_FOR_HUMAN
    assert final.order("T-001").status is WorkOrderStatus.FAILED
    assert "src/notes.txt" in (final.order("T-001").failure_reason or "")


def test_the_outbound_symlink_refusal_is_in_the_ledger(
    automation_home: Path, tmp_path: Path
) -> None:
    repo, _ = symlink_escape_repo(tmp_path)
    controller = make_controller(
        {"fake": scripted(plan=plan_payload(allowed=("src",), argv=("pytest", "-q")))}
    )
    store, _ = controller.start(project_path=repo, goal="update the notes")

    with pytest.raises(AutomationError):
        controller.execute(store)

    events = [record["event"] for record in store.iter_events()]
    assert "symlink_scope_violation" in events
    violation = next(
        record
        for record in store.iter_events()
        if record["event"] == "symlink_scope_violation"
    )
    assert violation["task_id"] == "T-001"
    assert violation["paths"] == ["src/notes.txt"]
    assert violation["stage"] == "before the writer was invoked"


def test_a_symlink_the_worker_creates_is_caught_at_the_evidence_stage(
    automation_home: Path, tmp_path: Path
) -> None:
    """The second gate: the worktree was clean when the writer started.

    The pre-invocation check cannot see a link that does not exist yet, so
    evidence collection repeats it before scope enforcement is allowed to pass.
    """

    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("canonical content\n", encoding="utf-8")

    repo = init_repo(tmp_path / "project")
    provider = scripted(
        coder=ScriptedResponse(
            text="implemented add",
            write_files={"adder.py": FIXED_MODULE},
            create_symlinks={"escape.txt": str(target)},
        )
    )
    controller = make_controller({"fake": provider})
    store, _ = controller.start(project_path=repo, goal="implement add")

    with pytest.raises(AutomationError, match="resolving outside"):
        controller.execute(store)

    assert provider.requests_for(Role.CODER), "this gate runs after the writer"
    final = store.load()
    assert final.state is RunState.FAILED
    assert final.order("T-001").status is WorkOrderStatus.FAILED

    violation = next(
        record
        for record in store.iter_events()
        if record["event"] == "symlink_scope_violation"
    )
    assert violation["stage"] == "while collecting execution evidence"
    assert violation["paths"] == ["escape.txt"]


def test_a_symlink_that_stays_inside_the_worktree_does_not_block_a_run(
    automation_home: Path, tmp_path: Path
) -> None:
    """Containment, not a ban on symlinks."""

    repo = init_repo(tmp_path / "project")
    # Relative, so it still points inside whichever worktree it is checked out
    # into. An absolute link back at the canonical checkout would be an escape.
    (repo / "alias.py").symlink_to("adder.py")
    commit_all(repo, "add an internal symlink")

    controller = make_controller({"fake": scripted()})
    store, _ = controller.start(project_path=repo, goal="implement add")
    final = controller.execute(store)

    assert final.state is RunState.READY_FOR_HUMAN


def test_an_ordinary_allowed_path_still_works(
    automation_home: Path, tmp_path: Path
) -> None:
    """The repair must not have made the normal case stricter."""

    ctx = start_run(tmp_path)
    final = ctx.controller.execute(ctx.store)

    assert final.state is RunState.READY_FOR_HUMAN
    assert final.order("T-001").changed_paths == ["adder.py"]


# -- audit regression: read-only workers run outside the canonical repository -


def test_read_only_workers_do_not_run_in_the_canonical_repository(
    automation_home: Path, tmp_path: Path
) -> None:
    """A worker with no tools also has no reason to stand in the researcher's tree.

    The planner and reviewer receive everything they reason about in the context
    packet, so the canonical path appears to them as data. Keeping the process
    out of the checkout means a later change to their tool set cannot silently
    inherit that position.
    """

    ctx = start_run(tmp_path)
    final = ctx.controller.execute(ctx.store)
    canonical = ctx.repo.resolve()

    for role in (Role.PLANNER, Role.REVIEWER):
        calls = ctx.provider.requests_for(role)
        assert calls, f"{role} was never invoked"
        for call in calls:
            cwd = Path(call.cwd).resolve()
            assert cwd != canonical
            assert canonical not in cwd.parents
            assert cwd == ctx.store.path("context").resolve()

    for invocation in final.invocations:
        recorded = Path(invocation.cwd).resolve()
        if invocation.read_only:
            assert recorded != canonical
            assert canonical not in recorded.parents


def test_the_recorded_cwd_says_where_each_worker_actually_ran(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start_run(tmp_path)
    final = ctx.controller.execute(ctx.store)

    by_role = {item.role: Path(item.cwd) for item in final.invocations}
    assert by_role[Role.PLANNER] == ctx.store.path("context")
    assert by_role[Role.REVIEWER] == ctx.store.path("context")
    assert by_role[Role.CODER] == worktree_path(final.run_id, "T-001")


def test_a_read_only_worker_pointed_at_the_repository_is_refused(
    automation_home: Path, tmp_path: Path
) -> None:
    """The invariant is enforced in code, not merely arranged by the caller."""

    ctx = start_run(tmp_path, skip_planner=True)
    setting = ctx.run.roles["planner"]

    with pytest.raises(AutomationError, match="inside the canonical repository"):
        ctx.controller._invoke(
            ctx.store,
            ctx.run,
            role=Role.PLANNER,
            setting=setting,
            prompt="plan",
            cwd=ctx.repo,
            timeout_seconds=60,
        )

    assert ctx.provider.requests_for(Role.PLANNER) == []


def test_a_refused_acceptance_command_is_never_executed(
    automation_home: Path, tmp_path: Path
) -> None:
    """Execution-time fail-closed, independent of plan validation.

    Plan validation already refuses these, so this asserts the second gate: an
    order carrying an unauthorised command fails its checks without the command
    reaching the operating system.
    """

    ctx = start_run(tmp_path)
    run = ctx.store.load()
    order = run.order("T-001")
    smuggled = order.model_copy(
        update={
            "acceptance_commands": [
                AcceptanceCommand(argv=["python", "-c", "print(1)"])
            ]
        }
    )
    ctx.store.save(run.model_copy(update={"work_orders": [smuggled]}))

    with pytest.raises(AutomationError, match="not authorised"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.order("T-001").status is WorkOrderStatus.CHECKS_FAILED
    executed = [
        record["command"]
        for record in ctx.store.iter_events()
        if record["event"] == "command_executed"
    ]
    assert executed == []
    assert any(
        record["event"] == "command_refused" for record in ctx.store.iter_events()
    )
