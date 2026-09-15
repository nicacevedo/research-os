"""Runs, events, approvals, jobs and provider health.

The properties asserted here are the ones other modules depend on: a terminal
run stays terminal, an event with a dedup key is recorded once, an approval
cannot be decided twice or applied twice, and a submission is recorded before
the scheduler is told.
"""

from __future__ import annotations

import pytest

from research_os.runtime.db import Database
from research_os.runtime.findings import FindingKind, RuntimeFinding
from research_os.runtime.models import (
    ApprovalStatus,
    ExternalJobStatus,
    ModelCallStatus,
    RunStatus,
    TerminalState,
)
from research_os.runtime.store import RuntimeStateError, RuntimeStore


@pytest.fixture
def store(runtime_db: Database) -> RuntimeStore:
    return RuntimeStore(runtime_db)


def test_a_run_gets_a_thread_id_derived_from_its_own_id(
    store: RuntimeStore, runtime_project: str
) -> None:
    """One thread per bounded cycle, so checkpoint retention has a unit to work on."""

    run = store.create_run(project_id=runtime_project, objective="find out whether X")
    assert run.thread_id == f"cycle:{run.run_id}"
    assert run.status is RunStatus.CREATED
    assert run.cycle_index == 0


def test_a_terminal_run_cannot_be_revived(
    store: RuntimeStore, runtime_project: str
) -> None:
    """A resumed worker acting on stale state is the only thing that tries this."""

    run = store.create_run(project_id=runtime_project, objective="o")
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    store.set_run_status(
        run.run_id, RunStatus.SUCCEEDED, terminal_state=TerminalState.DONE_FOR_NOW
    )
    with pytest.raises(RuntimeStateError, match="already SUCCEEDED"):
        store.set_run_status(run.run_id, RunStatus.RUNNING)


def test_finishing_a_run_stamps_the_time_once(
    store: RuntimeStore, runtime_project: str
) -> None:
    run = store.create_run(project_id=runtime_project, objective="o")
    started = store.set_run_status(run.run_id, RunStatus.RUNNING)
    assert started.started_at is not None
    finished = store.set_run_status(
        run.run_id, RunStatus.SUCCEEDED, terminal_state=TerminalState.DONE_FOR_NOW
    )
    assert finished.finished_at is not None
    assert finished.terminal_state is TerminalState.DONE_FOR_NOW
    assert finished.terminal


def test_cycle_lineage_is_countable(store: RuntimeStore, runtime_project: str) -> None:
    """The continuation policy needs a hard bound, so depth must be measurable."""

    first = store.create_run(project_id=runtime_project, objective="o")
    second = store.create_run(
        project_id=runtime_project,
        objective="o",
        parent_run_id=first.run_id,
        cycle_index=1,
    )
    third = store.create_run(
        project_id=runtime_project,
        objective="o",
        parent_run_id=second.run_id,
        cycle_index=2,
    )
    assert store.lineage_depth(first.run_id) == 0
    assert store.lineage_depth(third.run_id) == 2


def test_an_event_with_a_dedup_key_is_recorded_once(
    store: RuntimeStore, runtime_project: str
) -> None:
    first, created_first = store.record_event(
        kind="WORK_COMPLETED", project_id=runtime_project, dedup_key="w1:done"
    )
    second, created_second = store.record_event(
        kind="WORK_COMPLETED", project_id=runtime_project, dedup_key="w1:done"
    )
    assert created_first is True
    assert created_second is False
    assert second.event_id == first.event_id


def test_claiming_events_hands_each_one_to_exactly_one_consumer(
    store: RuntimeStore, runtime_project: str
) -> None:
    for index in range(3):
        store.record_event(
            kind="TICK", project_id=runtime_project, dedup_key=f"t{index}"
        )
    first = store.claim_events(limit=2)
    second = store.claim_events(limit=2)
    assert len(first) == 2
    assert len(second) == 1
    assert store.claim_events() == ()


