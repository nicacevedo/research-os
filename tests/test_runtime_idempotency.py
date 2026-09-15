"""At-least-once execution, exactly-once visible side effects.

The headline test here (:func:`test_a_side_effect_survives_a_crash_without_happening_twice`)
runs a real child process, performs a real side effect, and kills the process
with ``os._exit`` before it can record what it did. That is the failure the
whole ledger exists for, and asserting it against a mocked crash would assert
nothing: ``os._exit`` skips the ``finally`` blocks that a simulated exception
would run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from research_os.runtime.db import Database
from research_os.runtime.idempotency import (
    IdempotencyError,
    InvocationLedger,
    UnreconciledInvocationError,
    idempotency_key,
)
from research_os.runtime.models import InvocationStatus
from research_os.runtime.queue import WorkQueue

SCRIPT = Path(__file__).parent / "runtime_scripts" / "crash_after_effect.py"


@pytest.fixture
def ledger(runtime_db: Database) -> InvocationLedger:
    return InvocationLedger(runtime_db)


def test_the_key_is_derived_from_the_action_not_from_the_attempt() -> None:
    """Keying on anything per-attempt produces a ledger that prevents nothing."""

    first = idempotency_key("slurm.submit", "WORK-1", "spec-digest-abc")
    second = idempotency_key("slurm.submit", "WORK-1", "spec-digest-abc")
    assert first == second
    assert first != idempotency_key("slurm.submit", "WORK-2", "spec-digest-abc")
    assert first != idempotency_key("git.commit", "WORK-1", "spec-digest-abc")


def test_a_completed_action_is_reused_and_not_performed_again(
    ledger: InvocationLedger,
) -> None:
    calls: list[int] = []

    def perform() -> dict[str, object]:
        calls.append(1)
        return {"job": "12345"}

    key = idempotency_key("demo", "a")
    first = ledger.run(key=key, kind="demo", perform=perform)
    second = ledger.run(key=key, kind="demo", perform=perform)

    assert first.reused is False
    assert second.reused is True
    assert second.result == {"job": "12345"}
    assert len(calls) == 1, "the side effect ran twice"


def test_a_side_effect_survives_a_crash_without_happening_twice(
    runtime_db: Database, pg_dsn: str, runtime_project: str, tmp_path: Path
) -> None:
    """The scenario, end to end, with a real process death in the middle.

    1. the action succeeds
    2. the worker dies before acknowledging it
    3. the lease expires and the work is retried
    4. the runtime finds the unfinished record
    5. the reconciler confirms the action happened
    6. the stored result is reused
    7. the action is NOT performed a second time
    """

    queue = WorkQueue(runtime_db)
    work_id = queue.enqueue(project_id=runtime_project, kind="demo.submit").item.work_id
    queue.claim(owner="doomed-worker", lease_seconds=1)
    effects = tmp_path / "effects.log"

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), pg_dsn, str(effects), work_id],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 9, completed.stderr
    assert effects.read_text().splitlines() == ["submitted"], (
        "the side effect did not happen"
    )

    ledger = InvocationLedger(runtime_db)
    key = idempotency_key("demo.submit", work_id)
    stranded = ledger.get(key)
    assert stranded is not None
    assert stranded.status is InvocationStatus.IN_FLIGHT, "the crash left no evidence"

    # The lease expires; the daemon reclaims the work and ages out the invocation.
    with runtime_db.tx() as conn:
        conn.execute("update work_items set lease_expires_at = now() - interval '1s'")
    assert len(queue.reclaim_expired()) == 1
    assert len(ledger.abandon_stale(older_than_seconds=0)) == 1

    # The retry. `perform` would emit a second submission; the reconciler looks
    # at the world instead and finds the first one.
    performed: list[int] = []

    def perform() -> dict[str, object]:
        performed.append(1)
        with effects.open("a", encoding="utf-8") as handle:
            handle.write("submitted\n")
        return {"duplicate": True}

    def reconcile(_invocation: object) -> dict[str, object] | None:
        lines = effects.read_text().splitlines()
        return {"recovered": True, "count": len(lines)} if lines else None

    claimed = queue.claim(owner="replacement-worker", lease_seconds=60)
    assert len(claimed) == 1
    outcome = ledger.run(
        key=key,
        kind="demo.submit",
        work_id=work_id,
        perform=perform,
        reconcile=reconcile,
        owner="replacement-worker",
    )

    assert outcome.reused is True
    assert outcome.result == {"recovered": True, "count": 1}
    assert performed == [], "the side effect was emitted a second time"
    assert effects.read_text().splitlines() == ["submitted"]
    assert ledger.get(key).status is InvocationStatus.COMPLETED  # type: ignore[union-attr]


def test_an_abandoned_action_with_no_reconciler_is_refused_not_guessed(
    ledger: InvocationLedger,
) -> None:
    """Unknown is not the same as "did not happen".

    Where the runtime cannot go and look, it refuses rather than gambling that
    the first attempt failed. Guessing wrong here submits the same experiment to
    a cluster twice.
    """

    key = idempotency_key("demo", "unknowable")
    # Strand an invocation the way a killed worker does.
    ledger._claim(
        key=key, kind="demo", request={}, run_id=None, work_id=None, owner="ghost"
    )
    ledger.abandon_stale(older_than_seconds=0)

    with pytest.raises(UnreconciledInvocationError, match="outcome is unknown"):
        ledger.run(key=key, kind="demo", perform=lambda: {"performed": True})


def test_a_reconciler_that_finds_nothing_lets_the_action_happen_once(
    ledger: InvocationLedger,
) -> None:
    key = idempotency_key("demo", "nothing-happened")
    ledger._claim(
        key=key, kind="demo", request={}, run_id=None, work_id=None, owner="ghost"
    )
    ledger.abandon_stale(older_than_seconds=0)

    performed: list[int] = []

    def perform() -> dict[str, object]:
        performed.append(1)
        return {"done": True}

    outcome = ledger.run(
        key=key, kind="demo", perform=perform, reconcile=lambda _i: None
    )
    assert outcome.reused is False
    assert performed == [1]
    assert outcome.result == {"done": True}


def test_an_action_held_by_a_live_worker_is_not_performed_by_a_second_one(
    ledger: InvocationLedger,
) -> None:
    key = idempotency_key("demo", "contended")
    ledger._claim(
        key=key, kind="demo", request={}, run_id=None, work_id=None, owner="worker-1"
    )
    with pytest.raises(IdempotencyError, match="already in flight"):
        ledger.run(key=key, kind="demo", perform=lambda: {"second": True})


def test_a_definite_failure_may_be_retried_under_the_same_key(
    ledger: InvocationLedger,
) -> None:
    """A failure recorded by the performer means the effect did not take hold.

    That is knowledge, not a guess, so the same key may be used again.
    """

    key = idempotency_key("demo", "transient")
    attempts: list[int] = []

    def flaky() -> dict[str, object]:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("provider hiccup")
        return {"ok": True}

    with pytest.raises(RuntimeError, match="hiccup"):
        ledger.run(key=key, kind="demo", perform=flaky)
    assert ledger.get(key).status is InvocationStatus.FAILED  # type: ignore[union-attr]

    outcome = ledger.run(key=key, kind="demo", perform=flaky)
    assert outcome.reused is False
    assert outcome.result == {"ok": True}
    assert len(attempts) == 2


def test_a_failure_marked_unretryable_stays_refused(ledger: InvocationLedger) -> None:
    key = idempotency_key("demo", "fatal")
    with pytest.raises(RuntimeError):
        ledger.run(
            key=key,
            kind="demo",
            perform=lambda: (_ for _ in ()).throw(RuntimeError("no")),
        )
    with pytest.raises(IdempotencyError, match="not retryable"):
        ledger.run(
            key=key, kind="demo", perform=lambda: {"x": 1}, retry_on_failure=False
        )


def test_a_failure_recorded_after_the_effect_landed_does_not_repeat_it(
    ledger: InvocationLedger,
) -> None:
    """The defect an independent review found, and the most dangerous one here.

    ``perform`` routinely raises *after* the external world has changed: an
    ``sbatch`` that succeeded and then a database blip while recording the job
    id. The first version treated any exception as "the effect did not happen",
    deleted the ledger row, and re-performed -- submitting the same experiment
    to a cluster twice. The reconciler now gets the first word on a FAILED row
    just as it does on an ABANDONED one.
    """

    effects: list[str] = []
    key = idempotency_key("slurm.submit", "WORK-1", "spec-abc")

    def perform_then_fail() -> dict[str, object]:
        effects.append("submitted")
        raise RuntimeError("the database went away after sbatch returned")

    def reconcile(_invocation: object) -> dict[str, object] | None:
        return {"job": "4711", "recovered": True} if effects else None

    with pytest.raises(RuntimeError, match="after sbatch"):
        ledger.run(
            key=key, kind="slurm.submit", perform=perform_then_fail, reconcile=reconcile
        )
    assert ledger.get(key).status is InvocationStatus.FAILED  # type: ignore[union-attr]

    performed_again: list[str] = []

    def perform_again() -> dict[str, object]:
        performed_again.append("submitted")
        effects.append("submitted")
        return {"job": "duplicate"}

    outcome = ledger.run(
        key=key, kind="slurm.submit", perform=perform_again, reconcile=reconcile
    )
    assert outcome.reused is True
    assert outcome.result == {"job": "4711", "recovered": True}
    assert performed_again == [], "the action was performed a second time"
    assert effects == ["submitted"]
    assert ledger.get(key).status is InvocationStatus.COMPLETED  # type: ignore[union-attr]


def test_a_genuine_pre_effect_failure_still_retries(ledger: InvocationLedger) -> None:
    """The reconciler saying "it did not happen" must not block the retry."""

    attempts: list[int] = []
    key = idempotency_key("demo", "pre-effect")

    def flaky() -> dict[str, object]:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("refused before doing anything")
        return {"ok": True}

    with pytest.raises(RuntimeError):
        ledger.run(key=key, kind="demo", perform=flaky, reconcile=lambda _i: None)
    outcome = ledger.run(key=key, kind="demo", perform=flaky, reconcile=lambda _i: None)
    assert outcome.reused is False
    assert outcome.result == {"ok": True}
    assert len(attempts) == 2


def test_a_taken_over_invocation_refuses_the_late_workers_result(
    ledger: InvocationLedger,
) -> None:
    """A slow worker must not overwrite the record of whoever took over.

    Without the ownership guard, a late ``mark_failed`` would record FAILED for
    an action that had completed -- and the next retry would read that as
    permission to perform it again.
    """

    from research_os.runtime.idempotency import InvocationTakenOverError

    key = idempotency_key("demo", "contended")
    first, _claimed = ledger._claim(
        key=key, kind="demo", request={}, run_id=None, work_id=None, owner="slow"
    )
    ledger.abandon_stale(older_than_seconds=0)
    assert ledger._retake(first.invocation_id, owner="took-over")
    ledger.complete(first.invocation_id, result={"by": "took-over"}, owner="took-over")

    with pytest.raises(InvocationTakenOverError, match="discarded"):
        ledger.complete(first.invocation_id, result={"by": "slow"}, owner="slow")
    with pytest.raises(InvocationTakenOverError, match="not recorded over it"):
        ledger.mark_failed(
            first.invocation_id, error="slow worker failed", owner="slow"
        )

    assert ledger.get(key).result == {"by": "took-over"}  # type: ignore[union-attr]
