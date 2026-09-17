"""Failure injection. The runtime is not durable until it has been attacked.

Each test names a way the world breaks and asserts the invariant that must
survive it. The invariants, from `docs/RUNTIME.md`:

```text
no corrupted scientific state
no duplicate scientific side effect
recoverable work is recovered
nonrecoverable work is clearly classified
human authority cannot be bypassed
provenance remains complete
```

Where a crash is claimed, a real process is killed with ``os._exit`` or
``SIGKILL``. A simulated exception runs the ``finally`` blocks whose absence is
the failure mode, so it would assert nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.artifacts import ArtifactMissingError, FilesystemArtifactStore
from research_os.runtime.budgets import BudgetExhaustedError, BudgetLedger, Dimension
from research_os.runtime.checkpoints import ensure_tables
from research_os.runtime.clock import FrozenClock
from research_os.runtime.daemon import Daemon, WorkKind
from research_os.runtime.db import (
    Database,
    RuntimeDatabaseError,
    TransientDatabaseError,
    classify_db_error,
)
from research_os.runtime.executors import (
    ExecutionHandle,
    LocalExecutor,
    classify_slurm_state,
    job_status_for,
    poll_job,
)
from research_os.runtime.failures import FailureClass, Response, response_for
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.interfaces import ExecutionSpec
from research_os.runtime.locks import RepositoryBusyError, repository_lock
from research_os.runtime.models import (
    BudgetScope,
    ExternalJobStatus,
    RunStatus,
    TerminalState,
    WorkStatus,
)
from research_os.runtime.notify import CollectingNotifier
from research_os.runtime.policy import ActionKind
from research_os.runtime.queue import LeaseLostError, WorkQueue
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    plan_answer,
    review_answer,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BETWEEN_NODES = Path(__file__).parent / "runtime_scripts" / "die_between_nodes.py"


@pytest.fixture
def chaos(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project")
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="alpha-project", repo_path=str(repo))
    notifier = CollectingNotifier()
    config = make_config(pg_dsn, tmp_path / "artifacts")
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.VALIDATE_CAPSULE)),
            "scientific_reviewer": review_answer(),
        }
    )
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "store": store,
        "queue": WorkQueue(runtime_db),
        "config": config,
        "notifier": notifier,
        "router": router,
        "tmp_path": tmp_path,
        "daemon": Daemon(
            config=config,
            db=runtime_db,
            repo_for=lambda _p: repo,
            models=lambda _r, _p, _w: router,
            notifier=notifier,
            clock=FrozenClock(),
            owner="chaos-worker",
        ),
    }


# ------------------------------------------------ the database goes away ----
def test_a_terminated_connection_is_transient_not_a_task_failure(
    chaos: dict[str, Any], pg_dsn: str
) -> None:
    """A dropped connection must be retried, not counted as broken work.

    Simulated with ``pg_terminate_backend``, which is what a server restart
    looks like from a client's point of view.
    """

    victim = Database(pg_dsn)
    victim.ping()
    with victim.tx() as conn:
        pid = conn.execute("select pg_backend_pid() as pid").fetchone()["pid"]
    with chaos["db"].autocommit() as admin:
        admin.execute("select pg_terminate_backend(%s)", (pid,))

    # The pool notices and replaces the connection, so the next call succeeds.
    # What matters is that a failure, if one surfaces, is classified transient.
    try:
        victim.ping()
    except RuntimeDatabaseError as exc:
        assert isinstance(exc, TransientDatabaseError), f"{type(exc).__name__}: {exc}"
    finally:
        victim.close()


def test_driver_errors_are_classified_and_only_some_retry() -> None:
    """Retrying a syntax error forever burns the budget real failures need."""

    import psycopg

    assert isinstance(
        classify_db_error(psycopg.OperationalError("server closed the connection")),
        TransientDatabaseError,
    )
    assert isinstance(
        classify_db_error(psycopg.errors.SerializationFailure("retry me")),
        TransientDatabaseError,
    )
    programming = classify_db_error(psycopg.errors.SyntaxError("nope"))
    assert isinstance(programming, RuntimeDatabaseError)
    assert not isinstance(programming, TransientDatabaseError)
    assert response_for(FailureClass.DATABASE_TRANSIENT) is Response.RETRY_AFTER_BACKOFF


def test_the_loop_survives_the_database_being_unreachable(
    chaos: dict[str, Any],
) -> None:
    """The daemon must wait, not exit, when PostgreSQL is briefly gone."""

    clock = FrozenClock()
    calls: list[int] = []

    class Flaky(Daemon):
        def tick(self) -> Any:  # type: ignore[override]
            calls.append(1)
            if len(calls) == 1:
                raise TransientDatabaseError("server closed the connection")
            return super().tick()

    daemon = Flaky(
        config=chaos["config"],
        db=chaos["db"],
        repo_for=lambda _p: chaos["repo"],
        models=lambda _r, _p, _w: chaos["router"],
        notifier=chaos["notifier"],
        clock=clock,
        owner="flaky-worker",
    )
    daemon.run_forever(max_ticks=2)
    assert len(calls) == 2, "the loop exited on a transient database failure"
    assert clock.slept, "the loop did not back off"


# ------------------------------------------------------ dying mid-graph ----
def test_a_crash_replays_only_the_node_it_happened_in(chaos: dict[str, Any]) -> None:
    """Where the recovery boundary actually is, measured rather than assumed.

    The first version of this test expected a gap "between supersteps" that a
    process could die in without re-running the node that had just finished.
    There is no such gap. With ``durability="sync"`` the checkpoint is written
    *after* the node function returns to LangGraph, so dying at the very end of
    a node -- the last thing the node body does -- still loses that node's
    result and replays it.

    What the checkpoint does guarantee, and what this asserts, is that
    *earlier* nodes are not replayed. ``hydrate_project_state`` completed and
    was checkpointed before ``plan_one_action`` began, and it runs exactly once
    across the crash.

    The consequence is worth stating plainly, because it is a cost rather than a
    bug: a crash in a node that makes a model call means paying for that call
    twice. That is why model calls are replay-safe by design (they leave no
    trace outside the process), why the budget is reserved per call rather than
    per node, and why anything that *does* leave a trace goes through the
    invocation ledger instead.
    """

    store: RuntimeStore = chaos["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    node_log = chaos["tmp_path"] / "nodes.log"

    def child(*, crash_after: str = "", expect: int = 0) -> dict[str, Any]:
        env = dict(os.environ)
        env["NODE_LOG"] = str(node_log)
        env["PYTHONPATH"] = str(REPO_ROOT)
        if crash_after:
            env["CRASH_AFTER_NODE"] = crash_after
        else:
            env.pop("CRASH_AFTER_NODE", None)
        completed = subprocess.run(
            [
                sys.executable,
                str(BETWEEN_NODES),
                "resume",
                chaos["dsn"],
                str(chaos["repo"]),
                str(chaos["config"].artifacts_root),
                run.run_id,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=env,
            cwd=REPO_ROOT,
        )
        assert completed.returncode == expect, completed.stderr
        return (
            json.loads(completed.stdout.strip().splitlines()[-1]) if expect == 0 else {}
        )

    child(crash_after="plan_one_action", expect=9)
    assert node_log.read_text().splitlines() == [
        "hydrate_project_state",
        "plan_one_action",
    ]

    result = child()
    after = node_log.read_text().splitlines()
    assert result["status"] == str(RunStatus.SUCCEEDED)

    # Checkpointed before the crash: not replayed.
    assert after.count("hydrate_project_state") == 1

    # Completed but not yet checkpointed when the process died: replayed once.
    assert after.count("plan_one_action") == 2

    # Everything after the crash point runs exactly once.
    for node in ("validate_plan", "perform_action", "deterministic_check", "conclude"):
        assert after.count(node) == 1, f"{node} ran {after.count(node)} times"


def test_a_replayed_node_does_not_duplicate_a_recorded_side_effect(
    chaos: dict[str, Any],
) -> None:
    """The consequence of the finding above, closed by the ledger.

    ``validate_capsule`` is replay-safe, so the cycle above is harmless. This
    asserts the general protection: an action registered as effectful is
    performed once across a replay, whatever the node does.
    """

    ledger = InvocationLedger(chaos["db"])
    performed: list[int] = []

    def perform() -> dict[str, Any]:
        performed.append(1)
        return {"submitted": True}

    from research_os.runtime.idempotency import idempotency_key

    key = idempotency_key("cycle.submit", "RRUN-x", 0, "slurm")
    ledger.run(key=key, kind="cycle.submit", perform=perform)
    ledger.run(key=key, kind="cycle.submit", perform=perform)
    assert performed == [1]


# ---------------------------------------------------------- git collision ----
def test_two_workers_cannot_mutate_one_repository_at_once(
    chaos: dict[str, Any], pg_dsn: str
) -> None:
    """The second is refused fast rather than blocking a worker slot."""

    with (
        repository_lock(chaos["db"], str(chaos["repo"])),
        Database(pg_dsn) as other,
        pytest.raises(RepositoryBusyError),
        repository_lock(other, str(chaos["repo"]), wait=False),
    ):
        pass


def test_a_repository_collision_is_retryable(chaos: dict[str, Any]) -> None:
    """Contention is not breakage: the item goes back on the queue."""

    assert response_for(FailureClass.GIT_CONFLICT) is Response.REPAIR
    work = (
        chaos["queue"]
        .enqueue(project_id="alpha-project", kind=WorkKind.RUN_CYCLE, max_attempts=3)
        .item
    )
    chaos["queue"].claim(owner="w", lease_seconds=60)
    after = chaos["queue"].fail(
        work.work_id,
        owner="w",
        failure_class=FailureClass.GIT_CONFLICT,
        error="another worker holds the repository",
    )
    assert after.status is WorkStatus.PENDING


# ------------------------------------------------------ derived index loss ----
def test_losing_the_derived_index_loses_no_science(chaos: dict[str, Any]) -> None:
    """Invariant 7, tested by deleting it.

    The literature index is derived from rows it can be rebuilt from, and the
    capsule is untouched by its absence.
    """

    from research_os.literature.store import LiteratureStore

    store = LiteratureStore.open()
    path = store.path
    store.close()
    assert path is not None and path.is_file()

    before = chaos["store"].get_project("alpha-project")
    capsule_before = sorted(
        p.name for p in (chaos["repo"] / ".research").rglob("*.yaml")
    )

    path.unlink()
    assert not path.exists()

    # The capsule is entirely unaffected, and still validates.
    from research_os.runtime.kernel import ScientificKernelAdapter

    report = ScientificKernelAdapter(chaos["repo"]).validate()
    assert report.ok
    assert sorted(p.name for p in (chaos["repo"] / ".research").rglob("*.yaml")) == (
        capsule_before
    )
    assert chaos["store"].get_project("alpha-project") == before

    # And the index comes back.
    rebuilt = LiteratureStore.open()
    assert rebuilt.path is not None and rebuilt.path.is_file()
    rebuilt.close()
    assert (
        response_for(FailureClass.DERIVED_INDEX_CORRUPT)
        is Response.REBUILD_DERIVED_STATE
    )


# --------------------------------------------------------- artifact loss ----
def test_a_missing_artifact_is_named_and_not_retried(chaos: dict[str, Any]) -> None:
    """Retrying will not make the bytes reappear."""

    store = FilesystemArtifactStore(chaos["config"].artifacts_root)
    ref = store.put_text("evidence", role="result")
    store.path_for(ref.artifact_id).unlink()
    with pytest.raises(ArtifactMissingError):
        store.get_bytes(ref.artifact_id)
    assert response_for(FailureClass.ARTIFACT_MISSING) is Response.FAIL_PERMANENTLY


def test_silent_artifact_corruption_is_detected_by_the_audit(
    chaos: dict[str, Any],
) -> None:
    """The file is present and the right length; only the hash disagrees."""

    store = FilesystemArtifactStore(
        chaos["config"].artifacts_root, store=chaos["store"]
    )
    ref = store.put_text("trustworthy", role="result")
    store.path_for(ref.artifact_id).write_text("tamperedXX", encoding="utf-8")

    work = (
        chaos["queue"]
        .enqueue(project_id="alpha-project", kind=WorkKind.INTEGRITY_AUDIT)
        .item
    )
    chaos["daemon"].tick()
    result = chaos["queue"].get(work.work_id)
    assert result is not None
    assert result.status is WorkStatus.SUCCEEDED
    assert ref.artifact_id in (result.result or {}).get("corrupt", [])
    assert any(
        "integrity" in str(sent["subject"]).lower() for sent in chaos["notifier"].sent
    )


# ------------------------------------------------------- executor failure ----
def test_a_local_command_that_does_not_exist_is_reported_not_raised(
    chaos: dict[str, Any],
) -> None:
    spec = ExecutionSpec(
        name="missing", argv=("this-command-does-not-exist",), cwd=str(chaos["repo"])
    )
    handle = LocalExecutor().submit(spec, run_dir=chaos["tmp_path"])
    assert handle.finished
    assert handle.exit_code is None
    assert "not on PATH" in handle.detail


def test_a_local_command_that_times_out_says_so(chaos: dict[str, Any]) -> None:
    run_dir = chaos["tmp_path"] / "timeout-run"
    (run_dir / "logs").mkdir(parents=True)
    spec = ExecutionSpec(
        name="slow",
        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=str(chaos["repo"]),
        timeout_seconds=1,
    )
    handle = LocalExecutor().submit(spec, run_dir=run_dir)
    assert handle.finished
    assert handle.exit_code is None
    assert "timed out" in handle.detail


def test_a_nonzero_exit_is_recorded_without_a_scientific_opinion(
    chaos: dict[str, Any],
) -> None:
    run_dir = chaos["tmp_path"] / "failing-run"
    (run_dir / "logs").mkdir(parents=True)
    spec = ExecutionSpec(
        name="failing",
        argv=(sys.executable, "-c", "raise SystemExit(3)"),
        cwd=str(chaos["repo"]),
    )
    handle = LocalExecutor().submit(spec, run_dir=run_dir)
    assert handle.exit_code == 3
    assert handle.detail == "non-zero exit"


# ------------------------------------------------------------ slurm chaos ----
@pytest.mark.parametrize(
    ("raw", "status", "failure", "response"),
    [
        ("COMPLETED", ExternalJobStatus.COMPLETED, None, None),
        (
            "PREEMPTED",
            ExternalJobStatus.CANCELLED,
            FailureClass.SLURM_PREEMPTED,
            Response.RETRY,
        ),
        (
            "NODE_FAIL",
            ExternalJobStatus.FAILED,
            FailureClass.SLURM_NODE_FAILURE,
            Response.RETRY,
        ),
        (
            "OUT_OF_MEMORY",
            ExternalJobStatus.FAILED,
            FailureClass.SLURM_OUT_OF_MEMORY,
            Response.RESCHEDULE_WITH_MORE_RESOURCES,
        ),
        (
            "TIMEOUT",
            ExternalJobStatus.TIMED_OUT,
            FailureClass.SLURM_TIMEOUT,
            Response.RESCHEDULE_WITH_MORE_RESOURCES,
        ),
        ("CANCELLED by 1234", ExternalJobStatus.CANCELLED, None, None),
        ("SOME_FUTURE_STATE", ExternalJobStatus.UNKNOWN, None, None),
    ],
)
def test_each_slurm_outcome_gets_its_own_treatment(
    raw: str,
    status: ExternalJobStatus,
    failure: FailureClass | None,
    response: Response | None,
) -> None:
    """Preemption, node failure, OOM and timeout are four different problems.

    The v1 ExecutionState mapping collapses them, which is right for reporting
    and wrong for deciding what to do next. A resubmitted OOM fails the same
    way; a resubmitted preemption usually does not.
    """

    assert job_status_for(raw) is status
    assert classify_slurm_state(raw) is failure
    if failure is not None:
        assert response_for(failure) is response


def test_a_mocked_preemption_reconciles_through_the_control_plane(
    chaos: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole poll path, with a fake scheduler.

    No Slurm on this machine, so the executor is replaced. What is being tested
    is the reconciliation -- that a preempted job lands in the job row with the
    right status and failure class, and emits the finished event that resumes
    its cycle.
    """

    store: RuntimeStore = chaos["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    job = store.create_external_job(
        project_id="alpha-project",
        run_id=run.run_id,
        executor="slurm",
        spec_digest="d" * 64,
        run_dir=str(chaos["repo"]),
    )
    store.update_external_job(
        job.job_id, status=ExternalJobStatus.RUNNING, scheduler_job_id="4711"
    )

    class PreemptingSlurm:
        name = "slurm"

        def poll(self, handle: ExecutionHandle) -> ExecutionHandle:
            return ExecutionHandle(
                executor="slurm",
                run_dir=handle.run_dir,
                spec_digest=handle.spec_digest,
                scheduler_job_id=handle.scheduler_job_id,
                finished=True,
                exit_code=None,
                detail="PREEMPTED",
            )

    monkeypatch.setattr(
        "research_os.runtime.executors.build_executors",
        lambda config, project_id=None: {"slurm": PreemptingSlurm()},
    )
    polled = poll_job(
        store.list_external_jobs(run_id=run.run_id)[0],
        store=store,
        config=chaos["config"],
    )
    assert polled.status is ExternalJobStatus.CANCELLED
    assert polled.failure_class == FailureClass.SLURM_PREEMPTED
    assert polled.finished_at is not None


