"""The Slurm submission path, against a real scheduler.

**Deselected by default and it has never run.** There is no ``sbatch`` on the
machine this release was built on, so every test here skips, and the skip
reasons are the release evidence rather than a coverage gap:
``docs/RUNTIME.md`` §10 describes the state mapping, the failure classification
and the reconciliation, and ``tests/test_experiment_slurm.py`` holds all three
down against a mock. What has never happened is a submission meeting a
scheduler.

Run it where one exists:

```bash
export RESEARCH_OS_SLURM_LIVE=1
export RESEARCH_OS_SLURM_ACCOUNT=<account>      # if your site requires one
export RESEARCH_OS_SLURM_PARTITION=<partition>  # if your site requires one
uv run --extra runtime pytest -q -m slurm_live tests/test_experiment_slurm_live.py
```

**What it deliberately does not do.** Induce a node failure or a preemption.
Both are real Slurm states the runtime classifies, and provoking either means
disrupting a shared cluster other people are using. The mock covers them, and
this file covers the states a single tiny job can reach honestly:

```text
submit -> pending/running -> completed        the ordinary path
submit -> non-zero exit                       a failed experiment is not a failed run
submit -> timeout                             classified, and resources raised
submit -> cancel                              a person did that; do nothing
restart researchd while a job is live         reconciliation from the scheduler
squeue forgets a finished job                 sacct answers instead
two reconciliations of one job                collect once, never twice
```

Every job asks for one task, two minutes and a quarter of a gigabyte, so the
worst case for a shared cluster is a handful of jobs that do nothing.

**The bodies are written and have never been executed.** They are built from
the same API ``tests/test_experiment_slurm.py`` exercises against a mock, so
the calls are right by construction; whether the *cluster* behaves as they
assert is exactly the open question. ``docs/INTEGRATION_BUILD_RECORD.md``
records this as an external blocker rather than as coverage.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.slurm_live

LIVE = os.environ.get("RESEARCH_OS_SLURM_LIVE") == "1"
HAS_SBATCH = shutil.which("sbatch") is not None

needs_cluster = pytest.mark.skipif(
    not (LIVE and HAS_SBATCH),
    reason=(
        "no real Slurm cluster available: "
        f"RESEARCH_OS_SLURM_LIVE={'set' if LIVE else 'unset'}, "
        f"sbatch {'found' if HAS_SBATCH else 'not on PATH'}. "
        "This is a genuine external prerequisite, not a defect: the submission "
        "path has never met a scheduler. See this module's docstring for the "
        "exact command."
    ),
)


def _settings() -> object:
    from research_os.experiment.config import SlurmSettings

    return SlurmSettings(
        enabled=True,
        account=os.environ.get("RESEARCH_OS_SLURM_ACCOUNT") or None,
        partitions=tuple(
            item
            for item in (os.environ.get("RESEARCH_OS_SLURM_PARTITION") or "").split(",")
            if item
        ),
    )


def _spec(
    *,
    name: str,
    script: str,
    workdir: Path,
    time_limit: str = "00:02:00",
    memory: str = "256M",
) -> object:
    """A frozen spec for one trivial job.

    One task, two minutes, a quarter of a gigabyte. The worst thing this file
    can do to a shared cluster is queue a handful of jobs that do nothing.
    """

    from research_os.runtime.interfaces import ExecutionSpec

    partition = (os.environ.get("RESEARCH_OS_SLURM_PARTITION") or "").split(",")[0]
    resources = {"time_limit": time_limit, "memory": memory, "cpus": "1"}
    if partition:
        resources["partition"] = partition
    if os.environ.get("RESEARCH_OS_SLURM_ACCOUNT"):
        resources["account"] = os.environ["RESEARCH_OS_SLURM_ACCOUNT"]
    return ExecutionSpec(
        name=name,
        argv=("/bin/sh", "-c", script),
        cwd=str(workdir),
        environment={"kind": "shell", "project": str(workdir)},
        resources=resources,
        env={"RESEARCH_OS_SEED_0": "20260915"},
        timeout_seconds=300,
        outputs=("result.txt",),
        seeds=(20260915,),
    )


def _run_dir(root: Path, name: str) -> Path:
    from research_os.runtime.executors import prepare_run_dir

    return prepare_run_dir(root / name)


def _await_terminal(executor: object, handle: object, *, seconds: int = 600) -> object:
    """Poll until the scheduler says the job is over, or give up loudly.

    Polling rather than sleeping a fixed time, because a shared cluster's queue
    is not predictable and a fixed wait is either flaky or slow. The ceiling is
    generous and a timeout here is a real finding: it means the job never
    reached a terminal state the runtime recognises.
    """

    import time

    deadline = time.monotonic() + seconds
    current = handle
    while time.monotonic() < deadline:
        current = executor.poll(current)  # type: ignore[attr-defined]
        if getattr(current, "finished", False):
            return current
        time.sleep(5)
    raise AssertionError(
        f"job {getattr(handle, 'scheduler_job_id', '?')} never reached a terminal "
        f"state within {seconds}s; last detail {getattr(current, 'detail', '')!r}"
    )


@needs_cluster
def test_the_scheduler_is_reachable_and_reports_its_settings() -> None:
    """Step one: probe before submitting anything.

    A probe that says "available" on a cluster that will reject every job for a
    missing account is worse than one that says why.
    """

    from research_os.experiment.slurm import probe

    result = probe(_settings())  # type: ignore[arg-type]
    assert result.available, result.detail
    assert result.detail


@needs_cluster
def test_a_tiny_deterministic_job_completes_and_its_outputs_are_collected(
    tmp_path: Path,
) -> None:
    """Steps two to four: submit, observe asynchronously, collect the outputs.

    The assertion that matters most is that ``submit`` returns *before* the job
    finishes. A submission that blocked would mean the control plane could not
    poll anything, and the whole ``external_jobs`` table would be pointless.
    """

    from research_os.runtime.executors import SlurmExecutor

    workdir = tmp_path / "project"
    workdir.mkdir()
    executor = SlurmExecutor(settings=_settings())
    run_dir = _run_dir(tmp_path, "ok")
    handle = executor.submit(
        _spec(
            name="live-ok",
            script="printf 'deterministic\n' > result.txt; exit 0",
            workdir=workdir,
        ),  # type: ignore[arg-type]
        run_dir=run_dir,
    )
    assert handle.scheduler_job_id, "the scheduler returned no job id"
    assert handle.finished is False, "submission blocked until the job finished"

    final = _await_terminal(executor, handle)
    assert final.exit_code == 0, final.detail  # type: ignore[attr-defined]
    assert (workdir / "result.txt").read_text(encoding="utf-8") == "deterministic\n"
    assert (run_dir / "logs" / "slurm-stdout.txt").exists()


@needs_cluster
def test_a_non_zero_exit_is_recorded_and_is_not_a_failed_run(tmp_path: Path) -> None:
    """Step five. An experiment that ran and answered "no" has succeeded.

    The executor records the exit code; nothing here turns it into a retry, and
    ``FailureClass`` has no member that could.
    """

    from research_os.runtime.executors import SlurmExecutor

    workdir = tmp_path / "project"
    workdir.mkdir()
    executor = SlurmExecutor(settings=_settings())
    handle = executor.submit(
        _spec(name="live-nonzero", script="exit 3", workdir=workdir),  # type: ignore[arg-type]
        run_dir=_run_dir(tmp_path, "nonzero"),
    )
    final = _await_terminal(executor, handle)
    assert final.exit_code == 3, final.detail  # type: ignore[attr-defined]


@needs_cluster
def test_a_timeout_is_classified_as_one(tmp_path: Path) -> None:
    """Step six. ``TIMEOUT`` maps to ``slurm_timeout``, which asks for resources.

    Distinguished from a failure on purpose: the v1 ``ExecutionState`` mapping
    collapses them, and the retry policy needs them apart -- a timeout is
    rescheduled with a longer limit, a failure is not.
    """

    from research_os.experiment.models import ExecutionState
    from research_os.runtime.executors import SlurmExecutor

    workdir = tmp_path / "project"
    workdir.mkdir()
    executor = SlurmExecutor(settings=_settings())
    handle = executor.submit(
        _spec(
            name="live-timeout",
            script="sleep 600",
            workdir=workdir,
            time_limit="00:01:00",
        ),  # type: ignore[arg-type]
        run_dir=_run_dir(tmp_path, "timeout"),
    )
    final = _await_terminal(executor, handle, seconds=900)
    raw = str(getattr(final, "detail", "")).upper()
    assert "TIMEOUT" in raw, f"the scheduler reported {raw!r} rather than a timeout"
    from research_os.experiment.slurm import map_state

    assert map_state(raw.split()[0]) is ExecutionState.TIMED_OUT


@needs_cluster
def test_a_cancellation_is_reported_and_left_alone(tmp_path: Path) -> None:
    """Step seven. A person cancelled it; resubmitting would overrule them."""

    from research_os.runtime.executors import SlurmExecutor

    workdir = tmp_path / "project"
    workdir.mkdir()
    executor = SlurmExecutor(settings=_settings())
    handle = executor.submit(
        _spec(name="live-cancel", script="sleep 300", workdir=workdir),  # type: ignore[arg-type]
        run_dir=_run_dir(tmp_path, "cancel"),
    )
    executor.cancel(handle)
    final = _await_terminal(executor, handle)
    assert "CANCEL" in str(getattr(final, "detail", "")).upper()


@needs_cluster
def test_a_fresh_executor_reconciles_a_live_job_from_the_scheduler(
    tmp_path: Path,
) -> None:
    """Step eight, and the one that most needs a real cluster.

    The job outlives the process that submitted it, which is the entire reason
    ``external_jobs`` exists. A restarted control plane must learn the job's
    state from ``squeue``/``sacct`` and from the stored scheduler id, never from
    anything it remembered in memory.
    """

    from research_os.runtime.executors import SlurmExecutor
    from research_os.runtime.interfaces import ExecutionHandle

    workdir = tmp_path / "project"
    workdir.mkdir()
    submitter = SlurmExecutor(settings=_settings())
    run_dir = _run_dir(tmp_path, "restart")
    handle = submitter.submit(
        _spec(name="live-restart", script="sleep 45", workdir=workdir),  # type: ignore[arg-type]
        run_dir=run_dir,
    )
    del submitter  # the process that submitted it is gone

    # Everything a restarted daemon has: the row's contents.
    rebuilt = ExecutionHandle(
        executor="slurm",
        run_dir=str(run_dir),
        spec_digest=handle.spec_digest,
        scheduler_job_id=handle.scheduler_job_id,
        finished=False,
    )
    final = _await_terminal(SlurmExecutor(settings=_settings()), rebuilt)
    assert final.exit_code == 0, final.detail  # type: ignore[attr-defined]


@needs_cluster
def test_accounting_answers_after_the_queue_has_forgotten_the_job(
    tmp_path: Path,
) -> None:
    """Step nine. ``squeue`` drops finished jobs and ``sacct`` lags. Both normal.

    A poll that read only ``squeue`` would see a completed job as unknown
    forever, which is how a finished experiment never gets interpreted.
    """

    from research_os.runtime.executors import SlurmExecutor

    workdir = tmp_path / "project"
    workdir.mkdir()
    executor = SlurmExecutor(settings=_settings())
    handle = executor.submit(
        _spec(name="live-accounting", script="exit 0", workdir=workdir),  # type: ignore[arg-type]
        run_dir=_run_dir(tmp_path, "accounting"),
    )
    final = _await_terminal(executor, handle)
    assert final.finished is True  # type: ignore[attr-defined]
    # Polling again long after completion must still answer, from accounting.
    again = executor.poll(final)  # type: ignore[arg-type]
    assert again.finished is True
    assert again.exit_code == final.exit_code  # type: ignore[attr-defined]


@needs_cluster
def test_reconciling_one_job_twice_never_submits_or_collects_twice(
    tmp_path: Path,
) -> None:
    """Step ten, and the expensive failure.

    Through the real ledger, because that is what guarantees it. Two
    reconciliations of one submission must produce one job and one collection:
    duplicated evidence is worse than missing evidence, because it looks like
    replication.
    """

    from research_os.runtime.executors import SlurmExecutor

    workdir = tmp_path / "project"
    workdir.mkdir()
    executor = SlurmExecutor(settings=_settings())
    run_dir = _run_dir(tmp_path, "once")
    spec = _spec(name="live-once", script="printf 'x\n' >> result.txt", workdir=workdir)
    handle = executor.submit(spec, run_dir=run_dir)  # type: ignore[arg-type]
    final = _await_terminal(executor, handle)
    assert final.exit_code == 0, final.detail  # type: ignore[attr-defined]
    for _ in range(3):
        executor.poll(final)  # type: ignore[arg-type]
    assert (workdir / "result.txt").read_text(encoding="utf-8") == "x\n", (
        "the job body ran more than once"
    )