def test_one_interrupt_key_yields_one_approval(
    store: RuntimeStore, runtime_project: str
) -> None:
    """A replayed interrupt node must not open a second identical question."""

    run = store.create_run(project_id=runtime_project, objective="o")
    first, created_first = store.request_approval(
        run_id=run.run_id,
        project_id=runtime_project,
        kind="post_hoc_endpoint_change",
        question="Change the primary endpoint after seeing results?",
        packet={"why": "the prespecified test was underpowered"},
        interrupt_key=f"{run.run_id}:endpoint",
    )
    second, created_second = store.request_approval(
        run_id=run.run_id,
        project_id=runtime_project,
        kind="post_hoc_endpoint_change",
        question="Change the primary endpoint after seeing results?",
        packet={"why": "the prespecified test was underpowered"},
        interrupt_key=f"{run.run_id}:endpoint",
    )
    assert created_first is True
    assert created_second is False
    assert second.approval_id == first.approval_id


def test_an_approval_cannot_be_decided_twice(
    store: RuntimeStore, runtime_project: str
) -> None:
    run = store.create_run(project_id=runtime_project, objective="o")
    approval, _ = store.request_approval(
        run_id=run.run_id,
        project_id=runtime_project,
        kind="k",
        question="q",
        packet={},
        interrupt_key="once",
    )
    decided = store.record_decision(
        approval.approval_id,
        granted=True,
        decision={"choice": "a"},
        decided_by="researcher",
    )
    assert decided.status is ApprovalStatus.GRANTED
    with pytest.raises(RuntimeStateError, match="already decided"):
        store.record_decision(
            approval.approval_id, granted=False, decision={}, decided_by="researcher"
        )


def test_a_decision_can_be_applied_by_exactly_one_caller(
    store: RuntimeStore, runtime_project: str
) -> None:
    """A replayed apply node must not act on one approval twice."""

    run = store.create_run(project_id=runtime_project, objective="o")
    approval, _ = store.request_approval(
        run_id=run.run_id,
        project_id=runtime_project,
        kind="k",
        question="q",
        packet={},
        interrupt_key="apply-once",
    )
    store.record_decision(
        approval.approval_id,
        granted=True,
        decision={"choice": "a"},
        decided_by="researcher",
    )
    assert store.mark_approval_applied(approval.approval_id) is True
    assert store.mark_approval_applied(approval.approval_id) is False
    # The status still says what was decided; `applied_at` says it was acted on.
    acted = store.get_approval(approval.approval_id)
    assert acted is not None
    assert acted.status is ApprovalStatus.GRANTED
    assert acted.applied_at is not None


def test_a_submission_is_recorded_before_the_scheduler_is_told(
    store: RuntimeStore, runtime_project: str
) -> None:
    """A crash between recording and submitting must leave something to reconcile."""

    job = store.create_external_job(
        project_id=runtime_project,
        executor="slurm",
        spec_digest="abc123",
        run_dir="/tmp/xrun",
    )
    assert job.status is ExternalJobStatus.SUBMITTING
    assert job.scheduler_job_id is None
    # `active_external_jobs` is a claim: it stamps `last_polled_at`, so compare
    # identities rather than whole records.
    assert job.job_id in {claimed.job_id for claimed in store.active_external_jobs()}

    submitted = store.update_external_job(
        job.job_id, status=ExternalJobStatus.SUBMITTED, scheduler_job_id="4711"
    )
    assert submitted.scheduler_job_id == "4711"
    assert submitted.finished_at is None

    done = store.update_external_job(
        job.job_id, status=ExternalJobStatus.COMPLETED, exit_code=0, polled=True
    )
    assert done.finished_at is not None
    assert done.job_id not in {
        claimed.job_id for claimed in store.active_external_jobs()
    }


