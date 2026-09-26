"""INV-03 -- a stage has at most one live execution owner, and time is not death.

The HIGH finding this file closes (final adversarial review of 37e8afe, H3):
``track.advance_idea`` never passed its work item's id to ``open_action``, so
every ``idea_actions.work_id`` was NULL, ``stale_actions`` joined against
NULL, and any stage older than ``stale_action_grace_seconds`` (600 s) was
reclaimed as ``worker_crash`` while its worker was still running. The idea
was then free to be bought again, and the first execution's late rows landed
beside the second's.

The invariant (``docs/ARCHITECTURE_INVARIANTS.md``, INV-03): at most one valid
live owner; elapsed time alone is never proof of a crash; re-execution needs
a new attempt identity; a late result from an obsolete attempt writes nothing.

What is held here:

- a stage running far past the old grace period, with a live owner, is not
  reclaimed -- the original reproduction, in the daemon's own shape;
- the lease is a heartbeat: renewed, the owner is live; lapsed, and with no
  session lock, it is dead;
- a worker killed with ``os._exit`` mid-stage is reclaimable once its lease
  has truly run out, and not before;
- a second purchase of a live stage starts no second execution;
- a late result from an attempt that lost ownership writes nothing;
- after a restart, the retried work item takes over the dead owner's action
  at once, and the stage completes under the new attempt.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import stages
from research_os.portfolio.allocation import ADVANCE_IDEA
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    ActionStatus,
    EvidenceKind,
    OperationalState,
    Stage,
)
from research_os.portfolio.store import (
    OWNER_DEAD,
    OWNER_LIVE,
    OWNER_UNKNOWN,
    PortfolioStore,
    StaleExecutionError,
)
from research_os.portfolio.track import TrackResult, advance_idea
from research_os.runtime.db import Database
from research_os.runtime.leases import LeaseKeeper
from research_os.runtime.locks import LockClass, lock_key, research_run_lock
from research_os.runtime.migrations import ADVISORY_NAMESPACE
from research_os.runtime.models import RunStatus
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_graph_helpers import make_config
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_promotion import (
    LITERATURE_FALSIFIER,
    TerminologyAwareLiterature,
    TwoPathRouter,
    _router,
    checkpoint_tables,
)
from tests.test_portfolio_tick import _tick

__all__ = [
    "checkpoint_tables",
    "pg_dsn",
    "portfolio",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).parent / "runtime_scripts" / "die_mid_stage.py"


# --------------------------------------------------------------- helpers ----
def _step(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
    idea_id: str,
    router: Any,
    *,
    work_id: str | None = None,
) -> TrackResult:
    return advance_idea(
        runtime_config=make_config(pg_dsn, tmp_path / "artifacts"),
        portfolio_config=load_config(),
        db=runtime_db,
        project_id=project,
        idea_id=idea_id,
        models=router,
        literature=TerminologyAwareLiterature(),
        work_id=work_id,
    )


def _to_the_audit(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
) -> str:
    """An idea whose next stage is the literature audit -- a real model call."""

    idea, _ = seed_idea(portfolio, project, falsifier=LITERATURE_FALSIFIER)
    router = _router(runtime_db)
    for _ in range(12):
        snapshot = stages.snapshot_for(portfolio, portfolio.require_idea(idea.idea_id))
        assert snapshot is not None
        stage, _reason = stages.select_stage(snapshot, load_config())
        if stage is Stage.LITERATURE_AUDIT:
            return idea.idea_id
        result = _step(runtime_db, pg_dsn, tmp_path, project, idea.idea_id, router)
        assert result.ok, result.detail
    raise AssertionError("the idea never reached its literature audit")


def _leased(runtime_db: Database, project: str, idea_id: str, *, owner: str) -> str:
    queue = WorkQueue(runtime_db)
    queue.enqueue(
        project_id=project,
        kind=ADVANCE_IDEA,
        payload={"idea_id": idea_id},
        dedup_key=f"ownership:{owner}:{time.monotonic_ns()}",
    )
    (item,) = queue.claim(owner=owner, lease_seconds=3600, kinds=(ADVANCE_IDEA,))
    return item.work_id


class _BlockingScout(TwoPathRouter):
    """A scout whose call blocks until the test lets it return."""

    entered: threading.Event
    release: threading.Event

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        if str(request.role) == "literature_scout":
            self.entered.set()
            assert self.release.wait(120), "the test never released the scout"
        return super().complete(request)


def _blocking(runtime_db: Database) -> _BlockingScout:
    base = _router(runtime_db)
    router = _BlockingScout(answers=base.answers, store=base.store)
    router.entered = threading.Event()
    router.release = threading.Event()
    return router


class _Recorder(TwoPathRouter):
    calls: list[str]
    call_ids: set[str]

    def complete(self, request: Any) -> Any:  # type: ignore[override]
        self.calls.append(str(request.role))
        response = super().complete(request)
        self.call_ids.add(str(response.call_id))
        return response


def _recording(runtime_db: Database) -> _Recorder:
    base = _router(runtime_db)
    router = _Recorder(answers=base.answers, store=base.store)
    router.calls = []
    router.call_ids = set()
    return router


def _in_thread(fn: Any) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _age(runtime_db: Database, idea_id: str, *, minutes: int = 20) -> None:
    with runtime_db.tx() as conn:
        conn.execute(
            "update idea_actions set updated_at = now() - make_interval(mins => %s) "
            "where idea_id = %s and status = 'ACTIVE'",
            (minutes, idea_id),
        )


def _expire_lease(runtime_db: Database, work_id: str) -> None:
    with runtime_db.tx() as conn:
        conn.execute(
            "update work_items set lease_expires_at = now() - interval '1 second' "
            "where work_id = %s",
            (work_id,),
        )


def _kill_lock_session(runtime_db: Database, run_id: str) -> None:
    """End the session holding a run's lock, as a dropped connection would."""

    key = lock_key(LockClass.RESEARCH_RUN, run_id)
    with runtime_db.tx() as conn:
        rows = conn.execute(
            "select pid from pg_locks where locktype = 'advisory' and granted "
            "and classid = %s and objid = %s",
            (ADVISORY_NAMESPACE, key),
        ).fetchall()
        for row in rows:
            conn.execute("select pg_terminate_backend(%s)", (row["pid"],))
    assert rows, "no session held the run lock"


