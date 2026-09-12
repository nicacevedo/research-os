"""Unified research orchestration: what it dispatches, what it refuses, when it stops.

These tests drive the real controller against real repositories and real
delegated runs. The model is the only thing faked, so what is exercised is the
actual dispatch table, the actual budgets, the actual checkpoint machinery, and
the actual handoff to the automation, proposal, experiment, and paper layers.

Four properties are the reason this layer exists, and each has tests that fail
loudly if it regresses:

- a kind with no handler is refused before anything runs;
- a plan naming an undeclared experiment command is refused at planning time;
- a run with no authority to spend compute resolves the command and stops;
- a checkpoint stops the run until a person answers, and answering "stop" ends it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_os.errors import (
    BudgetExceededError,
    ProviderInvocationError,
    ResearchPlanError,
    ResearchRunNotFoundError,
    ResearchStateError,
    ResearchStoreError,
)
from research_os.research.controller import MINIMUM_CALLS, ready_blockers
from research_os.research.models import (
    RESUMABLE_STATES,
    TERMINAL_STATES,
    ResearchBudget,
    ResearchRun,
    ResearchState,
    ResearchTask,
    TaskKind,
    TaskStatus,
    allowed_transitions,
)
from research_os.research.planner import (
    ResearchPlan,
    parse_research_plan,
    to_tasks,
    validate_research_plan,
)
from research_os.research.report import render_run, render_run_list, render_status
from research_os.research.store import ResearchStore, make_research_run_id
from tests.fake_providers import ScriptedResponse
from tests.research_helpers import (
    PROJECT_ID,
    analysis_task,
    checkpoint_task,
    code_task,
    experiment_config,
    experiment_task,
    fit_command,
    init_repo,
    make_controller,
    plan_payload,
    scripted,
    task,
)


@pytest.fixture
def research_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every Research OS directory into ``tmp_path``.

    A research run writes runtime state, creates worktrees, reads the project
    registry, and opens the literature store, so a test that relocated only some
    of those would touch the researcher's real machine.
    """

    root = tmp_path / "xdg"
    mapping = {
        "RESEARCH_OS_CONFIG_HOME": root / "config",
        "RESEARCH_OS_DATA_HOME": root / "data",
        "RESEARCH_OS_CACHE_HOME": root / "cache",
        "RESEARCH_OS_STATE_HOME": root / "state",
    }
    for name, path in mapping.items():
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    return mapping["RESEARCH_OS_STATE_HOME"]


def start(
    tmp_path: Path,
    *,
    plan: dict[str, Any] | None = None,
    provider: Any = None,
    experiments: Any = None,
    budget: ResearchBudget | None = None,
    execute_experiments: bool = False,
    goal: str = "Find out whether widget deformation stays linear above 10N.",
) -> tuple[Any, ResearchStore, ResearchRun, Path]:
    repo = init_repo(tmp_path / "project")
    fake = provider if provider is not None else scripted(plan=plan)
    controller = make_controller(fake, experiments=experiments)
    store, run = controller.start(
        project_path=repo,
        goal=goal,
        budget=budget or ResearchBudget(),
        execute_experiments=execute_experiments,
    )
    return controller, store, run, repo


# -- the model ---------------------------------------------------------------


def test_a_task_kind_without_a_handler_cannot_be_dispatched() -> None:
    """Every kind the enum admits has a handler and a declared minimum cost.

    This is the fail-closed property stated as a test rather than as a comment:
    adding a ``TaskKind`` without wiring it up breaks here, not in a run.
    """

    from research_os.research.controller import ResearchController
    from research_os.research.report import ARTIFACT_HOMES

    controller = ResearchController(providers={}, config=None)  # type: ignore[arg-type]
    assert set(controller.dispatch_table()) == set(TaskKind)
    assert set(MINIMUM_CALLS) == set(TaskKind)
    assert set(ARTIFACT_HOMES) <= set(TaskKind)


def test_a_plan_must_be_a_forward_only_graph() -> None:
    with pytest.raises(ValueError, match="forward-only graph"):
        ResearchRun(
            run_id="RR-20260912T101500Z-0a1b2c3d",
            project_path="/tmp/project",
            goal="anything",
            tasks=[
                ResearchTask(
                    task_id="T-001",
                    kind=TaskKind.LITERATURE,
                    title="first",
                    goal="first",
                    query="q",
                    depends_on=["T-002"],
                ),
                ResearchTask(
                    task_id="T-002",
                    kind=TaskKind.LITERATURE,
                    title="second",
                    goal="second",
                    query="q",
                ),
            ],
        )


