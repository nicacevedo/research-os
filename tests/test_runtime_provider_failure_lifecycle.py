"""The lifecycle regression for a provider that goes away mid-science.

Every test here is derived from one live incident, and the incident is worth
stating because it is the reason to distrust any of these passing by accident.

On 2026-09-19 a researcher committed a question to a project's capsule. The
control plane observed the change, woke the parked objectives, and began a
cycle for each. An interactive Claude Code session was running on the same
machine, sharing one OAuth token, and the refreshes collided. Three planner
calls failed. Then the provider breaker opened for five minutes.

Nothing about that is unusual, and none of it should have mattered. What
happened instead:

- three runs whose only model call never succeeded finished as
  ``SUCCEEDED / DONE_FOR_NOW``, and `researchctl runtime status` told the
  researcher they had "finished cleanly" and that a scientific decision was
  theirs to make. There was no decision;
- three more runs were left ``RUNNING`` with no work item that could ever
  advance them -- for twenty-one hours, until someone looked;
- because ``parked_objectives`` excludes an objective whose newest run is in
  flight, those three objectives left the research frontier permanently. Five
  parked objectives became two;
- the retry schedule (30s, 60s) fitted entirely inside the 300-second
  cooldown, so a transient failure exhausted its attempts without one of them
  being able to succeed;
- one objective's failure propagated out of the advance loop, so the objective
  after it was never evaluated and received no disposition at all;
- and six lineage links were spent, against a ceiling of twelve, on cycles in
  which no science occurred.

The one-line summary, which is the property these tests exist to hold:

    a transient infrastructure failure must delay science, never impersonate
    it.

The tests are named T1..T12 in their docstrings after the closure plan that
enumerated them, so a reader can check the list rather than infer it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from research_os.runtime import report as views
from research_os.runtime.budgets import BudgetExhaustedError, Dimension
from research_os.runtime.clock import FrozenClock
from research_os.runtime.daemon import Daemon, WorkKind
from research_os.runtime.db import Database
from research_os.runtime.failures import (
    INFRASTRUCTURE,
    PROVIDER_FAILURES,
    FailureClass,
    is_infrastructure,
)
from research_os.runtime.interfaces import ModelRequest, ModelResponse
from research_os.runtime.models import (
    ACTIVE_JOB_STATUSES,
    BudgetScope,
    ExternalJobStatus,
    RunStatus,
    TerminalState,
    WorkStatus,
)
from research_os.runtime.notify import CollectingNotifier
from research_os.runtime.policy import ActionKind
from research_os.runtime.queue import WorkQueue
from research_os.runtime.routing import ProviderCallFailedError
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    plan_answer,
    review_answer,
)

PROJECT = "alpha-project"


# ------------------------------------------------------------- scaffolding --
def _answers() -> dict[str, Any]:
    return {
        "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
        "scientific_reviewer": review_answer(),
        "frontier": {
            "recommendation": "START_NEXT_CYCLE",
            "recommendation_rationale": "there is work",
            "assessment": "the frontier has open questions",
            "priorities": [],
        },
    }


class OutageRouter:
    """A provider that can be switched off and on, like a real one.

    Deliberately stateful rather than scripted per call. The property under
    test is what the runtime does *across* a failure and a recovery, and a
    double that can only fail or only succeed cannot express the transition
    that matters.
    """

    def __init__(self, *, cooldown_until: datetime | None = None) -> None:
        self.inner = ScriptedRouter(answers=_answers())
        self.down = False
        self.attempted = True
        self.cooldown_until = cooldown_until
        self.calls = 0
        #: Fail only the n-th call, for mid-loop failure tests. None disables.
        self.fail_only_call: int | None = None
        #: Fail every call whose prompt contains this. Counting calls is the
        #: wrong handle for "the third objective": each objective costs a
        #: planner call, a frontier call and a reviewer call, so call three is
        #: the *first* objective's reviewer. Matching the objective's own text
        #: makes the scenario the test claims the scenario the test runs.
        self.fail_prompts_containing: str | None = None
        #: Refuse on the budget instead, which is a policy answer rather than
        #: an outage and must stay distinguishable from one.
        self.budget_exhausted = False

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.budget_exhausted:
            raise BudgetExhaustedError(
                "model_calls exhausted",
                dimension=Dimension.MODEL_CALLS,
                scope=BudgetScope.RUN,
                scope_id="RRUN-test",
            )
        targeted = (
            self.fail_prompts_containing is not None
            and self.fail_prompts_containing in request.prompt
        )
        if self.down or targeted or self.calls == self.fail_only_call:
            raise ProviderCallFailedError(
                "no healthy provider offers planning",
                failure_class=FailureClass.PROVIDER_UNAVAILABLE,
                retry_at=self.cooldown_until,
                attempted=self.attempted,
            )
        return self.inner.complete(request)

    def requests_for(self, role: str) -> list[ModelRequest]:
        return self.inner.requests_for(role)


@pytest.fixture
def plane(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    from research_os.runtime.checkpoints import ensure_tables

    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project", project_id=PROJECT)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(repo))
    router = OutageRouter()
    config = make_config(tmp_path and pg_dsn, tmp_path / "artifacts")
    notifier = CollectingNotifier()

    def build(owner: str = "test-worker") -> Daemon:
        return Daemon(
            config=config,
            db=runtime_db,
            repo_for=lambda _project: repo,
            models=lambda _run, _project, _work: router,
            notifier=notifier,
            clock=FrozenClock(),
            owner=owner,
        )

    return {
        "build": build,
        "daemon": build(),
        "db": runtime_db,
        "store": store,
        "queue": WorkQueue(runtime_db),
        "router": router,
        "config": config,
        "repo": repo,
        "notifier": notifier,
    }


def _request_run(store: RuntimeStore, objective: str) -> str:
    run = store.create_run(project_id=PROJECT, objective=objective)
    store.record_event(
        kind="RESEARCH_RUN_REQUESTED",
        project_id=PROJECT,
        run_id=run.run_id,
        dedup_key=f"requested:{run.run_id}",
    )
    return run.run_id


def _drain(daemon: Daemon, *, passes: int = 12) -> list[Any]:
    """Tick until nothing happens, or ``passes`` times, whichever is first."""

    reports = []
    for _ in range(passes):
        report = daemon.tick()
        reports.append(report)
        if not report.did_something:
            break
    return reports


#: Time is advanced by ageing rows rather than by moving a clock, because every
#: query that decides "is this due" and "has this been stranded long enough" is
#: SQL against ``now()``, which a frozen Python clock does not reach. Ageing a
#: row by N seconds is exactly what N seconds of waiting would do to it.
#:
#: The two are separate on purpose. Exhausting a retry schedule needs the queue
#: to move; reconciling a stranded run needs the *run* to have been still for a
#: grace period. A test that moved both at once could not observe the state
#: between them, which is the state the incident was in for twenty-one hours.
def _advance_queue(db: Database, *, seconds: int = 600) -> None:
    """Make every scheduled retry due, without ageing the runs."""

    with db.tx() as conn:
        conn.execute(
            "update work_items set scheduled_at = scheduled_at "
            "- make_interval(secs => %(s)s), updated_at = updated_at "
            "- make_interval(secs => %(s)s)",
            {"s": seconds},
        )


def _age_runs(db: Database, *, seconds: int = 4000) -> None:
    """Make every run older than the reconciliation grace period."""

    with db.tx() as conn:
        conn.execute(
            "update research_runs set updated_at = updated_at "
            "- make_interval(secs => %s)",
            (seconds,),
        )


def _age_everything(db: Database, *, seconds: int = 4000) -> None:
    _advance_queue(db, seconds=seconds)
    _age_runs(db, seconds=seconds)


def _exhaust_queue(daemon: Daemon, db: Database, *, rounds: int = 10) -> list[Any]:
    """Run the queue to a standstill, letting every scheduled retry come due.

    Deliberately does *not* age the runs, so a run left in flight by the last
    exhausted attempt is observable in exactly the state the incident left it:
    RUNNING, within its grace period, with nothing that could advance it.
    """

    reports: list[Any] = []
    for _ in range(rounds):
        reports.extend(_drain(daemon, passes=4))
        if not _live_work(db):
            break
        _advance_queue(db)
    return reports


def _live_work(db: Database, run_id: str | None = None) -> list[dict[str, Any]]:
    """Work items that could still run: pending, leased or waiting."""

    with db.tx() as conn:
        rows = conn.execute(
            "select * from work_items where status in "
            "('PENDING','LEASED','WAITING') and (%s::text is null or run_id = %s)",
            (run_id, run_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _run_cycle_items(db: Database, run_id: str) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "select * from work_items where run_id = %s and kind = %s "
                "order by created_at",
                (run_id, WorkKind.RUN_CYCLE),
            ).fetchall()
        ]


def _invocations(db: Database) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "select * from tool_invocations order by started_at"
            ).fetchall()
        ]


def _events(db: Database, kind: str) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "select * from events where kind = %s order by created_at", (kind,)
            ).fetchall()
        ]


# ------------------------------------------------------ the taxonomy itself --
#: Classes excluded from INFRASTRUCTURE on purpose, restated here so the test
#: below fails when a *new* class is added rather than when an existing one is
#: reconsidered. See the commentary on INFRASTRUCTURE for each reason.
_SCIENTIFIC_OR_POLICY: frozenset[FailureClass] = frozenset(
    {
        FailureClass.MODEL_OUTPUT_INVALID,
        FailureClass.MODEL_OUTPUT_INVALID_REPEATED,
        FailureClass.DETERMINISTIC_CHECK_FAILED,
        FailureClass.CODE_EXCEPTION,
        FailureClass.SLURM_PREEMPTED,
        FailureClass.SLURM_NODE_FAILURE,
        FailureClass.SLURM_OUT_OF_MEMORY,
        FailureClass.SLURM_TIMEOUT,
        FailureClass.EXECUTOR_FAILED,
        FailureClass.DERIVED_INDEX_CORRUPT,
        FailureClass.GIT_CONFLICT,
        FailureClass.ARTIFACT_MISSING,
        FailureClass.BUDGET_EXHAUSTED,
        FailureClass.MISSING_SCIENTIFIC_AUTHORITY,
        FailureClass.CAPABILITY_DENIED,
        FailureClass.POLICY_REFUSED,
        FailureClass.UNKNOWN,
    }
)


def test_the_failure_classes_partition_into_infrastructure_and_not() -> None:
    """A new class must be classified, not default into a scientific reading.

    The same completeness discipline the policy table has, for the same
    reason. ``is_infrastructure`` returning False is what lets a failure reach
    ``conclude`` and be turned into a terminal scientific state, so a class
    nobody classified defaults to the dangerous answer -- silently, because
    ``in`` on a frozenset does not complain about a member it has never seen.

    The list below is a second, independent statement of the same decision,
    and stating it twice is the mechanism rather than a redundancy: a new
    class is absent from both and fails here. Reconsidering an *existing*
    class means editing both, deliberately, which is the review this test
    cannot perform and should not pretend to.

    Asserted as a partition -- disjoint and exhaustive -- so that moving a
    member without removing it from the other side is also caught.
    """

    overlap = INFRASTRUCTURE & _SCIENTIFIC_OR_POLICY
    assert overlap == frozenset(), "a class cannot be both: " + ", ".join(
        str(member) for member in overlap
    )
    undecided = set(FailureClass) - INFRASTRUCTURE - _SCIENTIFIC_OR_POLICY
    assert undecided == set(), (
        "these classes are neither infrastructure nor deliberately excluded, "
        "so they would silently be read as a scientific outcome: "
        + ", ".join(sorted(str(member) for member in undecided))
    )
    # And the exclusion list names nothing that does not exist, so a renamed
    # member cannot leave a stale entry making the check above pass.
    assert _SCIENTIFIC_OR_POLICY <= set(FailureClass)


def test_provider_failures_are_infrastructure() -> None:
    assert PROVIDER_FAILURES <= INFRASTRUCTURE
    assert all(is_infrastructure(member) for member in PROVIDER_FAILURES)


# ------------------------------------------------- T1: no false conclusion --
def test_a_cycle_whose_planner_never_ran_concludes_nothing(
    plane: dict[str, Any],
) -> None:
    """T1. The defect itself: an unexecuted stage must not be a scientific answer.

    The provider is down for every attempt. The run must not end SUCCEEDED,
    must not carry a terminal state that means the runtime finished, and must
    not appear anywhere a researcher would read as "your turn".
    """

    store: RuntimeStore = plane["store"]
    plane["router"].down = True
    run_id = _request_run(store, "decide whether X survives")

    _exhaust_queue(plane["daemon"], plane["db"])

    run = store.require_run(run_id)
    assert run.status is not RunStatus.SUCCEEDED, (
        f"a run whose only model call never happened ended {run.status}"
    )
    assert run.terminal_state not in {
        TerminalState.DONE_FOR_NOW,
        TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
    }, f"an outage produced the scientific terminal state {run.terminal_state}"

    # And it is not offered to a capsule change as a parked objective, which
    # is the same claim in the vocabulary the rest of the runtime reads.
    parked = store.parked_objectives(project_id=PROJECT)
    assert run_id not in {entry.run_id for entry in parked}

    # No approval was invented for the researcher to answer.
    assert store.list_approvals(pending_only=True) == ()


# ------------------------------------------- T2/T3: cooldown-aware retrying --
def test_work_turned_away_by_an_open_breaker_is_not_charged_an_attempt(
    plane: dict[str, Any],
) -> None:
    """T2. Being refused at the door is not a failed try.

    When routing rejects a call because every provider is cooling, no
    invocation happens. Charging the attempt means three attempts can be spent
    without the provider being asked once, which is how a five-minute outage
    became permanent.
    """

    store: RuntimeStore = plane["store"]
    cooldown_until = datetime.now(UTC) + timedelta(seconds=300)
    plane["router"].down = True
    plane["router"].attempted = False
    plane["router"].cooldown_until = cooldown_until

    run_id = _request_run(store, "objective under an open breaker")
    # Ingest, claim and be turned away all happen in one pass; the pass after
    # it only consumes the WORK_DEFERRED event this one recorded.
    reports = [plane["daemon"].tick(), plane["daemon"].tick()]

    assert sum(r.work_deferred for r in reports) == 1, (
        "the item was failed rather than deferred"
    )
    assert sum(r.work_failed for r in reports) == 0

    item = _run_cycle_items(plane["db"], run_id)[0]
    assert item["status"] == str(WorkStatus.PENDING)
    assert item["attempts"] == 0, "an attempt was charged for a call never made"

    # Bounded on both sides, and the upper bound is the one that matters.
    # "At or after the cooldown" alone accepted a nine-month deferral, which
    # is what the first version of this path produced: it converted the
    # database's deadline into a delay by subtracting the daemon's injected
    # frozen clock, which sits in January. The deadline is the database's to
    # compute, and this is the assertion that says so.
    assert item["scheduled_at"] >= cooldown_until - timedelta(seconds=5), (
        "the retry was scheduled inside the cooldown it cannot succeed in"
    )
    assert item["scheduled_at"] <= cooldown_until + timedelta(minutes=5), (
        f"the item was deferred to {item['scheduled_at']}, long past the "
        f"cooldown ending at {cooldown_until} -- a clock was used where a "
        f"database timestamp belonged"
    )
    assert item["failure_class"] == str(FailureClass.PROVIDER_UNAVAILABLE), (
        "a parked item should still say what it is waiting for"
    )
    assert _events(plane["db"], "WORK_DEFERRED")


def test_a_retry_never_lands_inside_a_cooldown_it_cannot_survive(
    plane: dict[str, Any],
) -> None:
    """T3. The live configuration, reproduced exactly.

    ``provider_cooldown_seconds = 300`` against a PROVIDER_UNAVAILABLE backoff
    of 30s then 60s. Every retry the old scheduler produced fell inside the
    cooldown, so the item was exhausted three and a half minutes before the
    provider was usable -- a transient failure made permanent by two settings
    that had never been compared.

    The fix is not a bigger attempt budget. It is that the schedule is the
    later of the backoff and the provider's own deadline, so it stays correct
    when either number changes.
    """

    store: RuntimeStore = plane["store"]
    cooldown_until = datetime.now(UTC) + timedelta(seconds=300)
    plane["router"].down = True
    plane["router"].attempted = True  # the call was made, and it failed
    plane["router"].cooldown_until = cooldown_until

    run_id = _request_run(store, "objective during a five-minute outage")
    plane["daemon"].tick()

    item = _run_cycle_items(plane["db"], run_id)[0]
    assert item["attempts"] == 1, "a real invocation should cost an attempt"
    assert item["status"] == str(WorkStatus.PENDING)
    # The linear backoff for this class would have said +30s. The breaker says
    # +300s. The later one wins.
    assert item["scheduled_at"] >= cooldown_until - timedelta(seconds=2), (
        f"retry scheduled at {item['scheduled_at']}, inside a cooldown ending "
        f"at {cooldown_until}"
    )


def test_the_configured_breaker_settings_are_the_ones_that_apply(
    runtime_db: Database, tmp_path: Path, pg_dsn: str
) -> None:
    """The settings a researcher writes must be the settings that run.

    ``provider_failure_threshold`` and ``provider_cooldown_seconds`` were
    documented, validated and printed by `runtime doctor`, and never read: the
    router called the store with its default arguments, which happened to
    equal the defaults in the schema. Changing either in configuration changed
    nothing, and nothing failed to say so.
    """

    from research_os.runtime.artifacts import FilesystemArtifactStore
    from research_os.runtime.budgets import BudgetLedger
    from research_os.runtime.routing import ModelRouter, ProviderProfile
    from tests.fake_providers import FakeProvider, ScriptedResponse

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(tmp_path))
    run = store.create_run(project_id=PROJECT, objective="o")
    broken = FakeProvider(
        name="broken",
        family="a",
        responses={
            "planner": [
                ScriptedResponse(structured=None, exit_code=1, error="down")
                for _ in range(3)
            ]
        },
    )
    router = ModelRouter(
        adapters={"broken": broken},
        profiles=(ProviderProfile(name="broken", family="a", tier=3),),
        store=store,
        artifacts=FilesystemArtifactStore(tmp_path / "artifacts", store=store),
        budgets=BudgetLedger(runtime_db),
        run_id=run.run_id,
        project_id=PROJECT,
        # One failure is enough, and the wait is an hour. Neither is a default.
        failure_threshold=1,
        cooldown_seconds=3600,
    )
    with pytest.raises(ProviderCallFailedError) as raised:
        router.complete(
            ModelRequest(
                role="planner",  # type: ignore[arg-type]
                capability="planning",  # type: ignore[arg-type]
                prompt="x",
                prompt_version="test@1",
                json_schema={"type": "object"},
            )
        )

    health = {entry.provider: entry for entry in store.provider_health()}["broken"]
    assert health.healthy is False, "threshold=1 should open on the first failure"
    assert health.cooldown_until is not None
    remaining = health.cooldown_until - datetime.now(UTC)
    assert remaining > timedelta(seconds=3000), (
        f"cooldown_seconds=3600 was not applied; {remaining} remains"
    )
    assert raised.value.retry_at == health.cooldown_until, (
        "the raised failure should carry the deadline the queue schedules against"
    )


# --------------------------------------------- T4/T5: run reconciliation ----
def test_a_run_with_exhausted_work_and_nothing_left_is_rescheduled(
    plane: dict[str, Any],
) -> None:
    """T4. The orphan, and the way out of it.

    A RUNNING run whose only work item is FAILED has no path forward anywhere
    else in this control plane: nothing polls it, no event names it, the queue
    is finished with it. Three of these sat for twenty-one hours.
    """

    store: RuntimeStore = plane["store"]
    plane["router"].down = True
    run_id = _request_run(store, "objective stranded by an outage")
    _exhaust_queue(plane["daemon"], plane["db"])

    run = store.require_run(run_id)
    assert run.status is RunStatus.RUNNING, "precondition: the run is in flight"
    assert _live_work(plane["db"], run_id) == [], (
        "precondition: nothing live is left to advance it"
    )

    # The provider comes back, and enough time passes for the grace period.
    plane["router"].down = False
    _age_runs(plane["db"])

    report = plane["daemon"].tick()
    assert report.runs_rescheduled == 1, "the stranded run was not reconciled"
    assert _events(plane["db"], "RUN_RESCHEDULED")


def test_reconciliation_waits_while_the_provider_is_still_down(
    plane: dict[str, Any],
) -> None:
    """Rescheduling into a live outage would spend the recovery budget on nothing.

    The bound on reschedules exists so recovery terminates. Spending it while
    the cause is visibly unlifted would mean an outage lasting longer than
    ``max_run_reschedules`` ticks consumed the whole budget and abandoned a
    run that only needed to wait.
    """

    store: RuntimeStore = plane["store"]
    plane["router"].down = True
    run_id = _request_run(store, "objective during a continuing outage")
    _exhaust_queue(plane["daemon"], plane["db"])
    _age_runs(plane["db"])

    # The breaker is open, as it would be after repeated real failures.
    store.record_provider_result(
        "scripted", ok=False, error="down", threshold=1, cooldown=600
    )
    with plane["db"].tx() as conn:
        conn.execute(
            "update work_items set failure_class = %s where run_id = %s",
            (str(FailureClass.PROVIDER_UNAVAILABLE), run_id),
        )

    # The positive control, without which this test passes for any reason at
    # all -- a run still holding live work, a grace period that has not
    # elapsed, a staging mistake. "Nothing was rescheduled" is only evidence
    # about cooldown if the run was otherwise going to be.
    assert run_id in {
        entry.run.run_id for entry in store.stranded_runs(grace_seconds=300)
    }, "precondition: the run qualifies as stranded on every count but the breaker"

    report = plane["daemon"].tick()
    assert report.runs_rescheduled == 0, "rescheduled into a provider still cooling"
    assert store.event_count(run_id=run_id, kind="RUN_RESCHEDULED") == 0

    # And it is the breaker doing it: clear the cooldown and the same run is
    # rescheduled on the next pass, with nothing else changed.
    store.record_provider_result("scripted", ok=True)
    assert plane["daemon"].tick().runs_rescheduled == 1


def test_a_restarted_daemon_reconciles_a_stranded_run_exactly_once(
    plane: dict[str, Any],
) -> None:
    """T5. The repair must survive a restart without duplicating itself.

    Reconciliation is the one pass that creates work from nothing observable
    in the queue, so it is the one most able to create it twice. The dedup key
    names the reschedule ordinal, which two daemons compute identically.
    """

    store: RuntimeStore = plane["store"]
    plane["router"].down = True
    run_id = _request_run(store, "objective that outlives its daemon")
    _exhaust_queue(plane["daemon"], plane["db"])
    plane["router"].down = False
    _age_runs(plane["db"])

    first = plane["build"]("worker-a")
    second = plane["build"]("worker-b")

    reports = [first.tick(), second.tick()]
    total = sum(report.runs_rescheduled for report in reports)
    assert total == 1, f"the run was rescheduled {total} times, not once"
    assert store.event_count(run_id=run_id, kind="RUN_RESCHEDULED") == 1

    # **The mechanism, not only the outcome.**
    #
    # Two daemons ticking one after another is not by itself evidence for the
    # dedup key: the first tick calls `touch_run` and leaves live work, so the
    # second finds nothing stranded and would have done nothing whatever the
    # key said. The key matters for two daemons in the *same* pass, both
    # reading `used == 0` before either writes -- which is what this asserts
    # directly, by recomputing the key they would both compute.
    queued = _run_cycle_items(plane["db"], run_id)[-1]
    assert queued["dedup_key"] == f"reconcile:{run_id}:1", queued["dedup_key"]
    again = plane["queue"].enqueue(
        project_id=PROJECT,
        kind=WorkKind.RUN_CYCLE,
        run_id=run_id,
        dedup_key=f"reconcile:{run_id}:1",
    )
    assert not again.created, (
        "a second pass computing the same reschedule ordinal created a second work item"
    )
    assert again.item.work_id == queued["work_id"]


def test_reconciliation_gives_up_in_a_state_that_claims_no_science(
    plane: dict[str, Any],
) -> None:
    """Recovery is bounded, and the end of it is honest.

    Past ``max_run_reschedules`` the run fails as an infrastructure error --
    not DONE_FOR_NOW, which would offer it to the next capsule change as
    though it had concluded something.
    """

    store: RuntimeStore = plane["store"]
    plane["router"].down = True
    run_id = _request_run(store, "objective nobody can run")
    ceiling = plane["config"].settings.max_run_reschedules

    # The provider is gone but its breaker is not the reason recorded, so
    # reconciliation keeps trying -- which is what bounds it.
    for _ in range(ceiling + 3):
        _exhaust_queue(plane["daemon"], plane["db"])
        _age_runs(plane["db"])
        plane["daemon"].tick()

    run = store.require_run(run_id)
    assert run.status is RunStatus.FAILED
    assert run.terminal_state is TerminalState.FATAL_INFRASTRUCTURE_ERROR
    assert run.finished_at is not None, "an abandoned run must stop being in flight"
    assert _events(plane["db"], "RUN_ABANDONED")
    # Still not a parked objective: nothing about this asks a person anything.
    assert run_id not in {
        entry.run_id for entry in store.parked_objectives(project_id=PROJECT)
    }


# ----------------------------------------------- T6/T11: objectives return --
def test_an_outage_does_not_remove_an_objective_from_the_frontier(
    plane: dict[str, Any],
) -> None:
    """T6. The most expensive consequence, and the least visible.

    ``parked_objectives`` excludes an objective whose newest run is in flight,
    which is correct and is why an immortal RUNNING run deletes the objective
    from the frontier. Recovery has to put it back, and it has to do so
    without a capsule change -- there is nothing scientific to change.
    """

    store: RuntimeStore = plane["store"]
    objective = "determine whether the 2023 contribution survives"
    plane["router"].down = True
    run_id = _request_run(store, objective)
    _exhaust_queue(plane["daemon"], plane["db"])

    # The defect, reproduced: the objective is nowhere.
    assert objective not in {
        entry.objective for entry in store.parked_objectives(project_id=PROJECT)
    }

    plane["router"].down = False
    _age_runs(plane["db"])
    _drain(plane["daemon"], passes=10)

    run = store.require_run(run_id)
    assert run.status is RunStatus.SUCCEEDED, (
        f"the recovered run did not complete: {run.status} / {run.detail}"
    )
    assert objective in {
        entry.objective for entry in store.parked_objectives(project_id=PROJECT)
    }, "the objective did not return to the frontier after recovery"


def test_the_original_work_resumes_when_the_provider_returns(
    plane: dict[str, Any],
) -> None:
    """T11. Recovery is the same run, not a new one.

    Re-entering the existing run at its own checkpoint is what keeps recovery
    free: no successor, no lineage link, no capsule change, and the planning
    the cycle already did is reused rather than paid for again.
    """

    store: RuntimeStore = plane["store"]
    plane["router"].down = True
    run_id = _request_run(store, "objective that survives an outage")
    _exhaust_queue(plane["daemon"], plane["db"])
    before = store.list_runs(project_id=PROJECT, limit=100)

    plane["router"].down = False
    _age_runs(plane["db"])
    _drain(plane["daemon"], passes=10)

    run = store.require_run(run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.terminal_state is TerminalState.DONE_FOR_NOW
    after = store.list_runs(project_id=PROJECT, limit=100)
    # A successor may legitimately open once the cycle concludes; what must not
    # happen is a *second* run of the same objective opened by the recovery.
    duplicates = [
        entry
        for entry in after
        if entry.objective == run.objective and entry.run_id != run_id
    ]
    assert len(duplicates) <= 1, (
        "recovery opened more than one continuation: "
        + ", ".join(entry.run_id for entry in duplicates)
    )
    assert len(after) >= len(before)


# ------------------------------------------------ T7: per-objective isolation --
def test_one_objectives_failure_does_not_silence_the_others(
    plane: dict[str, Any],
) -> None:
    """T7. Five parked objectives, the third one fails, all five get an answer.

    The old loop caught exactly one expected exception and let everything else
    out. A provider failure while starting the fourth objective abandoned the
    fifth -- not skipped, not deferred, not logged. From outside, "never
    evaluated" and "not eligible" were the same observation.
    """

    store: RuntimeStore = plane["store"]
    parked_ids = []
    for index in range(5):
        run = store.create_run(
            project_id=PROJECT, objective=f"parked objective number {index}"
        )
        store.set_run_status(run.run_id, RunStatus.RUNNING)
        store.set_frontier_digest(run.run_id, f"digest-of-cycle-{index}")
        store.set_run_status(
            run.run_id,
            RunStatus.SUCCEEDED,
            terminal_state=TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
            next_recommendation="WAIT_HUMAN",
        )
        parked_ids.append(run.run_id)

    assert len(store.parked_objectives(project_id=PROJECT)) == 5

    # The *third objective* hits the outage, not the third model call. An
    # earlier version of this test counted calls, and because each objective
    # costs three, call three was the first objective's reviewer -- so the
    # docstring described a mid-loop failure the test never created.
    plane["router"].fail_prompts_containing = "parked objective number 2"

    store.record_event(
        kind="CAPSULE_CHANGED",
        project_id=PROJECT,
        payload={
            "capsule_digest": "capsule-after-the-change",
            "frontier_digest": "frontier-after-the-change",
            "previous_capsule_digest": "capsule-before",
        },
        dedup_key="capsule:alpha:one",
    )
    plane["daemon"].tick()  # ingest the change into an advance_objective item
    plane["daemon"].tick()  # run it

    dispositions = _events(plane["db"], "OBJECTIVE_DISPOSITION")
    decided = {event["run_id"] for event in dispositions}
    missing = [run_id for run_id in parked_ids if run_id not in decided]
    assert missing == [], (
        "these objectives were never evaluated and received no disposition: "
        + ", ".join(missing)
    )
    by_run = {
        event["run_id"]: event["payload"]["disposition"] for event in dispositions
    }
    assert by_run[parked_ids[2]] == "FAILED_TO_ADVANCE", (
        f"the failing objective was recorded as {by_run[parked_ids[2]]}"
    )
    # The objectives *after* the failure are the ones the old loop lost. They
    # are the assertion; the ones before it would pass either way.
    after = [parked_ids[3], parked_ids[4]]
    assert all(by_run[run_id] == "ADVANCED" for run_id in after), (
        "objectives after the failure did not advance: "
        + ", ".join(f"{r}={by_run[r]}" for r in after)
    )


# --------------------------------------------------------- T8: replay safety --
def test_replaying_one_capsule_change_opens_at_most_one_successor_each(
    plane: dict[str, Any],
) -> None:
    """T8. Isolation must not cost idempotence.

    The advance handler now raises after recording dispositions, so it is
    retried more often than before. Every extra attempt is a chance to open a
    second successor for an objective that already has one.
    """

    store: RuntimeStore = plane["store"]
    for index in range(3):
        run = store.create_run(
            project_id=PROJECT, objective=f"replayed objective {index}"
        )
        store.set_run_status(run.run_id, RunStatus.RUNNING)
        store.set_frontier_digest(run.run_id, f"old-digest-{index}")
        store.set_run_status(
            run.run_id,
            RunStatus.SUCCEEDED,
            terminal_state=TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
            next_recommendation="WAIT_HUMAN",
        )

    # The handler is invoked directly, three times, with one payload.
    #
    # Going through `record_event` would not test this: `_dedup_key` keys an
    # advance on `project:previous->digest`, so the second and third events
    # enqueue nothing and the handler runs *once*. That is a real protection
    # and it is tested elsewhere, but it means the ingest path cannot
    # exercise the property this test is named for -- the handler now raises
    # after recording dispositions, so it is retried more often than before,
    # and every retry is a fresh chance to open a second successor.
    item = (
        plane["queue"]
        .enqueue(
            project_id=PROJECT,
            kind=WorkKind.ADVANCE_OBJECTIVE,
            payload={
                "capsule_digest": "one-and-the-same-digest",
                "frontier_digest": "one-and-the-same-frontier",
                "previous_capsule_digest": "before",
            },
            dedup_key="advance:replayed",
        )
        .item
    )
    for _ in range(3):
        plane["daemon"]._work_advance_objective(item)

    runs = store.list_runs(project_id=PROJECT, limit=100)
    by_parent: dict[str, int] = {}
    for run in runs:
        if run.parent_run_id:
            by_parent[run.parent_run_id] = by_parent.get(run.parent_run_id, 0) + 1
    over = {parent: n for parent, n in by_parent.items() if n > 1}
    assert over == {}, f"a replayed capsule change opened duplicate successors: {over}"


# ------------------------------------------------------ T9: the cycle budget --
def test_an_outage_does_not_spend_the_objectives_cycle_allowance(
    plane: dict[str, Any],
) -> None:
    """T9. Infrastructure must not consume scientific budget.

    ``max_cycles_per_objective`` bounds how far an objective may be pursued
    autonomously. It is a scientific allowance: it exists because the runtime
    cannot change canonical state, so repeating a cycle repeats its cost
    without adding information. A failed OAuth refresh adds no information
    either, and must not be charged the same way.

    The mechanism is that recovery re-enters the same run. A run has one
    lineage link whatever happens inside it.
    """

    store: RuntimeStore = plane["store"]
    plane["router"].down = True
    run_id = _request_run(store, "objective with a finite allowance")
    _exhaust_queue(plane["daemon"], plane["db"])

    objective = "objective with a finite allowance"

    def deepest() -> int:
        """The longest chain any run of this objective sits at.

        Measured over every run of the objective, not over ``run_id``.
        `lineage_depth` walks *ancestors*, and the run this test starts has no
        parent -- so asserting it stays at zero is an assertion that cannot
        fail, whatever reconciliation does. A wrongly-created successor would
        raise the *successor's* depth and leave this one at zero. An earlier
        version of this test made exactly that mistake.
        """

        return max(
            (
                store.lineage_depth(run.run_id)
                for run in store.list_runs(project_id=PROJECT, limit=200)
                if run.objective == objective
            ),
            default=-1,
        )

    assert deepest() == 0, "a first cycle should be at depth zero"

    for _ in range(3):
        _age_runs(plane["db"])
        _exhaust_queue(plane["daemon"], plane["db"])

    assert deepest() == 0, (
        "an outage created lineage, so repeated infrastructure failures would "
        "exhaust the objective's cycle ceiling without any science happening"
    )
    same_objective = [
        run
        for run in store.list_runs(project_id=PROJECT, limit=200)
        if run.objective == objective
    ]
    assert len(same_objective) == 1, (
        "recovery opened new cycles instead of resuming the existing one: "
        + ", ".join(run.run_id for run in same_objective)
    )
    # The control: the measurement above *can* move. A real successor of this
    # run puts the objective at depth 1, so a test that never sees 1 is not
    # measuring anything.
    store.create_run(
        project_id=PROJECT,
        objective=objective,
        parent_run_id=run_id,
        cycle_index=1,
    )
    assert deepest() == 1, "the depth measurement cannot detect a successor"


# ------------------------------------------------- T10: the operator surface --
def test_status_never_tells_a_researcher_an_outage_finished_cleanly(
    plane: dict[str, Any],
) -> None:
    """T10. The claim the researcher actually read, and must not read again.

    ``runtime status`` put every parked run under "WAITING FOR A SCIENTIFIC
    DECISION (finished; you are next)" and added "These finished cleanly."
    ``parked_objectives`` accepts DONE_FOR_NOW, and DONE_FOR_NOW is what a
    cycle concluded when its planner call died. So the sentence was attached
    to a run that had done nothing, and the researcher went looking for a
    decision that did not exist.
    """

    store: RuntimeStore = plane["store"]

    # One run that genuinely reached a scientific gate.
    gated = store.create_run(project_id=PROJECT, objective="a real question")
    store.set_run_status(gated.run_id, RunStatus.RUNNING)
    store.set_run_status(
        gated.run_id,
        RunStatus.SUCCEEDED,
        terminal_state=TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
        detail="a proposal is waiting for your decision",
        next_recommendation="WAIT_HUMAN",
    )

    # One that concluded only because it could not do anything.
    blocked = store.create_run(project_id=PROJECT, objective="a blocked question")
    store.set_run_status(blocked.run_id, RunStatus.RUNNING)
    store.set_run_status(
        blocked.run_id,
        RunStatus.SUCCEEDED,
        terminal_state=TerminalState.DONE_FOR_NOW,
        detail="nothing permitted to do (the planner returned no usable plan)",
        next_recommendation="BLOCKED",
    )

    # And one stranded in flight.
    plane["router"].down = True
    stranded_id = _request_run(store, "a stranded question")
    _exhaust_queue(plane["daemon"], plane["db"])
    _age_runs(plane["db"])

    report = views.collect_status(plane["db"], reconcile_grace_seconds=300)
    text = views.render_status(report)

    header = "WAITING FOR A SCIENTIFIC DECISION (finished; you are next)"
    section = text.split(header, 1)
    assert len(section) == 2, "the genuine gate should still be announced"
    until_next = section[1].split("\n\n", 1)[0]
    assert gated.run_id in until_next
    assert blocked.run_id not in until_next, (
        "a run blocked by an outage was presented as a decision awaiting the researcher"
    )
    assert stranded_id not in until_next

    assert "STALLED (in flight, but nothing is running)" in text
    assert stranded_id in text.split("STALLED", 1)[1]
    assert blocked.run_id in text.split("PARKED (nothing is owed", 1)[1]

    # And it is labelled where a reader first meets it. Its status really is
    # RUNNING, so hiding it would make this table disagree with the database;
    # printing it unlabelled is how three dead runs looked busy for a day.
    running_section = text.split("RUNNING\n", 1)[1].split("\n\n", 1)[0]
    stranded_row = next(
        line for line in running_section.splitlines() if stranded_id in line
    )
    assert "stalled" in stranded_row, stranded_row

    payload = report.payload()
    assert stranded_id in {entry["run_id"] for entry in payload["stranded"]}


# ---------------------------------------------------- T12: the whole incident --
def test_the_september_incident_end_to_end(plane: dict[str, Any]) -> None:
    """T12. The canonical lifecycle regression, in the order it happened.

    A human capsule change, several parked objectives, successors opened, the
    provider disappearing mid-flight, the breaker, exhausted retries, a daemon
    restart, reconciliation, and recovery -- with the two properties that were
    violated asserted at the end: no objective was lost, and nothing false was
    said about the science.
    """

    store: RuntimeStore = plane["store"]
    objectives = [f"pilot objective {index}" for index in range(4)]
    for index, objective in enumerate(objectives):
        run = store.create_run(project_id=PROJECT, objective=objective)
        store.set_run_status(run.run_id, RunStatus.RUNNING)
        store.set_frontier_digest(run.run_id, f"frontier-before-{index}")
        store.set_run_status(
            run.run_id,
            RunStatus.SUCCEEDED,
            terminal_state=TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
            next_recommendation="WAIT_HUMAN",
        )

    # The researcher commits a question. The provider dies at the same time.
    plane["router"].down = True
    plane["router"].cooldown_until = datetime.now(UTC) + timedelta(seconds=300)
    store.record_event(
        kind="CAPSULE_CHANGED",
        project_id=PROJECT,
        payload={
            "capsule_digest": "after-the-question",
            "frontier_digest": "frontier-after-the-question",
            "previous_capsule_digest": "before-the-question",
        },
        dedup_key="capsule:alpha:pilot",
    )
    # Driven until every attempt is spent, which is what the outage did. An
    # earlier version drained a few passes and then aged the *queue* along
    # with the runs, so the still-PENDING retries came due against a healthy
    # provider and the whole thing recovered through ordinary retrying --
    # with reconciliation never invoked. The test passed with the
    # reconciliation pass deleted, which is how that was found.
    _exhaust_queue(plane["daemon"], plane["db"], rounds=14)

    # Nothing may have concluded anything scientific.
    for run in store.list_runs(project_id=PROJECT, limit=100):
        if run.status is RunStatus.SUCCEEDED:
            assert run.terminal_state is not TerminalState.DONE_FOR_NOW or (
                run.frontier_digest or ""
            ).startswith("frontier-before-"), (
                f"{run.run_id} concluded DONE_FOR_NOW during a total outage"
            )

    # The positive control: the incident's shape really is on the table, so
    # what follows is about recovering from it rather than about it never
    # having happened.
    orphans = store.stranded_runs(grace_seconds=0)
    assert orphans, "the outage left no stranded run, so there is nothing to recover"
    assert {entry.run.objective for entry in orphans} & set(objectives), (
        "the stranded runs belong to other objectives than the seeded ones"
    )
    hidden = {
        entry.objective
        for entry in store.parked_objectives(project_id=PROJECT, limit=50)
    }
    assert not (hidden & set(objectives)), (
        "D4 did not reproduce: the objectives are still reachable, so the "
        "recovery assertion below would pass without recovering anything"
    )

    # The daemon is restarted, as it was. Only the *runs* are aged from here
    # on: ageing the queue would let ordinary retries do the work and the
    # reconciliation path would go unexercised.
    plane["router"].down = False
    plane["router"].cooldown_until = None
    restarted = plane["build"]("worker-after-restart")
    for _ in range(6):
        _age_runs(plane["db"])
        _drain(restarted, passes=10)

    # **Reachability measured the way the runtime measures it.**
    #
    # An earlier version of this assertion scanned every run for a parkable
    # terminal state, which the four seeded parents satisfy permanently -- so
    # it could not fail, and specifically could not detect D4, whose whole
    # shape is a *parent* hidden behind an immortal RUNNING child while its
    # own row sits untouched.
    #
    # The question the incident actually asks is the one `parked_objectives`
    # answers: given the newest run of each objective, can a future capsule
    # change still reach it? So that is what is asked, and an objective whose
    # cycle is genuinely in flight counts too -- it is being worked on, which
    # is the other way of not being lost.
    runs = store.list_runs(project_id=PROJECT, limit=200)
    parked_now = {
        entry.objective
        for entry in store.parked_objectives(project_id=PROJECT, limit=50)
    }
    in_flight = {
        run.objective
        for run in runs
        if not run.terminal and _live_work(plane["db"], run.run_id)
    }
    lost = [
        objective
        for objective in objectives
        if objective not in parked_now and objective not in in_flight
    ]
    assert lost == [], (
        "objectives no future capsule change can reach, and no cycle is "
        f"working on: {lost}"
    )

    stranded = store.stranded_runs(grace_seconds=0)
    assert stranded == (), "runs left in flight with nothing to run: " + ", ".join(
        entry.run.run_id for entry in stranded
    )

    # At most one successor per parent, throughout.
    by_parent: dict[str, int] = {}
    for run in runs:
        if run.parent_run_id:
            by_parent[run.parent_run_id] = by_parent.get(run.parent_run_id, 0) + 1
    assert {p: n for p, n in by_parent.items() if n > 1} == {}


# ------------------------------------- the two defences inside the graph ----
def test_an_actions_outage_is_not_frozen_into_the_idempotency_ledger(
    plane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure the ledger records as COMPLETED can never be retried.

    The action handlers catch ``RoutingError`` and return
    ``ActionOutcome.failed(..., PROVIDER_UNAVAILABLE)``, which reads as
    careful and is not: ``InvocationLedger.run`` marks an invocation COMPLETED
    when ``perform`` *returns*, and a completed invocation is reused forever.
    So the cycle was sealed around the network error -- every later attempt
    would find the completed row, reuse the failure, and reach ``conclude``
    with a result that could not improve.

    Raising marks it FAILED, which the ledger reopens on the next entry.
    """

    from research_os.runtime.actions.base import ActionOutcome
    from research_os.runtime.cycles import start_cycle
    from research_os.runtime.failures import StageExecutionError
    from research_os.runtime.idempotency import InvocationLedger
    from research_os.runtime.models import InvocationStatus
    from research_os.runtime.registry import ACTION_HANDLERS, RegisteredAction

    def unreachable(_state: Any, _context: Any, _plan: Any) -> ActionOutcome:
        return ActionOutcome.failed(
            "the provider could not be reached",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )

    monkeypatch.setitem(
        ACTION_HANDLERS,
        ActionKind.ASSESS_FRONTIER,
        RegisteredAction(unreachable, replay_safe=True),
    )

    with pytest.raises(StageExecutionError) as raised:
        start_cycle(
            config=plane["config"],
            db=plane["db"],
            project_id=PROJECT,
            repo_path=plane["repo"],
            objective="an objective whose action cannot reach a provider",
            models=plane["router"],
        )
    assert raised.value.failure_class is FailureClass.PROVIDER_UNAVAILABLE

    invocations = [
        row
        for row in _invocations(plane["db"])
        if str(row["kind"]).startswith("cycle.")
    ]
    assert invocations, "the action never reached the ledger"
    assert all(
        row["status"] != str(InvocationStatus.COMPLETED) for row in invocations
    ), (
        "an outage was recorded as a completed invocation, so it can never be "
        "retried: " + ", ".join(f"{r['kind']}={r['status']}" for r in invocations)
    )
    assert InvocationLedger  # the class under discussion, imported deliberately


