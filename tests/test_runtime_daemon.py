"""researchd: the control plane, driven a tick at a time.

``Daemon.tick`` performs one pass of everything and returns a report, which is
what makes the control plane testable without threads or sleeping. The tests
that matter most here are the ones about *choreography*: one request turning
into a running cycle, a stopped cycle being resumed without anyone asking, a
finished cycle opening its successor, and a dead worker's work being picked up
by a live one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.clock import FrozenClock
from research_os.runtime.daemon import EVENT_WORK, Daemon, WorkKind
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import (
    ApprovalStatus,
    ExternalJobStatus,
    RunStatus,
    TerminalState,
    WorkStatus,
)
from research_os.runtime.notify import CollectingNotifier
from research_os.runtime.policy import ActionKind
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    make_router,
    plan_answer,
    review_answer,
)


def _why_failed(db: Database) -> str:
    """The error the queue recorded, for an assertion message worth reading.

    A bare ``assert work_failed == 0`` tells you a number. This tells you which
    handler broke and why, which is the difference between a two-minute fix and
    twenty minutes of adding print statements.
    """

    with db.tx() as conn:
        rows = conn.execute(
            "select kind, status, failure_class, last_error from work_items "
            "where failure_class is not null order by updated_at desc limit 5"
        ).fetchall()
    if not rows:
        return "no failed work recorded"
    return "; ".join(
        f"{row['kind']}[{row['status']}]/{row['failure_class']}: {row['last_error']}"
        for row in rows
    )


@pytest.fixture
def plane(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    from research_os.runtime.checkpoints import ensure_tables

    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project")
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="alpha-project", repo_path=str(repo))
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
            "scientific_reviewer": review_answer(),
        }
    )
    notifier = CollectingNotifier()
    config = make_config(pg_dsn, tmp_path / "artifacts")
    daemon = Daemon(
        config=config,
        db=runtime_db,
        repo_for=lambda _project: repo,
        models=lambda _run, _project, _work: router,
        notifier=notifier,
        clock=FrozenClock(),
        owner="test-worker",
    )
    return {
        "daemon": daemon,
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "store": store,
        "queue": WorkQueue(runtime_db),
        "router": router,
        "notifier": notifier,
        "config": config,
    }


# ------------------------------------------------------------ event -> work --
def test_every_mapped_event_names_a_work_kind_the_daemon_can_run() -> None:
    """A mapping to a kind with no handler would queue work nobody can do."""

    known = {
        WorkKind.RUN_CYCLE,
        WorkKind.RESUME_CYCLE,
        WorkKind.CONTINUE_OBJECTIVE,
        WorkKind.POLL_EXTERNAL_JOBS,
        WorkKind.PRUNE_CHECKPOINTS,
        WorkKind.INTEGRITY_AUDIT,
    }
    assert set(EVENT_WORK.values()) <= known


def test_an_unconsumed_event_becomes_queued_work(plane: dict[str, Any]) -> None:
    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    store.record_event(
        kind="RESEARCH_RUN_REQUESTED",
        project_id="alpha-project",
        run_id=run.run_id,
        dedup_key=f"requested:{run.run_id}",
    )
    report = plane["daemon"].tick()
    assert report.events_ingested == 1
    assert report.work_enqueued == 1
    items = plane["queue"].list_for_run(run.run_id)
    assert [item.kind for item in items] == [WorkKind.RUN_CYCLE]


def test_the_same_event_cannot_queue_the_same_work_twice(plane: dict[str, Any]) -> None:
    """A crash between consuming an event and queueing its work must not double."""

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    event, _ = store.record_event(
        kind="RESEARCH_RUN_REQUESTED",
        project_id="alpha-project",
        run_id=run.run_id,
        dedup_key=f"requested:{run.run_id}",
    )
    first = plane["queue"].enqueue(
        project_id="alpha-project",
        kind=WorkKind.RUN_CYCLE,
        run_id=run.run_id,
        dedup_key=f"{WorkKind.RUN_CYCLE}:{event.event_id}",
    )
    second = plane["queue"].enqueue(
        project_id="alpha-project",
        kind=WorkKind.RUN_CYCLE,
        run_id=run.run_id,
        dedup_key=f"{WorkKind.RUN_CYCLE}:{event.event_id}",
    )
    assert first.created and not second.created
    assert len(plane["queue"].list_for_run(run.run_id)) == 1


def test_an_unmapped_event_queues_nothing(plane: dict[str, Any]) -> None:
    plane["store"].record_event(
        kind="SOMETHING_INFORMATIONAL", project_id="alpha-project", dedup_key="info:1"
    )
    report = plane["daemon"].tick()
    assert report.events_ingested == 1
    assert report.work_enqueued == 0


# --------------------------------------------------------- the choreography --
def test_one_request_becomes_a_finished_cycle_with_no_further_instruction(
    plane: dict[str, Any],
) -> None:
    """The whole point: nobody tells it which step runs next.

    A request is recorded. From there the daemon ingests it, queues the work,
    claims it, runs the cycle, and records the outcome -- with no further input.
    """

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="find out whether X")
    store.record_event(
        kind="RESEARCH_RUN_REQUESTED",
        project_id="alpha-project",
        run_id=run.run_id,
        dedup_key=f"requested:{run.run_id}",
    )

    ingest = plane["daemon"].tick()
    assert ingest.work_enqueued == 1

    execute = plane["daemon"].tick()
    assert execute.work_claimed == 1
    assert execute.work_succeeded == 1

    finished = store.require_run(run.run_id)
    assert finished.status is RunStatus.SUCCEEDED
    assert finished.terminal_state is TerminalState.DONE_FOR_NOW
    assert plane["router"].requests_for("planner"), "no model was ever consulted"


def test_a_finished_cycle_opens_its_successor_without_being_asked(
    plane: dict[str, Any],
) -> None:
    """Continuation is the control plane's decision, in a new thread."""

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="keep going")
    store.record_event(
        kind="RESEARCH_CYCLE_FINISHED",
        project_id="alpha-project",
        run_id=run.run_id,
        payload={"recommendation": "START_NEXT_CYCLE"},
        dedup_key=f"finished:{run.run_id}",
    )
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    store.set_run_status(
        run.run_id, RunStatus.SUCCEEDED, terminal_state=TerminalState.DONE_FOR_NOW
    )

    plane["daemon"].tick()  # ingest
    report = plane["daemon"].tick()  # run the continuation
    assert report.work_succeeded == 1

    runs = store.list_runs(project_id="alpha-project")
    successors = [r for r in runs if r.parent_run_id == run.run_id]
    assert len(successors) == 1
    assert successors[0].cycle_index == run.cycle_index + 1
    assert successors[0].thread_id != run.thread_id, "a successor must be a new thread"