def test_model_call_provenance_records_the_independence_group(
    store: RuntimeStore, runtime_project: str
) -> None:
    """ "Independently reviewed" must be auditable, not asserted."""

    run = store.create_run(project_id=runtime_project, objective="o")
    call = store.record_model_call(
        run_id=run.run_id,
        provider="claude",
        model="opus",
        role="scientific_reviewer",
        status=ModelCallStatus.OK,
        criticality="critical",
        independence_group="review:HYP-0001",
        prompt_version="scientific_reviewer@1",
        input_digest="sha256:aaa",
        tokens_in=100,
        tokens_out=20,
        cost_usd=0.25,
        latency_ms=1500,
    )
    assert call.independence_group == "review:HYP-0001"
    assert call.prompt_version == "scientific_reviewer@1"
    assert float(call.cost_usd) == 0.25
    assert store.list_model_calls(run_id=run.run_id) == (call,)


def test_a_provider_is_marked_unhealthy_after_repeated_failures(
    store: RuntimeStore,
) -> None:
    for _ in range(2):
        health = store.record_provider_result(
            "flaky", ok=False, error="500", threshold=3
        )
        assert health.healthy is True
    health = store.record_provider_result("flaky", ok=False, error="500", threshold=3)
    assert health.healthy is False
    assert health.consecutive_failures == 3
    assert health.cooldown_until is not None
    assert store.usable_providers(("flaky", "other")) == ("other",)


def test_one_success_makes_a_provider_usable_again(store: RuntimeStore) -> None:
    """A provider that works is working; the counter resets rather than decays."""

    for _ in range(3):
        store.record_provider_result("flaky", ok=False, error="500", threshold=3)
    recovered = store.record_provider_result("flaky", ok=True)
    assert recovered.healthy is True
    assert recovered.consecutive_failures == 0
    assert recovered.cooldown_until is None
    assert store.usable_providers(("flaky",)) == ("flaky",)


def test_a_schedule_that_fires_advances_in_the_same_transaction(
    store: RuntimeStore, runtime_project: str
) -> None:
    """Otherwise two daemon loops ticking together fire it twice."""

    store.create_schedule(
        kind="literature_refresh", interval_seconds=3600, project_id=runtime_project
    )
    first = store.claim_due_schedules()
    assert len(first) == 1
    assert store.claim_due_schedules() == ()
    assert first[0].next_run_at > first[0].created_at


# ------------------------------------------------------- runtime findings ----
def _finding(project_id: str, **overrides: object) -> RuntimeFinding:
    payload: dict[str, object] = {
        "project_id": project_id,
        "kind": FindingKind.FRONTIER,
        "summary": "Two hypotheses have no experiment.",
    }
    payload.update(overrides)
    return RuntimeFinding(**payload)  # type: ignore[arg-type]


def test_the_same_observation_recorded_twice_is_one_finding(
    store: RuntimeStore, runtime_project: str
) -> None:
    """The property that makes a finding citable.

    The runtime recomputes an unchanged frontier on every cycle. Without
    content dedupe, one fact would acquire a new citable identifier per cycle,
    and a proposal grounded in "the finding from cycle 3" would be grounded in
    something a reader could not tell apart from six others saying the same.
    """

    first, created_first = store.record_finding(_finding(runtime_project))
    second, created_second = store.record_finding(_finding(runtime_project))
    assert created_first is True
    assert created_second is False
    assert first.finding_id == second.finding_id
    assert len(store.list_findings(project_id=runtime_project)) == 1


def test_a_different_observation_is_a_different_finding(
    store: RuntimeStore, runtime_project: str
) -> None:
    first, _ = store.record_finding(_finding(runtime_project))
    second, created = store.record_finding(
        _finding(runtime_project, summary="One claim has contrary evidence.")
    )
    assert created is True
    assert first.finding_id != second.finding_id