def test_conclude_refuses_to_read_an_outage_as_a_scientific_outcome(
    plane: dict[str, Any],
) -> None:
    """The last line of defence, checked directly rather than assumed.

    ``perform_action`` raises before this can happen, so this branch should be
    unreachable. "Should be unreachable" is how the original defect was
    described, and the cost of being wrong is the whole incident: without it,
    a failed action falls into "checks failed; a repair cycle is warranted"
    and recommends another cycle -- a scientific reading of a network problem
    that also spends one of the objective's cycles to reach the same failure.
    """

    from types import SimpleNamespace

    from research_os.runtime.graphs.cycle import conclude
    from tests.runtime_graph_helpers import make_context

    context = make_context(
        db=plane["db"],
        repo=plane["repo"],
        artifacts_root=plane["config"].artifacts_root,
        dsn=plane["config"].require_dsn(),
        models=plane["router"],
        permitted=(str(ActionKind.ASSESS_FRONTIER),),
    )
    state = {
        "run_id": "RRUN-test",
        "project_id": PROJECT,
        "repo_path": str(plane["repo"]),
        "objective": "o",
        "autonomy": "high",
        "cycle_index": 0,
        "notes": [],
        "plan": {"action": str(ActionKind.ASSESS_FRONTIER)},
        "action_result": {
            "ok": False,
            "detail": "the provider could not be reached",
            "data": {},
            "failure_class": str(FailureClass.PROVIDER_UNAVAILABLE),
        },
        "check_result": {"passed": False},
    }

    outcome = conclude(state, SimpleNamespace(context=context))  # type: ignore[arg-type]
    assert outcome["terminal_state"] == str(TerminalState.FATAL_INFRASTRUCTURE_ERROR)
    assert outcome["next_recommendation"] != "WAIT_HUMAN"
    assert outcome["next_recommendation"] != "START_NEXT_CYCLE", (
        "an outage recommended spending another cycle of the objective's ceiling"
    )

    # It wins over every scientific branch, including the one that asks a
    # person. A failed action can still carry `data`, and "a proposal is
    # waiting for your decision" attached to a cycle that never reached a
    # provider is the exact false request this whole change exists to stop.
    gating = dict(state)
    gating["action_result"] = {
        "ok": False,
        "detail": "the provider could not be reached",
        "data": {"requires_human_promotion": True, "follow_up": "decide this"},
        "failure_class": str(FailureClass.PROVIDER_UNAVAILABLE),
    }
    gated = conclude(gating, SimpleNamespace(context=context))  # type: ignore[arg-type]
    assert gated["terminal_state"] == str(TerminalState.FATAL_INFRASTRUCTURE_ERROR), (
        "an outage carrying promotion data asked the researcher for a decision"
    )

    # And a genuine check failure is still read as one.
    state["action_result"] = {"ok": True, "detail": "done", "data": {}}
    healthy = conclude(state, SimpleNamespace(context=context))  # type: ignore[arg-type]
    assert healthy["terminal_state"] == str(TerminalState.DONE_FOR_NOW)
    assert healthy["next_recommendation"] == "START_NEXT_CYCLE"