def test_continuation_refuses_past_the_ceiling(
    plane: dict[str, Any], tmp_path: Path
) -> None:
    store: RuntimeStore = plane["store"]
    tight = make_config(
        plane["dsn"], tmp_path / "artifacts", settings={"max_cycles_per_objective": 1}
    )
    daemon = Daemon(
        config=tight,
        db=plane["db"],
        repo_for=lambda _p: plane["repo"],
        models=lambda _r, _p, _w: plane["router"],
        notifier=plane["notifier"],
        clock=FrozenClock(),
        owner="tight-worker",
    )
    run = store.create_run(project_id="alpha-project", objective="o")
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    store.set_run_status(
        run.run_id, RunStatus.SUCCEEDED, terminal_state=TerminalState.DONE_FOR_NOW
    )
    store.record_event(
        kind="RESEARCH_CYCLE_FINISHED",
        project_id="alpha-project",
        run_id=run.run_id,
        payload={"recommendation": "START_NEXT_CYCLE"},
        dedup_key=f"finished:{run.run_id}",
    )
    daemon.tick()
    daemon.tick()
    successors = [
        r
        for r in store.list_runs(project_id="alpha-project")
        if r.parent_run_id == run.run_id
    ]
    assert successors == []


# ---------------------------------------------------------------- recovery --
def test_a_dead_workers_item_is_recovered_before_new_work_is_claimed(
    plane: dict[str, Any],
) -> None:
    """Recovery first, or a restarting daemon strands old work under new work."""

    queue: WorkQueue = plane["queue"]
    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    work = queue.enqueue(
        project_id="alpha-project", kind=WorkKind.RUN_CYCLE, run_id=run.run_id
    ).item
    queue.claim(owner="a-worker-that-died", lease_seconds=60)
    with plane["db"].tx() as conn:
        conn.execute("update work_items set lease_expires_at = now() - interval '1s'")

    report = plane["daemon"].tick()
    assert report.leases_reclaimed == 1
    recovered = queue.get(work.work_id)
    assert recovered is not None
    assert recovered.status in {
        WorkStatus.LEASED,
        WorkStatus.PENDING,
        WorkStatus.SUCCEEDED,
    }
    kinds = [event.kind for event in store.list_events(run_id=run.run_id)]
    assert "WORKER_RECOVERED" in kinds


