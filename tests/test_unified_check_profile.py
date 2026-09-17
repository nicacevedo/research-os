"""One project, one acceptance profile, whatever dispatched the work.

v1.1 gave a researcher a way to say what validates their project --
``projects.<id>.check_profiles`` in ``automation.yaml`` -- and exactly one
caller honoured it. ``researchctl research run`` resolved a task's named checks
against those profiles; every other way into ``AutomationController`` ran
acceptance commands a *planner* had written. Both sets passed the same command
policy, so nothing was unsafe; they were simply different gates on the same
code, and the only way to find out which one had run was to read two run
directories.

These tests establish the property the divergence broke: **the commands that
actually execute are the same on every path**. Not the same resolution function
called twice -- the same argv, observed in the run record, after the pipeline
has really run them.

The four paths are the four ways a coding task reaches the controller:

    direct automation          researchctl auto run
    runtime coding             an autonomous cycle's edit_in_worktree
    resumed runtime coding     the same, after the process died mid-run
    worktree coding            the same, against a Git worktree of the project

The fifth caller, ``ResearchController``, supplies a plan whose commands it
already resolved from these same profiles. That path is asserted too, because
"we left it alone" is a claim about behaviour and deserves a test rather than a
comment.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.checkprofiles import CheckProfileSpec
from research_os.automation.config import ProjectSettings
from research_os.automation.controller import WHITESPACE_CHECK, AutomationController
from research_os.automation.models import RunState
from research_os.automation.store import RunStore
from tests.automation_helpers import commit_all, fake_config
from tests.fs_helpers import make_git_repo, write_minimal_capsule
from tests.test_auto_controller import BROKEN_MODULE, TINY_TEST, scripted

PROJECT_ID = "profiled-project"

#: What the researcher declared. Every acceptance command -- configured or
#: planned -- passes the same command-policy *grammar*, so a declaration cannot
#: introduce a form the policy forbids. These are deliberately shapes no
#: planner and no discovery rule in this build produces, so a command observed
#: with them can only have come from the configuration.
DECLARED_SMOKE = ["pytest", "-q", "--no-header", "test_adder.py"]
DECLARED_TESTS = ["pytest", "-q", "-x", "test_adder.py"]

#: What the planner writes when nobody overrules it.
PLANNER_ARGV = ("pytest", "-q")


def profiled_config(**kwargs: Any) -> Any:
    """A fake-provider config in which this project declares its own checks."""

    base = fake_config(**kwargs)
    return dataclasses.replace(
        base,
        projects={
            PROJECT_ID: ProjectSettings(
                check_profiles={
                    "tests": CheckProfileSpec(
                        argv=list(DECLARED_TESTS), description="the declared tests"
                    ),
                    "smoke": CheckProfileSpec(
                        argv=list(DECLARED_SMOKE), description="the declared smoke run"
                    ),
                }
            )
        },
    )


def profiled_repo(path: Path) -> Path:
    """A capsule project whose id is the one the configuration keys on."""

    repo = make_git_repo(path)
    write_minimal_capsule(repo, project_id=PROJECT_ID)
    (repo / "adder.py").write_text(BROKEN_MODULE, encoding="utf-8")
    (repo / "test_adder.py").write_text(TINY_TEST, encoding="utf-8")
    commit_all(repo)
    return repo


def planned_argv(run: Any) -> list[list[str]]:
    """The acceptance commands this run will actually execute, in order."""

    return [
        list(command.argv)
        for order in run.work_orders
        for command in order.acceptance_commands
    ]


def executed_argv(run: Any) -> list[list[str]]:
    """The acceptance commands this run *did* execute, from the check results.

    Includes the controller's own ``git diff --check HEAD``, which it appends to
    every coder order and which no configuration can remove. Asserted rather
    than filtered: a test that quietly dropped it would not notice if it stopped
    running.
    """

    return [
        list(result.argv) for order in run.work_orders for result in order.check_results
    ]


#: In the order the controller resolves them, which is by check id.
DECLARED = [DECLARED_SMOKE, DECLARED_TESTS]

#: The controller's own trailing check, which is not a profile and is not
#: configurable. Named here so the executed sequence can be asserted whole.
WHITESPACE = list(WHITESPACE_CHECK.argv)


# -- the four dispatch paths -------------------------------------------------


def test_direct_automation_gates_on_the_declared_commands(
    automation_home: Path, tmp_path: Path
) -> None:
    """`researchctl auto run` in all but name: a goal, a path, no plan."""

    repo = profiled_repo(tmp_path / "project")
    controller = AutomationController(
        providers={"fake": scripted()}, config=profiled_config()
    )
    _store, run = controller.start(project_path=repo, goal="Implement add.")

    assert planned_argv(run) == DECLARED, (
        "a project that declares check_profiles must be gated on them even when "
        "the caller supplied no plan"
    )
    assert PLANNER_ARGV not in {tuple(argv) for argv in planned_argv(run)}


def test_the_planner_authored_commands_are_preserved_as_provenance(
    automation_home: Path, tmp_path: Path
) -> None:
    """Substituting the gate must not erase what the plan asked for.

    `plan/plan.json` is what the planner wrote and `plan/plan.effective.json` is
    what will run. Keeping only the second would make a substitution invisible;
    keeping only the first would make the run report lie about its own gate.
    """

    repo = profiled_repo(tmp_path / "project")
    controller = AutomationController(
        providers={"fake": scripted()}, config=profiled_config()
    )
    store, _run = controller.start(project_path=repo, goal="Implement add.")

    written = json.loads(store.path("plan", "plan.json").read_text(encoding="utf-8"))
    effective = json.loads(
        store.path("plan", "plan.effective.json").read_text(encoding="utf-8")
    )
    assert written["tasks"][0]["acceptance_commands"][0]["argv"] == list(PLANNER_ARGV)
    assert [
        item["argv"] for item in effective["tasks"][0]["acceptance_commands"]
    ] == DECLARED

    events = [
        item for item in store.iter_events() if item["event"] == "checks_substituted"
    ]
    assert len(events) == 1
    assert events[0]["check_ids"] == ["smoke", "tests"]


def test_runtime_coding_gates_on_the_declared_commands(
    action_env_profiled: dict[str, Any],
) -> None:
    """The autonomous cycle's coding action, end to end, really executing."""

    run = _run_runtime_coding(action_env_profiled)
    assert executed_argv(run) == [*DECLARED, WHITESPACE], (
        "the runtime's coding action ran planner-authored commands while "
        "`researchctl research run` ran the researcher's: that is the divergence"
    )