def test_the_cycle_that_observed_it_does_not_change_its_identity(
    store: RuntimeStore, runtime_project: str
) -> None:
    """Deliberate: see ``RuntimeFinding.digest``.

    A successor cycle observing the same thing must not mint a second id. The
    row still records which cycle saw it first.
    """

    first, _ = store.record_finding(_finding(runtime_project, source_cycle=0))
    second, created = store.record_finding(_finding(runtime_project, source_cycle=7))
    assert created is False
    assert second.finding_id == first.finding_id


def test_the_same_observation_in_two_projects_is_two_findings(
    store: RuntimeStore, runtime_project: str
) -> None:
    """Invariant 4: scientific state is project-isolated, findings included."""

    store.upsert_project(project_id="beta", repo_path="/tmp/beta")
    first, _ = store.record_finding(_finding(runtime_project))
    second, created = store.record_finding(_finding("beta"))
    assert created is True
    assert first.finding_id != second.finding_id


def test_a_finding_carries_its_references_back(
    store: RuntimeStore, runtime_project: str
) -> None:
    stored, _ = store.record_finding(
        _finding(
            runtime_project,
            artifact_ids=("a" * 64,),
            capsule_refs=("HYP-0001", "Q-0001"),
            literature_keys=("openalex:W1",),
        )
    )
    loaded = store.get_finding(stored.finding_id)
    assert loaded is not None
    assert loaded.artifact_ids == ("a" * 64,)
    assert loaded.capsule_refs == ("HYP-0001", "Q-0001")
    assert loaded.literature_keys == ("openalex:W1",)


def test_resolving_a_finding_this_project_lacks_fails_closed(
    store: RuntimeStore, runtime_project: str
) -> None:
    """Fail-closed, because an allowlist assembled by dropping what it could not
    find is an allowlist that permits a proposal to rest on nothing."""

    stored, _ = store.record_finding(_finding(runtime_project))
    assert store.resolve_findings((stored.finding_id,), project_id=runtime_project)
    with pytest.raises(RuntimeStateError, match="no runtime finding"):
        store.resolve_findings(("FIND-nope",), project_id=runtime_project)


def test_a_finding_from_another_project_does_not_resolve(
    store: RuntimeStore, runtime_project: str
) -> None:
    store.upsert_project(project_id="beta", repo_path="/tmp/beta")
    foreign, _ = store.record_finding(_finding("beta"))
    with pytest.raises(RuntimeStateError, match="no runtime finding"):
        store.resolve_findings((foreign.finding_id,), project_id=runtime_project)


def test_one_reservation_key_always_yields_one_proposal_id(
    store: RuntimeStore, runtime_project: str
) -> None:
    """What makes a proposal replay-safe: the identity is reserved, not returned."""

    first_id, first_status, created_first = store.reserve_proposal(
        reservation_key="propose_capsule_change:RRUN-1:0",
        proposal_id="PROP-19700101T000000Z-aaaaaaaa",
        project_id=runtime_project,
    )
    second_id, second_status, created_second = store.reserve_proposal(
        reservation_key="propose_capsule_change:RRUN-1:0",
        # A different id offered; the reservation is authoritative.
        proposal_id="PROP-19700101T000000Z-bbbbbbbb",
        project_id=runtime_project,
    )
    assert created_first is True and created_second is False
    assert first_id == second_id == "PROP-19700101T000000Z-aaaaaaaa"
    assert first_status == second_status == "RESERVED"


def test_a_settled_reservation_cannot_be_reopened(
    store: RuntimeStore, runtime_project: str
) -> None:
    """A FAILED record of a proposal that did reach the store would be a lie."""

    store.reserve_proposal(
        reservation_key="k",
        proposal_id="PROP-19700101T000000Z-cccccccc",
        project_id=runtime_project,
    )
    store.settle_proposal_reservation("k", status="CREATED")
    store.settle_proposal_reservation("k", status="FAILED", detail="too late")
    _id, status, _created = store.reserve_proposal(
        reservation_key="k",
        proposal_id="PROP-19700101T000000Z-cccccccc",
        project_id=runtime_project,
    )
    assert status == "CREATED"
