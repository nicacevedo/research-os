"""The durable queue's guarantees, asserted against a real PostgreSQL.

These are the properties the rest of the runtime is allowed to assume. Each test
is named after the property rather than the method, because the method is an
implementation detail and the property is the contract.
"""

from __future__ import annotations

import pytest

from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import WorkStatus
from research_os.runtime.queue import LeaseLostError, WorkQueue


@pytest.fixture
def queue(runtime_db: Database) -> WorkQueue:
    return WorkQueue(runtime_db)


def _enqueue(queue: WorkQueue, project: str, **kwargs: object) -> str:
    return queue.enqueue(project_id=project, kind="demo", **kwargs).item.work_id  # type: ignore[arg-type]


def test_two_producers_with_one_dedup_key_make_one_item(
    queue: WorkQueue, runtime_project: str
) -> None:
    first = queue.enqueue(project_id=runtime_project, kind="demo", dedup_key="same")
    second = queue.enqueue(project_id=runtime_project, kind="demo", dedup_key="same")
    assert first.created is True
    assert second.created is False
    assert second.item.work_id == first.item.work_id
    assert sum(queue.counts_by_status().values()) == 1


def test_a_null_dedup_key_does_not_deduplicate(
    queue: WorkQueue, runtime_project: str
) -> None:
    """Postgres treats NULLs as distinct in a unique index, and we rely on it.

    Work that is genuinely allowed to happen many times -- a poll, a reassess --
    passes no key and must not collapse into one row.
    """

    _enqueue(queue, runtime_project)
    _enqueue(queue, runtime_project)
    assert queue.counts_by_status()[WorkStatus.PENDING] == 2


def test_two_workers_claiming_at_once_get_different_items(
    queue: WorkQueue, runtime_project: str
) -> None:
    _enqueue(queue, runtime_project)
    _enqueue(queue, runtime_project)
    first = queue.claim(owner="w1", lease_seconds=60)
    second = queue.claim(owner="w2", lease_seconds=60)
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].work_id != second[0].work_id


def test_claiming_takes_priority_then_age(
    queue: WorkQueue, runtime_project: str
) -> None:
    low = _enqueue(queue, runtime_project, priority=200)
    high = _enqueue(queue, runtime_project, priority=1)
    claimed = queue.claim(owner="w", lease_seconds=60, limit=2)
    assert [item.work_id for item in claimed] == [high, low]


def test_a_future_item_is_not_claimable_yet(
    queue: WorkQueue, runtime_project: str
) -> None:
    _enqueue(queue, runtime_project, delay_seconds=3600)
    assert queue.claim(owner="w", lease_seconds=60) == ()


def test_claiming_charges_the_attempt_not_finishing(
    queue: WorkQueue, runtime_project: str
) -> None:
    """A task that kills its worker must still run out of attempts.

    If attempts were charged on completion, work whose execution crashes the
    process would be claimed forever by a succession of victims.
    """

    work_id = _enqueue(queue, runtime_project)
    claimed = queue.claim(owner="w", lease_seconds=60)[0]
    assert claimed.attempts == 1
    assert queue.get(work_id).attempts == 1  # type: ignore[union-attr]


def test_a_lease_cannot_be_renewed_once_it_has_expired(
    queue: WorkQueue, runtime_db: Database, runtime_project: str
) -> None:
    """The renewing worker may already have been replaced.

    Letting it renew would hand one work item to two owners, which is the exact
    failure leases exist to prevent.
    """

    work_id = _enqueue(queue, runtime_project)
    queue.claim(owner="w1", lease_seconds=60)
    with runtime_db.tx() as conn:
        conn.execute(
            "update work_items set lease_expires_at = now() - interval '1 second' "
            "where work_id = %s",
            (work_id,),
        )
    with pytest.raises(LeaseLostError):
        queue.renew(work_id, owner="w1", lease_seconds=60)


def test_renewing_someone_elses_lease_is_refused(
    queue: WorkQueue, runtime_project: str
) -> None:
    work_id = _enqueue(queue, runtime_project)
    queue.claim(owner="w1", lease_seconds=60)
    with pytest.raises(LeaseLostError):
        queue.renew(work_id, owner="w2", lease_seconds=60)


def test_an_expired_lease_returns_the_work_to_the_queue(
    queue: WorkQueue, runtime_db: Database, runtime_project: str
) -> None:
    work_id = _enqueue(queue, runtime_project, max_attempts=3)
    queue.claim(owner="dead-worker", lease_seconds=60)
    with runtime_db.tx() as conn:
        conn.execute("update work_items set lease_expires_at = now() - interval '1s'")
    reclaimed = queue.reclaim_expired()
    assert [item.work_id for item in reclaimed] == [work_id]
    after = queue.get(work_id)
    assert after is not None
    assert after.status is WorkStatus.PENDING
    assert after.lease_owner is None
    assert after.lease_expires_at is None
    # Claimable again, by anyone.
    assert queue.claim(owner="fresh-worker", lease_seconds=60)[0].work_id == work_id