def test_work_with_no_handler_fails_terminally_rather_than_sitting_claimable(
    plane: dict[str, Any],
) -> None:
    queue: WorkQueue = plane["queue"]
    work = queue.enqueue(project_id="alpha-project", kind="something_unknown").item
    report = plane["daemon"].tick()
    assert report.work_failed == 1
    after = queue.get(work.work_id)
    assert after is not None
    assert after.status is WorkStatus.FAILED
    assert after.failure_class == FailureClass.POLICY_REFUSED


def test_a_stale_invocation_is_flagged_not_retried(plane: dict[str, Any]) -> None:
    from research_os.runtime.idempotency import InvocationLedger
    from research_os.runtime.models import InvocationStatus

    ledger = InvocationLedger(plane["db"])
    ledger._claim(
        key="k1",
        kind="slurm.submit",
        request={},
        run_id=None,
        work_id=None,
        owner="ghost",
    )
    with plane["db"].tx() as conn:
        conn.execute(
            "update tool_invocations set started_at = now() - interval '2 hours'"
        )
    report = plane["daemon"].tick()
    assert report.invocations_abandoned == 1
    assert any("needs reconciling" in note for note in report.notes)
    assert ledger.get("k1").status is InvocationStatus.ABANDONED  # type: ignore[union-attr]


def test_a_stale_reservation_is_released(plane: dict[str, Any]) -> None:
    from decimal import Decimal

    from research_os.runtime.budgets import BudgetLedger, Dimension
    from research_os.runtime.models import BudgetScope

    budgets = BudgetLedger(plane["db"])
    budgets.set_limit(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        limit_value=10,
    )
    budgets.reserve(
        scope=BudgetScope.RUN,
        scope_id="r1",
        dimension=Dimension.MODEL_COST_USD,
        amount=4,
    )
    with plane["db"].tx() as conn:
        conn.execute(
            "update budget_reservations set created_at = now() - interval '3 hours'"
        )
    report = plane["daemon"].tick()
    assert report.reservations_released == 1
    budget = budgets.get(
        scope=BudgetScope.RUN, scope_id="r1", dimension=Dimension.MODEL_COST_USD
    )
    assert budget is not None
    assert budget.reserved == Decimal(0)
    assert budget.spent == Decimal(0), "unknown spend must not be charged"


# --------------------------------------------------------------- approvals --
def test_a_pending_decision_notifies_a_person_exactly_once(
    plane: dict[str, Any],
) -> None:
    """A restart must not re-notify, and a week-old decision must not spam."""

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    store.request_approval(
        run_id=run.run_id,
        project_id="alpha-project",
        kind="accept_claim",
        question="Authorise accept_claim?",
        packet={"decision_required": "accept_claim"},
        interrupt_key="k",
    )
    first = plane["daemon"].tick()
    second = plane["daemon"].tick()
    third = plane["daemon"].tick()
    assert first.approvals_surfaced == 1
    assert second.approvals_surfaced == 0
    assert third.approvals_surfaced == 0
    notifier: CollectingNotifier = plane["notifier"]
    assert len(notifier.sent) == 1
    assert "researchctl runtime approve" in str(notifier.sent[0]["body"])
    assert notifier.sent[0]["urgent"] is True