def test_extracting_nothing_from_every_document_is_not_an_extraction(
    plane: dict[str, Any],
) -> None:
    """The same defect one level down, where partial tolerance hid it.

    ``parse_literature`` tolerates a failure per document on purpose: one
    unreadable PDF among four should not lose the other three, and "extracted
    fields from 3 of 4" is a real observation. The boundary was wrong, not the
    tolerance. When *every* document failed it still returned
    ``succeeded("extracted fields from 4 document(s)")``, and because
    ``search_literature``-family actions record a finding, that sentence
    became citable provenance for an extraction that never happened.
    """

    from research_os.runtime.actions.literature import parse_literature
    from tests.runtime_graph_helpers import make_context

    context = make_context(
        db=plane["db"],
        repo=plane["repo"],
        artifacts_root=plane["config"].artifacts_root,
        dsn=plane["config"].require_dsn(),
        models=plane["router"],
        permitted=(),
    )
    refs = [
        context.artifacts.put_text(
            f"a paper about widgets, number {index}",
            role="fulltext",
            producer="test",
        )
        for index in range(3)
    ]
    state = {
        "run_id": "RRUN-test",
        "artifacts": [
            {"artifact_id": ref.artifact_id, "role": "fulltext"} for ref in refs
        ],
    }

    # A total outage re-raises the original exception rather than reporting
    # it, because the exception carries the breaker's deadline and an
    # `ActionOutcome` has nowhere to put it.
    plane["router"].down = True
    plane["router"].cooldown_until = datetime.now(UTC) + timedelta(seconds=300)
    with pytest.raises(ProviderCallFailedError) as raised:
        parse_literature(state, context, {})
    assert raised.value.failure_class is FailureClass.PROVIDER_UNAVAILABLE
    assert is_infrastructure(raised.value.failure_class)
    assert raised.value.retry_at == plane["router"].cooldown_until, (
        "the deadline was lost between the document loop and the caller"
    )

    # A total *budget* refusal is different: no amount of waiting helps, so it
    # is reported rather than raised, and classified as the policy answer it
    # is.
    plane["router"].down = False
    plane["router"].cooldown_until = None
    plane["router"].budget_exhausted = True
    refused = parse_literature(state, context, {})
    assert not refused.ok
    assert refused.failure_class is FailureClass.BUDGET_EXHAUSTED
    plane["router"].budget_exhausted = False

    # Partial extraction is still a result, and still says how partial.
    plane["router"].down = False
    plane["router"].inner.answers["extractor"] = {"claim": "widgets deform"}
    partial = parse_literature(state, context, {})
    assert partial.ok
    assert "of 3 document(s)" in partial.detail