def test_task_ids_must_be_sequential() -> None:
    with pytest.raises(ValueError, match="sequential"):
        ResearchRun(
            run_id="RR-20260912T101500Z-0a1b2c3d",
            project_path="/tmp/project",
            goal="anything",
            tasks=[
                ResearchTask(
                    task_id="T-002",
                    kind=TaskKind.LITERATURE,
                    title="only",
                    goal="only",
                    query="q",
                )
            ],
        )


@pytest.mark.parametrize(
    ("kind", "fields", "message"),
    [
        (TaskKind.LITERATURE, {}, "no query"),
        (TaskKind.ANALYSIS, {}, "no read_paths"),
        (TaskKind.CODE, {"allowed_paths": ["src"]}, "no acceptance command"),
        (TaskKind.CODE, {"acceptance_commands": [["pytest"]]}, "no allowed_paths"),
        (TaskKind.EXPERIMENT, {}, "names no declared"),
        (TaskKind.PAPER, {"allowed_paths": ["paper"]}, "no section"),
        (TaskKind.HUMAN_CHECKPOINT, {}, "asks the human nothing"),
    ],
)
def test_a_task_missing_what_its_kind_needs_is_refused(
    kind: TaskKind, fields: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ResearchTask(task_id="T-001", kind=kind, title="t", goal="g", **fields)


def test_no_task_may_write_under_dot_research() -> None:
    with pytest.raises(ValueError, match=r"\.research/"):
        ResearchTask(
            task_id="T-001",
            kind=TaskKind.PAPER,
            title="t",
            goal="g",
            section="results",
            allowed_paths=[".research/claims"],
        )


def test_a_run_may_be_resumed_only_from_unambiguous_states() -> None:
    assert RESUMABLE_STATES == {
        ResearchState.PLAN_READY,
        ResearchState.WAITING_FOR_HUMAN,
    }
    for state in TERMINAL_STATES:
        assert allowed_transitions(state) == frozenset()


def test_the_repair_ceiling_is_enforced_by_the_field() -> None:
    """No configuration can turn the single bounded repair into a loop."""

    with pytest.raises(ValueError):
        ResearchBudget(max_repair_attempts=2)


# -- the planner -------------------------------------------------------------


def test_a_plan_naming_an_undeclared_experiment_is_refused() -> None:
    plan = ResearchPlan.model_validate(
        plan_payload(tasks=[experiment_task(experiment_task="rm-everything")])
    )
    with pytest.raises(ResearchPlanError, match="has not declared"):
        validate_research_plan(
            plan,
            budget=ResearchBudget(),
            declared_experiments=frozenset({"fit-model"}),
        )


def test_a_declared_experiment_is_accepted() -> None:
    plan = ResearchPlan.model_validate(plan_payload(tasks=[experiment_task()]))
    validate_research_plan(
        plan, budget=ResearchBudget(), declared_experiments=frozenset({"fit-model"})
    )


def test_a_plan_over_its_task_budget_is_refused() -> None:
    tasks = [task(task_id=f"T-{index:03d}") for index in range(1, 5)]
    plan = ResearchPlan.model_validate(plan_payload(tasks=tasks))
    with pytest.raises(ResearchPlanError, match="budget allows 3"):
        validate_research_plan(
            plan,
            budget=ResearchBudget(max_tasks=3),
            declared_experiments=frozenset(),
        )


def test_a_plan_over_its_write_budget_is_refused() -> None:
    plan = ResearchPlan.model_validate(
        plan_payload(
            tasks=[code_task(), code_task(task_id="T-002")],
        )
    )
    with pytest.raises(ResearchPlanError, match="writing tasks"):
        validate_research_plan(
            plan,
            budget=ResearchBudget(max_write_tasks=1),
            declared_experiments=frozenset(),
        )


def test_a_planner_that_returns_no_json_is_a_plan_error() -> None:
    with pytest.raises(ResearchPlanError, match="no JSON object"):
        parse_research_plan(structured=None, text="I would rather describe my plan.")


def test_a_plan_becomes_tasks_with_their_bounds_fixed() -> None:
    plan = ResearchPlan.model_validate(plan_payload(tasks=[code_task()]))
    tasks = to_tasks(plan)
    assert tasks[0].allowed_paths == ["adder.py"]
    assert tasks[0].acceptance_commands == [["pytest", "-q"]]
    assert tasks[0].status is TaskStatus.PENDING


# -- the store ---------------------------------------------------------------


def test_a_run_id_is_determined_by_its_inputs() -> None:
    first = make_research_run_id(
        project_path="/p", goal="g", created_at="2026-09-12T10:15:00Z"
    )
    second = make_research_run_id(
        project_path="/p", goal="g", created_at="2026-09-12T10:15:00Z"
    )
    assert (
        first == second == "RR-20260912T101500Z" + first[len("RR-20260912T101500Z") :]
    )
    assert first.startswith("RR-20260912T101500Z-")


def test_the_store_refuses_a_path_outside_the_run_directory(
    research_home: Path, tmp_path: Path
) -> None:
    _, store, _, _ = start(tmp_path)
    with pytest.raises(ResearchStoreError, match="escapes"):
        store.path("..", "elsewhere")


def test_opening_an_unknown_run_says_so(research_home: Path) -> None:
    with pytest.raises(ResearchRunNotFoundError):
        ResearchStore.open("RR-20260912T101500Z-deadbeef")
    with pytest.raises(ResearchRunNotFoundError, match="not a Research OS"):
        ResearchStore.open("not-a-run-id")


# -- planning a real run -----------------------------------------------------


def test_start_plans_and_stops(research_home: Path, tmp_path: Path) -> None:
    _, store, run, _ = start(tmp_path)
    assert run.state is ResearchState.PLAN_READY
    assert [item.kind for item in run.tasks] == [TaskKind.LITERATURE]
    assert run.project_id == PROJECT_ID
    assert run.model_calls_used == 1
    assert store.read_json("plan/plan.json")["summary"]
    events = [record["event"] for record in store.iter_events()]
    assert events[:2] == ["run_created", "state_changed"]
    assert "plan_accepted" in events


def test_a_plan_the_budget_could_never_pay_for_is_refused(
    research_home: Path, tmp_path: Path
) -> None:
    """Refused at planning time, so the run has not spent a write task first."""

    plan = plan_payload(
        tasks=[code_task(), code_task(task_id="T-002"), code_task(task_id="T-003")]
    )
    with pytest.raises(ResearchPlanError, match="needs at least"):
        start(
            tmp_path,
            plan=plan,
            budget=ResearchBudget(max_model_calls=3, max_write_tasks=3),
        )


def test_a_failed_plan_leaves_a_failed_run_on_disk(
    research_home: Path, tmp_path: Path
) -> None:
    provider = scripted()
    provider.responses["planner"] = [
        ScriptedResponse(structured=None, text="no plan here")
    ]
    with pytest.raises(ResearchPlanError):
        start(tmp_path, provider=provider)
    run_id = ResearchStore.list_run_ids()[-1]
    run = ResearchStore.open(run_id).load()
    assert run.state is ResearchState.FAILED
    assert run.failure_reason


# -- dispatching -------------------------------------------------------------


def test_a_literature_task_runs_deterministically_and_costs_no_model_call(
    research_home: Path, tmp_path: Path
) -> None:
    """Offline with no enabled sources: the run still finishes and says so."""

    controller, store, run, _ = start(tmp_path)
    run = controller.execute(store)
    assert run.state is ResearchState.READY_FOR_HUMAN
    assert run.task("T-001").status is TaskStatus.DONE
    assert run.model_calls_used == 1
    assert "no provider" in run.task("T-001").detail


def test_an_analysis_task_is_delegated_to_the_automation_controller(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, run, _ = start(
        tmp_path, plan=plan_payload(tasks=[analysis_task()])
    )
    run = controller.execute(store)
    assert run.state is ResearchState.READY_FOR_HUMAN
    finished = run.task("T-001")
    assert finished.status is TaskStatus.DONE
    assert finished.artifact_id and finished.artifact_id.startswith("RUN-")
    assert finished.model_calls >= 1
    assert run.model_calls_used > 1


def test_a_code_task_gets_the_whole_automation_pipeline(
    research_home: Path, tmp_path: Path
) -> None:
    """The delegated run really writes, really checks, and really reviews."""

    controller, store, run, repo = start(
        tmp_path, plan=plan_payload(tasks=[code_task()])
    )
    run = controller.execute(store)
    assert run.state is ResearchState.READY_FOR_HUMAN
    finished = run.task("T-001")
    assert finished.status is TaskStatus.DONE
    assert run.write_tasks_used == 1

    from research_os.automation.store import RunStore

    inner = RunStore.open(finished.artifact_id or "").load()
    assert inner.state.value == "READY_FOR_HUMAN"
    order = inner.work_orders[0]
    assert order.branch
    # The canonical checkout was never written to.
    assert (
        (repo / "adder.py")
        .read_text(encoding="utf-8")
        .endswith("raise NotImplementedError\n")
    )


def test_a_plan_over_the_write_budget_never_starts(
    research_home: Path, tmp_path: Path
) -> None:
    """Refused at planning time, before a worktree exists."""

    with pytest.raises(ResearchPlanError, match="writing tasks"):
        start(
            tmp_path,
            plan=plan_payload(tasks=[code_task()]),
            budget=ResearchBudget(max_write_tasks=0),
        )


def test_a_code_task_beyond_the_write_budget_is_refused_before_it_writes(
    research_home: Path, tmp_path: Path
) -> None:
    """The runtime guard, exercised on a run that already spent its allowance.

    This is the resumed-run case: the plan was affordable when it was made, and
    the counter on disk says the allowance is gone. The guard runs before the
    worktree is created, so nothing is written and nothing is reverted.
    """

    controller, store, run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[code_task()]),
        budget=ResearchBudget(max_write_tasks=1),
    )
    store.save(run.model_copy(update={"write_tasks_used": 1}))
    with pytest.raises(BudgetExceededError, match="write task number 2"):
        controller.execute(store)
    run = store.load()
    assert run.state is ResearchState.FAILED
    assert run.task("T-001").status is TaskStatus.FAILED
    assert not list((tmp_path / "project").glob("**/adder.py.orig"))