def test_recording_a_decision_resumes_the_cycle_without_being_asked(
    plane: dict[str, Any], tmp_path: Path
) -> None:
    """The interaction the researcher should have: answer once, walk away."""

    store: RuntimeStore = plane["store"]
    gated_router = ScriptedRouter(
        answers={"planner": plan_answer(str(ActionKind.ACCEPT_CLAIM))}
    )
    daemon = Daemon(
        config=plane["config"],
        db=plane["db"],
        repo_for=lambda _p: plane["repo"],
        models=lambda _r, _p, _w: gated_router,
        notifier=plane["notifier"],
        clock=FrozenClock(),
        owner="gate-worker",
    )
    run = store.create_run(project_id="alpha-project", objective="o")
    store.record_event(
        kind="RESEARCH_RUN_REQUESTED",
        project_id="alpha-project",
        run_id=run.run_id,
        dedup_key=f"requested:{run.run_id}",
    )
    daemon.tick()  # ingest
    daemon.tick()  # run, and stop at the gate

    waiting = store.require_run(run.run_id)
    assert waiting.status is RunStatus.WAITING_HUMAN
    approvals = store.list_approvals(run_id=run.run_id)
    assert len(approvals) == 1

    # The researcher answers. Everything after this is automatic.
    store.record_decision(
        approvals[0].approval_id,
        granted=True,
        decision={"granted": True},
        decided_by="researcher",
    )
    store.record_event(
        kind="SCIENTIFIC_DECISION_RECORDED",
        project_id="alpha-project",
        run_id=run.run_id,
        payload={"approval_id": approvals[0].approval_id},
        dedup_key=f"decided:{approvals[0].approval_id}",
    )
    daemon.tick()  # ingest the decision
    daemon.tick()  # resume

    done = store.require_run(run.run_id)
    assert done.status is RunStatus.SUCCEEDED
    applied = store.get_approval(approvals[0].approval_id)
    assert applied is not None
    assert applied.status is ApprovalStatus.GRANTED
    assert applied.applied_at is not None


# --------------------------------------------------------------- schedules --
def test_a_schedule_produces_an_event_not_work(plane: dict[str, Any]) -> None:
    """So a scheduled action passes through the same provenance as a manual one."""

    store: RuntimeStore = plane["store"]
    store.create_schedule(
        kind="INTEGRITY_AUDIT_DUE", interval_seconds=3600, project_id="alpha-project"
    )
    report = plane["daemon"].tick()
    assert report.schedules_fired == 1
    kinds = [event.kind for event in store.list_events(project_id="alpha-project")]
    assert "INTEGRITY_AUDIT_DUE" in kinds


def test_a_schedule_does_not_fire_twice_in_one_interval(plane: dict[str, Any]) -> None:
    plane["store"].create_schedule(kind="TICK", interval_seconds=3600)
    assert plane["daemon"].tick().schedules_fired == 1
    assert plane["daemon"].tick().schedules_fired == 0


# ------------------------------------------------------------ external jobs --
def test_a_submission_with_no_scheduler_id_becomes_unknown_not_resubmitted(
    plane: dict[str, Any],
) -> None:
    """The crash window between recording and submitting.

    The submission may well have succeeded. Resubmitting would run the same
    experiment twice, so it is marked UNKNOWN for a person to resolve.
    """

    from research_os.runtime.executors import poll_job

    store: RuntimeStore = plane["store"]
    job = store.create_external_job(
        project_id="alpha-project",
        executor="slurm",
        spec_digest="abc",
        run_dir=str(plane["repo"]),
    )
    polled = poll_job(job, store=store, config=plane["config"])
    assert polled.status is ExternalJobStatus.UNKNOWN
    assert polled.failure_class == FailureClass.WORKER_CRASH
    assert "may or may not have reached the scheduler" in (polled.detail or "")


def test_a_finished_job_emits_an_event_that_resumes_its_cycle(
    plane: dict[str, Any],
) -> None:
    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    job = store.create_external_job(
        project_id="alpha-project",
        run_id=run.run_id,
        executor="local",
        spec_digest="abc",
        run_dir=str(plane["repo"]),
    )
    store.update_external_job(
        job.job_id, status=ExternalJobStatus.COMPLETED, exit_code=0, polled=True
    )
    store.record_event(
        kind="EXTERNAL_JOB_FINISHED",
        project_id="alpha-project",
        run_id=run.run_id,
        payload={"job_id": job.job_id, "status": "COMPLETED"},
        dedup_key=f"job-finished:{job.job_id}",
    )
    report = plane["daemon"].tick()
    assert report.work_enqueued == 1
    items = plane["queue"].list_for_run(run.run_id)
    assert [item.kind for item in items] == [WorkKind.RESUME_CYCLE]