def test_an_unreachable_scheduler_does_not_fail_the_job(
    chaos: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "We could not ask" is not "the job failed"."""

    store: RuntimeStore = chaos["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    job = store.create_external_job(
        project_id="alpha-project",
        run_id=run.run_id,
        executor="slurm",
        spec_digest="d" * 64,
        run_dir=str(chaos["repo"]),
    )
    store.update_external_job(
        job.job_id, status=ExternalJobStatus.RUNNING, scheduler_job_id="4711"
    )
    monkeypatch.setattr(
        "research_os.runtime.executors.build_executors",
        lambda config, project_id=None: {},
    )
    polled = poll_job(
        store.list_external_jobs(run_id=run.run_id)[0],
        store=store,
        config=chaos["config"],
    )
    assert polled.status is ExternalJobStatus.RUNNING
    assert "could not be reached" in (polled.detail or "")
    assert polled.finished_at is None


# ------------------------------------------------------ budget exhaustion ----
def test_an_exhausted_budget_ends_the_cycle_in_a_named_state(
    chaos: dict[str, Any],
) -> None:
    """A budget that retries is not a budget."""

    from research_os.runtime.cycles import resume_cycle

    store: RuntimeStore = chaos["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    ledger = BudgetLedger(chaos["db"])
    ledger.set_limit(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_CALLS,
        limit_value=0,
    )
    result = resume_cycle(
        config=chaos["config"],
        db=chaos["db"],
        run_id=run.run_id,
        repo_path=chaos["repo"],
        models=chaos["router"],
    )
    assert result.terminal_state is TerminalState.BUDGET_EXHAUSTED
    assert result.recommendation == "BUDGET_EXHAUSTED"
    assert chaos["router"].requests == [], "a model was called with no budget"
    assert response_for(FailureClass.BUDGET_EXHAUSTED) is Response.FAIL_PERMANENTLY


def test_an_exhausted_budget_stops_continuation(chaos: dict[str, Any]) -> None:
    """A chain must not continue into a cycle that cannot finish."""

    from research_os.runtime.cycles import CycleResult, should_continue

    store: RuntimeStore = chaos["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    BudgetLedger(chaos["db"]).set_limit(
        scope=BudgetScope.PROJECT,
        scope_id="alpha-project",
        dimension=Dimension.MODEL_COST_USD,
        limit_value=0,
    )
    proceed, why = should_continue(
        db=chaos["db"],
        config=chaos["config"],
        result=CycleResult(
            run=run,
            status=RunStatus.SUCCEEDED,
            terminal_state=TerminalState.DONE_FOR_NOW,
            pending_approval_id=None,
            recommendation="START_NEXT_CYCLE",
            notes=(),
            state={},
        ),
    )
    assert proceed is False
    assert "budget exhausted beyond this cycle" in why


def test_a_run_budget_exhaustion_does_not_stop_the_next_cycle(
    chaos: dict[str, Any],
) -> None:
    """Per-cycle budgets are meant to be spent; the successor gets its own."""

    from research_os.runtime.cycles import CycleResult, should_continue

    store: RuntimeStore = chaos["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    BudgetLedger(chaos["db"]).set_limit(
        scope=BudgetScope.RUN,
        scope_id=run.run_id,
        dimension=Dimension.MODEL_CALLS,
        limit_value=0,
    )
    proceed, _why = should_continue(
        db=chaos["db"],
        config=chaos["config"],
        result=CycleResult(
            run=run,
            status=RunStatus.SUCCEEDED,
            terminal_state=TerminalState.DONE_FOR_NOW,
            pending_approval_id=None,
            recommendation="START_NEXT_CYCLE",
            notes=(),
            state={},
        ),
    )
    assert proceed is True


# --------------------------------------------------- authority under chaos ----
def test_a_crash_cannot_produce_an_approved_decision(chaos: dict[str, Any]) -> None:
    """Human authority must not be reachable by breaking things.

    A pending approval stays pending through a reclaimed lease, an abandoned
    invocation and a released reservation. Nothing in recovery grants it.
    """

    store: RuntimeStore = chaos["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    approval, _ = store.request_approval(
        run_id=run.run_id,
        project_id="alpha-project",
        kind="accept_claim",
        question="Authorise accept_claim?",
        packet={"decision_required": "accept_claim"},
        interrupt_key="k",
    )
    ledger = InvocationLedger(chaos["db"])
    ledger._claim(
        key="k1",
        kind="cycle.accept_claim",
        request={},
        run_id=run.run_id,
        work_id=None,
        owner="ghost",
    )
    work = (
        chaos["queue"]
        .enqueue(
            project_id="alpha-project", kind=WorkKind.RESUME_CYCLE, run_id=run.run_id
        )
        .item
    )
    chaos["queue"].claim(owner="ghost", lease_seconds=60)
    with chaos["db"].tx() as conn:
        conn.execute("update work_items set lease_expires_at = now() - interval '1s'")
        conn.execute(
            "update tool_invocations set started_at = now() - interval '2 hours'"
        )

    report = chaos["daemon"].tick()
    assert report.leases_reclaimed == 1
    assert report.invocations_abandoned == 1

    unchanged = store.get_approval(approval.approval_id)
    assert unchanged is not None
    assert unchanged.status.value == "PENDING"
    assert unchanged.decided_at is None
    assert unchanged.decided_by is None
    assert store.mark_approval_applied(approval.approval_id) is False, (
        "an undecided approval must not be appliable"
    )
    assert work.work_id


def test_no_recovery_path_writes_a_capsule_file(chaos: dict[str, Any]) -> None:
    """The strongest invariant, checked by comparing the files byte for byte."""

    import hashlib

    def capsule_digest() -> dict[str, str]:
        return {
            str(path.relative_to(chaos["repo"])): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted((chaos["repo"] / ".research").rglob("*"))
            if path.is_file()
        }

    before = capsule_digest()
    store: RuntimeStore = chaos["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    chaos["queue"].enqueue(
        project_id="alpha-project", kind=WorkKind.RUN_CYCLE, run_id=run.run_id
    )
    for _ in range(4):
        chaos["daemon"].tick()
    ledger = BudgetLedger(chaos["db"])
    ledger.reconcile_stale(older_than_seconds=0)
    InvocationLedger(chaos["db"]).abandon_stale(older_than_seconds=0)
    chaos["queue"].reclaim_expired()

    assert capsule_digest() == before, "the runtime changed canonical scientific state"


# ------------------------------------------------ poison work terminates ----
def test_work_that_kills_its_worker_runs_out_of_attempts(chaos: dict[str, Any]) -> None:
    """Otherwise a poison task is claimed forever by a succession of victims."""

    queue: WorkQueue = chaos["queue"]
    work = queue.enqueue(
        project_id="alpha-project", kind=WorkKind.RUN_CYCLE, max_attempts=2
    ).item
    for attempt in range(2):
        claimed = queue.claim(owner=f"victim-{attempt}", lease_seconds=60)
        assert len(claimed) == 1
        with chaos["db"].tx() as conn:
            conn.execute(
                "update work_items set lease_expires_at = now() - interval '1s'"
            )
        queue.reclaim_expired()
    final = queue.get(work.work_id)
    assert final is not None
    assert final.status is WorkStatus.FAILED
    assert final.failure_class == FailureClass.WORKER_CRASH
    assert queue.claim(owner="another", lease_seconds=60) == ()


def test_a_lease_lost_mid_work_discards_the_result_not_the_work(
    chaos: dict[str, Any],
) -> None:
    """Whatever the slow worker did is durable and reusable by the new owner."""

    queue: WorkQueue = chaos["queue"]
    work = queue.enqueue(project_id="alpha-project", kind=WorkKind.RUN_CYCLE).item
    queue.claim(owner="slow", lease_seconds=60)
    with chaos["db"].tx() as conn:
        conn.execute("update work_items set lease_expires_at = now() - interval '1s'")
    queue.reclaim_expired()
    queue.claim(owner="fast", lease_seconds=60)
    with pytest.raises(LeaseLostError):
        queue.succeed(work.work_id, owner="slow", result={"stale": True})
    assert queue.succeed(work.work_id, owner="fast", result={"fresh": True}).result == {
        "fresh": True
    }


# ------------------------------------------------------- provider failure ----
def test_a_provider_timeout_is_transient_and_backs_off() -> None:
    assert response_for(FailureClass.PROVIDER_TIMEOUT) is Response.RETRY_AFTER_BACKOFF
    assert (
        response_for(FailureClass.PROVIDER_RATE_LIMIT) is Response.RETRY_AFTER_BACKOFF
    )


def test_repeated_malformed_output_stops_rather_than_looping() -> None:
    """One re-ask with the schema restated; then it is broken, not unlucky."""

    assert response_for(FailureClass.MODEL_OUTPUT_INVALID) is Response.RETRY
    assert (
        response_for(FailureClass.MODEL_OUTPUT_INVALID_REPEATED)
        is Response.FAIL_PERMANENTLY
    )


def test_every_provider_being_down_does_not_corrupt_anything(
    chaos: dict[str, Any],
) -> None:
    from research_os.runtime.cycles import resume_cycle

    store: RuntimeStore = chaos["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    dead = ScriptedRouter(fail_roles={"planner", "scientific_reviewer"})
    result = resume_cycle(
        config=chaos["config"],
        db=chaos["db"],
        run_id=run.run_id,
        repo_path=chaos["repo"],
        models=dead,
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.terminal_state is TerminalState.DONE_FOR_NOW
    assert any("planner failed" in note for note in result.notes)
    assert BudgetExhaustedError  # imported for the neighbouring tests


def test_a_resume_payload_cannot_assert_a_verdict(chaos: dict[str, Any]) -> None:
    """The narrowest authority bypass this system could have had.

    Whoever resumes an interrupted thread supplies a payload. An earlier version
    read `granted` from it when the approvals table could not be consulted, and
    the control plane helpfully sent `{"granted": True}` -- so a resume was an
    approval. Now the payload is a wake-up and the table is the only verdict.
    """

    from research_os.runtime.cycles import resume_cycle

    store: RuntimeStore = chaos["store"]
    gated = ScriptedRouter(
        answers={"planner": plan_answer(str(ActionKind.ACCEPT_CLAIM))}
    )
    run = store.create_run(project_id="alpha-project", objective="o")
    first = resume_cycle(
        config=chaos["config"],
        db=chaos["db"],
        run_id=run.run_id,
        repo_path=chaos["repo"],
        models=gated,
    )
    assert first.status is RunStatus.WAITING_HUMAN
    approval_id = first.pending_approval_id
    assert approval_id

    # Resume claiming approval, without any decision having been recorded.
    resumed = resume_cycle(
        config=chaos["config"],
        db=chaos["db"],
        run_id=run.run_id,
        repo_path=chaos["repo"],
        models=gated,
        resume_value={"granted": True, "decided_by": "definitely-a-human"},
    )
    still_pending = store.get_approval(approval_id)
    assert still_pending is not None
    assert str(still_pending.status) == "PENDING", "the payload decided the approval"
    assert still_pending.applied_at is None
    assert resumed.terminal_state is TerminalState.WAITING_FOR_SCIENTIFIC_DECISION
    assert any("no recorded decision" in note for note in resumed.notes)


def test_a_declined_decision_cannot_be_resumed_into_an_approval(
    chaos: dict[str, Any],
) -> None:
    """Declining is a decision, and it is not reversible by resuming again."""

    from research_os.runtime.cycles import resume_cycle

    store: RuntimeStore = chaos["store"]
    gated = ScriptedRouter(
        answers={"planner": plan_answer(str(ActionKind.ACCEPT_CLAIM))}
    )
    run = store.create_run(project_id="alpha-project", objective="o")
    first = resume_cycle(
        config=chaos["config"],
        db=chaos["db"],
        run_id=run.run_id,
        repo_path=chaos["repo"],
        models=gated,
    )
    approval_id = first.pending_approval_id
    assert approval_id
    store.record_decision(
        approval_id, granted=False, decision={"granted": False}, decided_by="researcher"
    )
    resumed = resume_cycle(
        config=chaos["config"],
        db=chaos["db"],
        run_id=run.run_id,
        repo_path=chaos["repo"],
        models=gated,
        resume_value={"granted": True},
    )
    assert resumed.terminal_state is TerminalState.DONE_FOR_NOW
    assert any("declined" in note for note in resumed.notes)


def test_a_coding_run_that_reaches_the_canonical_checkout_is_caught(
    chaos: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape this runtime can detect but not prevent.

    The pipeline runs the project's acceptance commands *after* the builder has
    written files in scope, so `pytest` executes Python a model wrote one step
    earlier, with the researcher's environment. Worktree isolation protects the
    canonical checkout from the builder; it is not an OS sandbox, and
    SECURITY.md has always said so. What R5 changes is that nobody decides to
    run it.

    A sandbox is the fix, and `research_os/sandbox.py` now provides one where the
    host allows it -- which this host does not, so on this machine the detection
    below is still the only thing standing. The guard detects: the canonical
    capsule and every Git ref are hashed before and after, and a difference
    fails the action as POLICY_REFUSED with the paths named, which turns a
    silent escape into a loud one. It does not prevent the write.

    Run at `medium` autonomy deliberately. At `high` the pipeline would not run
    at all here -- containment is required and unavailable, which is asserted
    separately in `tests/test_sandbox.py` -- and the property under test is the
    fingerprint guard, which is what protects the path a person can still take.
    """

    from research_os.runtime.actions import coding
    from research_os.runtime.actions.coding import (
        canonical_fingerprint,
        run_coding_task,
    )

    repo = chaos["repo"]
    before = canonical_fingerprint(repo)
    assert any(key.startswith(".research/") for key in before)
    # One entry per ref rather than one hash of all of them, so a refusal can
    # name what moved and an exemption can be scoped to one namespace. The
    # single `<git-refs>` digest could do neither, which is why it flagged the
    # pipeline's own worktree branch -- see `tests/test_runtime_coding_pipeline.py`.
    assert any(key.startswith("<git-ref> refs/heads/") for key in before)

    class EscapingController:
        """Stands in for a pipeline whose acceptance command escaped."""

        def start(self, **_kwargs: Any) -> tuple[Any, Any]:
            # What executed test code could do: write a human review into the
            # canonical capsule.
            forged = repo / ".research" / "reviews"
            forged.mkdir(parents=True, exist_ok=True)
            (forged / "REV-9999.yaml").write_text(
                "id: REV-9999\nreviewer_kind: human\nverdict: approve\n",
                encoding="utf-8",
            )
            return _FakeStore(), None

        def execute(self, _store: Any) -> Any:
            return _FakeRun()

    class _FakeStore:
        directory = repo
        run_id = "RUN-20260101T000000Z-aaaaaaaa"

        def load(self) -> Any:
            return _FakeRun()

        def path(self, *parts: str) -> Path:
            return repo.joinpath(*parts)

    class _FakeRun:
        from research_os.automation.models import RunState as _RunState

        run_id = "RUN-20260101T000000Z-aaaaaaaa"
        state = _RunState.READY_FOR_HUMAN
        base_commit = "0" * 40
        base_branch = "main"
        work_orders: tuple[Any, ...] = ()
        reviews: tuple[Any, ...] = ()
        model_calls_used = 1
        failure_reason = None

        def total_cost_usd(self) -> float:
            return 0.0

    monkeypatch.setattr(
        coding, "_controller", lambda _context, autonomy: EscapingController()
    )

    from tests.runtime_graph_helpers import make_context

    context = make_context(
        db=chaos["db"],
        repo=repo,
        artifacts_root=chaos["config"].artifacts_root,
        dsn=chaos["dsn"],
        models=chaos["router"],
        permitted=(),
    )
    run = chaos["store"].create_run(project_id="alpha-project", objective="o")
    outcome = run_coding_task(
        {
            "run_id": run.run_id,
            "project_id": "alpha-project",
            "repo_path": str(repo),
            "cycle_index": 0,
            "autonomy": "medium",
        },
        context,
        {"rationale": "do something"},
    )

    assert not outcome.ok
    assert outcome.failure_class is FailureClass.POLICY_REFUSED, (
        "an escape must not be repaired and retried; repairing it would run the "
        "same escaping code again"
    )
    assert "changed the canonical checkout" in outcome.detail
    assert "REV-9999" in outcome.data["canonical_drift"]


def test_the_fingerprint_ignores_the_gitignored_runtime_directory(
    chaos: dict[str, Any],
) -> None:
    """`.research/runtime/` is reserved scratch space and is not scientific state."""

    from research_os.runtime.actions.coding import canonical_fingerprint

    repo = chaos["repo"]
    before = canonical_fingerprint(repo)
    scratch = repo / ".research" / "runtime"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "scratch.txt").write_text("noise", encoding="utf-8")
    assert canonical_fingerprint(repo) == before