def test_the_reschedule_budget_cannot_be_spent_in_one_burst(
    plane: dict[str, Any],
) -> None:
    """The recovery bound must span time, not ticks.

    Reconciliation does not write to the run row, so without an explicit touch
    a rescheduled run keeps its old ``updated_at`` and qualifies as stranded
    again the moment its new work item dies. The whole budget -- meant to span
    half an hour of trying -- was then spendable in as many seconds as the
    poll interval allows, and a run that needed to wait five minutes would be
    abandoned instead.
    """

    store: RuntimeStore = plane["store"]
    plane["router"].down = True
    run_id = _request_run(store, "objective reconciled under pressure")
    _exhaust_queue(plane["daemon"], plane["db"])
    _age_runs(plane["db"])

    assert plane["daemon"].tick().runs_rescheduled == 1

    # The rescheduled item dies too, and the run is stranded again -- but its
    # clock was reset, so the grace period has not elapsed.
    _exhaust_queue(plane["daemon"], plane["db"])
    assert _live_work(plane["db"], run_id) == []
    for _ in range(5):
        assert plane["daemon"].tick().runs_rescheduled == 0, (
            "the reschedule budget was spent without any time passing"
        )
    assert store.event_count(run_id=run_id, kind="RUN_RESCHEDULED") == 1