# -------------------------------------------------------------- the loop ----
def test_an_idle_loop_sleeps_and_a_busy_one_does_not(plane: dict[str, Any]) -> None:
    """A busy queue is drained at speed; an idle one costs one cheap query."""

    clock = FrozenClock()
    daemon = Daemon(
        config=plane["config"],
        db=plane["db"],
        repo_for=lambda _p: plane["repo"],
        models=lambda _r, _p, _w: plane["router"],
        notifier=plane["notifier"],
        clock=clock,
        owner="loop-worker",
    )
    daemon.run_forever(max_ticks=3)
    assert len(clock.slept) == 3, "an idle pass must sleep"

    plane["store"].create_schedule(kind="TICK", interval_seconds=3600)
    before = len(clock.slept)
    daemon.run_forever(max_ticks=1)
    assert len(clock.slept) == before, "a pass that did something must not sleep"


def test_stopping_ends_the_loop_after_the_current_pass(plane: dict[str, Any]) -> None:
    daemon: Daemon = plane["daemon"]
    daemon.stop()
    total = daemon.run_forever(max_ticks=10)
    assert total.work_claimed == 0


def test_the_daemon_never_calls_a_model_itself() -> None:
    """Invariant 2: a background *process* is not a background *agent*.

    Asserted structurally. The daemon builds a router and hands it to a cycle;
    it must not call ``complete`` itself, because that would be frontier
    reasoning happening outside a claimed, budgeted work item.
    """

    import ast
    from pathlib import Path as _Path

    source = (
        _Path(__file__).resolve().parents[1]
        / "src"
        / "research_os"
        / "runtime"
        / "daemon.py"
    ).read_text(encoding="utf-8")
    called = {
        getattr(node.func, "attr", None)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }
    assert "complete" not in called, "researchd must not invoke a model directly"


def test_a_successor_cycles_model_calls_are_recorded_against_the_successor(
    plane: dict[str, Any],
) -> None:
    """The regression for a foreign key the placeholder run id violated.

    The router is built per run because provenance is per run, so a continuation
    that constructs it before creating the successor has no id to use. The first
    version passed the string "pending", the foreign key rejected it, and every
    continuation failed with ``work_failed`` after the cycle itself had
    succeeded -- visible only in ``runtime status``.
    """

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="keep going")
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    store.set_run_status(
        run.run_id, RunStatus.SUCCEEDED, terminal_state=TerminalState.DONE_FOR_NOW
    )
    store.record_event(
        kind="RESEARCH_CYCLE_FINISHED",
        project_id="alpha-project",
        run_id=run.run_id,
        payload={"recommendation": "START_NEXT_CYCLE"},
        dedup_key=f"finished:{run.run_id}",
    )
    # The *real* router, so provenance is actually written. A ScriptedRouter
    # records nothing and would pass this test while the bug was present.
    recording = Daemon(
        config=plane["config"],
        db=plane["db"],
        repo_for=lambda _p: plane["repo"],
        models=lambda run_id, project_id, work_id: make_router(
            db=plane["db"],
            artifacts_root=plane["config"].artifacts_root,
            run_id=run_id,
            project_id=project_id,
            work_id=work_id,
            answers={
                "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
                "reviewer": review_answer(),
            },
        ),
        notifier=plane["notifier"],
        clock=FrozenClock(),
        owner="recording-worker",
    )
    recording.tick()
    report = recording.tick()
    assert report.work_failed == 0, _why_failed(plane["db"])
    assert report.work_succeeded == 1

    successors = [
        r
        for r in store.list_runs(project_id="alpha-project")
        if r.parent_run_id == run.run_id
    ]
    assert len(successors) == 1
    calls = store.list_model_calls(run_id=successors[0].run_id)
    assert calls, "the successor recorded no model calls"
    assert all(call.run_id == successors[0].run_id for call in calls)
    assert {call.model for call in calls} == {"fake-1"}, (
        "the model that answered must be recorded, not the profile's hint"
    )


def test_two_finished_events_for_one_run_open_one_successor(
    plane: dict[str, Any],
) -> None:
    """Defence in depth: the continuation dedup key is per run, not per event."""

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    store.set_run_status(
        run.run_id, RunStatus.SUCCEEDED, terminal_state=TerminalState.DONE_FOR_NOW
    )
    for index in range(2):
        store.record_event(
            kind="RESEARCH_CYCLE_FINISHED",
            project_id="alpha-project",
            run_id=run.run_id,
            payload={"recommendation": "START_NEXT_CYCLE"},
            dedup_key=f"finished:{run.run_id}:{index}",
        )
    ingest = plane["daemon"].tick()
    assert ingest.events_ingested == 2
    assert ingest.work_enqueued == 1, "two events must not fork the lineage"
    plane["daemon"].tick()
    successors = [
        r
        for r in store.list_runs(project_id="alpha-project")
        if r.parent_run_id == run.run_id
    ]
    assert len(successors) == 1