# -------------------------------------------- the original reproduction ----
def test_a_live_stage_far_past_the_grace_period_is_not_reclaimed(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """H3 in the daemon's shape: a leased item, a scout call that is slow.

    While the call is in flight the action is aged twenty minutes -- twice
    the old grace period -- and the portfolio tick runs. The frozen tick
    reclaimed it as ``worker_crash``; the worker then finished and could not
    complete an action that was already FAILED.
    """

    idea_id = _to_the_audit(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project)
    work_id = _leased(runtime_db, runtime_project, idea_id, owner="slow-worker")
    router = _blocking(runtime_db)
    thread, box = _in_thread(
        lambda: _step(
            runtime_db,
            pg_dsn,
            tmp_path,
            runtime_project,
            idea_id,
            router,
            work_id=work_id,
        )
    )
    assert router.entered.wait(60), box
    action = portfolio.active_action(idea_id)
    assert action is not None
    assert action.work_id == work_id, "the work item's id was not recorded"
    assert action.run_id and action.attempt == 1 and action.lease_owner == "slow-worker"

    _age(runtime_db, idea_id, minutes=20)
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.stale_actions_reclaimed == 0, "a live stage was reclaimed"
    assert portfolio.owner_is_live(action.action_id) == OWNER_LIVE

    router.release.set()
    thread.join(120)
    assert "error" not in box, box.get("error")
    assert box["result"].ok, box["result"].detail
    (done,) = [
        item
        for item in portfolio.list_actions(idea_id=idea_id)
        if item.action_id == action.action_id
    ]
    assert done.status is ActionStatus.SUCCEEDED


def test_the_lock_alone_keeps_an_owner_live_whatever_its_age(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Neighbour: no work item at all (a direct call) and an hour of age."""

    idea_id = _to_the_audit(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project)
    router = _blocking(runtime_db)
    thread, box = _in_thread(
        lambda: _step(runtime_db, pg_dsn, tmp_path, runtime_project, idea_id, router)
    )
    assert router.entered.wait(60), box
    _age(runtime_db, idea_id, minutes=60)
    assert (
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project).stale_actions_reclaimed
        == 0
    )
    router.release.set()
    thread.join(120)
    assert box["result"].ok, box


# ---------------------------------------------------- heartbeat / lease ----
def test_a_renewed_lease_is_a_heartbeat_and_a_lapsed_one_is_not(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The lease signal on its own: no session lock is held here."""

    idea, _ = seed_idea(portfolio, runtime_project)
    queue = WorkQueue(runtime_db)
    queue.enqueue(project_id=runtime_project, kind=ADVANCE_IDEA, dedup_key="hb")
    (item,) = queue.claim(owner="beating", lease_seconds=2, kinds=(ADVANCE_IDEA,))
    action = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="hb",
        work_id=item.work_id,
        attempt=item.attempts,
        lease_owner="beating",
    )
    with LeaseKeeper(
        queue, work_id=item.work_id, owner="beating", lease_seconds=2, renew_every=0.5
    ):
        time.sleep(3.0)  # longer than the lease: only renewal keeps it
        _age(runtime_db, idea.idea_id, minutes=30)
        assert portfolio.owner_is_live(action.action_id) == OWNER_LIVE
        assert (
            portfolio.reclaim_dead_actions(
                project_id=runtime_project, older_than_seconds=600
            )
            == ()
        )
    time.sleep(2.5)  # the keeper stopped: the lease lapses
    assert portfolio.owner_is_live(action.action_id) == OWNER_DEAD
    (reclaimed,) = portfolio.reclaim_dead_actions(
        project_id=runtime_project, older_than_seconds=600
    )
    assert reclaimed.action_id == action.action_id
    assert reclaimed.failure_class == "worker_crash"
    assert (
        portfolio.require_idea(idea.idea_id).operational_state is OperationalState.IDLE
    )


def test_a_later_attempt_of_the_same_item_is_a_different_owner(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Re-execution is a new identity: attempt 2's lease does not keep attempt 1 alive."""

    idea, _ = seed_idea(portfolio, runtime_project)
    queue = WorkQueue(runtime_db)
    queue.enqueue(project_id=runtime_project, kind=ADVANCE_IDEA, dedup_key="a2")
    (first,) = queue.claim(owner="w1", lease_seconds=3600, kinds=(ADVANCE_IDEA,))
    action = portfolio.open_action(
        idea_id=idea.idea_id,
        idea_version=1,
        stage=Stage.FALSIFY,
        basis_digest="a2",
        work_id=first.work_id,
        attempt=first.attempts,
        lease_owner="w1",
    )
    _expire_lease(runtime_db, first.work_id)
    queue.reclaim_expired()
    (second,) = queue.claim(owner="w2", lease_seconds=3600, kinds=(ADVANCE_IDEA,))
    assert second.work_id == first.work_id and second.attempts == 2
    assert portfolio.owner_is_live(action.action_id) == OWNER_DEAD


def test_an_action_with_no_recorded_owner_is_judged_by_age_as_before(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Only an action opened outside ``advance_idea`` has none; nothing proves it alive."""

    idea, _ = seed_idea(portfolio, runtime_project)
    action = portfolio.open_action(
        idea_id=idea.idea_id, idea_version=1, stage=Stage.FALSIFY, basis_digest="o"
    )
    assert portfolio.owner_is_live(action.action_id) == OWNER_UNKNOWN
    assert (
        portfolio.reclaim_dead_actions(
            project_id=runtime_project, older_than_seconds=600
        )
        == ()
    ), "a fresh ownerless action is not reclaimed"
    _age(runtime_db, idea.idea_id, minutes=20)
    (reclaimed,) = portfolio.reclaim_dead_actions(
        project_id=runtime_project, older_than_seconds=600
    )
    assert reclaimed.action_id == action.action_id


def test_a_held_lock_refuses_a_takeover_even_with_the_lease_gone(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    from research_os.portfolio.store import ActiveTrackExistsError

    idea, _ = seed_idea(portfolio, runtime_project)
    run = RuntimeStore(runtime_db).create_run(project_id=runtime_project, objective="o")
    with research_run_lock(runtime_db, run.run_id):
        portfolio.open_action(
            idea_id=idea.idea_id,
            idea_version=1,
            stage=Stage.FALSIFY,
            basis_digest="l1",
            run_id=run.run_id,
        )
        with pytest.raises(ActiveTrackExistsError):
            portfolio.open_action(
                idea_id=idea.idea_id,
                idea_version=1,
                stage=Stage.FALSIFY,
                basis_digest="l2",
            )
    # The lock is released: the owner is gone, and the takeover proceeds.
    taken = portfolio.open_action(
        idea_id=idea.idea_id, idea_version=1, stage=Stage.FALSIFY, basis_digest="l2"
    )
    old = [
        a
        for a in portfolio.list_actions(idea_id=idea.idea_id)
        if a.basis_digest == "l1"
    ]
    assert old[0].status is ActionStatus.FAILED
    assert taken.action_id in (old[0].detail or "")


# -------------------------------------------------- duplicate purchase ----
def test_a_second_purchase_of_a_live_stage_starts_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The scheduler buys the same idea twice while it runs: one execution."""

    idea_id = _to_the_audit(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project)
    first = _leased(runtime_db, runtime_project, idea_id, owner="first")
    router = _blocking(runtime_db)
    thread, box = _in_thread(
        lambda: _step(
            runtime_db,
            pg_dsn,
            tmp_path,
            runtime_project,
            idea_id,
            router,
            work_id=first,
        )
    )
    assert router.entered.wait(60), box
    _age(runtime_db, idea_id, minutes=30)
    report = _tick(runtime_db, pg_dsn, tmp_path, runtime_project)
    assert report.stale_actions_reclaimed == 0
    assert not any(item.idea_id == idea_id for item in report.allocations), (
        "the tick bought a stage of an idea that is running"
    )

    second_router = _recording(runtime_db)
    second = _leased(runtime_db, runtime_project, idea_id, owner="second")
    duplicate = _step(
        runtime_db,
        pg_dsn,
        tmp_path,
        runtime_project,
        idea_id,
        second_router,
        work_id=second,
    )
    assert duplicate.action_id is None
    assert "already doing this" in duplicate.detail
    assert second_router.calls == [], "a duplicate execution made a model call"

    router.release.set()
    thread.join(120)
    assert box["result"].ok, box


# ----------------------------------------------- late result from old ----
def test_a_late_result_from_an_attempt_that_lost_ownership_writes_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """Attempt A loses its session and its lease mid-call; B takes over; A wakes.

    A's scout answers after B has finished the stage. Everything A would
    write goes through its fenced store and is refused, so the only
    literature rows on the version are B's.
    """

    idea_id = _to_the_audit(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project)
    before = {row.evidence_id for row in portfolio.list_evidence(idea_id=idea_id)}
    first = _leased(runtime_db, runtime_project, idea_id, owner="first")
    slow = _blocking(runtime_db)
    thread, box = _in_thread(
        lambda: _step(
            runtime_db, pg_dsn, tmp_path, runtime_project, idea_id, slow, work_id=first
        )
    )
    assert slow.entered.wait(60), box
    a = portfolio.active_action(idea_id)
    assert a is not None and a.run_id

    # A's owner dies by both signals: its session is closed by the server and
    # its lease runs out. Nothing about the age of the row is touched.
    _kill_lock_session(runtime_db, a.run_id)
    _expire_lease(runtime_db, first)
    assert portfolio.owner_is_live(a.action_id) == OWNER_DEAD

    fast = _recording(runtime_db)
    b_work = _leased(runtime_db, runtime_project, idea_id, owner="second")
    b = _step(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea_id, fast, work_id=b_work
    )
    assert b.ok and b.stage is Stage.LITERATURE_AUDIT, b.detail
    assert "literature_scout" in fast.calls
    rows_after_b = portfolio.list_evidence(idea_id=idea_id)

    slow.release.set()
    thread.join(120)
    assert isinstance(box.get("error"), StaleExecutionError), box
    assert portfolio.list_evidence(idea_id=idea_id) == rows_after_b, (
        "the obsolete attempt's late result was written"
    )
    by_id = {item.action_id: item for item in portfolio.list_actions(idea_id=idea_id)}
    assert by_id[a.action_id].status is ActionStatus.FAILED
    assert (
        b.action_id is not None and by_id[b.action_id].status is ActionStatus.SUCCEEDED
    )
    assert RuntimeStore(runtime_db).require_run(a.run_id).status is RunStatus.CANCELLED
    written = [
        row
        for row in portfolio.list_evidence(idea_id=idea_id)
        if row.evidence_id not in before
    ]
    assert written, "the stage that owned the idea wrote nothing"
    assert all(row.kind is EvidenceKind.LITERATURE for row in written)
    assert {row.source_call_id for row in written} <= fast.call_ids, (
        "a row written during this stage came from the obsolete attempt"
    )


def test_a_fenced_store_refuses_every_write_once_its_action_is_closed(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    from research_os.portfolio.models import EvidenceStrength

    idea, _ = seed_idea(portfolio, runtime_project)
    action = portfolio.open_action(
        idea_id=idea.idea_id, idea_version=1, stage=Stage.FALSIFY, basis_digest="f"
    )
    fenced = portfolio.fenced(action.action_id)
    fenced.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.CONSISTENT_WITH,
        summary="written while it owned the stage",
        literature_key="openalex:W1",
    )
    portfolio.complete_action(action_id=action.action_id, status=ActionStatus.FAILED)
    with pytest.raises(StaleExecutionError):
        fenced.add_evidence(
            idea_id=idea.idea_id,
            idea_version=1,
            kind=EvidenceKind.LITERATURE,
            strength=EvidenceStrength.CONSISTENT_WITH,
            summary="written after it lost it",
            literature_key="openalex:W2",
        )
    with pytest.raises(StaleExecutionError):
        fenced.set_status(
            idea_id=idea.idea_id, status=portfolio.require_idea(idea.idea_id).status
        )
    assert len(portfolio.list_evidence(idea_id=idea.idea_id)) == 1


# --------------------------------------------------- death and restart ----
def _die_mid_stage(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    project: str,
    idea_id: str,
) -> tuple[str, str]:
    work_id = _leased(runtime_db, project, idea_id, owner="doomed-worker")
    marker = tmp_path / "died-in-the-scout"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            pg_dsn,
            project,
            idea_id,
            work_id,
            str(tmp_path / "artifacts"),
            str(marker),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=env,
        cwd=REPO_ROOT,
    )
    assert completed.returncode == 9, completed.stderr[-3000:]
    assert marker.exists(), "the child never reached the scout"
    action = PortfolioStore(runtime_db).active_action(idea_id)
    assert action is not None, "the dead worker's action is not ACTIVE"
    return work_id, action.action_id


def test_a_killed_worker_is_reclaimed_after_its_lease_truly_expires(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    idea_id = _to_the_audit(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project)
    work_id, action_id = _die_mid_stage(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea_id
    )
    _age(runtime_db, idea_id, minutes=30)
    # The process is gone and its lock with it, but its lease is its last
    # heartbeat: until the lease lapses the owner is not provably dead.
    assert portfolio.owner_is_live(action_id) == OWNER_LIVE
    assert (
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project).stale_actions_reclaimed
        == 0
    )

    _expire_lease(runtime_db, work_id)
    assert portfolio.owner_is_live(action_id) == OWNER_DEAD
    assert (
        _tick(runtime_db, pg_dsn, tmp_path, runtime_project).stale_actions_reclaimed
        == 1
    )
    (dead,) = [
        a for a in portfolio.list_actions(idea_id=idea_id) if a.action_id == action_id
    ]
    assert dead.status is ActionStatus.FAILED and dead.failure_class == "worker_crash"


def test_after_a_restart_the_retry_takes_over_the_dead_owner_at_once(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    tmp_path: Path,
    runtime_project: str,
) -> None:
    """The daemon restarts: its lease reclaimer requeues the item; attempt 2 runs it."""

    idea_id = _to_the_audit(portfolio, runtime_db, pg_dsn, tmp_path, runtime_project)
    work_id, dead_id = _die_mid_stage(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea_id
    )
    queue = WorkQueue(runtime_db)
    _expire_lease(runtime_db, work_id)
    queue.reclaim_expired()
    (retry,) = queue.claim(
        owner="restarted-worker", lease_seconds=3600, kinds=(ADVANCE_IDEA,)
    )
    assert retry.work_id == work_id and retry.attempts == 2

    router = _recording(runtime_db)
    result = _step(
        runtime_db, pg_dsn, tmp_path, runtime_project, idea_id, router, work_id=work_id
    )
    assert result.ok and result.stage is Stage.LITERATURE_AUDIT, result.detail
    by_id = {item.action_id: item for item in portfolio.list_actions(idea_id=idea_id)}
    assert by_id[dead_id].status is ActionStatus.FAILED
    assert result.action_id is not None and result.action_id in (
        by_id[dead_id].detail or ""
    )
    new = by_id[result.action_id]
    assert new.status is ActionStatus.SUCCEEDED
    assert (new.work_id, new.attempt, new.lease_owner) == (
        work_id,
        2,
        "restarted-worker",
    )
