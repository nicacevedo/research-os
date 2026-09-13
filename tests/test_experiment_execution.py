"""Running an experiment, ingesting what it produced, and refusing what it costs.

Real subprocesses, real files, real digests. What is checked is the whole chain:
a command that was declared runs, a command that was not cannot, the results are
identified by content, the declared checks decide whether the packet is usable,
and every refusal says which limit it was.

The packet is a candidate throughout. Nothing in these tests -- and nothing in
the code they exercise -- creates capsule Evidence or marks anything accepted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from research_os.automation.filescope import outbound_symlinks
from research_os.errors import (
    ExperimentAuthorizationError,
    ExperimentConfigError,
)
from research_os.experiment.config import (
    ExecutionLimits,
    ExperimentConfig,
    ProjectExperiments,
    SlurmSettings,
    load_config,
)
from research_os.experiment.controller import (
    AUTHORIZED_EXPLICIT,
    ExperimentBudget,
    ExperimentController,
)
from research_os.experiment.models import ExecutionState
from research_os.experiment.report import render_run
from research_os.experiment.spec import CommandSpec
from research_os.experiment.store import ExperimentStore

PROJECT_ID = "widget-study"


@pytest.fixture
def research_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "xdg"
    for name, subdirectory in (
        ("RESEARCH_OS_CONFIG_HOME", "config"),
        ("RESEARCH_OS_DATA_HOME", "data"),
        ("RESEARCH_OS_CACHE_HOME", "cache"),
        ("RESEARCH_OS_STATE_HOME", "state"),
    ):
        path = root / subdirectory
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    return root / "state"


def git_project(path: Path, *, script: str) -> Path:
    """A committed Git project whose experiment is a small Python program."""

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "tests"], cwd=path, check=True)
    (path / "fit.py").write_text(script, encoding="utf-8")
    (path / "README.md").write_text("# widget\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)
    return path


SUCCESS_SCRIPT = """\
import json, pathlib, sys
seed = int(sys.argv[sys.argv.index("--seed") + 1])
pathlib.Path("results").mkdir(exist_ok=True)
pathlib.Path("results/fit.json").write_text(json.dumps({"seed": seed, "rmse": 0.1}))
print("fit complete")
"""

FAILING_SCRIPT = """\
import sys
print("something went wrong", file=sys.stderr)
raise SystemExit(3)
"""

SILENT_SCRIPT = """\
print("I claim success but write nothing")
"""

SLOW_SCRIPT = """\
import time
time.sleep(30)
"""

STRAY_SCRIPT = """\
import json, pathlib
pathlib.Path("results").mkdir(exist_ok=True)
pathlib.Path("results/fit.json").write_text(json.dumps({"ok": True}))
pathlib.Path("results/undeclared.txt").write_text("nobody declared me")
"""

BROKEN_JSON_SCRIPT = """\
import pathlib
pathlib.Path("results").mkdir(exist_ok=True)
pathlib.Path("results/fit.json").write_text("this is not json{")
"""


def command(
    *,
    name: str = "fit-model",
    argv: list[str] | None = None,
    outputs: list[str] | None = None,
    checks: list[str] | None = None,
    timeout_seconds: int = 60,
    executor: str = "local",
) -> CommandSpec:
    return CommandSpec.model_validate(
        {
            "name": name,
            "argv": argv or ["python3", "fit.py", "--seed", "{seed}"],
            "parameters": [
                {"name": "seed", "type": "integer", "required": True, "minimum": 0}
            ],
            "outputs": outputs if outputs is not None else ["results/fit.json"],
            "checks": checks if checks is not None else ["outputs_exist"],
            "timeout_seconds": timeout_seconds,
            "executor": executor,
        }
    )


def config(
    *,
    commands: list[CommandSpec] | None = None,
    limits: ExecutionLimits | None = None,
    slurm: SlurmSettings | None = None,
) -> ExperimentConfig:
    declared = commands or [command()]
    return ExperimentConfig(
        slurm=slurm or SlurmSettings(),
        limits=limits or ExecutionLimits(),
        projects={
            PROJECT_ID: ProjectExperiments(
                commands={item.name: item for item in declared}
            )
        },
        source=None,
    )


# -- a successful run ---------------------------------------------------------


def test_a_declared_experiment_runs_and_its_results_are_identified_by_content(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())

    store, run, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert run.state is ExecutionState.COMPLETED
    assert run.exit_code == 0
    assert run.authorized_by == AUTHORIZED_EXPLICIT
    assert packet is not None and packet.usable
    result = next(item for item in run.artifacts if item.path == "results/fit.json")
    assert len(result.sha256) == 64
    assert result.byte_size > 0
    assert (project / "results" / "fit.json").exists()
    assert store.packet_file.is_file()


def test_the_run_records_what_it_ran_and_from_which_commit(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())

    _, run, _ = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert run.argv == ["python3", "fit.py", "--seed", "7"]
    assert run.parameters == {"seed": "7"}
    assert run.base_commit is not None and len(run.base_commit) == 40
    assert run.config_digest


def test_wall_clock_is_measured_and_unobservable_fields_stay_unknown(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())

    _, run, _ = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert run.usage.wall_clock_seconds is not None
    assert run.usage.wall_clock_seconds >= 0
    assert run.usage.observed_by
    assert run.usage.gpu_count is None, "nothing measured GPUs, so nothing claims to"
    assert "unknown" in render_run(run)


# -- what did not go well -----------------------------------------------------


def test_a_failing_command_produces_an_unusable_packet(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=FAILING_SCRIPT)
    controller = ExperimentController(config=config())

    _, run, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert run.state is ExecutionState.FAILED
    assert run.exit_code == 3
    assert packet is not None and not packet.usable
    assert packet.blocking_notes


def test_a_command_that_claims_success_but_writes_nothing_is_not_usable(
    research_home: Path, tmp_path: Path
) -> None:
    """Exit zero is not a result if the declared output does not exist."""

    project = git_project(tmp_path / "project", script=SILENT_SCRIPT)
    controller = ExperimentController(config=config())

    _, run, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert run.exit_code == 0
    assert run.missing_outputs == ["results/fit.json"]
    assert packet is not None and not packet.usable
    assert any("not produced" in note for note in packet.blocking_notes)


def test_a_run_that_exceeds_its_timeout_is_stopped_and_says_so(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SLOW_SCRIPT)
    controller = ExperimentController(
        config=config(commands=[command(timeout_seconds=1)])
    )

    _, run, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert run.state is ExecutionState.TIMED_OUT
    assert "timeout" in (run.failure_reason or "")
    assert packet is not None and not packet.usable


def test_an_undeclared_write_is_reported_rather_than_hidden(
    research_home: Path, tmp_path: Path
) -> None:
    """It is how a result quietly comes to depend on a file nobody tracked."""

    project = git_project(tmp_path / "project", script=STRAY_SCRIPT)
    controller = ExperimentController(
        config=config(
            commands=[command(argv=["python3", "fit.py", "--seed", "{seed}"])]
        )
    )

    _, run, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert "results/undeclared.txt" in run.undeclared_artifacts
    assert packet is not None
    assert any("did not declare" in note for note in packet.notes)
    assert packet.usable, "an undeclared write is reported, not a failure"


def test_a_declared_json_output_that_does_not_parse_fails_its_check(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=BROKEN_JSON_SCRIPT)
    controller = ExperimentController(
        config=config(
            commands=[
                command(
                    argv=["python3", "fit.py", "--seed", "{seed}"],
                    checks=["outputs_exist", "outputs_are_json"],
                )
            ]
        )
    )

    _, _, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert packet is not None and not packet.usable
    json_check = next(item for item in packet.checks if item.name == "outputs_are_json")
    assert not json_check.passed


def test_a_check_this_build_does_not_implement_fails_rather_than_being_skipped(
    research_home: Path, tmp_path: Path
) -> None:
    """A guarantee nobody provides must not look as though it held."""

    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(
        config=config(commands=[command(checks=["statistical_significance"])])
    )

    _, _, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert packet is not None and not packet.usable
    assert any(
        "not a check this build implements" in item.detail for item in packet.checks
    )


# -- what may not run ---------------------------------------------------------


def test_nothing_runs_without_explicit_authorisation(
    research_home: Path, tmp_path: Path
) -> None:
    """A literature question and a cluster job must not look the same."""

    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())

    with pytest.raises(ExperimentAuthorizationError, match="--execute"):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=False,
        )

    assert not (project / "results").exists()
    assert ExperimentStore.list_run_ids() == ()


def test_a_command_nobody_declared_cannot_be_run(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())

    with pytest.raises(ExperimentConfigError, match="declares no experiment command"):
        controller.run(
            project_path=project,
            task_name="rm-rf-slash",
            worktree=project,
            project_id=PROJECT_ID,
            execute=True,
        )


def test_a_project_with_nothing_declared_says_where_to_declare_it(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())

    with pytest.raises(ExperimentConfigError, match="Declare them in"):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            project_id="some-other-project",
            execute=True,
        )


def test_a_run_that_has_spent_its_local_budget_is_refused(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(
        config=config(limits=ExecutionLimits(max_local_runs_per_run=1)),
        budget=ExperimentBudget(local_runs=1),
    )

    with pytest.raises(ExperimentAuthorizationError, match="its limit"):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=True,
        )


def test_a_command_asking_for_more_wall_clock_than_allowed_is_refused(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(
        config=config(
            commands=[command(timeout_seconds=7200)],
            limits=ExecutionLimits(max_wall_clock_seconds=60),
        )
    )

    with pytest.raises(ExperimentAuthorizationError, match="wall clock"):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=True,
        )


def test_a_worktree_with_a_symlink_out_is_refused_before_anything_runs(
    research_home: Path, tmp_path: Path
) -> None:
    """A declared output could otherwise land outside the checkout invisibly."""

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.json").write_text("{}", encoding="utf-8")
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    (project / "escape.json").symlink_to(outside / "target.json")
    controller = ExperimentController(config=config())

    from research_os.errors import SymlinkScopeError

    with pytest.raises(SymlinkScopeError):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=True,
        )


def test_execution_can_be_allowed_without_a_flag_and_the_run_records_that(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(
        config=config(limits=ExecutionLimits(require_explicit_execute=False))
    )

    _, run, _ = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=False,
    )

    assert run.state is ExecutionState.COMPLETED
    assert "require_explicit_execute is false" in run.authorized_by


def test_a_preview_runs_nothing_and_records_nothing(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())

    resolved = controller.preview(
        project_id=PROJECT_ID, task_name="fit-model", parameters={"seed": 7}
    )

    assert resolved.argv == ("python3", "fit.py", "--seed", "7")
    assert ExperimentStore.list_run_ids() == ()
    assert not (project / "results").exists()


# -- the record ---------------------------------------------------------------


def test_everything_is_archived_in_the_run_store(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())

    store, run, _ = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    reopened = ExperimentStore.open(run.run_id)
    assert reopened.load().run_id == run.run_id
    assert reopened.load_packet() is not None
    assert store.path("logs", "stdout.txt").is_file()
    events = [item["event"] for item in store.iter_events()]
    assert events == [
        "experiment_prepared",
        "experiment_executed",
        "evidence_packet_built",
    ]


def test_the_report_never_calls_a_packet_evidence(
    research_home: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())

    _, run, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    rendered = render_run(run, packet)
    assert "NOT capsule Evidence, NOT accepted" in rendered
    assert "Research OS does not create Evidence" in rendered
    assert "contains no" in rendered


def test_configuration_is_read_from_outside_every_worktree(
    research_home: Path, tmp_path: Path
) -> None:
    """A worker confined to a worktree cannot reach the file that declares commands."""

    from research_os.experiment.config import config_path

    target = config_path()
    assert target.parent == research_home.parent / "config"
    assert tmp_path / "project" not in target.parents

    target.write_text(
        "schema_version: 1\n"
        "projects:\n"
        f"  {PROJECT_ID}:\n"
        "    commands:\n"
        "      noop:\n"
        "        name: noop\n"
        "        argv: [python3, -c, pass]\n",
        encoding="utf-8",
    )
    loaded = load_config()

    assert loaded.command(PROJECT_ID, "noop").argv == ["python3", "-c", "pass"]
    assert loaded.source == target


# -- isolation, which every earlier test in this file opted out of ------------


def test_an_experiment_runs_in_an_isolated_worktree_by_default(
    research_home: Path, tmp_path: Path
) -> None:
    """Found by an independent reviewer: every caller passed the checkout.

    Each test above supplies ``worktree=project``, so the isolation property was
    never exercised by any of them. This one supplies nothing, which is what the
    CLI and the research controller now do.
    """

    project = git_project(tmp_path / "widget", script=SUCCESS_SCRIPT)
    before = {
        item.relative_to(project).as_posix(): item.read_bytes()
        for item in sorted(project.rglob("*"))
        if item.is_file() and ".git" not in item.relative_to(project).parts
    }

    controller = ExperimentController(config=config())
    store, run, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert run.state is ExecutionState.COMPLETED
    assert packet is not None and packet.usable
    assert run.isolated is True
    assert run.branch

    worktree = Path(run.worktree_path or "")
    assert worktree.is_dir()
    assert project.resolve() not in worktree.resolve().parents
    assert worktree != project

    # The result was written in the worktree, and the checkout is untouched.
    assert (worktree / "results" / "fit.json").is_file()
    assert not (project / "results").exists()
    after = {
        item.relative_to(project).as_posix(): item.read_bytes()
        for item in sorted(project.rglob("*"))
        if item.is_file() and ".git" not in item.relative_to(project).parts
    }
    assert after == before
    assert store.load().artifacts


def test_a_project_with_a_virtualenv_is_not_refused(
    research_home: Path, tmp_path: Path
) -> None:
    """The functional half of the same defect, and the one a user hits first.

    An interpreter symlink in ``.venv/bin`` points out of the repository. When
    the containment scan covered the whole checkout, every ordinary Python
    project was refused -- after the run record had already been written.
    """

    project = git_project(tmp_path / "widget", script=SUCCESS_SCRIPT)
    venv = project / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to("/usr/bin/python3")
    assert outbound_symlinks(project), "the fixture must reproduce the condition"

    controller = ExperimentController(config=config())
    _store, run, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )
    assert run.state is ExecutionState.COMPLETED
    assert packet is not None and packet.usable


def test_supplying_a_worktree_is_recorded_as_not_isolated(
    research_home: Path, tmp_path: Path
) -> None:
    """The override stays available, and the record says it was used."""

    project = git_project(tmp_path / "widget", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())
    _store, run, _packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )
    assert run.isolated is False
    assert run.worktree_path == str(project)


def test_an_experiment_worktree_can_be_released(
    research_home: Path, tmp_path: Path
) -> None:
    """Making experiments isolated created something that needed cleaning up.

    An independent reviewer measured the consequence of not adding it: runs
    leaving permanent branches and orphaned lock files that neither ``doctor``
    nor ``storage --reclaim`` could see.
    """

    from research_os.automation.worktree import lock_path as worktree_lock_path

    project = git_project(tmp_path / "widget", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())
    store, run, _packet = controller.run(
        project_path=project,
        task_name="fit-model",
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )
    worktree = Path(run.worktree_path or "")
    assert worktree.is_dir()
    assert worktree_lock_path(worktree).exists()

    _run, removed = controller.cleanup(store)
    assert removed == (str(worktree),)
    assert not worktree.exists()
    assert not worktree_lock_path(worktree).exists(), "the lock file leaked"

    # The branch is kept: it holds the tree the experiment ran in.
    branches = subprocess.run(
        ["git", "branch", "--list", run.branch or ""],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert (run.branch or "") in branches

    # Cleaning up twice is not an error.
    _run, again = controller.cleanup(store)
    assert again == ()


def test_storage_sees_and_reclaims_an_experiment_worktree(
    research_home: Path, tmp_path: Path
) -> None:
    from research_os import diagnostics

    project = git_project(tmp_path / "widget", script=SUCCESS_SCRIPT)
    controller = ExperimentController(config=config())
    _store, run, _packet = controller.run(
        project_path=project,
        task_name="fit-model",
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )
    worktree = Path(run.worktree_path or "")

    measured = {item.name: item for item in diagnostics.storage_usage()}
    assert measured["worktrees"].reclaimable > 0, "invisible to storage"

    result = diagnostics.reclaim()
    assert run.run_id in result.run_ids
    assert not result.failures
    assert not worktree.exists()