def test_an_unchanged_frontier_stops_the_chain(plane: dict[str, Any]) -> None:
    """The waste a real pilot demonstrated, now refused.

    The first pilot against a real project ran seven chained cycles over an
    identical frontier, each concluding START_NEXT_CYCLE, stopping only at the
    configured ceiling -- fifteen model calls and 2.65 USD for one assessment's
    worth of information.

    The cause is structural: the runtime cannot write a capsule, so its own work
    never changes the frontier its planning is derived from. Progress has to be
    checked, not assumed.
    """

    from research_os.runtime.cycles import CycleResult, should_continue

    store: RuntimeStore = plane["store"]
    parent = store.create_run(project_id="alpha-project", objective="o")
    store.set_frontier_digest(parent.run_id, "the-same-frontier")
    child = store.create_run(
        project_id="alpha-project",
        objective="o",
        parent_run_id=parent.run_id,
        cycle_index=1,
    )
    store.set_frontier_digest(child.run_id, "the-same-frontier")

    proceed, why = should_continue(
        db=plane["db"],
        config=plane["config"],
        result=CycleResult(
            run=store.require_run(child.run_id),
            status=RunStatus.SUCCEEDED,
            terminal_state=TerminalState.DONE_FOR_NOW,
            pending_approval_id=None,
            recommendation="START_NEXT_CYCLE",
            notes=(),
            state={},
        ),
    )
    assert proceed is False
    assert "unchanged from the previous cycle" in why
    assert "needs a person" in why


def test_a_changed_frontier_still_continues(plane: dict[str, Any]) -> None:
    """Progress must not be mistaken for repetition either."""

    from research_os.runtime.cycles import CycleResult, should_continue

    store: RuntimeStore = plane["store"]
    parent = store.create_run(project_id="alpha-project", objective="o")
    store.set_frontier_digest(parent.run_id, "before")
    child = store.create_run(
        project_id="alpha-project",
        objective="o",
        parent_run_id=parent.run_id,
        cycle_index=1,
    )
    store.set_frontier_digest(child.run_id, "after")
    proceed, _why = should_continue(
        db=plane["db"],
        config=plane["config"],
        result=CycleResult(
            run=store.require_run(child.run_id),
            status=RunStatus.SUCCEEDED,
            terminal_state=TerminalState.DONE_FOR_NOW,
            pending_approval_id=None,
            recommendation="START_NEXT_CYCLE",
            notes=(),
            state={},
        ),
    )
    assert proceed is True


def test_work_that_ran_out_of_attempts_is_not_replaced_with_fresh_work(
    plane: dict[str, Any],
) -> None:
    """The attempt cap was defeated by the thing meant to honour it.

    ``_recover`` emitted WORKER_RECOVERED for every item it touched, including
    the ones it had just marked FAILED -- and that event mapped to a *new*
    work item with a *fresh* attempt budget. So an item that killed three
    workers was marked FAILED and immediately replaced by one that would kill
    three more, and the backlog for one run grew without bound. Found by an
    independent review.
    """

    queue: WorkQueue = plane["queue"]
    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    work = queue.enqueue(
        project_id="alpha-project",
        kind=WorkKind.RUN_CYCLE,
        run_id=run.run_id,
        max_attempts=1,
    ).item
    queue.claim(owner="doomed", lease_seconds=60)
    with plane["db"].tx() as conn:
        conn.execute("update work_items set lease_expires_at = now() - interval '1s'")

    report = plane["daemon"].tick()
    assert report.leases_reclaimed == 1
    assert report.work_enqueued == 0, "exhausted work was replaced with fresh work"
    assert any("out of attempts" in note for note in report.notes)

    assert queue.get(work.work_id).status is WorkStatus.FAILED  # type: ignore[union-attr]
    kinds = [event.kind for event in store.list_events(run_id=run.run_id)]
    assert "WORK_EXHAUSTED" in kinds
    assert "WORKER_RECOVERED" not in kinds
    # And no new item exists for the run.
    assert len(queue.list_for_run(run.run_id)) == 1


