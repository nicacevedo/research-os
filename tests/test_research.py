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

from research_os.automation.models import Role
from research_os.automation.planner import (
    PlanDocument,
    PlannedRole,
)
from research_os.automation.planner import PlannedTask as AutomationPlannedTask
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
    PLAN_SCHEMA,
    PlannedTask,
    ResearchPlan,
    _is_placeholder,
    assert_plan_says_something,
    build_research_plan_prompt,
    parse_research_plan,
    to_tasks,
    validate_research_plan,
)
from research_os.research.report import render_run, render_run_list, render_status
from research_os.research.store import ResearchStore, make_research_run_id
from tests.fake_providers import FakeProvider, ScriptedResponse
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
        (TaskKind.CODE, {"allowed_paths": ["src"]}, "nothing to verify it"),
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
        ResearchState.INTERRUPTED,
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


def test_a_plan_omitting_a_required_experiment_parameter_is_refused() -> None:
    """Found by a live pilot, at the worst possible moment to find it.

    Checking that the command *name* was declared and not that its required
    parameters were supplied left the check half-done. A synthetic full-workflow
    run named a declared command, omitted its required seed, and only discovered
    it at the moment of spending: three model calls and one human checkpoint
    after the plan arrived, on something decidable the instant it did.
    """

    plan = ResearchPlan.model_validate(
        plan_payload(tasks=[experiment_task(experiment_parameters={})])
    )
    with pytest.raises(ResearchPlanError, match="without the parameter"):
        validate_research_plan(
            plan,
            budget=ResearchBudget(),
            declared_experiments=frozenset({"fit-model"}),
            required_parameters={"fit-model": frozenset({"seed"})},
        )


def test_a_plan_supplying_every_required_experiment_parameter_is_accepted() -> None:
    plan = ResearchPlan.model_validate(plan_payload(tasks=[experiment_task()]))

    validate_research_plan(
        plan,
        budget=ResearchBudget(),
        declared_experiments=frozenset({"fit-model"}),
        required_parameters={"fit-model": frozenset({"seed"})},
    )


def test_an_optional_experiment_parameter_need_not_be_supplied() -> None:
    """Only required parameters are required. The rule may not invent strictness."""

    plan = ResearchPlan.model_validate(
        plan_payload(tasks=[experiment_task(experiment_parameters={"seed": "7"})])
    )

    validate_research_plan(
        plan,
        budget=ResearchBudget(),
        declared_experiments=frozenset({"fit-model"}),
        required_parameters={"fit-model": frozenset({"seed"})},
    )


def test_a_missing_required_parameter_is_correctable_by_the_bounded_re_ask(
    research_home: Path, tmp_path: Path
) -> None:
    """The point of refusing at plan time: the existing correction can fix it."""

    from tests.research_helpers import make_controller

    provider = planner_scripted(
        [
            ScriptedResponse(
                structured=plan_payload(
                    tasks=[experiment_task(experiment_parameters={})]
                )
            ),
            ScriptedResponse(structured=plan_payload(tasks=[analysis_task()])),
        ]
    )
    from research_os.experiment.config import (
        ExecutionLimits,
        ExperimentConfig,
        ProjectExperiments,
        SlurmSettings,
    )
    from research_os.experiment.spec import CommandSpec, ParameterSpec

    spec = CommandSpec(
        name="fit-model",
        description="Fit the model.",
        argv=["python3", "fit.py", "--seed", "{seed}"],
        parameters=[ParameterSpec(name="seed", type="integer", required=True)],
        outputs=["results/fit.json"],
        timeout_seconds=60,
    )
    controller = make_controller(
        provider,
        experiments=ExperimentConfig(
            slurm=SlurmSettings(),
            limits=ExecutionLimits(),
            projects={"widget": ProjectExperiments(commands={"fit-model": spec})},
            source=None,
        ),
    )
    repo = init_repo(tmp_path / "project")
    store, run = controller.start(
        project_path=repo,
        goal="Find out whether widget deformation stays linear above 10N.",
        budget=ResearchBudget(),
    )

    events = [record.get("event") for record in store.iter_events()]
    assert "plan_rejected" in events
    assert "plan_correction_started" in events
    assert run.state is ResearchState.PLAN_READY, "the re-ask recovered the run"


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


# -- recovering an interrupted run -------------------------------------------


def interrupt(store: ResearchStore, task_id: str = "T-001") -> ResearchRun:
    """Leave the run exactly as a killed process would: mid-task, mid-state."""

    run = store.load()
    tasks = [
        item.model_copy(update={"status": TaskStatus.RUNNING, "started_at": "now"})
        if item.task_id == task_id
        else item
        for item in run.tasks
    ]
    return store.save(
        run.model_copy(update={"state": ResearchState.EXECUTING, "tasks": tasks})
    )


def test_an_interrupted_run_cannot_simply_be_executed_again(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, _run, _ = start(tmp_path)
    interrupt(store)
    with pytest.raises(ResearchStateError, match="resume"):
        controller.execute(store)


def test_resume_marks_the_in_flight_task_failed_by_default(
    research_home: Path, tmp_path: Path
) -> None:
    """The conservative reading: nothing is repeated, because it may have spent."""

    controller, store, _run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[task(), task(task_id="T-002", depends_on=["T-001"])]),
    )
    interrupt(store)
    run = controller.resume(store)
    assert run.state is ResearchState.INTERRUPTED
    assert run.task("T-001").status is TaskStatus.FAILED
    assert "may already have spent" in (run.task("T-001").failure_reason or "")
    assert run.resumable