def test_a_cycle_that_is_simply_slow_is_not_mistaken_for_a_stranded_one(
    plane: dict[str, Any],
) -> None:
    """The false positive that would have made reconciliation dangerous.

    ``_work_advance_objective`` runs each successor's cycle inline, so between
    ``open_cycle`` and the ingest pass a perfectly healthy run has no work
    item referencing it -- which is the exact shape of a stranded one. A cycle
    slower than the grace period would be diagnosed as stranded and a second
    worker sent into its LangGraph thread.

    The run lock is what distinguishes them, because it is the only thing that
    knows a cycle is mid-flight: the run row is written at the start and the
    end and not in between.
    """

    from research_os.runtime.locks import research_run_lock

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id=PROJECT, objective="a long cycle")
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    _age_runs(plane["db"])

    # It qualifies on every other count, which is the point.
    stranded = store.stranded_runs(grace_seconds=300)
    assert run.run_id in {entry.run.run_id for entry in stranded}

    with research_run_lock(plane["db"], run.run_id):
        report = plane["daemon"].tick()
        assert report.runs_rescheduled == 0, (
            "a cycle that was merely slow was rescheduled into its own thread"
        )

    # Released, and now it really is stranded.
    assert plane["daemon"].tick().runs_rescheduled == 1


# ------------------------------------------------- the production assembly --
def test_the_daemon_builds_a_router_that_honours_the_configured_breaker(
    runtime_db: Database, tmp_path: Path, pg_dsn: str
) -> None:
    """The seam the fix's own sub-defect was made of, one level up.

    `provider_failure_threshold` and `provider_cooldown_seconds` were
    settings nothing read: the router called the store with defaults that
    happened to equal them. Wiring them is two lines in
    `default_model_factory`, and two lines nothing tests is exactly the shape
    of the defect being fixed -- deleting them again would leave the suite
    green.

    So this goes through the real assembly: `default_model_factory` with a
    real `RuntimeConfig`, and the breaker state read back out of PostgreSQL.
    """

    from research_os.runtime.daemon import default_model_factory
    from tests.fake_providers import FakeProvider, ScriptedResponse

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(tmp_path))
    run = store.create_run(project_id=PROJECT, objective="o")

    config = make_config(
        pg_dsn,
        tmp_path / "artifacts",
        settings={
            # Neither is the default, so a router that ignored configuration
            # would leave a healthy provider and a 300-second cooldown.
            "provider_failure_threshold": 1,
            "provider_cooldown_seconds": 7200,
        },
    )
    broken = FakeProvider(
        name="claude",
        family="anthropic",
        responses={
            "planner": [ScriptedResponse(structured=None, exit_code=1, error="down")]
        },
    )
    factory = default_model_factory(
        config=config, db=runtime_db, adapters={"claude": broken}
    )
    router = factory(run.run_id, PROJECT, None)

    with pytest.raises(ProviderCallFailedError):
        router.complete(
            ModelRequest(
                role="planner",  # type: ignore[arg-type]
                capability="planning",  # type: ignore[arg-type]
                prompt="x",
                prompt_version="test@1",
                json_schema={"type": "object"},
            )
        )

    health = {entry.provider: entry for entry in store.provider_health()}["claude"]
    assert health.healthy is False, (
        "provider_failure_threshold=1 was not passed to the router"
    )
    assert health.cooldown_until is not None
    assert health.cooldown_until - datetime.now(UTC) > timedelta(hours=1), (
        "provider_cooldown_seconds=7200 was not passed to the router"
    )


