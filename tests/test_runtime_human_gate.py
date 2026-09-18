"""The human gate: what the runtime does when the correct action is nothing.

Three properties, each of which failed on the live thesis runtime before it was
asserted here.

**A conclusion must outlive the message that carried it.** A cycle's
recommendation lived only in the ``RESEARCH_CYCLE_FINISHED`` event payload and
in the ``continue_objective`` work item copied from it. A run whose frontier
concluded ``WAIT_HUMAN`` therefore left a queued instruction reading
``START_NEXT_CYCLE`` behind it, and fixing how the conclusion is *reached* could
not fix the copy already on the queue. ``research_runs.next_recommendation`` is
the durable answer and this file asserts that continuation reads it.

**A repeated assessment is one assessment.** A frontier assessment over
identical scientific state concluding the identical thing is one observation,
however differently the model phrases it. Twenty repetitions must not consume
twenty slots of the planner's bounded finding window, because operational
repetition is not scientific progress.

**One legitimate change, one successor.** Not zero, not two, not one per
restart and not one per duplicate event.

Nothing here asserts a message; every test drives the real graph, the real
queue and the real ``Daemon.tick``, because every one of these defects was
invisible to a test that stubbed the layer below it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.checkpoints import ensure_tables
from research_os.runtime.clock import FrozenClock
from research_os.runtime.cycles import resume_cycle, start_cycle
from research_os.runtime.daemon import Daemon, WorkKind
from research_os.runtime.db import Database
from research_os.runtime.findings import FindingKind, RuntimeFinding
from research_os.runtime.interfaces import Independence, ModelResponse
from research_os.runtime.models import (
    Autonomy,
    RunStatus,
    TerminalState,
    WorkStatus,
)
from research_os.runtime.notify import CollectingNotifier
from research_os.runtime.policy import ActionKind
from research_os.runtime.queue import WorkQueue
from research_os.runtime.sciencecontext import (
    MAX_PLANNER_FINDINGS,
    noncanonical_science,
)
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    plan_answer,
    review_answer,
)

PROJECT = "gate-project"


def frontier_answer(
    recommendation: str,
    *,
    rationale: str = "the questions are already in front of the researcher.",
    action: str = "propose_capsule_change",
    addresses: tuple[str, ...] = ("Q-0001",),
    importance: str = "low",
) -> dict[str, Any]:
    return {
        "recommendation": recommendation,
        "recommendation_rationale": rationale,
        "ranked_actions": [
            {
                "action": action,
                "addresses": list(addresses),
                "importance": importance,
                "information_gain": "low",
                "feasibility": "high",
                "cost": "medium",
                "rationale": "an unaudited third ask would restate a question.",
            }
        ],
    }


@pytest.fixture
def plane(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project", project_id=PROJECT)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(repo))
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
            "scientific_reviewer": review_answer(),
            "frontier": frontier_answer("WAIT_HUMAN"),
        }
    )
    config = make_config(pg_dsn, tmp_path / "artifacts")

    def build(owner: str = "worker-1") -> Daemon:
        """A *fresh* control plane over the same database.

        Restart is the property under test in half this file, and a restart
        that reuses the object holds whatever the object cached. Every
        ``build()`` is a new ``Daemon`` reading the same rows, which is what
        ``systemctl --user restart researchd`` is.
        """

        return Daemon(
            config=config,
            db=runtime_db,
            repo_for=lambda _project: repo,
            models=lambda _run, _project, _work: router,
            notifier=CollectingNotifier(),
            clock=FrozenClock(),
            owner=owner,
        )

    return {
        "build": build,
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "store": store,
        "queue": WorkQueue(runtime_db),
        "router": router,
        "config": config,
    }


def cycle(plane: dict[str, Any], **overrides: Any) -> Any:
    """One real cycle, through the real graph."""

    return start_cycle(
        config=plane["config"],
        db=plane["db"],
        project_id=PROJECT,
        repo_path=plane["repo"],
        objective=overrides.pop(
            "objective", "decide whether another cycle is warranted"
        ),
        models=plane["router"],
        **overrides,
    )


def drain(
    plane: dict[str, Any], *, ticks: int = 6, owner: str = "worker-1"
) -> list[Any]:
    """Tick a fresh control plane until it has nothing left to do."""

    daemon = plane["build"](owner)
    reports = []
    for _ in range(ticks):
        report = daemon.tick()
        reports.append(report)
        if not report.did_something:
            break
    return reports


def successors_of(plane: dict[str, Any], run_id: str) -> list[str]:
    with plane["db"].tx() as conn:
        rows = conn.execute(
            "select run_id from research_runs where parent_run_id = %s "
            "order by created_at",
            (run_id,),
        ).fetchall()
    return [str(row["run_id"]) for row in rows]


def continuation_results(
    plane: dict[str, Any], run_id: str, *, dedup_key: str | None = None
) -> list[dict[str, Any]]:
    """What each ``continue_objective`` item for this run recorded.

    ``dedup_key`` picks one item out. Needed because a real cycle's own
    ``RESEARCH_CYCLE_FINISHED`` event already produces a continuation item, so
    a test that plants a second one has two, and "the last one by clock" is not
    the same as "the one I planted".
    """

    clause = "and dedup_key = %s" if dedup_key else ""
    params: tuple[Any, ...] = (run_id, WorkKind.CONTINUE_OBJECTIVE)
    if dedup_key:
        params = (*params, dedup_key)
    with plane["db"].tx() as conn:
        rows = conn.execute(
            "select result from work_items where run_id = %s and kind = %s "
            f"{clause} order by updated_at",
            params,
        ).fetchall()
    return [dict(row["result"] or {}) for row in rows]


# ------------------------------------------- the conclusion, durably stored --
def test_a_cycle_records_what_it_concluded_on_its_own_row(
    plane: dict[str, Any],
) -> None:
    """Not only in the event. The event is a notification; the row is the record."""

    result = cycle(plane)
    assert result.recommendation == "WAIT_HUMAN"
    stored = plane["store"].require_run(result.run.run_id)
    assert stored.next_recommendation == "WAIT_HUMAN"
    assert stored.terminal_state is TerminalState.DONE_FOR_NOW


def test_the_recommendation_and_the_terminal_state_land_together(
    plane: dict[str, Any],
) -> None:
    """One row update, so a crash cannot leave one without the other.

    A run that is terminal with no recorded conclusion is a run whose
    continuation decision has to be guessed, which is the state this column
    exists to make unreachable.
    """

    result = cycle(plane)
    with plane["db"].tx() as conn:
        row = conn.execute(
            "select status, terminal_state, next_recommendation, finished_at "
            "from research_runs where run_id = %s",
            (result.run.run_id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == str(RunStatus.SUCCEEDED)
    assert row["terminal_state"] is not None
    assert row["next_recommendation"] == "WAIT_HUMAN"
    assert row["finished_at"] is not None


def test_a_stale_start_next_cycle_payload_cannot_overrule_a_later_wait(
    plane: dict[str, Any],
) -> None:
    """The live defect, reproduced exactly.

    ``RRUN-20260918T054218Z-cb4962f4`` asked the frontier whether another cycle
    was warranted, was told ``WAIT_HUMAN``, recorded that in a finding -- and
    the build of the day concluded ``START_NEXT_CYCLE``, which is the word that
    reached the event, the work item, and the successor the daemon opened. The
    work item was still on the queue with that word in it when this release
    began, and a restart would have believed it.

    So: a run that concluded ``WAIT_HUMAN``, and a ``continue_objective`` item
    whose payload says otherwise. The run wins, and the result says so.
    """

    result = cycle(plane)
    assert plane["store"].require_run(result.run.run_id).next_recommendation == (
        "WAIT_HUMAN"
    )

    plane["queue"].enqueue(
        run_id=result.run.run_id,
        project_id=PROJECT,
        kind=WorkKind.CONTINUE_OBJECTIVE,
        payload={
            "event_id": "EVT-19700101T000000Z-stale000",
            "event_kind": "RESEARCH_CYCLE_FINISHED",
            "recommendation": "START_NEXT_CYCLE",
            "terminal_state": "DONE_FOR_NOW",
        },
        dedup_key=f"continue_objective:{result.run.run_id}:stale",
    )
    drain(plane)

    assert successors_of(plane, result.run.run_id) == []
    results = continuation_results(
        plane,
        result.run.run_id,
        dedup_key=f"continue_objective:{result.run.run_id}:stale",
    )
    assert results, "the continuation item must have run and recorded why it stopped"
    stopped = results[-1]
    assert stopped["continued"] is False
    assert stopped["parent_recommendation"] == "WAIT_HUMAN"
    assert stopped["parent_recommendation_source"] == "run record"
    # And the discarded instruction is named, so an auditor reading the row can
    # see that a decision was overruled rather than merely not taken.
    assert stopped["superseded_payload_recommendation"] == "START_NEXT_CYCLE"


def test_a_run_from_a_build_without_the_column_falls_back_and_says_so(
    plane: dict[str, Any],
) -> None:
    """Null is "we do not know", not "it recommended nothing".

    An upgraded deployment holds runs finished before the column existed. The
    honest behaviour for those is the old one -- read the payload -- and to
    record in the result that this is what happened, because a continuation
    decided from a message rather than from a run is a thing worth being able
    to find later.
    """

    result = cycle(plane)
    with plane["db"].tx() as conn:
        conn.execute(
            "update research_runs set next_recommendation = null where run_id = %s",
            (result.run.run_id,),
        )
    plane["queue"].enqueue(
        run_id=result.run.run_id,
        project_id=PROJECT,
        kind=WorkKind.CONTINUE_OBJECTIVE,
        payload={"recommendation": "WAIT_HUMAN", "terminal_state": "DONE_FOR_NOW"},
        dedup_key=f"continue_objective:{result.run.run_id}:legacy",
    )
    drain(plane)

    stopped = continuation_results(
        plane,
        result.run.run_id,
        dedup_key=f"continue_objective:{result.run.run_id}:legacy",
    )[-1]
    assert stopped["continued"] is False
    assert stopped["parent_recommendation"] == "WAIT_HUMAN"
    assert stopped["parent_recommendation_source"] == "event payload"
    assert "superseded_payload_recommendation" not in stopped


# ------------------------------------------------- WAIT_HUMAN, and it holds --
def test_wait_human_survives_a_daemon_restart(plane: dict[str, Any]) -> None:
    """A fresh control plane over the same rows must reach the same answer.

    **With a disagreeing payload planted before the restart**, which an audit
    was right to insist on: in this fixture the cycle's own event payload also
    says WAIT_HUMAN, so "no successor after a restart" was already true and the
    test proved only that a Postgres column survives a new object. The property
    that matters is that the *row* wins across a restart, and that needs
    something for it to win against.
    """

    result = cycle(plane)
    drain(plane, owner="worker-1")
    assert successors_of(plane, result.run.run_id) == []

    plane["queue"].enqueue(
        run_id=result.run.run_id,
        project_id=PROJECT,
        kind=WorkKind.CONTINUE_OBJECTIVE,
        payload={"recommendation": "START_NEXT_CYCLE"},
        dedup_key=f"continue_objective:{result.run.run_id}:across-restart",
    )

    for owner in ("worker-2", "worker-3"):
        drain(plane, owner=owner)
        stored = plane["store"].require_run(result.run.run_id)
        assert stored.next_recommendation == "WAIT_HUMAN"
        assert successors_of(plane, result.run.run_id) == []
    stopped = continuation_results(
        plane,
        result.run.run_id,
        dedup_key=f"continue_objective:{result.run.run_id}:across-restart",
    )[-1]
    assert stopped["parent_recommendation_source"] == "run record"
    assert stopped["superseded_payload_recommendation"] == "START_NEXT_CYCLE"


def test_wait_human_survives_a_database_reconnect(
    plane: dict[str, Any], pg_dsn: str
) -> None:
    """The recommendation is a row, so it must not depend on the connection.

    A separate :class:`Database` -- its own pool, its own connections -- reads
    the same conclusion. Before the column this was not reachable at all: the
    recommendation was in memory and in a message, and neither survives a pool
    being closed.
    """

    result = cycle(plane)
    with Database(pg_dsn) as reconnected:
        store = RuntimeStore(reconnected)
        run = store.require_run(result.run.run_id)
        assert run.next_recommendation == "WAIT_HUMAN"
        assert run.terminal_state is TerminalState.DONE_FOR_NOW
        # And a finding's identity is stable across the reconnect too: the
        # same key recorded through a separate pool returns the existing row
        # rather than a second citable identifier.
        keyed = RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.FRONTIER,
            summary="recorded again through a different connection pool",
            source_action=str(ActionKind.ASSESS_FRONTIER),
            semantic_key=_a_key("reconnect"),
        )
        first, created_first = store.record_finding(keyed)
        second, created_second = store.record_finding(keyed)
        assert created_first is True
        assert created_second is False
        assert second.finding_id == first.finding_id


def test_wait_human_stays_idle_across_many_polling_intervals(
    plane: dict[str, Any],
) -> None:
    """Ten passes, no science.

    The property a researcher actually cares about: the machine left running
    overnight at a human gate must still be at that gate in the morning, having
    spent nothing.
    """

    result = cycle(plane)
    drain(plane)
    calls_before = len(plane["router"].requests)
    runs_before = _run_count(plane)

    daemon = plane["build"]("overnight")
    for _ in range(10):
        daemon.tick()

    assert _run_count(plane) == runs_before
    assert len(plane["router"].requests) == calls_before, (
        "an idle control plane at a human gate must make no model call"
    )
    assert successors_of(plane, result.run.run_id) == []


def test_a_duplicate_cycle_finished_event_is_harmless(plane: dict[str, Any]) -> None:
    """Delivered twice, ingested once. The dedup key is the run."""

    result = cycle(plane)
    plane["store"].record_event(
        kind="RESEARCH_CYCLE_FINISHED",
        project_id=PROJECT,
        run_id=result.run.run_id,
        payload={"terminal_state": "DONE_FOR_NOW", "recommendation": "WAIT_HUMAN"},
        dedup_key=f"cycle-finished:{result.run.run_id}",
    )
    drain(plane)

    with plane["db"].tx() as conn:
        count = conn.execute(
            "select count(*) as n from events where dedup_key = %s",
            (f"cycle-finished:{result.run.run_id}",),
        ).fetchone()
    assert count is not None and int(count["n"]) == 1
    assert successors_of(plane, result.run.run_id) == []


def test_a_duplicate_capsule_change_event_is_harmless(plane: dict[str, Any]) -> None:
    """Two observations of one scientific change are one change."""

    key = f"capsule-changed:{PROJECT}:aaaa->bbbb"
    for _ in range(3):
        plane["store"].record_event(
            kind="CAPSULE_CHANGED",
            project_id=PROJECT,
            payload={"capsule_digest": "b" * 64, "frontier_digest": "c" * 64},
            dedup_key=key,
        )
    with plane["db"].tx() as conn:
        count = conn.execute(
            "select count(*) as n from events where dedup_key = %s", (key,)
        ).fetchone()
    assert count is not None and int(count["n"]) == 1


def test_a_cancelled_successor_does_not_revive_on_restart(
    plane: dict[str, Any],
) -> None:
    """Cancelling a cycle is a person's act; a retried queue row cannot undo it.

    The live state this release began from: a successor opened, a researcher
    cancelled it, and the ``continue_objective`` item that opened it was left
    ``LEASED`` with an expired lease. Recovery reclaims it and runs it again.
    Before the pre-check, that hit ``research_runs_one_successor_idx`` as an
    exception -- three failed attempts and a dead-lettered item for a system
    behaving exactly as designed.
    """

    parent = cycle(plane, objective="continue me")
    with plane["db"].tx() as conn:
        conn.execute(
            "update research_runs set next_recommendation = 'START_NEXT_CYCLE' "
            "where run_id = %s",
            (parent.run.run_id,),
        )
    # The successor the parent's continuation opened, and its cancellation.
    successor = plane["store"].create_run(
        project_id=PROJECT,
        objective="continue me",
        parent_run_id=parent.run.run_id,
        cycle_index=parent.run.cycle_index + 1,
    )
    plane["store"].set_run_status(
        successor.run_id,
        RunStatus.CANCELLED,
        terminal_state=TerminalState.CANCELLED,
        detail="a person stopped this",
    )

    plane["queue"].enqueue(
        run_id=parent.run.run_id,
        project_id=PROJECT,
        kind=WorkKind.CONTINUE_OBJECTIVE,
        payload={"recommendation": "START_NEXT_CYCLE"},
        dedup_key=f"continue_objective:{parent.run.run_id}",
    )
    drain(plane)

    assert successors_of(plane, parent.run.run_id) == [successor.run_id]
    stopped = continuation_results(plane, parent.run.run_id)[-1]
    assert stopped["continued"] is False
    assert "already has a successor" in stopped["reason"]
    # A refusal, not a failure. The item must not be retried or dead-lettered.
    with plane["db"].tx() as conn:
        row = conn.execute(
            "select status, attempts, failure_class from work_items "
            "where run_id = %s and kind = %s",
            (parent.run.run_id, WorkKind.CONTINUE_OBJECTIVE),
        ).fetchone()
    assert row is not None
    assert row["status"] == str(WorkStatus.SUCCEEDED)
    assert row["failure_class"] is None


def test_replaying_the_continuation_creates_no_second_successor(
    plane: dict[str, Any],
) -> None:
    """The same continuation run twice -- a lease lost, a crash, a duplicate
    delivery -- must add nothing the first one did not."""

    parent = cycle(plane, objective="continue me twice")
    with plane["db"].tx() as conn:
        conn.execute(
            "update research_runs set next_recommendation = 'START_NEXT_CYCLE' "
            "where run_id = %s",
            (parent.run.run_id,),
        )
    for suffix in ("first", "second", "third"):
        plane["queue"].enqueue(
            run_id=parent.run.run_id,
            project_id=PROJECT,
            kind=WorkKind.CONTINUE_OBJECTIVE,
            payload={"recommendation": "START_NEXT_CYCLE"},
            dedup_key=f"continue_objective:{parent.run.run_id}:{suffix}",
        )
    drain(plane, ticks=12)

    assert len(successors_of(plane, parent.run.run_id)) == 1, (
        "one parent, at most one successor, however many times it is asked"
    )
    # The count alone proves only `research_runs_one_successor_idx`, which
    # predates this work -- an audit pointed that out, correctly. What this
    # handler adds is that losing the race is a *refusal* and not a failure:
    # without the pre-check and the catch, the second and third items raise
    # out of `start_cycle`, retry, and dead-letter, while the successor count
    # stays at one and this test stays green.
    with plane["db"].tx() as conn:
        rows = conn.execute(
            "select status, attempts, failure_class, result from work_items "
            "where run_id = %s and kind = %s order by created_at",
            (parent.run.run_id, WorkKind.CONTINUE_OBJECTIVE),
        ).fetchall()
    assert len(rows) >= 3
    for row in rows:
        assert row["status"] == str(WorkStatus.SUCCEEDED), dict(row)
        assert row["failure_class"] is None, dict(row)
        assert int(row["attempts"]) == 1, "a refusal must not be retried"
    refused = [
        dict(row["result"] or {})
        for row in rows
        if not dict(row["result"] or {}).get("continued")
    ]
    assert refused, "the later items must record why they opened nothing"
    assert any("already has a successor" in str(item.get("reason")) for item in refused)


def test_a_successful_continuation_records_both_cycles_distinctly(
    plane: dict[str, Any],
) -> None:
    """The parent's conclusion and the successor's are different facts.

    The success path already reports the successor's ``recommendation``, from
    ``_cycle_result_payload``. The first version of this handler's result put
    the *parent's* under the same key, so a refusal reported the parent and a
    success silently reported the child -- while the source field beside it
    described the parent in both cases. An auditor reading the row would have
    been reading the wrong cycle.
    """

    parent = cycle(plane, objective="continue me and record both")
    with plane["db"].tx() as conn:
        conn.execute(
            "update research_runs set next_recommendation = 'START_NEXT_CYCLE' "
            "where run_id = %s",
            (parent.run.run_id,),
        )
    plane["queue"].enqueue(
        run_id=parent.run.run_id,
        project_id=PROJECT,
        kind=WorkKind.CONTINUE_OBJECTIVE,
        payload={"recommendation": "START_NEXT_CYCLE"},
        dedup_key=f"continue_objective:{parent.run.run_id}:both",
    )
    drain(plane, ticks=12)

    successors = successors_of(plane, parent.run.run_id)
    assert len(successors) == 1
    opened = continuation_results(
        plane,
        parent.run.run_id,
        dedup_key=f"continue_objective:{parent.run.run_id}:both",
    )[-1]
    assert opened["continued"] is True
    assert opened["successor_run_id"] == successors[0]
    # The parent's conclusion, which licensed the continuation.
    assert opened["parent_recommendation"] == "START_NEXT_CYCLE"
    assert opened["parent_recommendation_source"] == "run record"
    # The successor's own conclusion, which is a different cycle's answer. The
    # scripted frontier recommends waiting, so the two differ -- which is the
    # whole point of keeping them apart.
    assert opened["recommendation"] == "WAIT_HUMAN"
    assert opened["run_id"] == successors[0]


def test_the_lineage_ceiling_refuses_a_durable_recommendation_too(
    plane: dict[str, Any],
) -> None:
    """The bounds still bind when the recommendation comes from the row.

    An audit noticed that every pre-existing continuation test builds its runs
    without ``next_recommendation``, so after this change they all exercise the
    *fallback* branch -- and the ceiling was never refused from a durable
    recommendation. The bound lives in ``should_continue`` and does not care
    which source the recommendation came from, and that is exactly the kind of
    claim worth one assertion.
    """

    ceiling = plane["config"].settings.max_cycles_per_objective
    store: RuntimeStore = plane["store"]
    # A real lineage, because the ceiling is measured by `lineage_depth`'s
    # recursive walk of `parent_run_id` and not by `cycle_index`. Setting the
    # index would have produced a test that passes for the wrong reason.
    leaf = store.create_run(project_id=PROJECT, objective="run into the ceiling")
    for index in range(1, ceiling):
        leaf = store.create_run(
            project_id=PROJECT,
            objective="run into the ceiling",
            parent_run_id=leaf.run_id,
            cycle_index=index,
        )
    assert store.lineage_depth(leaf.run_id) + 1 >= ceiling

    store.set_run_status(leaf.run_id, RunStatus.RUNNING)
    store.set_run_status(
        leaf.run_id,
        RunStatus.SUCCEEDED,
        terminal_state=TerminalState.DONE_FOR_NOW,
        next_recommendation="START_NEXT_CYCLE",
    )
    plane["queue"].enqueue(
        run_id=leaf.run_id,
        project_id=PROJECT,
        kind=WorkKind.CONTINUE_OBJECTIVE,
        payload={"recommendation": "START_NEXT_CYCLE"},
        dedup_key=f"continue_objective:{leaf.run_id}:ceiling",
    )
    drain(plane)

    assert successors_of(plane, leaf.run_id) == []
    stopped = continuation_results(
        plane, leaf.run_id, dedup_key=f"continue_objective:{leaf.run_id}:ceiling"
    )[-1]
    assert stopped["continued"] is False
    assert stopped["parent_recommendation_source"] == "run record"
    assert "ceiling" in stopped["reason"], stopped["reason"]


def test_the_same_human_decision_delivered_twice_resumes_once(
    plane: dict[str, Any],
) -> None:
    """Duplicate delivery of one decision, which had no test.

    The event's dedup key is the approval, so a second copy of the *same*
    decision is swallowed at ingest; and if one did get through, the resume
    handler short-circuits on a run that has already finished. Both halves
    asserted, because "it is deduplicated" and "it would be harmless anyway"
    are different guarantees and a change could remove either.
    """

    parent = cycle(plane)
    approval = plane["store"].request_approval(
        run_id=parent.run.run_id,
        project_id=PROJECT,
        kind="accept_claim",
        question="Authorise accept_claim?",
        packet={"decision_required": "accept_claim"},
        interrupt_key=f"{parent.run.run_id}:0:accept_claim",
    )[0]
    for _ in range(3):
        plane["store"].record_event(
            kind="SCIENTIFIC_DECISION_RECORDED",
            project_id=PROJECT,
            run_id=parent.run.run_id,
            payload={"approval_id": approval.approval_id, "granted": True},
            dedup_key=f"decision:{approval.approval_id}",
        )

    with plane["db"].tx() as conn:
        events = conn.execute(
            "select count(*) as n from events where dedup_key = %s",
            (f"decision:{approval.approval_id}",),
        ).fetchone()
    assert events is not None and int(events["n"]) == 1, "one decision, one event"

    runs_before = _run_count(plane)
    drain(plane, ticks=8)

    # The run already concluded, so the resume is a recorded no-op rather than
    # a re-entry into a finished thread.
    with plane["db"].tx() as conn:
        rows = conn.execute(
            "select status, result from work_items where run_id = %s and kind = %s",
            (parent.run.run_id, WorkKind.RESUME_CYCLE),
        ).fetchall()
    assert len(rows) == 1, "one resume item, however many deliveries"
    assert rows[0]["status"] == str(WorkStatus.SUCCEEDED)
    assert "skipped" in dict(rows[0]["result"] or {})
    assert _run_count(plane) == runs_before


def _run_count(plane: dict[str, Any]) -> int:
    with plane["db"].tx() as conn:
        row = conn.execute("select count(*) as n from research_runs").fetchone()
    return int(row["n"]) if row else 0


def test_a_cycle_waiting_on_a_gate_records_wait_human_on_its_row(
    runtime_db: Database, pg_dsn: str, tmp_path: Path
) -> None:
    """The interrupt branch, asserted.

    An audit found this gap and called it the highest-value, lowest-cost one in
    the change: ``cycles._execute``'s interrupt branch writes
    ``next_recommendation="WAIT_HUMAN"`` with the comment "a cycle sitting on
    an interrupt is the state that must survive a restart unchanged", three
    existing tests execute that branch, and nothing anywhere asserted the
    write. Every cycle in the rest of this file plans ``ASSESS_FRONTIER``,
    which is A1 and never gates, so none of them reach it.

    A cycle that needs A2 authority gates on a real LangGraph interrupt. The
    row must then say WAIT_HUMAN, so that a restart arriving before the person
    decides reads the same answer.
    """

    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project", project_id=PROJECT)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(repo))
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ACCEPT_CLAIM)),
            "scientific_reviewer": review_answer(),
        }
    )
    result = start_cycle(
        config=make_config(pg_dsn, tmp_path / "artifacts"),
        db=runtime_db,
        project_id=PROJECT,
        repo_path=repo,
        objective="accept a claim, which needs a person",
        models=router,
        autonomy=Autonomy.HIGH,
    )

    assert result.status is RunStatus.WAITING_HUMAN, result.notes
    assert result.terminal_state is TerminalState.WAITING_FOR_SCIENTIFIC_DECISION
    assert result.recommendation == "WAIT_HUMAN"
    # The row, which is the point: the in-memory result is gone after a restart.
    stored = store.require_run(result.run.run_id)
    assert stored.next_recommendation == "WAIT_HUMAN"
    with Database(pg_dsn) as reconnected:
        assert (
            RuntimeStore(reconnected).require_run(result.run.run_id).next_recommendation
            == "WAIT_HUMAN"
        )


def test_the_recorded_conclusion_follows_the_run_through_a_resume(
    plane: dict[str, Any], runtime_db: Database, pg_dsn: str, tmp_path: Path
) -> None:
    """The column is write-once-sticky, so a transition has to be asserted.

    ``set_run_status`` writes ``next_recommendation = coalesce(%(new)s,
    next_recommendation)``, which can replace a value and can never clear one.
    Every call site that omits the parameter -- the ``RUNNING`` transition,
    cancellation, the failure paths -- silently preserves whatever was there.

    An audit named the hazard: if a future conclusion path stops passing the
    parameter, a run that once gated keeps ``WAIT_HUMAN`` forever and
    ``_work_continue_objective`` refuses its continuation permanently, with
    nothing failing. So this drives one run through gate -> decision -> resume
    -> conclude and asserts the column ends at the *resumed* conclusion rather
    than at the gate's.
    """

    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "resumed", project_id=PROJECT)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(repo))
    config = make_config(pg_dsn, tmp_path / "artifacts")
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ACCEPT_CLAIM)),
            "scientific_reviewer": review_answer(),
        }
    )
    gated = start_cycle(
        config=config,
        db=runtime_db,
        project_id=PROJECT,
        repo_path=repo,
        objective="accept a claim, then carry on",
        models=router,
        autonomy=Autonomy.HIGH,
    )
    assert store.require_run(gated.run.run_id).next_recommendation == "WAIT_HUMAN"

    # The person declines, which is a recorded decision either way.
    approvals = store.list_approvals(run_id=gated.run.run_id)
    assert approvals, "the gate recorded no approval to decide"
    store.record_decision(
        approvals[0].approval_id,
        granted=False,
        decision={"verdict": "declined", "reason": "not on this evidence"},
        decided_by="a-researcher",
    )
    resumed = resume_cycle(
        config=config,
        db=runtime_db,
        run_id=gated.run.run_id,
        repo_path=repo,
        models=router,
        resume_value={"source": "recorded decision"},
    )

    assert resumed.terminal_state is not TerminalState.WAITING_FOR_SCIENTIFIC_DECISION
    stored = store.require_run(gated.run.run_id)
    assert stored.next_recommendation == resumed.recommendation, (
        "the row must carry the resumed conclusion, not the gate's WAIT_HUMAN"
    )
    assert stored.next_recommendation != "WAIT_HUMAN" or resumed.recommendation == (
        "WAIT_HUMAN"
    )


# ------------------------------------------------- frontier finding identity --
def _a_key(seed: str) -> str:
    """A well-formed semantic key for a test.

    The shape is enforced now: a constant such as ``"assess_frontier"`` is
    refused, because a producer that set one would collapse every finding of
    its kind onto the first row. So a test's keys have to be digests too.
    """

    import hashlib

    return f"assess_frontier:v2:{hashlib.sha256(seed.encode()).hexdigest()}"


def _record_frontier_finding(
    plane: dict[str, Any], *, semantic_key: str, summary: str, excerpt: str = ""
) -> tuple[Any, bool]:
    return plane["store"].record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.FRONTIER,
            summary=summary,
            excerpt=excerpt,
            source_action=str(ActionKind.ASSESS_FRONTIER),
            semantic_key=semantic_key,
        )
    )


def test_two_identical_assessments_are_one_finding(plane: dict[str, Any]) -> None:
    first, created_first = _record_frontier_finding(
        plane,
        semantic_key=_a_key("k"),
        summary="5 ranked candidate(s); recommends WAIT_HUMAN",
    )
    second, created_second = _record_frontier_finding(
        plane,
        semantic_key=_a_key("k"),
        summary="5 ranked candidate(s); recommends WAIT_HUMAN",
    )
    assert created_first is True
    assert created_second is False
    assert second.finding_id == first.finding_id


#: The excerpt the first occurrence stores, and the variants of it below.
#:
#: The whitespace case was mislabelled in the first version of this file -- an
#: audit noticed that the two strings were entirely different rather than
#: whitespace variants of one, so both parameters proved the same thing. It is
#: a real variant now.
_ORIGINAL_EXCERPT = "why: the questions are already in front of the researcher"


@pytest.mark.parametrize(
    ("summary", "excerpt", "what"),
    [
        (
            "5 ranked candidate(s); recommends WAIT_HUMAN",
            "why:   the questions   are already  in front of the researcher\n\n",
            "cosmetic whitespace",
        ),
        (
            "5 ranked candidate(s); recommends WAIT_HUMAN",
            "why: the same conclusion, said differently this time",
            "reworded prose",
        ),
    ],
    ids=["whitespace", "rewording"],
)
def test_wording_does_not_mint_a_second_finding(
    plane: dict[str, Any], summary: str, excerpt: str, what: str
) -> None:
    """The defect, at the level of the store.

    The content digest moves with the prose, so a reworded rationale was a new
    citable identifier for the same observation.
    """

    first, _ = _record_frontier_finding(
        plane, semantic_key=_a_key("k"), summary=summary, excerpt=_ORIGINAL_EXCERPT
    )
    second, created = _record_frontier_finding(
        plane, semantic_key=_a_key("k"), summary=summary, excerpt=excerpt
    )
    assert created is False, f"{what} must not create a finding"
    assert second.finding_id == first.finding_id
    # The first occurrence's wording is what is stored, because a finding is
    # immutable: a citation is worth nothing if the cited text can change.
    assert second.excerpt == _ORIGINAL_EXCERPT


def test_the_identity_survives_a_fresh_store_and_daemon(
    plane: dict[str, Any],
) -> None:
    """Case 2 of the twelve, which had no test.

    The digest is computed from the finding, so nothing about it depends on
    process state -- but "nothing depends on process state" is the kind of
    claim that is worth one cheap assertion rather than an argument, and the
    audit was right that restart was asserted for the run column and not for
    finding identity.
    """

    key = _a_key("across-a-restart")
    first, created_first = _record_frontier_finding(
        plane, semantic_key=key, summary="recorded before the restart"
    )
    assert created_first is True

    # A fresh store and a fresh control plane over the same rows.
    fresh = RuntimeStore(plane["db"])
    plane["build"]("worker-after-restart").tick()
    second, created_second = fresh.record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.FRONTIER,
            summary="recorded after the restart, differently worded",
            source_action=str(ActionKind.ASSESS_FRONTIER),
            semantic_key=key,
        )
    )
    assert created_second is False
    assert second.finding_id == first.finding_id


def test_a_different_conclusion_is_a_different_finding(plane: dict[str, Any]) -> None:
    first, _ = _record_frontier_finding(
        plane, semantic_key=_a_key("wait"), summary="waiting"
    )
    second, created = _record_frontier_finding(
        plane, semantic_key=_a_key("continue"), summary="continuing"
    )
    assert created is True
    assert second.finding_id != first.finding_id


def test_a_finding_without_a_stated_identity_keeps_the_content_digest(
    plane: dict[str, Any],
) -> None:
    """Every other handler is unaffected, and every row already cited keeps
    the digest it was cited under."""

    unkeyed = RuntimeFinding(
        project_id=PROJECT,
        kind=FindingKind.REVIEW,
        summary="6 alternative explanation(s) for 5 target(s)",
        source_action=str(ActionKind.CRITIQUE_HYPOTHESES),
    )
    keyed = unkeyed.model_copy(update={"semantic_key": _a_key("k")})
    assert unkeyed.digest != keyed.digest
    # And the unkeyed digest is exactly the historical material: adding the
    # field must not restate findings a proposal already rests on.
    assert unkeyed.digest == _historical_digest(unkeyed)


def _historical_digest(finding: RuntimeFinding) -> str:
    """The digest as it was computed before ``semantic_key`` existed."""

    import hashlib

    material = json.dumps(
        {
            "v": 1,
            "project_id": finding.project_id,
            "kind": str(finding.kind),
            "summary": finding.summary,
            **({"excerpt": finding.excerpt} if finding.excerpt else {}),
            "source_action": finding.source_action or "",
            "artifact_ids": sorted(finding.artifact_ids),
            "capsule_refs": sorted(finding.capsule_refs),
            "literature_keys": sorted(finding.literature_keys),
            "experiment_job_id": finding.experiment_job_id or "",
            "spec_digest": finding.spec_digest or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def test_only_the_frontier_assessment_states_a_semantic_identity() -> None:
    """Structural, because a wrong key merges citable findings permanently.

    Both independent reviews raised this. ``SEMANTIC_KEY`` is a new dedup
    extension point; a producer that set a *constant* would collapse every
    finding of its ``(project, kind, source_action)`` onto the first row, and
    the obligation in ``actions.base.SEMANTIC_KEY`` was prose only. The shape
    validator on ``RuntimeFinding.semantic_key`` now refuses a constant, and
    this is the other half: a second producer appearing at all is a decision
    that should be made deliberately rather than noticed later.

    Written the way ``tests/test_runtime_authority.py`` writes its boundaries
    -- by parsing the package, not by trusting this docstring.
    """

    import ast

    runtime = Path(__file__).resolve().parents[1] / "src" / "research_os" / "runtime"
    producers: list[str] = []
    for path in sorted(runtime.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `SEMANTIC_KEY: <expr>` as a dict key is how a handler sets one.
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Name) and key.id == "SEMANTIC_KEY":
                        producers.append(path.relative_to(runtime).as_posix())
    assert sorted(set(producers)) == ["actions/review.py"], (
        "a second handler sets a semantic identity. That is allowed, and it is "
        "a deliberate decision: two findings with equal keys are the same "
        "finding permanently and citably. Read "
        "research_os.runtime.actions.base.SEMANTIC_KEY, satisfy its "
        f"obligation, then add the module here. Found: {sorted(set(producers))}"
    )


def test_a_degraded_assessment_is_one_finding_however_often_it_repeats(
    plane: dict[str, Any],
) -> None:
    """The path the identity fix skipped, and the one most likely to repeat.

    Both reviews found it independently. When the provider is unavailable the
    handler degrades to "the deterministic frontier stands" -- and the summary
    it records embeds the exception text. ``BudgetExhaustedError``'s message
    carries the run id and a moving dollar figure, so the summary was
    *guaranteed* unique per cycle: every outage minted a new citable finding,
    and twenty of them filled the planner's window with a provider problem.

    Driven through the real graph with the frontier role failing twice, and
    **with a different error string each time** -- which is the part that makes
    this test mean anything. A first version used ``ScriptedRouter.fail_roles``,
    whose error text is a constant, so the content digest was already stable
    and the test passed with the fix removed. A real provider error is not a
    constant: ``BudgetExhaustedError`` names the run and how much is left.
    """

    class FailingFrontier:
        """A router whose frontier error differs per call, as a real one does."""

        def __init__(self, inner: ScriptedRouter) -> None:
            self._inner = inner
            self.calls = 0
            self.requests = inner.requests

        def complete(self, request: Any) -> Any:
            if str(request.role) == "frontier":
                self.calls += 1
                self.requests.append(request)
                return ModelResponse(
                    provider="scripted",
                    model="scripted-1",
                    error=(
                        "model_cost_usd budget for run:RRUN-"
                        f"{self.calls:028d} has 0.01 left; 0.50 was requested"
                    ),
                    independence=Independence.NONE,
                )
            return self._inner.complete(request)

        def requests_for(self, role: str) -> list[Any]:
            return self._inner.requests_for(role)

    plane["router"] = FailingFrontier(plane["router"])
    first = cycle(plane, objective="assess with no provider")
    second = cycle(plane, objective="assess with no provider")
    assert first.run.run_id != second.run.run_id
    assert plane["router"].calls == 2, "the frontier role must have been asked twice"

    degraded = [
        item
        for item in plane["store"].list_findings(project_id=PROJECT, limit=50)
        if item.source_action == str(ActionKind.ASSESS_FRONTIER)
    ]
    assert len(degraded) == 1, (
        "a provider outage repeated is one observation about the frontier, not "
        f"one per cycle; got {[item.summary for item in degraded]}"
    )
    assert degraded[0].semantic_key.startswith("assess_frontier:v2:")


def test_the_identity_ignores_run_cycle_and_timestamp(plane: dict[str, Any]) -> None:
    """Which cycle noticed something is not part of what was noticed."""

    base: dict[str, Any] = {
        "project_id": PROJECT,
        "kind": FindingKind.FRONTIER,
        "summary": "5 ranked candidate(s); recommends WAIT_HUMAN",
        "source_action": str(ActionKind.ASSESS_FRONTIER),
        "semantic_key": _a_key("k"),
    }
    first = RuntimeFinding(
        **base, source_run_id="RRUN-A", source_cycle=0, source_work_id="WORK-A"
    )
    second = RuntimeFinding(
        **base,
        source_run_id="RRUN-B",
        source_cycle=7,
        source_work_id="WORK-B",
        created_at="2030-01-01T00:00:00Z",
    )
    assert first.digest == second.digest


def test_the_identity_ignores_the_order_of_the_frontier_it_assessed() -> None:
    """A reordered input is the same input.

    ``assessment_identity`` hashes the frontier digest, which is over sorted
    identifiers, and each candidate's addresses, which are sorted here. So a
    frontier listing the same questions in a different order, ranked by an
    answer listing the same targets in a different order, is one assessment.
    """

    from research_os.runtime.actions.review import assessment_identity

    class _Frontier:
        def __init__(self, questions: tuple[str, ...]) -> None:
            self.open_questions = questions
            self.actionable_hypotheses = ()
            self.hypotheses_without_tests = ()
            self.claims_awaiting_review = ()
            self.claims_with_stale_review = ()
            self.contested_claims = ()
            self.evidence_gaps = ()
            self.pending_experiments = ()
            self.validation_errors = ()

        @property
        def empty(self) -> bool:
            return False

    forward = assessment_identity(
        _Frontier(("Q-0001", "Q-0002")),
        recommendation="WAIT_HUMAN",
        actions=[
            {"action": "propose_capsule_change", "addresses": ["Q-0001", "Q-0002"]}
        ],
    )
    reversed_ = assessment_identity(
        _Frontier(("Q-0002", "Q-0001")),
        recommendation="WAIT_HUMAN",
        actions=[
            {"action": "propose_capsule_change", "addresses": ["Q-0002", "Q-0001"]}
        ],
    )
    assert forward == reversed_

    # And a frontier that actually gained a question is a different assessment.
    grown = assessment_identity(
        _Frontier(("Q-0001", "Q-0002", "Q-0003")),
        recommendation="WAIT_HUMAN",
        actions=[
            {"action": "propose_capsule_change", "addresses": ["Q-0001", "Q-0002"]}
        ],
    )
    assert grown != forward

    # **Case 11, which had no test.** Two assessments identical but for what
    # they propose to address. `addresses` is the field coverage is computed
    # from, so collapsing these would deduplicate away a real change of mind
    # about which question the next cycle is for.
    assert (
        assessment_identity(
            _Frontier(("Q-0001", "Q-0002")),
            recommendation="WAIT_HUMAN",
            actions=[{"action": "propose_capsule_change", "addresses": ["Q-0002"]}],
        )
        != forward
    )

    # As is one that changed its recommendation, or what it ranked first.
    assert (
        assessment_identity(
            _Frontier(("Q-0001", "Q-0002")),
            recommendation="START_NEXT_CYCLE",
            actions=[
                {"action": "propose_capsule_change", "addresses": ["Q-0001", "Q-0002"]}
            ],
        )
        != forward
    )
    assert (
        assessment_identity(
            _Frontier(("Q-0001", "Q-0002")),
            recommendation="WAIT_HUMAN",
            actions=[
                {"action": "derive_mathematics", "addresses": ["Q-0001", "Q-0002"]}
            ],
        )
        != forward
    )


def test_twenty_repeated_assessments_do_not_fill_the_planner_window(
    plane: dict[str, Any],
) -> None:
    """The consequence that matters, measured at the window rather than the row.

    The planner sees at most ``MAX_PLANNER_FINDINGS``. Twenty repetitions of one
    assessment used to occupy all twelve and push the project's real findings
    out -- so a cycle that had audited the literature was shown twelve copies
    of "we assessed the frontier" and nothing about the audit.
    """

    audit, _ = plane["store"].record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.LITERATURE,
            summary="8 works on working-set methods",
            source_action=str(ActionKind.SEARCH_LITERATURE),
        )
    )
    for index in range(20):
        _record_frontier_finding(
            plane,
            semantic_key=_a_key("k"),
            summary="5 ranked candidate(s); recommends WAIT_HUMAN",
            excerpt=f"why: phrasing number {index}",
        )

    assert plane["store"].count_findings(project_id=PROJECT) == 2
    science = noncanonical_science(plane["store"], project_id=PROJECT)
    assert len(science.findings) <= MAX_PLANNER_FINDINGS
    shown = {str(entry["finding_id"]) for entry in science.findings}
    assert audit.finding_id in shown, (
        "repeated assessments must not push real findings out of the window"
    )


def test_the_handler_sets_a_stable_key_across_cycles(plane: dict[str, Any]) -> None:
    """That the handler states an identity at all, and that it is stable.

    **Retitled after an audit, which was right.** The old name --
    "a real repeated assessment produces one finding" -- claimed the headline
    property, and the assertion that would establish it is vacuous here: with
    an identical scripted answer the excerpt and the ranking artifact hash
    identically, so one finding is what the *content* digest would have given
    too. What this test does establish is that a key is set, that it is
    well-formed, and that two cycles over one frontier produce the same one.
    The property itself is
    ``test_the_handler_states_an_identity_that_excludes_its_own_prose``, where
    only the wording differs between the two cycles.
    """

    first = cycle(plane)
    second = cycle(plane)
    assert first.run.run_id != second.run.run_id

    frontier_findings = [
        item
        for item in plane["store"].list_findings(project_id=PROJECT, limit=50)
        if item.source_action == str(ActionKind.ASSESS_FRONTIER)
    ]
    assert len(frontier_findings) == 1, (
        "two assessments of one frontier reaching one conclusion is one finding"
    )
    assert frontier_findings[0].semantic_key.startswith("assess_frontier:v2:")
    # And the key reached the row, so "why are these the same finding" is
    # answerable without recomputing a digest.
    assert frontier_findings[0].source_run_id == first.run.run_id


def test_the_handler_states_an_identity_that_excludes_its_own_prose(
    plane: dict[str, Any],
) -> None:
    """Change only the wording of the answer; the identity must not move."""

    first = cycle(plane)
    plane["router"].answers["frontier"] = frontier_answer(
        "WAIT_HUMAN", rationale="entirely different words, identical conclusion."
    )
    second = cycle(plane)

    frontier_findings = [
        item
        for item in plane["store"].list_findings(project_id=PROJECT, limit=50)
        if item.source_action == str(ActionKind.ASSESS_FRONTIER)
    ]
    assert len(frontier_findings) == 1, (
        "a rationale reworded is not a second observation"
    )
    assert first.recommendation == second.recommendation == "WAIT_HUMAN"
    assert frontier_findings[0].semantic_key.startswith("assess_frontier:v2:")