def test_an_expired_lease_on_its_last_attempt_fails_as_a_worker_crash(
    queue: WorkQueue, runtime_db: Database, runtime_project: str
) -> None:
    work_id = _enqueue(queue, runtime_project, max_attempts=1)
    queue.claim(owner="dead-worker", lease_seconds=60)
    with runtime_db.tx() as conn:
        conn.execute("update work_items set lease_expires_at = now() - interval '1s'")
    queue.reclaim_expired()
    after = queue.get(work_id)
    assert after is not None
    assert after.status is WorkStatus.FAILED
    assert after.failure_class == FailureClass.WORKER_CRASH
    assert queue.claim(owner="fresh", lease_seconds=60) == ()


def test_a_worker_that_lost_its_lease_cannot_write_a_result(
    queue: WorkQueue, runtime_db: Database, runtime_project: str
) -> None:
    """Otherwise a slow worker overwrites the work of whoever took over."""

    work_id = _enqueue(queue, runtime_project)
    queue.claim(owner="slow", lease_seconds=60)
    with runtime_db.tx() as conn:
        conn.execute("update work_items set lease_expires_at = now() - interval '1s'")
    queue.reclaim_expired()
    queue.claim(owner="took-over", lease_seconds=60)
    with pytest.raises(LeaseLostError):
        queue.succeed(work_id, owner="slow", result={"from": "slow"})
    assert queue.succeed(work_id, owner="took-over", result={"from": "new"}).result == {
        "from": "new"
    }


def test_a_retryable_failure_goes_back_to_pending_with_a_delay(
    queue: WorkQueue, runtime_project: str
) -> None:
    work_id = _enqueue(queue, runtime_project, max_attempts=3)
    queue.claim(owner="w", lease_seconds=60)
    item = queue.fail(
        work_id,
        owner="w",
        failure_class=FailureClass.PROVIDER_RATE_LIMIT,
        error="429",
    )
    assert item.status is WorkStatus.PENDING
    assert item.scheduled_at > item.created_at
    # Not claimable immediately: the backoff is real, not cosmetic.
    assert queue.claim(owner="w", lease_seconds=60) == ()


def test_a_terminal_failure_class_does_not_retry(
    queue: WorkQueue, runtime_project: str
) -> None:
    work_id = _enqueue(queue, runtime_project, max_attempts=5)
    queue.claim(owner="w", lease_seconds=60)
    item = queue.fail(
        work_id, owner="w", failure_class=FailureClass.CAPABILITY_DENIED, error="no"
    )
    assert item.status is WorkStatus.FAILED
    assert (
        item.attempts_remaining == 4
    )  # attempts left, but the class forbids using them


def test_running_out_of_attempts_is_terminal(
    queue: WorkQueue, runtime_project: str
) -> None:
    work_id = _enqueue(queue, runtime_project, max_attempts=1)
    queue.claim(owner="w", lease_seconds=60)
    item = queue.fail(
        work_id, owner="w", failure_class=FailureClass.PROVIDER_TRANSIENT, error="boom"
    )
    assert item.status is WorkStatus.FAILED


def test_waiting_for_an_external_dependency_refunds_the_attempt(
    queue: WorkQueue, runtime_project: str
) -> None:
    """Waiting for a cluster must not consume the budget that exists for breakage."""

    work_id = _enqueue(queue, runtime_project, max_attempts=2)
    queue.claim(owner="w", lease_seconds=60)
    item = queue.wait_for_external(
        work_id, owner="w", detail="job PENDING", retry_after_seconds=0.0
    )
    assert item.status is WorkStatus.PENDING
    assert item.attempts == 0
    assert item.failure_class is None
    # And it can be picked up again without having spent anything.
    assert queue.claim(owner="w", lease_seconds=60)[0].attempts == 1


def test_cancelling_a_run_cancels_its_leased_work_too(
    queue: WorkQueue, runtime_db: Database, runtime_project: str
) -> None:
    from research_os.runtime.store import RuntimeStore

    run = RuntimeStore(runtime_db).create_run(project_id=runtime_project, objective="o")
    pending_id = queue.enqueue(
        project_id=runtime_project, kind="demo", run_id=run.run_id
    ).item.work_id
    leased_id = queue.enqueue(
        project_id=runtime_project, kind="demo", run_id=run.run_id
    ).item.work_id
    queue.claim(owner="w", lease_seconds=60, limit=1)
    assert queue.cancel_run_work(run.run_id) == 2
    assert {queue.get(pending_id).status, queue.get(leased_id).status} == {  # type: ignore[union-attr]
        WorkStatus.CANCELLED
    }
    # The worker finds out by being refused, which beats waiting for a long task.
    with pytest.raises(LeaseLostError):
        queue.succeed(leased_id, owner="w", result={})