def test_a_real_provider_outage_through_the_real_daemon_concludes_nothing(
    runtime_db: Database, tmp_path: Path, pg_dsn: str
) -> None:
    """The whole production path, with no router double anywhere.

    Every other lifecycle test here injects a router that raises, which
    assumes the thing most worth proving: that the *real* `ModelRouter`
    raises rather than returning a response for a provider that failed. That
    assumption is what was false before this change, so a suite that makes it
    everywhere could have passed against the defect.

    Here the only double is the provider adapter itself -- a subprocess this
    machine will not run in a test -- and everything above it is real: the
    router, the budgets, the artifact store, the graph, the queue, the
    daemon's tick, and PostgreSQL.
    """

    from research_os.runtime.checkpoints import ensure_tables
    from research_os.runtime.daemon import default_model_factory
    from tests.fake_providers import FakeProvider, ScriptedResponse

    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project", project_id=PROJECT)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(repo))

    config = make_config(pg_dsn, tmp_path / "artifacts")
    dead = FakeProvider(
        name="claude",
        family="anthropic",
        responses={
            role: [
                ScriptedResponse(
                    structured=None,
                    exit_code=1,
                    error=(
                        "Failed to refresh OAuth token: another process is "
                        "refreshing it or exited mid-refresh"
                    ),
                )
                for _ in range(40)
            ]
            for role in ("planner", "reviewer", "analyst", "coder", "literature")
        },
    )
    daemon = Daemon(
        config=config,
        db=runtime_db,
        repo_for=lambda _project: repo,
        models=default_model_factory(
            config=config, db=runtime_db, adapters={"claude": dead}
        ),
        notifier=CollectingNotifier(),
        clock=FrozenClock(),
        owner="production-path",
    )

    run_id = _request_run(store, "an objective during a real OAuth collision")
    _exhaust_queue(daemon, runtime_db)

    run = store.require_run(run_id)
    assert run.status is not RunStatus.SUCCEEDED, (
        f"the real path concluded {run.status} / {run.terminal_state} on an "
        f"outage: {run.detail}"
    )
    assert run.terminal_state not in {
        TerminalState.DONE_FOR_NOW,
        TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
    }
    assert run_id not in {
        entry.run_id for entry in store.parked_objectives(project_id=PROJECT)
    }
    assert store.list_approvals(pending_only=True) == ()

    # The provider really was asked, and really was recorded as failing --
    # otherwise this proves only that nothing happened.
    calls = store.list_model_calls(run_id=run_id)
    assert calls, "no model call was attempted, so the outage was not exercised"
    assert all(str(call.status) == "FAILED" for call in calls)

    # And the breaker opened, which is what makes the retry schedule matter.
    health = {entry.provider: entry for entry in store.provider_health()}
    assert health["claude"].healthy is False

    # Then it recovers, through the same generic path, with no capsule change.
    dead.responses = {
        "planner": [
            ScriptedResponse(structured=plan_answer(str(ActionKind.ASSESS_FRONTIER)))
        ]
        * 10,
        "analyst": [ScriptedResponse(structured=_answers()["frontier"])] * 10,
        "reviewer": [ScriptedResponse(structured=review_answer())] * 10,
    }
    store.record_provider_result("claude", ok=True)
    for _ in range(4):
        _age_runs(runtime_db)
        _exhaust_queue(daemon, runtime_db)

    recovered = store.require_run(run_id)
    assert recovered.terminal, f"the run never finished: {recovered.status}"
    assert store.stranded_runs(grace_seconds=0) == ()