def test_resume_with_retry_puts_the_task_back_in_the_queue(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, _run, _ = start(tmp_path)
    interrupt(store)
    run = controller.resume(store, retry=True)
    assert run.task("T-001").status is TaskStatus.PENDING
    assert run.task("T-001").started_at is None
    run = controller.execute(store)
    assert run.state is ResearchState.READY_FOR_HUMAN
    assert run.task("T-001").status is TaskStatus.DONE


def test_a_resumed_run_continues_with_what_does_not_depend_on_the_failure(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, _run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[task(), task(task_id="T-002", depends_on=["T-001"])]),
    )
    interrupt(store)
    controller.resume(store)
    with pytest.raises(ResearchPlanError, match="blocked"):
        controller.execute(store)
    run = store.load()
    assert run.task("T-002").status is TaskStatus.BLOCKED
    assert run.state is ResearchState.FAILED


def test_retrying_an_interrupted_experiment_needs_force(
    research_home: Path, tmp_path: Path
) -> None:
    """The one retry that can cost real money is the one that must be deliberate."""

    controller, store, _run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[experiment_task()]),
        experiments=experiment_config(),
        execute_experiments=True,
    )
    interrupt(store)
    with pytest.raises(ResearchStateError, match="spend the same compute twice"):
        controller.resume(store, retry=True)
    run = controller.resume(store, retry=True, force=True)
    assert run.task("T-001").status is TaskStatus.PENDING
    assert any(
        record.get("event") == "task_recovered" and record.get("forced") is True
        for record in store.iter_events()
    )