def test_a_dependency_that_did_not_succeed_blocks_what_follows(
    research_home: Path, tmp_path: Path
) -> None:
    provider = scripted(
        plan=plan_payload(
            tasks=[analysis_task(), code_task(task_id="T-002", depends_on=["T-001"])]
        )
    )
    provider.responses["analyst"] = [
        ScriptedResponse(structured=None, text="", error="the analyst fell over")
    ]
    controller, store, _run, _ = start(tmp_path, provider=provider)
    with pytest.raises(ProviderInvocationError):
        controller.execute(store)
    run = store.load()
    assert run.state is ResearchState.FAILED
    assert run.task("T-001").status is TaskStatus.FAILED
    assert run.task("T-002").status is TaskStatus.PENDING


# -- experiments -------------------------------------------------------------


def test_an_unauthorised_experiment_resolves_its_command_and_stops(
    research_home: Path, tmp_path: Path
) -> None:
    """The most important refusal in this layer: no authority, no spending."""

    controller, store, run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[experiment_task()]),
        experiments=experiment_config(),
    )
    run = controller.execute(store)
    finished = run.task("T-001")
    assert finished.status is TaskStatus.SKIPPED
    assert "not authorised" in finished.detail
    assert "python3 fit.py --seed" in finished.detail
    assert run.experiments_used == 0
    assert run.state is ResearchState.READY_FOR_HUMAN