def test_a_recovered_item_does_not_become_a_second_concurrent_attempt(
    plane: dict[str, Any],
) -> None:
    """The requeued item *is* the retry; a second one would run in parallel."""

    queue: WorkQueue = plane["queue"]
    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    queue.enqueue(
        project_id="alpha-project",
        kind=WorkKind.RUN_CYCLE,
        run_id=run.run_id,
        max_attempts=4,
    )
    for _ in range(2):
        queue.claim(owner="flaky", lease_seconds=60)
        with plane["db"].tx() as conn:
            conn.execute(
                "update work_items set lease_expires_at = now() - interval '1s' "
                "where status = 'LEASED'"
            )
        plane["daemon"].tick()
    resume_items = [
        item
        for item in queue.list_for_run(run.run_id)
        if item.kind == WorkKind.RESUME_CYCLE
    ]
    assert len(resume_items) <= 1, "two resume items for one run"


def test_an_event_is_not_consumed_until_its_work_exists(plane: dict[str, Any]) -> None:
    """Consuming first lost the work permanently on a crash in between.

    Nothing re-emits these events: a run is requested once, a decision recorded
    once, a job finished once. An independent review found the comment claiming
    a "next producer" would cover it.
    """

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    store.record_event(
        kind="RESEARCH_RUN_REQUESTED",
        project_id="alpha-project",
        run_id=run.run_id,
        dedup_key=f"requested:{run.run_id}",
    )
    # Claiming leases; it does not consume.
    leased = store.claim_events(owner="w", lease_seconds=120)
    assert len(leased) == 1
    assert leased[0].consumed_at is None
    assert store.unconsumed_event_count() == 1

    # A second claimer sees nothing while the lease holds.
    assert store.claim_events(owner="other", lease_seconds=120) == ()

    # And once the lease expires, the event is claimable again -- which is what
    # makes a crash between claiming and enqueueing recoverable.
    with plane["db"].tx() as conn:
        conn.execute("update events set claimed_at = now() - interval '1 hour'")
    again = store.claim_events(owner="w2", lease_seconds=120)
    assert [event.event_id for event in again] == [leased[0].event_id]
    assert store.consume_event(again[0].event_id) is True
    assert store.consume_event(again[0].event_id) is False
    assert store.unconsumed_event_count() == 0


def test_a_decision_and_its_resuming_event_are_one_transaction(
    plane: dict[str, Any],
) -> None:
    """Two transactions left an approved gate with no event, stalling forever."""

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    approval, _ = store.request_approval(
        run_id=run.run_id,
        project_id="alpha-project",
        kind="accept_claim",
        question="q",
        packet={},
        interrupt_key="k",
    )
    store.record_decision(
        approval.approval_id,
        granted=True,
        decision={"granted": True},
        decided_by="tester@host",
    )
    kinds = [event.kind for event in store.list_events(run_id=run.run_id)]
    assert "SCIENTIFIC_DECISION_RECORDED" in kinds


def test_an_answered_gate_is_never_silently_dropped(plane: dict[str, Any]) -> None:
    """The worst failure this system had, and the narrowest.

    `RESUME_CYCLE` was briefly deduped per run, so once a recovery had claimed
    `resume_cycle:{run_id}` a researcher's answered gate produced
    SCIENTIFIC_DECISION_RECORDED, got `created=False`, and queued nothing --
    leaving the run in WAITING_HUMAN forever with a GRANTED approval and no
    operator verb to rescue it. Found by an independent review.
    """

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")

    # A recovery event and a decision event, in that order, for one run.
    store.record_event(
        kind="WORKER_RECOVERED",
        project_id="alpha-project",
        run_id=run.run_id,
        payload={"kind": WorkKind.RUN_CYCLE},
        dedup_key=f"recovered:{run.run_id}:1",
    )
    store.record_event(
        kind="EXTERNAL_JOB_FINISHED",
        project_id="alpha-project",
        run_id=run.run_id,
        payload={"job_id": "XJOB-x"},
        dedup_key="job-finished:XJOB-x",
    )
    store.record_event(
        kind="SCIENTIFIC_DECISION_RECORDED",
        project_id="alpha-project",
        run_id=run.run_id,
        payload={"approval_id": "APRV-x"},
        dedup_key="decided:APRV-x",
    )
    report = plane["daemon"].tick()
    assert report.events_ingested == 3

    kinds = [item.kind for item in plane["queue"].list_for_run(run.run_id)]
    # The recovery produces nothing -- the reclaimed item is its own retry --
    # and the two real events each produce their own work.
    assert kinds.count(WorkKind.RESUME_CYCLE) == 2, kinds
    payloads = [
        item.payload.get("event_kind")
        for item in plane["queue"].list_for_run(run.run_id)
    ]
    assert "SCIENTIFIC_DECISION_RECORDED" in payloads, (
        "the researcher's decision produced no work"
    )