def test_resuming_a_run_interrupted_while_planning_fails_it(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, run, _ = start(tmp_path)
    store.save(run.model_copy(update={"state": ResearchState.PLANNING}))
    run = controller.resume(store)
    assert run.state is ResearchState.FAILED
    assert "still planning" in (run.failure_reason or "")


def test_resume_refuses_a_run_that_was_not_interrupted(
    research_home: Path, tmp_path: Path
) -> None:
    controller, store, _run, _ = start(tmp_path)
    with pytest.raises(ResearchStateError, match="not an interrupted run"):
        controller.resume(store)


def test_a_pass_that_runs_out_of_time_stops_before_starting_the_next_task(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is on the loop, not a timeout on the work.

    Killing a task mid-flight is how a run ends up holding a worktree nobody
    knows about. Declining to start the next one leaves every artifact complete
    and the ledger honest about why it stopped.
    """

    controller, store, _run, _ = start(
        tmp_path,
        plan=plan_payload(tasks=[task(), task(task_id="T-002")]),
        budget=ResearchBudget(max_wall_clock_seconds=60),
    )

    clock = iter(
        ["2026-09-12T10:00:00Z", "2026-09-12T10:30:00Z"] + ["2026-09-12T10:30:00Z"] * 50
    )
    monkeypatch.setattr("research_os.research.controller.utc_now", lambda: next(clock))
    with pytest.raises(BudgetExceededError, match="wall-clock budget"):
        controller.execute(store)

    run = store.load()
    assert run.state is ResearchState.FAILED
    assert run.task("T-001").status is TaskStatus.PENDING
    assert any(
        record.get("event") == "wall_clock_exhausted" for record in store.iter_events()
    )


# -- the prompt and the schema must agree ------------------------------------


def _flowed(prompt: str) -> str:
    """Return the prompt with line wrapping removed.

    The prompt is hard-wrapped for the humans who maintain it, so asserting on
    a sentence means asserting across a newline. Normalising here keeps these
    tests about what the prompt says rather than where it happened to break.
    """

    return " ".join(prompt.split())


def test_the_schema_requires_only_what_every_task_has() -> None:
    """Requiring all thirteen task keys made the schema needlessly expensive.

    A planner had to emit an empty value for every key its kind did not use,
    and a live run failed outright when it did not. The rest of the keys have
    defaults on ``PlannedTask``, and the local validators explain a missing one
    far better than a structured-output retry loop does.

    This was *not* the cause of the placeholder plans -- replaying the archived
    prompts showed either prompt can produce either outcome -- but a schema
    that is cheap to satisfy is the right shape regardless.
    """

    assert PLAN_SCHEMA["properties"]["tasks"]["items"]["required"] == [
        "id",
        "kind",
        "title",
        "goal",
    ]
    # Everything else still has a place to go, and is still refused if invented.
    assert PLAN_SCHEMA["properties"]["tasks"]["items"]["additionalProperties"] is False


def test_a_task_giving_only_the_required_keys_is_dispatchable() -> None:
    """The defaults live on the model, so the planner need not write them."""

    plan = ResearchPlan.model_validate(
        plan_payload(
            tasks=[
                {
                    "id": "T-001",
                    "kind": "literature",
                    "title": "Read the field",
                    "goal": "Find out what is settled.",
                    "query": "widget deformation",
                }
            ]
        )
    )
    built = to_tasks(plan)[0]
    assert built.query == "widget deformation"
    assert built.read_paths == []
    assert built.experiment_parameters == {}


def test_the_prompt_names_every_key_the_schema_requires() -> None:
    """A required key the prompt never mentions is a run that cannot plan."""

    prompt = build_research_plan_prompt(
        goal="anything",
        budget=ResearchBudget(),
        science_context="",
        repository_context="",
        declared_experiments=["fit-model"],
    )
    required = PLAN_SCHEMA["properties"]["tasks"]["items"]["required"]
    missing = [key for key in required if key not in prompt]
    assert not missing, f"the prompt never mentions {missing}"


def test_the_prompt_says_what_to_put_in_a_key_that_does_not_apply() -> None:
    """Otherwise a model omits it, which the schema refuses."""

    prompt = _flowed(
        build_research_plan_prompt(
            goal="anything",
            budget=ResearchBudget(),
            science_context="",
            repository_context="",
            declared_experiments=[],
        )
    )
    assert 'Every task has "id", "kind", "title" and "goal"' in prompt
    assert "set only the keys the kind actually uses" in prompt


def test_the_output_shape_is_stated_as_prose_and_never_as_a_form() -> None:
    """Each key is described where its kind is, as the automation planner does.

    An earlier version stated them in a key-by-key table. Replaying the
    archived prompts showed the table was not what produced the placeholder
    plans, so this is a readability choice rather than a fix -- but it keeps
    the two planners in this codebase written the same way, and the one that
    predates it has never had the problem.
    """

    raw = build_research_plan_prompt(
        goal="anything",
        budget=ResearchBudget(),
        science_context="SCIENCE-MARKER",
        repository_context="REPO-MARKER",
        declared_experiments=[],
    )
    prompt = _flowed(raw)
    assert "set only the keys the kind actually uses" in prompt
    assert "the researcher reads them" in prompt
    # No field-by-field form: each key is described where its kind is.
    assert "FILLING IN THE JSON" not in prompt
    assert raw.index('Sets "query"') < raw.index("SCIENCE-MARKER")


def test_every_task_key_the_model_may_return_maps_onto_the_task_model() -> None:
    """The schema cannot drift from what a ResearchTask can actually hold."""

    schema_keys = set(PLAN_SCHEMA["properties"]["tasks"]["items"]["properties"])
    planned_keys = set(PlannedTask.model_fields)
    assert schema_keys == planned_keys
    task_fields = set(ResearchTask.model_fields)
    assert (planned_keys - {"id"}) <= task_fields


def test_a_plan_made_of_placeholders_is_refused(
    research_home: Path, tmp_path: Path
) -> None:
    """The failure this exists for, reproduced exactly.

    Seen live, three runs in a row, after thirteen thousand output tokens of
    genuine planning against a real project: a one-task plan whose summary,
    title, goal and query were all the word "test". Everything downstream then
    ran on it -- a literature search for "test" reached three providers and
    retrieved sixty works.

    Replaying the archived prompts showed the prompt is not the variable: the
    prompt that first produced an excellent four-task plan produces "test" on
    replay, and the one that produced "test" produces a good plan. A provider's
    structured-output enforcement can converge on the smallest object that
    validates, silently, so the controller assumes it can happen at any time.
    """

    stub = {
        "summary": "test",
        "tasks": [
            {
                "id": "T-001",
                "kind": "literature",
                "title": "test",
                "goal": "test",
                "query": "test",
            }
        ],
    }
    with pytest.raises(ResearchPlanError, match="placeholder"):
        start(tmp_path, plan=stub)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", "TODO"),
        ("summary", "n/a"),
        ("summary", "..."),
        ("title", "placeholder"),
        ("goal", "TBD."),
        ("query", "test"),
    ],
)
def test_each_placeholder_field_is_named_in_the_refusal(field: str, value: str) -> None:
    """A refusal that does not say which field is a refusal nobody can act on."""

    payload = plan_payload(tasks=[task()])
    if field == "summary":
        payload["summary"] = value
    else:
        payload["tasks"][0][field] = value
    plan = ResearchPlan.model_validate(payload)
    with pytest.raises(ResearchPlanError) as exit_info:
        validate_research_plan(
            plan, budget=ResearchBudget(), declared_experiments=frozenset()
        )
    message = str(exit_info.value)
    assert value.strip() in message
    assert "placeholder" in message


def test_a_terse_but_real_plan_is_not_second_guessed() -> None:
    """The check is about placeholders, not about prose quality.

    A controller that refused a short goal would be grading writing, which is
    not its job and not something it could do well.
    """

    plan = ResearchPlan.model_validate(
        plan_payload(
            summary="Read the field, then decide.",
            tasks=[
                task(
                    title="Read it",
                    goal="Find out what is settled.",
                    query="widget deformation",
                )
            ],
        )
    )
    validate_research_plan(
        plan, budget=ResearchBudget(), declared_experiments=frozenset()
    )


def test_a_word_that_merely_contains_a_placeholder_is_fine() -> None:
    """ "Testing the estimator" is a real goal. Only whole-token matches count."""

    plan = ResearchPlan.model_validate(
        plan_payload(
            summary="Testing the estimator against the Berry benchmark.",
            tasks=[
                task(
                    title="Testbed comparison",
                    goal="Compare the estimator against the published testbed.",
                    query="assessment ratio testbed",
                )
            ],
        )
    )
    validate_research_plan(
        plan, budget=ResearchBudget(), declared_experiments=frozenset()
    )


def test_a_placeholder_plan_is_re_asked_once_and_then_accepted(
    research_home: Path, tmp_path: Path
) -> None:
    """A degenerate plan is worth one more call, not a failed run."""

    stub = {
        "summary": "test",
        "tasks": [
            {
                "id": "T-001",
                "kind": "literature",
                "title": "test",
                "goal": "test",
                "query": "test",
            }
        ],
    }
    provider = scripted()
    provider.responses["planner"] = [
        ScriptedResponse(structured=stub),
        ScriptedResponse(structured=plan_payload()),
    ]
    _controller, store, run, _ = start(tmp_path, provider=provider)

    assert run.state is ResearchState.PLAN_READY
    assert run.plan_summary != "test"
    assert run.model_calls_used == 2
    events = [record["event"] for record in store.iter_events()]
    assert events.count("plan_rejected") == 1
    assert "plan_correction_started" in events
    assert "plan_correction_accepted" in events


def test_the_planner_correction_carries_the_validator_s_exact_objection(
    research_home: Path, tmp_path: Path
) -> None:
    stub = {
        "summary": "TODO",
        "tasks": [
            {
                "id": "T-001",
                "kind": "literature",
                "title": "TODO",
                "goal": "TODO",
                "query": "TODO",
            }
        ],
    }
    provider = scripted()
    provider.responses["planner"] = [
        ScriptedResponse(structured=stub),
        ScriptedResponse(structured=plan_payload()),
    ]
    start(tmp_path, provider=provider)

    retry = provider.requests_for(Role.PLANNER)[1]
    assert "YOUR PREVIOUS ANSWER WAS REJECTED" in retry.prompt
    assert "placeholder" in retry.prompt
    assert "This is your one correction" in retry.prompt


def test_a_second_placeholder_plan_ends_the_run(
    research_home: Path, tmp_path: Path
) -> None:
    """One correction, not a loop."""

    stub = {
        "summary": "test",
        "tasks": [
            {
                "id": "T-001",
                "kind": "literature",
                "title": "test",
                "goal": "test",
                "query": "test",
            }
        ],
    }
    provider = scripted()
    provider.responses["planner"] = [
        ScriptedResponse(structured=stub),
        ScriptedResponse(structured=stub),
    ]
    with pytest.raises(ResearchPlanError, match="placeholder"):
        start(tmp_path, provider=provider)

    run = ResearchStore.open(ResearchStore.list_run_ids()[-1]).load()
    assert run.state is ResearchState.FAILED
    assert len(provider.requests_for(Role.PLANNER)) == 2


def test_an_interrupted_delegated_task_is_charged_on_both_resume_paths(
    research_home: Path, tmp_path: Path
) -> None:
    """Found by an independent reviewer, twice over.

    Delegated model calls are charged in a ``finally`` that a killed process
    never reaches. The first fix reconciled only on ``--retry``; resume without
    it is the documented default, and the run goes on executing its remaining
    tasks either way, so the leak survived on the path most people take.

    The fixture is the part of that state reconciliation reads: an
    ``automation_run_started`` event naming a delegated run that really spent,
    no ``automation_run_finished`` event, and the task still RUNNING.

    It is deliberately *not* the whole of what a kill leaves. A real dispatch
    now writes ``automation_dispatch_attempted`` first, and a delta review
    pointed out that a fixture claiming to be the real state while omitting
    that event would quietly mask an attempt-accounting regression. The event
    is irrelevant to reconciliation, which reads only the started/finished
    pair; it is covered directly by
    ``test_a_dispatch_is_numbered_before_it_is_attempted``.
    """

    from research_os.automation.controller import AutomationController
    from tests.research_helpers import fake_config

    for retry in (False, True):
        case = tmp_path / f"case-{retry}"
        controller, store, _run, repo = start(
            case, plan=plan_payload(tasks=[analysis_task(), task(task_id="T-002")])
        )

        # A real delegated run that really spent model calls, dispatched the way
        # the research controller dispatches one.
        inner_controller = AutomationController(
            providers=controller.providers, config=fake_config()
        )
        inner_store, inner = inner_controller.start(
            project_path=repo,
            goal="Understand the adder.",
            plan=PlanDocument(
                summary="analyse it",
                tasks=[
                    AutomationPlannedTask(
                        id="T-001",
                        title="Understand the adder",
                        goal="Explain what adder.py does.",
                        role=PlannedRole.ANALYST,
                        read_only=True,
                        read_paths=["adder.py"],
                        completion_condition="findings about adder.py",
                    )
                ],
            ),
        )
        inner = inner_controller.execute(inner_store)
        inner_spend = inner.model_calls_used
        assert inner_spend >= 1

        # The ledger records the dispatch and nothing after it.
        store.append_event(
            "automation_run_started",
            task_id="T-001",
            automation_run_id=inner.run_id,
            directory=str(inner_store.directory),
            writes=False,
        )
        store.save(
            store.load().model_copy(
                update={
                    "state": ResearchState.EXECUTING,
                    "tasks": [
                        item.model_copy(update={"status": TaskStatus.RUNNING})
                        if item.task_id == "T-001"
                        else item
                        for item in store.load().tasks
                    ],
                }
            )
        )
        before = store.load().model_calls_used
        recovered = controller.resume(store, retry=retry)

        assert recovered.model_calls_used == before + inner_spend, (
            f"resume(retry={retry}) lost the interrupted spend"
        )
        assert any(
            record.get("event") == "delegated_spend_reconciled"
            for record in store.iter_events()
        )


def test_reconciling_twice_does_not_charge_the_same_attempt_again(
    research_home: Path, tmp_path: Path
) -> None:
    """A second interruption must not re-charge the first attempt.

    The task record reports zero calls for something that never finished, so
    reconciliation reads what it has already been told from the ledger instead.
    """

    from research_os.automation.controller import AutomationController
    from tests.research_helpers import fake_config

    controller, store, _run, repo = start(
        tmp_path, plan=plan_payload(tasks=[analysis_task()])
    )
    inner_controller = AutomationController(
        providers=controller.providers, config=fake_config()
    )
    inner_store, inner = inner_controller.start(
        project_path=repo,
        goal="Understand the adder.",
        plan=PlanDocument(
            summary="analyse it",
            tasks=[
                AutomationPlannedTask(
                    id="T-001",
                    title="Understand the adder",
                    goal="Explain what adder.py does.",
                    role=PlannedRole.ANALYST,
                    read_only=True,
                    read_paths=["adder.py"],
                    completion_condition="findings about adder.py",
                )
            ],
        ),
    )
    inner = inner_controller.execute(inner_store)
    store.append_event(
        "automation_run_started",
        task_id="T-001",
        automation_run_id=inner.run_id,
        directory=str(inner_store.directory),
        writes=False,
    )

    def interrupted():
        current = store.load()
        return current.model_copy(
            update={
                "state": ResearchState.EXECUTING,
                "tasks": [
                    item.model_copy(
                        update={"status": TaskStatus.RUNNING, "model_calls": 0}
                    )
                    for item in current.tasks
                ],
            }
        )

    store.save(interrupted())
    once = controller.resume(store, retry=True).model_calls_used
    store.save(interrupted())
    twice = controller.resume(store, retry=True).model_calls_used
    assert twice == once, "the first attempt was charged a second time"


def test_a_second_interruption_after_a_completed_attempt_is_not_double_charged(
    research_home: Path, tmp_path: Path
) -> None:
    """The exact sequence a third independent review reproduced.

    Plan, kill, reconcile, run to completion, kill again, reconcile again. The
    ledger recorded an inner run's *total* under one event and a reconcile
    *delta* under another, so the second pass compared a total against a delta
    and charged the difference a second time. Both now publish the same running
    total under one key.
    """

    controller, store, _run, _ = start(
        tmp_path, plan=plan_payload(tasks=[analysis_task()])
    )
    complete = controller.execute(store)
    truth = complete.model_calls_used

    def interrupt() -> None:
        current = store.load()
        store.save(
            current.model_copy(
                update={
                    "state": ResearchState.EXECUTING,
                    "tasks": [
                        item.model_copy(update={"status": TaskStatus.RUNNING})
                        for item in current.tasks
                    ],
                }
            )
        )

    interrupt()
    first = controller.resume(store, retry=False).model_calls_used
    interrupt()
    second = controller.resume(store, retry=False).model_calls_used

    assert first == truth, "reconciliation invented spend that never happened"
    assert second == truth, "the completed attempt was charged twice"


def test_a_plan_dressed_up_in_real_words_is_still_refused(
    research_home: Path, tmp_path: Path
) -> None:
    """The exact plan the release pilot produced against a real project.

    The first placeholder guard refused a field whose every token was a
    placeholder, so "test" was caught. This was not: "Test task", "Test goal."
    and "test query" all contain a real word, and the run went on to search
    three literature providers for "test query" and report success.

    The rule now refuses a field that contains a placeholder and nothing but
    structural filler besides -- which is what these are -- while leaving any
    field with actual subject matter alone.
    """

    pilot_plan = {
        "summary": "Test minimal plan to isolate schema validation issue.",
        "tasks": [
            {
                "id": "T-001",
                "kind": "literature",
                "title": "Test task",
                "goal": "Test goal.",
                "query": "test query",
            }
        ],
    }
    with pytest.raises(ResearchPlanError, match="placeholder"):
        start(tmp_path, plan=pilot_plan)


@pytest.mark.parametrize(
    "value",
    [
        "Test task",
        "Test goal.",
        "test query",
        "example data",
        "TODO: the plan",
        "placeholder step",
    ],
)
def test_placeholder_dressed_in_filler_is_refused(value: str) -> None:
    from research_os.research.planner import _is_placeholder

    assert _is_placeholder(value), f"{value!r} slipped through"


@pytest.mark.parametrize(
    "value",
    [
        "Read the field",
        "Read it",
        "Testing the estimator",
        "Prior art on curvature-approximation adequacy",
        "Find out whether anyone has settled this.",
        "widget deformation under load",
        "Assess the query planner's fallback behaviour",
        "Implement the goal-seeking solver",
        # The two the FILLER set was trimmed for. The trim's stated reason was
        # that these were being refused; without them here, putting "case" or
        # "values" back would re-break both silently.
        "Test the minimal case",
        "Test the output values",
    ],
)
def test_real_prose_containing_a_structural_word_is_not_refused(value: str) -> None:
    """The guard must not start grading writing.

    Several of these contain "query", "goal" or "the" -- words the filter treats
    as structural -- and every one of them is a legitimate thing to write.
    """

    from research_os.research.planner import _is_placeholder

    assert not _is_placeholder(value), f"{value!r} was wrongly refused"


def test_a_dispatch_is_numbered_before_it_is_attempted(
    research_home: Path, tmp_path: Path
) -> None:
    """The attempt number is written before the dispatch, not after it.

    A delta review found the third consecutive repair in this area shipping
    with no detector: ``automation_dispatch_attempted`` appeared nowhere in the
    suite, so deleting the line that writes it left every attempt numbered 1
    and the whole suite still green. This is the detector.
    """

    controller, store, _run, _ = start(
        tmp_path, plan=plan_payload(tasks=[analysis_task()])
    )
    controller.execute(store)

    dispatch = [
        record
        for record in store.iter_events()
        if record.get("event")
        in ("automation_dispatch_attempted", "automation_run_started")
    ]
    assert dispatch, "the task dispatched no delegated run at all"
    assert dispatch[0]["event"] == "automation_dispatch_attempted", (
        "the attempt number must be recorded before the dispatch that uses it, "
        f"but the first dispatch event was {dispatch[0]['event']!r}"
    )
    assert dispatch[0]["attempt"] == 1


def test_a_dispatch_that_dies_after_its_directory_exists_burns_its_number(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure the pre-dispatch event exists to prevent, driven for real.

    ``AutomationController.start`` creates the run directory and then does more
    work -- context, planning -- any of which can fail. When it does, the
    research controller never writes ``automation_run_started``. Counting
    *those* to number the next attempt therefore hands the retry the number the
    dead dispatch already used, and ``RunStore.create`` refuses the directory it
    just found. Only a retry inside the same UTC second collides, which is
    exactly what a prompt ``resume --retry`` is.

    A first version of this test hand-wrote the event and compared run ids. A
    release gate pointed out that it proved nothing: with no dispatch executed,
    its closing comparison reduced to ``make_run_id(X) == make_run_id(X)``, and
    the rest was a property of ``make_run_id``'s signature rather than of the
    dispatch path. So this one kills a real dispatch inside ``start``, freezes
    the clock so the retry lands in the colliding second, and lets the retry run.
    """

    from research_os.automation import controller as automation_controller

    controller, store, _run, _ = start(
        tmp_path, plan=plan_payload(tasks=[analysis_task()])
    )

    # Every dispatch now shares one second, which is the only case in which two
    # run ids can collide at all.
    monkeypatch.setattr(
        automation_controller, "utc_now", lambda: "2026-09-13T10:15:00Z"
    )

    real_build_context = automation_controller.build_context
    attempts: list[int] = []

    def die_on_the_first_dispatch(**kwargs: object):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("the worker died after the run directory existed")
        return real_build_context(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        automation_controller, "build_context", die_on_the_first_dispatch
    )

    with pytest.raises(RuntimeError):
        controller.execute(store)

    # The directory the dead dispatch created is still there, and it wrote no
    # ``automation_run_started`` -- which is what made the old counter blind.
    numbered = [
        record
        for record in store.iter_events()
        if record.get("event") == "automation_dispatch_attempted"
    ]
    started = [
        record
        for record in store.iter_events()
        if record.get("event") == "automation_run_started"
    ]
    assert [record["attempt"] for record in numbered] == [1]
    assert not started, "the fixture must leave a dispatch that never started"

    # The retry, in the same second, against the same project and goal. Without
    # the pre-dispatch event this raises RunStoreError: run directory already
    # exists.
    recovered = controller.resume(store, retry=True)
    controller.execute(store)
    del recovered

    numbered = [
        record
        for record in store.iter_events()
        if record.get("event") == "automation_dispatch_attempted"
    ]
    assert [record["attempt"] for record in numbered] == [1, 2], (
        "the retry reused the dead dispatch's attempt number"
    )
    started = [
        record
        for record in store.iter_events()
        if record.get("event") == "automation_run_started"
    ]
    assert len(started) == 1
    assert started[0]["automation_run_id"] != numbered[0].get("automation_run_id")


def test_two_delegated_runs_on_one_task_are_not_double_charged(
    research_home: Path, tmp_path: Path
) -> None:
    """A task with *two* delegated runs, which is where the units diverged.

    A final release review showed the previous test could not see this defect:
    with one delegated run, "this inner run's total" and "this task's cumulative
    total" are the same number, so writing one where the other was meant is
    invisible. With two they differ, and the reconciliation charged the
    difference again.

    The sequence is the reviewer's: execute, interrupt, resume --retry (a second
    delegated run), execute, interrupt, resume.
    """

    from research_os.automation.store import RunStore

    controller, store, _run, _ = start(
        tmp_path, plan=plan_payload(tasks=[analysis_task()])
    )
    controller.execute(store)

    def interrupt() -> None:
        current = store.load()
        store.save(
            current.model_copy(
                update={
                    "state": ResearchState.EXECUTING,
                    "tasks": [
                        item.model_copy(update={"status": TaskStatus.RUNNING})
                        for item in current.tasks
                    ],
                }
            )
        )

    # First interruption, retried -- this dispatches a *second* delegated run.
    interrupt()
    controller.resume(store, retry=True)
    controller.execute(store)

    # Second interruption, on the default path.
    interrupt()
    recovered = controller.resume(store, retry=False)

    # The truth: the planner's call plus every delegated run's own record.
    delegated = sum(
        RunStore.open(str(record["automation_run_id"])).load().model_calls_used
        for record in store.iter_events()
        if record.get("event") == "automation_run_started"
    )
    started = sum(
        1
        for record in store.iter_events()
        if record.get("event") == "automation_run_started"
    )
    assert started >= 2, "the fixture must produce more than one delegated run"
    assert recovered.model_calls_used == 1 + delegated, (
        f"charged {recovered.model_calls_used}, true total {1 + delegated} "
        f"across {started} delegated runs"
    )

    # And every charge event agrees on what the number means.
    totals = [
        int(record["charged_total"])
        for record in store.iter_events()
        if record.get("charged_total") is not None
    ]
    assert totals == sorted(totals), f"charged_total went backwards: {totals}"


# -- a provider that does not answer at all -----------------------------------


def planner_failure() -> ScriptedResponse:
    """The failure a live pilot actually hit: the provider gave up on the schema."""

    return ScriptedResponse(
        structured=None,
        text=None,
        exit_code=1,
        error="error_max_structured_output_retries",
    )


def planner_scripted(responses: list[ScriptedResponse]) -> Any:
    from tests.automation_helpers import analysis_payload, review_payload

    return FakeProvider(
        responses={
            str(Role.PLANNER): responses,
            str(Role.ANALYST): [ScriptedResponse(structured=analysis_payload())],
            str(Role.REVIEWER): [ScriptedResponse(structured=review_payload())],
        }
    )


def test_a_planner_provider_failure_is_retried_once_and_then_succeeds(
    research_home: Path, tmp_path: Path
) -> None:
    """Found by a live pilot, not by review.

    A read-only run against a real project died on its very first planner call
    with ``error_max_structured_output_retries`` -- the provider's own
    structured-output machinery giving up -- and the whole run was abandoned
    with thirteen of its fourteen model calls unspent. Nothing had been
    rejected, because nothing had arrived.
    """

    provider = planner_scripted(
        [
            planner_failure(),
            ScriptedResponse(structured=plan_payload(tasks=[analysis_task()])),
        ]
    )
    controller, store, run, _ = start(tmp_path, provider=provider)
    run = controller.execute(store)

    assert run.state is ResearchState.READY_FOR_HUMAN
    events = [record.get("event") for record in store.iter_events()]
    assert "plan_provider_failed" in events
    assert "plan_retry_started" in events
    planner_calls = [call for call in provider.calls if call.role is Role.PLANNER]
    assert len(planner_calls) == 2
    # The same question, because the question was never answered.
    assert planner_calls[0].prompt == planner_calls[1].prompt


def test_a_planner_that_fails_twice_ends_the_run(
    research_home: Path, tmp_path: Path
) -> None:
    """A provider that fails twice is a provider that is not working."""

    provider = planner_scripted(
        [
            planner_failure(),
            planner_failure(),
            ScriptedResponse(structured=plan_payload(tasks=[analysis_task()])),
        ]
    )
    with pytest.raises(ProviderInvocationError, match="error_max_structured"):
        start(tmp_path, provider=provider)

    planner_calls = [call for call in provider.calls if call.role is Role.PLANNER]
    assert len(planner_calls) == 2, "one retry, never two"


def test_a_provider_failure_and_a_rejected_plan_share_one_allowance(
    research_home: Path, tmp_path: Path
) -> None:
    """The bound is two planner calls however they are spent.

    Otherwise a run could fail over, then be corrected, then be corrected
    again -- and the single re-ask this controller promises would quietly have
    become three attempts.
    """

    provider = planner_scripted(
        [
            planner_failure(),
            ScriptedResponse(
                structured=plan_payload(tasks=[analysis_task(title="test")])
            ),
            ScriptedResponse(structured=plan_payload(tasks=[analysis_task()])),
        ]
    )
    with pytest.raises((ResearchPlanError, ProviderInvocationError)):
        start(tmp_path, provider=provider)

    planner_calls = [call for call in provider.calls if call.role is Role.PLANNER]
    assert len(planner_calls) == 2


def test_a_planner_failure_with_no_budget_left_is_not_retried(
    research_home: Path, tmp_path: Path
) -> None:
    provider = planner_scripted(
        [
            planner_failure(),
            ScriptedResponse(structured=plan_payload(tasks=[analysis_task()])),
        ]
    )
    with pytest.raises(ProviderInvocationError):
        start(tmp_path, provider=provider, budget=ResearchBudget(max_model_calls=1))

    planner_calls = [call for call in provider.calls if call.role is Role.PLANNER]
    assert len(planner_calls) == 1, "a retry it cannot pay for must not be attempted"


def test_a_failed_proposal_task_charges_the_run_for_what_it_spent(
    research_home: Path, tmp_path: Path
) -> None:
    """Otherwise a retry spends the same calls again against an unmoved ledger."""

    from research_os.errors import ProposalError
    from tests.proposal_helpers import assessment_payload, item, proposal_payload
    from tests.research_helpers import proposal_task

    provider = scripted(plan=plan_payload(tasks=[proposal_task()]))
    provider.responses["planner"] = [
        ScriptedResponse(structured=plan_payload(tasks=[proposal_task()])),
        # A proposal citing a capsule object this project does not hold. The
        # correction is spent, invents nothing better, and the task fails.
        ScriptedResponse(
            structured=proposal_payload(items=[item(addresses=["C-9999"])])
        ),
        ScriptedResponse(
            structured=proposal_payload(items=[item(addresses=["C-8888"])])
        ),
    ]
    provider.responses["reviewer"] = [ScriptedResponse(structured=assessment_payload())]
    controller, store, _run, _repo = start(tmp_path, provider=provider)
    before = store.load().model_calls_used

    with pytest.raises((ProposalError, ProviderInvocationError)):
        controller.execute(store)

    after = store.load().model_calls_used
    events = [record.get("event") for record in store.iter_events()]
    assert "proposal_failed" in events
    failed = next(
        record
        for record in store.iter_events()
        if record.get("event") == "proposal_failed"
    )
    assert after - before == failed["model_calls"], (
        "the run was charged exactly what the failed proposal spent"
    )


# -- the degenerate plans each live pilot has produced ------------------------


@pytest.mark.parametrize(
    "value",
    [
        "test",
        "TBD.",
        "...",
        "Test task",
        "test summary two",
        "t",
        "g",
        "q",
        "x",
    ],
)
def test_a_field_that_says_nothing_is_refused(value: str) -> None:
    """Every shape of degenerate field a live pilot has actually produced.

    Three releases have found this hole from three angles: "test", then "Test
    task", then `{"summary": "test summary two", "title": "t", "goal": "g",
    "query": "q"}` -- which reached READY_FOR_HUMAN having searched three
    literature providers for "q".
    """

    assert _is_placeholder(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "Read the field",
        "Testing the estimator",
        "fix",
        "Re-score Gate E.4 as a sensitivity disclosure",
        "Assess the solver's restart rule",
        "widget deformation under load",
        "Recover the Gate E.4 table",
        # Ordinary fields an all-filler rule refused for one commit, until an
        # independent review pointed out the cost: a re-ask spent on a plan that
        # was fine, then a failed run on the second identical phrasing.
        "Query the data",
        "Results summary",
        "First results",
        "Summary of results",
        "the data",
        "a plan",
    ],
)
def test_terse_but_real_prose_is_not_refused(value: str) -> None:
    """The guard grades emptiness, not brevity, and not ordinary vocabulary."""

    assert _is_placeholder(value) is False


def test_the_exact_plan_that_reached_ready_for_human_is_refused() -> None:
    """The live payload, replayed verbatim.

    Recorded from a real run against cuPDLP.jl. It validated, executed, searched
    crossref and openalex for "q", and reported success.
    """

    payload = {
        "summary": "test summary two",
        "tasks": [
            {
                "id": "T-001",
                "kind": "literature",
                "title": "t",
                "goal": "g",
                "query": "q",
            }
        ],
    }
    with pytest.raises(ResearchPlanError, match="placeholder"):
        assert_plan_says_something(ResearchPlan.model_validate(payload))


def test_the_prompt_names_every_declared_experiment_parameter() -> None:
    """A plan cannot satisfy a rule it was never told about.

    The validator refuses an experiment task that omits a required parameter --
    correctly, because the researcher declared it. The prompt listed only the
    command *names*, so a planner could not know the parameter existed. Two live
    Pilot E attempts were refused for exactly that, one release after the
    validator was completed and the prompt was not.
    """

    prompt = build_research_plan_prompt(
        goal="Measure the estimator.",
        budget=ResearchBudget(),
        science_context="",
        repository_context="",
        declared_experiments=["compare-estimators", "sweep"],
        experiment_parameters={
            "compare-estimators": ["seed (integer, required, 0..7)"],
            "sweep": [],
        },
    )
    assert "seed (integer, required, 0..7)" in prompt
    assert "parameters: none" in prompt
    assert "A plan that omits one is refused." in prompt


def test_a_command_with_no_parameters_says_none() -> None:
    prompt = build_research_plan_prompt(
        goal="Run it.",
        budget=ResearchBudget(),
        science_context="",
        repository_context="",
        declared_experiments=["fit-model"],
    )
    assert "fit-model" in prompt
    assert "parameters: none" in prompt


def test_a_declared_parameter_is_described_with_what_it_is_checked_against() -> None:
    from research_os.experiment.models import ParameterSpec
    from research_os.research.controller import _describe_parameter

    assert (
        _describe_parameter(
            ParameterSpec(
                name="seed", type="integer", required=True, minimum=0, maximum=7
            )
        )
        == "seed (integer, required, 0..7)"
    )
    assert (
        _describe_parameter(
            ParameterSpec(name="mode", type="choice", choices=["fast", "exact"])
        )
        == "mode (choice, optional, one of: fast, exact)"
    )