def test_an_authorised_experiment_actually_runs(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, _run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[experiment_task(experiment_parameters={"seed": "7"})]),
        experiments=experiment_config(),
        execute_experiments=True,
    )
    run = controller.execute(store)
    finished = run.task("T-001")
    assert finished.status is TaskStatus.DONE
    assert run.experiments_used == 1
    assert finished.artifact_id

    from research_os.experiment.store import ExperimentStore

    record = ExperimentStore.open(finished.artifact_id).load()
    assert record.state.value == "completed"


def test_an_experiment_the_project_never_declared_never_reaches_dispatch(
    research_home: Path, tmp_path: Path
) -> None:
    with pytest.raises(ResearchPlanError, match="has not declared"):
        start(
            tmp_path,
            plan=plan_payload(tasks=[experiment_task(experiment_task="wipe-disk")]),
            experiments=experiment_config(commands=[fit_command()]),
        )


def test_a_plan_over_the_experiment_budget_never_starts(
    research_home: Path, tmp_path: Path
) -> None:
    with pytest.raises(ResearchPlanError, match="experiment tasks"):
        start(
            tmp_path,
            plan=plan_payload(tasks=[experiment_task()]),
            experiments=experiment_config(),
            budget=ResearchBudget(max_experiments=0),
        )


def test_an_experiment_beyond_the_budget_is_refused_before_it_spends(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[experiment_task()]),
        experiments=experiment_config(),
        execute_experiments=True,
        budget=ResearchBudget(max_experiments=1),
    )
    store.save(run.model_copy(update={"experiments_used": 1}))
    with pytest.raises(BudgetExceededError, match="experiment number 2"):
        controller.execute(store)
    assert store.load().state is ResearchState.FAILED
    assert not (tmp_path / "project" / "results").exists()