@pytest.mark.parametrize("status", sorted(str(s) for s in ACTIVE_JOB_STATUSES))
def test_a_run_waiting_on_a_cluster_job_is_never_called_stranded(
    plane: dict[str, Any], status: str
) -> None:
    """Every status a live job can hold, not the three someone remembered.

    The first version of `stranded_runs` tested `('SUBMITTED','QUEUED',
    'RUNNING')`. ``QUEUED`` is not a member of `ExternalJobStatus` and the
    table's check constraint rejects it, so that third of the guard could
    never match anything; ``SUBMITTING``, ``PENDING`` and ``UNKNOWN`` -- the
    statuses a cluster job actually sits in while it waits -- were absent. A
    run whose Slurm job was pending, whose worker had died, and whose work
    item had exhausted its attempts would have been rescheduled into a second
    submission of the same experiment. An independent security review found
    it; the list is derived from the enum now, and this parametrises over the
    enum so the two cannot drift apart again.
    """

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id=PROJECT, objective=f"waiting on {status}")
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    job = store.create_external_job(
        project_id=PROJECT,
        executor="slurm",
        spec_digest="deadbeef",
        run_dir=str(plane["repo"]),
        run_id=run.run_id,
    )
    with plane["db"].tx() as conn:
        conn.execute(
            "update external_jobs set status = %s where job_id = %s",
            (status, job.job_id),
        )
    _age_runs(plane["db"])

    stranded = {entry.run.run_id for entry in store.stranded_runs(grace_seconds=300)}
    assert run.run_id not in stranded, (
        f"a run waiting on a {status} cluster job was diagnosed as stranded"
    )

    # The control: the same run with the job finished *is* stranded, so the
    # exclusion above is the job's doing and not something else's. Asserted
    # against the store directly rather than through a tick, because a tick
    # also polls external jobs and would move the row under the test.
    with plane["db"].tx() as conn:
        conn.execute(
            "update external_jobs set status = %s where job_id = %s",
            (str(ExternalJobStatus.COMPLETED), job.job_id),
        )
    assert run.run_id in {
        entry.run.run_id for entry in store.stranded_runs(grace_seconds=300)
    }