def test_resumed_runtime_coding_gates_on_the_declared_commands(
    action_env_profiled: dict[str, Any],
) -> None:
    """A run reconstructed from its store after the process died mid-pipeline.

    The orders were persisted at plan acceptance, so a resume executes what the
    substitution decided rather than re-asking anything. That is the point: a
    crash must not be a way to get a different gate.
    """

    env = action_env_profiled
    controller = env["controller"]
    repo = env["repo"]
    _store, _run = controller.start(
        project_path=repo,
        goal="Implement add.",
        reserved_run_id="RUN-19700101T000000Z-deadbeef",
    )
    # The process dies here. Nothing of the controller survives; the next
    # attempt has the run id and the store on disk and nothing else.
    reopened = RunStore.open("RUN-19700101T000000Z-deadbeef")
    resumed = AutomationController(
        providers={"fake": scripted()}, config=profiled_config()
    ).execute(reopened)

    assert resumed.state is RunState.READY_FOR_HUMAN
    assert executed_argv(resumed) == [*DECLARED, WHITESPACE]


def test_worktree_coding_gates_on_the_declared_commands(
    automation_home: Path, tmp_path: Path
) -> None:
    """A Git worktree of the project resolves the same project, so the same checks.

    Worth its own test because the capsule identity is read from the *checkout*,
    and a worktree is a second checkout of one repository. If the id came from
    somewhere less stable -- a directory name, a remote, a registry keyed on a
    path -- this is where the two paths would drift apart again.
    """

    repo = profiled_repo(tmp_path / "project")
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "wt-branch", str(worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    controller = AutomationController(
        providers={"fake": scripted()}, config=profiled_config()
    )
    _store, run = controller.start(project_path=worktree, goal="Implement add.")
    assert planned_argv(run) == DECLARED


def test_the_research_layer_keeps_the_narrowing_it_resolved(
    automation_home: Path, tmp_path: Path
) -> None:
    """A supplied plan whose commands are already declared is left alone.

    `ResearchController` resolves a task's `required_checks` -- often one of
    several -- through `resolve_required_checks`, from these same profiles. Its
    plan therefore carries a *subset*, and replacing a subset with the whole set
    would silently widen a gate a task deliberately narrowed. The rule is
    "drawn from the declared profiles", not "equal to all of them".
    """

    from research_os.automation.planner import PlanDocument, PlannedCommand, PlannedTask

    repo = profiled_repo(tmp_path / "project")
    controller = AutomationController(
        providers={"fake": scripted()}, config=profiled_config()
    )
    supplied = PlanDocument(
        summary="Implement add.",
        tasks=[
            PlannedTask(
                id="T-001",
                title="Implement add",
                goal="Make add return the sum of its two arguments.",
                read_only=False,
                allowed_paths=["adder.py"],
                acceptance_commands=[
                    PlannedCommand(
                        argv=list(DECLARED_TESTS),
                        description="controller-owned check 'tests'",
                    )
                ],
                completion_condition="the acceptance commands pass",
            )
        ],
    )
    store, run = controller.start(
        project_path=repo, goal="Implement add.", plan=supplied
    )

    assert planned_argv(run) == [DECLARED_TESTS]
    assert not store.path("plan", "plan.effective.json").exists()


def test_a_supplied_plan_that_invents_a_command_is_overruled(
    automation_home: Path, tmp_path: Path
) -> None:
    """Supplying a plan is not a way around the researcher's declaration.

    The subset rule is what preserves the research layer's narrowing, and it has
    to be a rule about the *commands* rather than about who is calling -- a
    check on the caller would be a check a future caller can fail to make.
    """

    from research_os.automation.planner import PlanDocument, PlannedCommand, PlannedTask

    repo = profiled_repo(tmp_path / "project")
    controller = AutomationController(
        providers={"fake": scripted()}, config=profiled_config()
    )
    supplied = PlanDocument(
        summary="Implement add.",
        tasks=[
            PlannedTask(
                id="T-001",
                title="Implement add",
                goal="Make add return the sum of its two arguments.",
                read_only=False,
                allowed_paths=["adder.py"],
                acceptance_commands=[
                    PlannedCommand(argv=["pytest", "-q"], description="invented")
                ],
                completion_condition="the acceptance commands pass",
            )
        ],
    )
    _store, run = controller.start(
        project_path=repo, goal="Implement add.", plan=supplied
    )
    assert planned_argv(run) == DECLARED


# -- the boundaries of the rule ----------------------------------------------


def test_an_unconfigured_project_keeps_the_planner_narrowing(
    automation_home: Path, tmp_path: Path
) -> None:
    """Discovery is a guess and a guess does not overrule a plan.

    The invariant a researcher can rely on is about what they *declared*.
    Letting discovery win as well would replace a planner's narrow, relevant
    command with this module's best guess at an ecosystem, which is a worse gate
    chosen by nobody.
    """

    repo = profiled_repo(tmp_path / "project")
    controller = AutomationController(
        providers={"fake": scripted()},
        config=fake_config(),  # no `projects` section at all
    )
    _store, run = controller.start(project_path=repo, goal="Implement add.")
    assert planned_argv(run) == [list(PLANNER_ARGV)]


def test_a_project_with_no_capsule_has_no_declaration_to_honour(
    automation_home: Path, tmp_path: Path
) -> None:
    """The id is read from the capsule, so a repository without one has none."""

    from research_os.automation.projectcontext import project_id_for
    from tests.automation_helpers import init_repo

    repo = init_repo(tmp_path / "plain")
    assert project_id_for(repo) is None

    controller = AutomationController(
        providers={"fake": scripted()}, config=profiled_config()
    )
    _store, run = controller.start(project_path=repo, goal="Implement add.")
    assert planned_argv(run) == [list(PLANNER_ARGV)]


def test_a_declared_command_the_policy_forbids_is_a_reported_defect(
    automation_home: Path, tmp_path: Path
) -> None:
    """A check the controller cannot run is a configuration error, not a silent skip.

    ``resolve_check_profiles`` refuses a configured argv the command policy
    forbids, and this path must surface that rather than fall back to the
    planner's commands -- falling back is exactly the divergence.
    """

    from research_os.errors import CommandPolicyError

    repo = profiled_repo(tmp_path / "project")
    base = profiled_config()
    config = dataclasses.replace(
        base,
        projects={
            PROJECT_ID: ProjectSettings(
                check_profiles={
                    "tests": CheckProfileSpec(argv=["curl", "https://example.invalid"])
                }
            )
        },
    )
    controller = AutomationController(providers={"fake": scripted()}, config=config)
    with pytest.raises(CommandPolicyError):
        controller.start(project_path=repo, goal="Implement add.")


def test_an_analysis_task_is_never_given_acceptance_commands(
    automation_home: Path, tmp_path: Path
) -> None:
    """An analyst writes nothing, so there is nothing to check.

    Substituting into one would make the controller run the project's whole
    validation suite against a read-only investigation that changed no files.
    """

    from tests.automation_helpers import analysis_task, coding_task, plan_payload
    from tests.test_auto_controller import scripted as scripted_with

    repo = profiled_repo(tmp_path / "project")
    provider = scripted_with(
        plan=plan_payload(tasks=[analysis_task(), coding_task()]),
    )
    controller = AutomationController(
        providers={"fake": provider}, config=profiled_config()
    )
    _store, run = controller.start(project_path=repo, goal="Investigate, then fix.")

    analysis = run.order("T-001")
    coding = run.order("T-002")
    assert list(analysis.acceptance_commands) == []
    assert [list(item.argv) for item in coding.acceptance_commands] == DECLARED


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def action_env_profiled(
    automation_home: Path,
    runtime_db: Any,
    pg_dsn: str,
    tmp_path: Path,
) -> dict[str, Any]:
    """A runtime run over a project that declares its own checks."""

    from research_os.runtime.store import RuntimeStore

    repo = profiled_repo(tmp_path / "project")
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT_ID, repo_path=str(repo))
    run = store.create_run(project_id=PROJECT_ID, objective="whether add works")
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "artifacts_root": tmp_path / "artifacts",
        "store": store,
        "run": run,
        "controller": AutomationController(
            providers={"fake": scripted()}, config=profiled_config()
        ),
        "state": {
            "run_id": run.run_id,
            "project_id": PROJECT_ID,
            "repo_path": str(repo),
            "objective": "whether add works",
            # `medium`, because `high` requires containment this host cannot
            # provide and the pipeline would refuse before reaching a check.
            "autonomy": "medium",
            "cycle_index": 0,
            "artifacts": [],
            "notes": [],
        },
    }


def _run_runtime_coding(env: dict[str, Any]) -> Any:
    """Drive `run_coding_task` to completion and return the automation run."""

    from research_os.runtime.actions import coding
    from research_os.runtime.actions.coding import run_coding_task
    from tests.runtime_graph_helpers import make_context, make_router

    built: list[AutomationController] = []

    def _controller(_context: Any, *, autonomy: str) -> AutomationController:
        controller = AutomationController(
            providers={"fake": scripted()}, config=profiled_config()
        )
        built.append(controller)
        return controller

    original = coding._controller
    coding._controller = _controller  # type: ignore[assignment]
    try:
        context = make_context(
            db=env["db"],
            repo=env["repo"],
            artifacts_root=env["artifacts_root"],
            dsn=env["dsn"],
            models=make_router(
                db=env["db"],
                artifacts_root=env["artifacts_root"],
                run_id=env["run"].run_id,
                project_id=PROJECT_ID,
                answers={},
            ),
            permitted=(),
        )
        outcome = run_coding_task(
            env["state"], context, {"rationale": "Implement add."}
        )
    finally:
        coding._controller = original  # type: ignore[assignment]

    assert outcome.ok, outcome.detail
    return RunStore.open(outcome.data["automation_run_id"]).load()