# -- human checkpoints -------------------------------------------------------


def test_a_checkpoint_stops_the_run_until_a_person_answers(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, run, _ = start(
        tmp_path,
        plan=plan_payload(
            tasks=[checkpoint_task(), task(task_id="T-002", depends_on=["T-001"])]
        ),
    )
    run = controller.execute(store)
    assert run.state is ResearchState.WAITING_FOR_HUMAN
    assert run.task("T-001").status is TaskStatus.AWAITING_HUMAN
    assert run.task("T-002").status is TaskStatus.PENDING
    assert [item.task_id for item in run.pending_checkpoints] == ["T-001"]

    with pytest.raises(ResearchStateError, match="waiting for you to answer"):
        controller.execute(store)

    run = controller.answer(store, answer="Yes, go ahead.")
    assert run.task("T-001").status is TaskStatus.DONE
    run = controller.execute(store)
    assert run.state is ResearchState.READY_FOR_HUMAN
    assert run.task("T-002").status is TaskStatus.DONE


def test_declining_a_checkpoint_ends_the_run(
    research_home: Path, tmp_path: Path
) -> None:
    """A researcher who says no has decided something the run must honour."""

    controller, store, run, _ = start(
        tmp_path,
        plan=plan_payload(
            tasks=[
                checkpoint_task(),
                code_task(task_id="T-002", depends_on=["T-001"]),
            ]
        ),
    )
    run = controller.execute(store)
    assert run.state is ResearchState.WAITING_FOR_HUMAN
    run = controller.answer(store, answer="No, not on this budget.", proceed=False)
    assert run.state is ResearchState.CANCELLED
    assert run.task("T-002").status is TaskStatus.PENDING
    assert "declined to continue" in (run.failure_reason or "")
    with pytest.raises(ResearchStateError, match="already CANCELLED"):
        controller.execute(store)


def test_answering_when_nothing_is_pending_is_refused(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, _, _ = start(tmp_path)
    with pytest.raises(ResearchStateError, match="not waiting"):
        controller.answer(store, answer="hello")


# -- the readiness gate ------------------------------------------------------


def test_a_run_with_an_unfinished_task_is_not_ready(
    research_home: Path, tmp_path: Path
) -> None:
    _, _store, run, _ = start(tmp_path)
    assert any("not finished" in item for item in ready_blockers(run))


def test_a_run_with_an_unanswered_checkpoint_is_not_ready(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, _, _ = start(
        tmp_path, plan=plan_payload(tasks=[checkpoint_task()])
    )
    run = controller.execute(store)
    assert any("no answer" in item for item in ready_blockers(run))


def test_cancelling_a_run_is_terminal(research_home: Path, tmp_path: Path) -> None:
    controller, store, _, _ = start(tmp_path)
    run = controller.cancel(store, reason="changed my mind")
    assert run.state is ResearchState.CANCELLED
    with pytest.raises(ResearchStateError, match="already"):
        controller.cancel(store, reason="again")


# -- the report --------------------------------------------------------------


def test_the_report_says_what_was_spent_and_what_was_not_run(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[experiment_task()]),
        experiments=experiment_config(),
    )
    run = controller.execute(store)
    text = render_run(run, events=list(store.iter_events()))
    assert "model calls" in text
    assert "cluster submissions" in text
    assert "not authorised to spend compute" in text
    assert "python3 fit.py --seed" in text
    assert "candidates, and drafts are prose" in text


def test_the_status_view_names_what_the_run_is_waiting_on(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, _, _ = start(
        tmp_path, plan=plan_payload(tasks=[checkpoint_task()])
    )
    run = controller.execute(store)
    text = render_status(run)
    assert "WAITING_FOR_HUMAN" in text
    assert "Should I run the fit on the cluster?" in text


def test_the_report_escapes_control_characters_from_a_worker(
    research_home: Path, tmp_path: Path
) -> None:
    """A planner-supplied string cannot repaint a researcher's terminal."""

    hostile = plan_payload(
        tasks=[task(title="normal\x1b[31m red", goal="also \x07 noisy")],
        summary="summary \x1b]0;pwned\x07 here",
    )
    _, _, run, _ = start(tmp_path, plan=hostile)
    text = render_run(run)
    assert "\x1b" not in text
    assert "\x07" not in text
    assert "\\x1b" in text


def test_the_run_list_renders_when_empty_and_when_not(
    research_home: Path, tmp_path: Path
) -> None:
    assert "No research runs yet" in render_run_list([])
    _, _, run, _ = start(tmp_path)
    assert run.run_id in render_run_list([run])


# -- the remaining handlers --------------------------------------------------


def test_a_proposal_task_is_delegated_and_produces_a_candidate(
    research_home: Path, tmp_path: Path
) -> None:
    """The proposal is a suggestion. Nothing in the capsule changes."""

    from research_os.proposal.store import ProposalStore
    from tests.proposal_helpers import assessment_payload, proposal_payload
    from tests.research_helpers import proposal_task

    provider = scripted(plan=plan_payload(tasks=[proposal_task()]))
    provider.responses["planner"] = [
        ScriptedResponse(structured=plan_payload(tasks=[proposal_task()])),
        ScriptedResponse(structured=proposal_payload()),
    ]
    provider.responses["reviewer"] = [ScriptedResponse(structured=assessment_payload())]
    controller, store, _run, repo = start(tmp_path, provider=provider)
    before = sorted(
        item.relative_to(repo).as_posix() for item in (repo / ".research").rglob("*")
    )
    run = controller.execute(store)

    finished = run.task("T-001")
    assert finished.status is TaskStatus.DONE
    assert finished.artifact_id and finished.artifact_id.startswith("PROP-")
    assert "proposed item" in finished.detail
    assert ProposalStore.open(finished.artifact_id).load().items
    # The capsule is byte-for-byte what it was: a proposal is not science.
    after = sorted(
        item.relative_to(repo).as_posix() for item in (repo / ".research").rglob("*")
    )
    assert after == before


def test_a_paper_task_drafts_only_from_accepted_science(
    research_home: Path, tmp_path: Path
) -> None:
    from research_os.paper.store import DraftStore
    from tests.paper_helpers import (
        GROUNDED_RESULTS,
        init_paper_project,
        manifest_payload,
    )
    from tests.research_helpers import (
        fake_config,
        offline_literature,
        paper_task,
    )

    repo = init_paper_project(tmp_path / "paper-project")
    provider = scripted(plan=plan_payload(tasks=[paper_task()]))
    provider.responses["coder"] = [
        ScriptedResponse(
            structured=manifest_payload(),
            text="Drafted the results section.",
            write_files={"paper/manuscript.md": GROUNDED_RESULTS},
        )
    ]

    from research_os.research.controller import ResearchController

    controller = ResearchController(
        providers={provider.name: provider},
        config=fake_config(),
        literature_config=offline_literature(),
    )
    store, run = controller.start(
        project_path=repo, goal="Write up the linearity result."
    )
    run = controller.execute(store)

    finished = run.task("T-001")
    assert finished.status is TaskStatus.DONE
    assert finished.artifact_id and finished.artifact_id.startswith("DRAFT-")
    assert run.write_tasks_used == 1
    draft = DraftStore.open(finished.artifact_id).load()
    assert draft.manifest.claim_ids == ["CLAIM-0001"]
    # The manuscript was drafted in an isolated worktree, not in the checkout.
    assert "(to be written)" in (repo / "paper" / "manuscript.md").read_text(
        encoding="utf-8"
    )


def test_a_cluster_submission_beyond_the_budget_is_refused_before_sbatch(
    research_home: Path, tmp_path: Path
) -> None:
    """The refusal has to land before the job exists, not after.

    A budget that reports an overspend once ``sbatch`` has already returned a
    job id is a report, not a limit. The command is resolved first so its
    executor is known, and a cluster task with no submissions left never reaches
    the scheduler at all.
    """

    controller, store, run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[experiment_task()]),
        experiments=experiment_config(commands=[fit_command(executor="slurm")]),
        execute_experiments=True,
        budget=ResearchBudget(max_experiments=4, max_cluster_submissions=1),
    )
    store.save(run.model_copy(update={"cluster_submissions_used": 1}))
    with pytest.raises(BudgetExceededError, match="Nothing was submitted."):
        controller.execute(store)
    assert store.load().state is ResearchState.FAILED
    events = [record["event"] for record in store.iter_events()]
    assert "experiment_executed" not in events