def test_a_recovery_event_produces_no_work(plane: dict[str, Any]) -> None:
    """The reclaimed item is the retry; a second item was pure duplication."""

    from research_os.runtime.daemon import EVENT_WORK

    assert "WORKER_RECOVERED" not in EVENT_WORK

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    store.record_event(
        kind="WORKER_RECOVERED",
        project_id="alpha-project",
        run_id=run.run_id,
        dedup_key="recovered:x:1",
    )
    report = plane["daemon"].tick()
    assert report.events_ingested == 1
    assert report.work_enqueued == 0
    assert plane["queue"].list_for_run(run.run_id) == ()


def test_a_project_less_event_does_not_stall_ingestion(plane: dict[str, Any]) -> None:
    """It used to be re-claimed every lease period, forever, at the head of the
    batch -- so fifty of them would starve every real event, silently."""

    store: RuntimeStore = plane["store"]
    # A project-less schedule is enough to produce one.
    store.create_schedule(kind="RESEARCH_RUN_REQUESTED", interval_seconds=3600)
    fired = plane["daemon"].tick()
    assert fired.schedules_fired == 1

    ingested = plane["daemon"].tick()
    assert ingested.events_ingested >= 1
    assert any("cannot become work" in note for note in ingested.notes)
    kinds = [event.kind for event in store.list_events()]
    assert "EVENT_UNROUTABLE" in kinds

    # And it is gone from the claimable set, so a later real event is seen.
    store.record_event(
        kind="RESEARCH_RUN_REQUESTED",
        project_id="alpha-project",
        run_id=store.create_run(project_id="alpha-project", objective="o").run_id,
        dedup_key="requested:real",
    )
    after = plane["daemon"].tick()
    assert after.work_enqueued == 1


def test_maintenance_prunes_checkpoints_without_being_asked(
    plane: dict[str, Any],
) -> None:
    """`prune_checkpoints` had a handler, a setting and a docstring, and nothing
    ever created the work. The checkpoint tables were append-only forever."""

    from research_os.runtime.cycles import resume_cycle

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    resume_cycle(
        config=plane["config"],
        db=plane["db"],
        run_id=run.run_id,
        repo_path=plane["repo"],
        models=plane["router"],
    )
    thread = store.require_run(run.run_id).thread_id
    assert thread
    with plane["db"].tx() as conn:
        present = conn.execute(
            "select count(*) as n from checkpoints where thread_id = %s", (thread,)
        ).fetchone()
    assert present["n"] > 0

    with plane["db"].tx() as conn:
        conn.execute(
            "update research_runs set finished_at = now() - interval '90 days' "
            "where run_id = %s",
            (run.run_id,),
        )
    # A fresh daemon, so the maintenance stamp is unset and the pass runs it.
    fresh = Daemon(
        config=plane["config"],
        db=plane["db"],
        repo_for=lambda _p: plane["repo"],
        models=lambda _r, _p, _w: plane["router"],
        notifier=plane["notifier"],
        clock=FrozenClock(),
        owner="maintenance-worker",
    )
    report = fresh.tick()
    assert report.checkpoints_pruned == 1
    with plane["db"].tx() as conn:
        after = conn.execute(
            "select count(*) as n from checkpoints where thread_id = %s", (thread,)
        ).fetchone()
    assert after["n"] == 0


def test_maintenance_does_not_run_on_every_pass(plane: dict[str, Any]) -> None:
    """One indexed query an hour, not one per tick."""

    daemon: Daemon = plane["daemon"]
    daemon.tick()
    second = daemon.tick()
    assert second.checkpoints_pruned == 0