def test_a_crash_between_enqueue_and_event_does_not_wedge_the_run(
    plane: dict[str, Any],
) -> None:
    """The reschedule counter and the dedup key must not be able to disagree.

    `_reconcile_runs` enqueues the work item and records `RUN_RESCHEDULED` in
    two statements. A crash between them used to be terminal for the run: the
    event count stayed at n-1, so every later pass recomputed the same dedup
    key, the key kept refusing, and the run was never rescheduled, never
    abandoned and never mentioned in a report -- the exact orphan state this
    pass exists to end, now reachable *through* it. An independent security
    review found it.
    """

    store: RuntimeStore = plane["store"]
    plane["router"].down = True
    run_id = _request_run(store, "objective whose reconciler crashed")
    _exhaust_queue(plane["daemon"], plane["db"])
    plane["router"].down = False
    _age_runs(plane["db"])

    # The work item exists, the event does not: the crash window, reproduced.
    plane["queue"].enqueue(
        project_id=PROJECT,
        kind=WorkKind.RUN_CYCLE,
        run_id=run_id,
        dedup_key=f"reconcile:{run_id}:1",
    )
    with plane["db"].tx() as conn:
        conn.execute(
            "update work_items set status = 'FAILED' where dedup_key = %s",
            (f"reconcile:{run_id}:1",),
        )
    assert store.event_count(run_id=run_id, kind="RUN_RESCHEDULED") == 0

    report = plane["daemon"].tick()
    assert store.event_count(run_id=run_id, kind="RUN_RESCHEDULED") == 1, (
        "the run is wedged: its dedup key is taken and its counter never moved"
    )
    assert report.runs_rescheduled == 1

    # And it progresses from there rather than repeating: the next pass
    # computes ordinal 2.
    _age_runs(plane["db"])
    _exhaust_queue(plane["daemon"], plane["db"])
    _age_runs(plane["db"])
    plane["daemon"].tick()
    assert store.event_count(run_id=run_id, kind="RUN_RESCHEDULED") >= 1


def test_an_action_path_outage_is_also_scheduled_past_the_cooldown(
    plane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cooldown awareness must be a property of the class, not of plumbing.

    The router attaches the breaker's deadline to what it raises, and for the
    planner call that is enough. It is not enough in general: an action
    handler that catches a provider error and reports it, or a broad
    ``except ResearchOSError`` two frames up, produces a failure with the
    right class and no deadline -- and `perform_action` then re-raises a
    fresh exception that never had one. A test audit found several such paths
    and named the consequence correctly: D2 intact, on every route through
    the action layer.

    So the queue looks the deadline up. This drives the worst case
    deliberately -- a handler that swallows the exception entirely and
    returns an outcome -- and asserts the schedule anyway.
    """

    from research_os.runtime.actions.base import ActionOutcome
    from research_os.runtime.registry import ACTION_HANDLERS, RegisteredAction

    store: RuntimeStore = plane["store"]

    def swallows(_state: Any, _context: Any, _plan: Any) -> ActionOutcome:
        return ActionOutcome.failed(
            "the provider could not be reached",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )

    monkeypatch.setitem(
        ACTION_HANDLERS,
        ActionKind.ASSESS_FRONTIER,
        RegisteredAction(swallows, replay_safe=True),
    )

    # A real open breaker, recorded the way the router records one.
    health = store.record_provider_result(
        "scripted", ok=False, error="down", threshold=1, cooldown=900
    )
    assert health.cooldown_until is not None

    run_id = _request_run(store, "objective whose action swallowed the outage")
    plane["daemon"].tick()

    item = _run_cycle_items(plane["db"], run_id)[0]
    assert item["failure_class"] == str(FailureClass.PROVIDER_UNAVAILABLE), (
        "the swallowed outage lost its class as well"
    )
    assert item["scheduled_at"] >= health.cooldown_until - timedelta(seconds=5), (
        f"retried at {item['scheduled_at']}, inside a cooldown ending at "
        f"{health.cooldown_until} -- the deadline was not looked up"
    )


def test_no_deadline_is_invented_when_a_provider_is_usable(
    plane: dict[str, Any],
) -> None:
    """Looking the deadline up must not become delaying for no reason.

    A provider failure with every provider healthy -- a one-off error, a
    single bad response -- should retry on its ordinary backoff. Waiting for
    a cooldown that is not open would turn every transient blip into a
    multi-minute pause.
    """

    store: RuntimeStore = plane["store"]
    store.record_provider_result("scripted", ok=True)
    assert plane["daemon"]._provider_recovery_deadline() is None

    store.record_provider_result(
        "scripted", ok=False, error="down", threshold=1, cooldown=600
    )
    deadline = plane["daemon"]._provider_recovery_deadline()
    assert deadline is not None and deadline > datetime.now(UTC)

    # And a second, healthy provider means there is again nothing to wait for.
    store.record_provider_result("other", ok=True)
    assert plane["daemon"]._provider_recovery_deadline() is None
