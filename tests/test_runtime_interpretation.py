"""Which experiment an interpretation is of, and how that survives a crash.

These tests exist because the previous implementation answered "which
experiment" with ``order by finished_at desc limit 1``. That is association by
temporal coincidence, and `DESIGN_INVARIANTS.md` invariant 11 -- every
experiment traceable to code, configuration, data and version -- is not
satisfied by a scientific reading attached to whichever job the scheduler
happened to reap last.

Four properties, in descending order of how much damage their absence does:

1. a named job is interpreted, and never a different one;
2. an experiment is interpreted **once** per interpreter version, however many
   times the handler runs, crashes or is resumed;
3. selection is a stable total order over eligible jobs, not a race;
4. an execution that did not run correctly yields no scientific conclusion, and
   an execution that ran correctly and refuted its hypothesis is a success.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.actions.experiments import (
    INTERPRETER_VERSION,
    interpret_results,
)
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import ExternalJobStatus, InterpretationStatus
from research_os.runtime.store import RuntimeStateError, RuntimeStore
from tests.runtime_graph_helpers import make_capsule, make_context

SCRIPTS = Path(__file__).parent / "runtime_scripts"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    repo = make_capsule(tmp_path / "project")
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="alpha-project", repo_path=str(repo))
    run = store.create_run(project_id="alpha-project", objective="whether X holds")
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "artifacts_root": tmp_path / "artifacts",
        "store": store,
        "run": run,
        "state": {
            "run_id": run.run_id,
            "project_id": "alpha-project",
            "repo_path": str(repo),
            "objective": "whether X holds",
            "autonomy": "high",
            "cycle_index": 0,
            "artifacts": [],
            "notes": [],
            "frontier": {"empty": False},
        },
    }


def _context(env: dict[str, Any]) -> Any:
    return make_context(
        db=env["db"],
        repo=env["repo"],
        artifacts_root=env["artifacts_root"],
        dsn=env["dsn"],
        models=None,
        permitted=(),
    )


def _preregistered_job(
    env: dict[str, Any],
    *,
    digest: str,
    endpoint: str,
    status: ExternalJobStatus = ExternalJobStatus.COMPLETED,
    exit_code: int = 0,
    project_id: str = "alpha-project",
    run_id: str | None = None,
    prereg: bool = True,
) -> Any:
    """A terminal external job with a matching stored preregistration.

    Built directly rather than by driving ``design_experiment``, because the
    property under test is which job gets read, not how a design is produced,
    and a model call per job would make the multi-job cases untestable.
    """

    context = _context(env)
    run = run_id or env["run"].run_id
    if prereg:
        record = {
            "hypothesis": "HYP-0001",
            "testable": True,
            "spec_digest": digest,
            "primary_endpoint": endpoint,
            "secondary_endpoints": [],
            "success_criteria": f"{endpoint} > 0.5",
            "failure_criteria": f"{endpoint} <= 0.5",
            "dataset_identity": "fixture",
        }
        ref = context.artifacts.put_text(
            json.dumps(record, indent=2, sort_keys=True),
            media_type="application/json",
            role="preregistration",
            producer="fixture",
        )
        context.artifacts.link(ref, role="preregistration", run_id=run)
    job = env["store"].create_external_job(
        project_id=project_id,
        run_id=run,
        executor="local",
        spec_digest=digest,
        run_dir=str(env["repo"]),
    )
    env["store"].update_external_job(job.job_id, status=status, exit_code=exit_code)
    return env["store"].get_external_job(job.job_id)


# ------------------------------------------------------------- identity ----
def test_an_explicit_job_id_is_interpreted_and_not_something_else(
    env: dict[str, Any],
) -> None:
    """The planner names a job; that job is read. Nothing chooses for it."""

    _preregistered_job(env, digest="a" * 64, endpoint="endpoint A")
    wanted = _preregistered_job(env, digest="b" * 64, endpoint="endpoint B")

    outcome = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": wanted.job_id}}
    )
    assert outcome.ok, outcome.detail
    assert outcome.data["job_id"] == wanted.job_id
    assert outcome.data["spec_digest"] == "b" * 64
    assert outcome.data["primary_endpoint"] == "endpoint B"


def test_several_jobs_finished_together_are_interpreted_oldest_first(
    env: dict[str, Any],
) -> None:
    """The case the old implementation got wrong.

    Three jobs, all terminal, all with the same ``finished_at`` to the extent
    the fixture can arrange it. ``order by finished_at desc limit 1`` gave the
    interpretation to whichever row PostgreSQL returned first; oldest-eligible
    with a ``job_id`` tiebreak is a total order, so three successive calls read
    three different experiments and each exactly once.
    """

    first = _preregistered_job(env, digest="a" * 64, endpoint="endpoint A")
    second = _preregistered_job(env, digest="b" * 64, endpoint="endpoint B")
    third = _preregistered_job(env, digest="c" * 64, endpoint="endpoint C")
    # One timestamp for all three: the ambiguity is the point.
    with env["db"].tx() as conn:
        conn.execute("update external_jobs set finished_at = now()")

    read: list[str] = []
    for _ in range(3):
        outcome = interpret_results(env["state"], _context(env), {})
        assert outcome.ok, outcome.detail
        assert outcome.data["interpreted"] is True
        read.append(str(outcome.data["job_id"]))

    assert sorted(read) == sorted([first.job_id, second.job_id, third.job_id])
    assert read == sorted(read), "the order must be stable, not a race"
    assert len(set(read)) == 3, "one job was interpreted twice"

    fourth = interpret_results(env["state"], _context(env), {})
    assert fourth.ok
    assert fourth.data["interpreted"] is False


def test_out_of_order_completion_does_not_reorder_interpretation(
    env: dict[str, Any],
) -> None:
    """A job submitted first but finishing second is still eligible.

    Eligibility is "terminal and not yet completely interpreted", and the order
    is by completion. What must not happen is a job becoming *permanently*
    ineligible because a later-submitted one finished first, which is what a
    "latest only" selector does to a backlog.
    """

    early = _preregistered_job(
        env, digest="a" * 64, endpoint="A", status=ExternalJobStatus.COMPLETED
    )
    late = _preregistered_job(
        env, digest="b" * 64, endpoint="B", status=ExternalJobStatus.COMPLETED
    )
    with env["db"].tx() as conn:
        conn.execute(
            "update external_jobs set finished_at = now() + interval '1 hour' "
            "where job_id = %s",
            (early.job_id,),
        )

    first = interpret_results(env["state"], _context(env), {})
    second = interpret_results(env["state"], _context(env), {})
    assert first.data["job_id"] == late.job_id
    assert second.data["job_id"] == early.job_id


def test_an_already_interpreted_job_is_not_interpreted_again(
    env: dict[str, Any],
) -> None:
    job = _preregistered_job(env, digest="a" * 64, endpoint="A")
    plan = {"parameters": {"job_id": job.job_id}}

    first = interpret_results(env["state"], _context(env), plan)
    second = interpret_results(env["state"], _context(env), plan)

    assert first.ok and second.ok
    assert first.data["reused"] is False
    assert second.data["reused"] is True
    assert second.data["interpretation_id"] == first.data["interpretation_id"]
    assert second.data["artifact_id"] == first.data["artifact_id"]
    assert len(env["store"].list_interpretations(project_id="alpha-project")) == 1


def test_an_interpretation_binds_the_job_and_its_exact_spec_digest(
    env: dict[str, Any],
) -> None:
    """Identity is (job, spec digest, reader), recorded, not inferred later."""

    job = _preregistered_job(env, digest="a" * 64, endpoint="A")
    interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )

    stored = env["store"].get_interpretation(
        job_id=job.job_id, interpreter_version=INTERPRETER_VERSION
    )
    assert stored is not None
    assert stored.spec_digest == "a" * 64
    assert stored.job_id == job.job_id
    assert stored.run_id == env["run"].run_id
    assert stored.status is InterpretationStatus.COMPLETED
    assert stored.completed_at is not None

    body = json.loads(_context(env).artifacts.get_text(str(stored.artifact_id)))
    assert body["spec_digest"] == "a" * 64
    assert body["job_id"] == job.job_id
    assert body["interpreter_version"] == INTERPRETER_VERSION


def test_a_job_from_another_project_is_refused_even_when_named(
    env: dict[str, Any],
) -> None:
    """An experiment is never interpreted across a project boundary.

    Invariant 4 is project isolation. A planner that names a job id belonging
    to someone else's project must be refused rather than obliged, because the
    preregistration lookup is project-scoped and would silently find nothing --
    or worse, find a digest collision.
    """

    env["store"].upsert_project(project_id="beta-project", repo_path="/tmp/beta")
    other_run = env["store"].create_run(project_id="beta-project", objective="other")
    foreign = _preregistered_job(
        env,
        digest="f" * 64,
        endpoint="F",
        project_id="beta-project",
        run_id=other_run.run_id,
    )

    outcome = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": foreign.job_id}}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.POLICY_REFUSED
    assert "never interpreted across a project boundary" in outcome.detail


def test_a_named_job_that_does_not_exist_is_an_error_not_a_substitution(
    env: dict[str, Any],
) -> None:
    _preregistered_job(env, digest="a" * 64, endpoint="A")
    outcome = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": "XJOB-nope"}}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.ARTIFACT_MISSING
    assert env["store"].list_interpretations(project_id="alpha-project") == ()


# ------------------------------------------------- what cannot be concluded --
def test_a_failed_execution_is_read_but_concludes_nothing_scientific(
    env: dict[str, Any],
) -> None:
    job = _preregistered_job(
        env,
        digest="a" * 64,
        endpoint="A",
        status=ExternalJobStatus.FAILED,
        exit_code=1,
    )
    outcome = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )
    assert outcome.ok, "reading a failed execution correctly is this action working"
    assert outcome.data["ran_correctly"] is False
    assert "no scientific conclusion follows" in outcome.detail


def test_a_timed_out_execution_concludes_nothing_scientific(
    env: dict[str, Any],
) -> None:
    job = _preregistered_job(
        env,
        digest="a" * 64,
        endpoint="A",
        status=ExternalJobStatus.TIMED_OUT,
        exit_code=124,
    )
    outcome = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )
    assert outcome.ok
    assert outcome.data["ran_correctly"] is False


def test_a_still_running_job_has_nothing_to_interpret_yet(
    env: dict[str, Any],
) -> None:
    job = env["store"].create_external_job(
        project_id="alpha-project",
        run_id=env["run"].run_id,
        executor="local",
        spec_digest="a" * 64,
        run_dir=str(env["repo"]),
    )
    env["store"].update_external_job(job.job_id, status=ExternalJobStatus.RUNNING)
    outcome = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.SCHEDULER_UNAVAILABLE
    assert env["store"].list_interpretations(project_id="alpha-project") == ()


def test_a_missing_preregistration_refuses_and_leaves_the_job_owed(
    env: dict[str, Any],
) -> None:
    """The refusal must not mark the experiment read.

    Without the criteria there is nothing to compare against. Completing the
    claim anyway would hide the missing preregistration permanently -- the job
    would never be eligible again -- so the claim stays ``IN_PROGRESS`` and the
    experiment is still owed a reading once the preregistration is there.
    """

    job = _preregistered_job(env, digest="a" * 64, endpoint="A", prereg=False)
    outcome = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.ARTIFACT_MISSING
    assert outcome.data["interpreted"] is False

    stored = env["store"].get_interpretation(
        job_id=job.job_id, interpreter_version=INTERPRETER_VERSION
    )
    assert stored is not None
    assert stored.status is InterpretationStatus.IN_PROGRESS
    assert stored.artifact_id is None
    # Still eligible: the refusal named a fixable problem.
    assert (
        env["store"]
        .eligible_job_for_interpretation(
            project_id="alpha-project", interpreter_version=INTERPRETER_VERSION
        )
        .job_id
        == job.job_id
    )


# ------------------------------------------------------- crash and recovery --
def test_a_duplicate_dispatch_produces_one_interpretation(
    env: dict[str, Any],
) -> None:
    """Two workers, one experiment, one reading.

    Not two processes here -- the unique constraint is what resolves it and a
    second connection is enough to exercise it -- but two independent claims
    against the same ``(job, interpreter version)``.
    """

    job = _preregistered_job(env, digest="a" * 64, endpoint="A")
    first, created_first = env["store"].claim_interpretation(
        job_id=job.job_id,
        project_id="alpha-project",
        spec_digest=job.spec_digest,
        interpreter_version=INTERPRETER_VERSION,
    )
    second, created_second = env["store"].claim_interpretation(
        job_id=job.job_id,
        project_id="alpha-project",
        spec_digest=job.spec_digest,
        interpreter_version=INTERPRETER_VERSION,
    )
    assert created_first is True
    assert created_second is False
    assert first.interpretation_id == second.interpretation_id


def test_a_crash_after_the_artifact_reconnects_the_same_one(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """The window the whole relation exists for.

    A real process writes the interpretation artifact and is killed with
    ``os._exit`` before the claim is completed -- so no ``finally`` runs, which
    is the failure mode a simulated exception cannot reproduce. The retry must
    attach *that* artifact to *that* claim, not produce a second scientific
    interpretation of one experiment.
    """

    job = _preregistered_job(env, digest="a" * 64, endpoint="A")
    report = tmp_path / "written-artifact-id"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "crash_after_interpretation.py"),
            env["dsn"],
            str(env["artifacts_root"]),
            str(env["repo"]),
            env["run"].run_id,
            job.job_id,
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert result.returncode == 17, result.stderr

    interrupted = env["store"].get_interpretation(
        job_id=job.job_id, interpreter_version=INTERPRETER_VERSION
    )
    assert interrupted is not None
    assert interrupted.status is InterpretationStatus.IN_PROGRESS, (
        "the child must have died before completing the claim, or this test "
        "proves nothing"
    )
    written = report.read_text(encoding="utf-8").strip()

    retry = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )
    assert retry.ok, retry.detail
    assert retry.data["interpretation_id"] == interrupted.interpretation_id
    assert retry.data["artifact_id"] == written, (
        "the retry produced a different artifact; the interpretation was "
        "performed twice"
    )
    assert len(env["store"].list_interpretations(project_id="alpha-project")) == 1


def test_a_restart_resumes_the_abandoned_claim_rather_than_starting_a_second(
    env: dict[str, Any],
) -> None:
    """What a daemon restart does to an interrupted reading.

    ``abandon_stale_interpretations`` marks it ``ABANDONED``: the outcome is
    unknown, exactly like a stale invocation. It is deliberately not deleted,
    because the row is the only record that a reading of this experiment was
    begun, and the next attempt needs it to reconnect the artifact.
    """

    job = _preregistered_job(env, digest="a" * 64, endpoint="A")
    claim, _ = env["store"].claim_interpretation(
        job_id=job.job_id,
        project_id="alpha-project",
        spec_digest=job.spec_digest,
        interpreter_version=INTERPRETER_VERSION,
    )
    with env["db"].tx() as conn:
        conn.execute(
            "update experiment_interpretations set created_at = now() - interval "
            "'1 day' where interpretation_id = %s",
            (claim.interpretation_id,),
        )

    abandoned = env["store"].abandon_stale_interpretations(older_than_seconds=60)
    assert [row.interpretation_id for row in abandoned] == [claim.interpretation_id]

    # Still eligible, and the retry adopts the same logical interpretation.
    retry = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )
    assert retry.ok, retry.detail
    assert retry.data["interpretation_id"] == claim.interpretation_id
    assert len(env["store"].list_interpretations(project_id="alpha-project")) == 1


def test_a_completed_interpretation_cannot_be_overwritten(
    env: dict[str, Any],
) -> None:
    """A slow worker must not replace the artifact of a finished reading."""

    job = _preregistered_job(env, digest="a" * 64, endpoint="A")
    claim, _ = env["store"].claim_interpretation(
        job_id=job.job_id,
        project_id="alpha-project",
        spec_digest=job.spec_digest,
        interpreter_version=INTERPRETER_VERSION,
    )
    done = env["store"].complete_interpretation(
        claim.interpretation_id, artifact_id=None, detail="first"
    )
    again = env["store"].complete_interpretation(
        claim.interpretation_id, artifact_id=None, detail="second"
    )
    assert done.completed_at == again.completed_at
    assert again.detail == "first"


def test_a_new_interpreter_version_reads_the_experiment_again(
    env: dict[str, Any],
) -> None:
    """Changing how a result is read is a reason to read it again.

    And the second reading is a *different* interpretation, not a correction of
    the first: both rows are kept, so a person can see that two readers of the
    same experiment disagreed. That is why ``interpreter_version`` is inside the
    identity key rather than outside it.
    """

    job = _preregistered_job(env, digest="a" * 64, endpoint="A")
    interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )

    assert (
        env["store"].eligible_job_for_interpretation(
            project_id="alpha-project", interpreter_version=INTERPRETER_VERSION
        )
        is None
    )
    later = env["store"].eligible_job_for_interpretation(
        project_id="alpha-project", interpreter_version="interpret_results@99"
    )
    assert later is not None and later.job_id == job.job_id

    second, created = env["store"].claim_interpretation(
        job_id=job.job_id,
        project_id="alpha-project",
        spec_digest=job.spec_digest,
        interpreter_version="interpret_results@99",
    )
    assert created is True
    first = env["store"].get_interpretation(
        job_id=job.job_id, interpreter_version=INTERPRETER_VERSION
    )
    assert first is not None
    assert second.interpretation_id != first.interpretation_id
    assert len(env["store"].list_interpretations(project_id="alpha-project")) == 2


# ------------------------------- what the adversarial audit executed --------
def test_a_replay_cannot_be_handed_criteria_designed_after_the_result(
    env: dict[str, Any],
) -> None:
    """The most serious defect an adversarial review found, as a test.

    ``spec_digest`` covers the *execution* and says nothing about the criteria,
    which live beside it in the preregistration record. So two design passes
    over one declared command store two preregistrations with the same digest
    and different primary endpoints — and a lookup by digest has to choose.

    It chose the newest. Which permits exactly this: read an experiment, crash
    before recording it, design again with a different endpoint, and the retry
    compares the same result against the new criteria while reporting
    ``criteria_were_fixed_before_results: True``. A post-hoc primary-endpoint
    change with no recorded Decision — the gate ``docs/RUNTIME.md`` calls the
    most important in the system, defeated by a query.

    The criteria are now resolved once, when the interpretation is claimed, and
    the artifact that carried them is named on the row.
    """

    job = _preregistered_job(env, digest="a" * 64, endpoint="the mean of column A")
    context = _context(env)

    # A reading begins and is interrupted before it completes.
    claim, created = env["store"].claim_interpretation(
        job_id=job.job_id,
        project_id="alpha-project",
        spec_digest=job.spec_digest,
        interpreter_version=INTERPRETER_VERSION,
        run_id=env["run"].run_id,
        preregistration_artifact_id=None,
    )
    assert created is True
    # Bind it the way the handler does.
    first = interpret_results(
        env["state"], context, {"parameters": {"job_id": job.job_id}}
    )
    assert first.ok, first.detail
    assert first.data["primary_endpoint"] == "the mean of column A"

    # Now a *second* preregistration for the same execution, with a different
    # endpoint, designed after the result is known.
    _preregistered_job(
        env,
        digest="a" * 64,
        endpoint="a different endpoint chosen after the result",
        prereg=True,
    )

    again = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )
    assert again.ok, again.detail
    assert again.data["reused"] is True, "the completed reading was redone"
    assert again.data["interpretation_id"] == claim.interpretation_id
    # And the recorded interpretation still names the criteria it was compared
    # against, not the ones designed since.
    stored = env["store"].get_interpretation(
        job_id=job.job_id, interpreter_version=INTERPRETER_VERSION
    )
    assert stored is not None
    body = json.loads(_context(env).artifacts.get_text(str(stored.artifact_id)))
    assert body["criteria"]["primary_endpoint"] == "the mean of column A"


def test_two_preregistrations_that_disagree_are_refused_rather_than_chosen(
    env: dict[str, Any],
) -> None:
    """ "Which of these two did we commit to" is not a question to answer by picking.

    The same digest with different criteria is an ambiguity a person has to
    resolve. Choosing either one silently is how a result gets compared against
    a commitment nobody made.
    """

    job = _preregistered_job(env, digest="a" * 64, endpoint="endpoint one")
    _preregistered_job(env, digest="a" * 64, endpoint="endpoint two")

    outcome = interpret_results(
        env["state"], _context(env), {"parameters": {"job_id": job.job_id}}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.ARTIFACT_MISSING
    assert "no preregistration found" in outcome.detail
    # Still owed a reading: the ambiguity is fixable and marking it read would
    # hide it permanently.
    stored = env["store"].get_interpretation(
        job_id=job.job_id, interpreter_version=INTERPRETER_VERSION
    )
    assert stored is not None
    assert stored.status is InterpretationStatus.IN_PROGRESS


def test_a_worker_whose_claim_was_reaped_for_age_still_records_its_reading(
    env: dict[str, Any],
) -> None:
    """The loss an adversarial review executed, and it was permanent.

    The recovery pass has no way to tell a slow worker from a dead one, so it
    abandons a claim on age alone. ``complete_interpretation`` was guarded on
    ``IN_PROGRESS``, so the worker's update matched nothing and the artifact it
    had just written was attached to nothing. The job stayed eligible, and
    every later cycle re-read the same experiment, re-wrote the same artifact,
    reported success, and never advanced.
    """

    job = _preregistered_job(env, digest="a" * 64, endpoint="A")
    claim, _created = env["store"].claim_interpretation(
        job_id=job.job_id,
        project_id="alpha-project",
        spec_digest=job.spec_digest,
        interpreter_version=INTERPRETER_VERSION,
        run_id=env["run"].run_id,
    )
    # The recovery pass reaps it while the worker is still going.
    with env["db"].tx() as conn:
        conn.execute(
            "update experiment_interpretations set created_at = now() - interval "
            "'1 day' where interpretation_id = %s",
            (claim.interpretation_id,),
        )
    env["store"].abandon_stale_interpretations(older_than_seconds=60)

    written = _context(env).artifacts.put_text(
        "the reading this worker produced", media_type="application/json"
    )
    completed = env["store"].complete_interpretation(
        claim.interpretation_id,
        artifact_id=written.artifact_id,
        detail="finished anyway",
    )
    assert completed.status is InterpretationStatus.COMPLETED
    assert completed.artifact_id == written.artifact_id, "the reading was lost"
    assert completed.completed_at is not None

    # And the job is no longer owed a reading, so no cycle re-reads it.
    assert (
        env["store"].eligible_job_for_interpretation(
            project_id="alpha-project", interpreter_version=INTERPRETER_VERSION
        )
        is None
    )


def test_completing_an_interpretation_never_reports_a_reading_it_did_not_record(
    env: dict[str, Any],
) -> None:
    """It used to fall back to a bare re-read, and hand back an ABANDONED row.

    The caller then reported ``interpreted: True`` with ``artifact_id: None``.
    "I could not record this reading" is a fact a caller has to handle, not a
    value to interpret.
    """

    job = _preregistered_job(env, digest="a" * 64, endpoint="A")
    claim, _created = env["store"].claim_interpretation(
        job_id=job.job_id,
        project_id="alpha-project",
        spec_digest=job.spec_digest,
        interpreter_version=INTERPRETER_VERSION,
    )
    context = _context(env)
    mine = context.artifacts.put_text("the first reading")
    theirs = context.artifacts.put_text("a later reading")
    first = env["store"].complete_interpretation(
        claim.interpretation_id, artifact_id=mine.artifact_id
    )
    # A second caller gets the completed row, with the first artifact.
    second = env["store"].complete_interpretation(
        claim.interpretation_id, artifact_id=theirs.artifact_id
    )
    assert second.artifact_id == first.artifact_id == mine.artifact_id
    assert second.completed_at == first.completed_at

    with pytest.raises(RuntimeStateError, match="no such interpretation"):
        env["store"].complete_interpretation("XINT-20260101T000000Z-deadbeef")


# -- selecting and claiming are one transaction ----------------------------
def test_two_workers_do_not_both_take_the_same_job(
    runtime_db: Database, pg_dsn: str
) -> None:
    """Select-then-claim was a check-then-act with a window between the calls.

    Both workers selected the oldest eligible job and both did the whole
    reading; the second one's work was discarded at the end by the unique
    constraint. Nothing incorrect was recorded, and the duplicated work is
    still real.

    Genuinely concurrent, because a sequential pair proves nothing here: the
    first transaction commits before the second begins, so the lock is gone and
    the second worker re-selects the same job on the recovery path. The other
    connection holds a real ``for update`` on the job row for the duration.
    """

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="race", repo_path="/tmp/race")
    run = store.create_run(project_id="race", objective="read them")
    jobs = []
    for index in range(2):
        job = store.create_external_job(
            project_id="race",
            run_id=run.run_id,
            executor="local",
            spec_digest=f"{index:064d}",
            run_dir=f"/tmp/race/{index}",
        )
        store.update_external_job(
            job.job_id, status=ExternalJobStatus.COMPLETED, exit_code=0
        )
        jobs.append(job.job_id)

    with Database(pg_dsn) as other, other.tx() as held:
        # A competing worker, mid-transaction, holding the oldest job.
        locked = held.execute(
            "select job_id from external_jobs where project_id = %s "
            "order by finished_at nulls last, job_id limit 1 for update",
            ("race",),
        ).fetchone()
        assert locked is not None
        taken = store.claim_next_interpretation(
            project_id="race", interpreter_version="probe@1"
        )

    assert taken is not None
    assert taken[0].job_id != str(locked["job_id"]), (
        "the second worker took the job the first one was holding"
    )
    assert taken[0].job_id in jobs
    assert taken[2] is True


def test_an_in_progress_claim_stays_recoverable(runtime_db: Database) -> None:
    """A claim whose worker died must not be stranded.

    Eligibility excludes a *completed* interpretation, not any interpretation,
    so a later worker re-selects the job and gets `created=False` -- which is
    the recovery path and has to keep working now that the select and the claim
    are in one transaction.
    """

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="recover", repo_path="/tmp/recover")
    run = store.create_run(project_id="recover", objective="read it")
    job = store.create_external_job(
        project_id="recover",
        run_id=run.run_id,
        executor="local",
        spec_digest="c" * 64,
        run_dir="/tmp/recover/run",
    )
    store.update_external_job(
        job.job_id, status=ExternalJobStatus.COMPLETED, exit_code=0
    )

    first = store.claim_next_interpretation(
        project_id="recover", interpreter_version="probe@1"
    )
    assert first is not None and first[2] is True

    again = store.claim_next_interpretation(
        project_id="recover", interpreter_version="probe@1"
    )
    assert again is not None
    assert again[0].job_id == job.job_id
    assert again[2] is False
    assert again[1].interpretation_id == first[1].interpretation_id


def test_the_claim_names_the_preregistration_it_was_resolved_against(
    runtime_db: Database,
) -> None:
    """The callback runs between the lock and the insert, once."""

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="bound", repo_path="/tmp/bound")
    run = store.create_run(project_id="bound", objective="read it")
    job = store.create_external_job(
        project_id="bound",
        run_id=run.run_id,
        executor="local",
        spec_digest="d" * 64,
        run_dir="/tmp/bound/run",
    )
    store.update_external_job(
        job.job_id, status=ExternalJobStatus.COMPLETED, exit_code=0
    )
    seen: list[str] = []

    def resolve(found: object) -> str | None:
        seen.append(str(getattr(found, "job_id", "")))
        return None

    taken = store.claim_next_interpretation(
        project_id="bound",
        interpreter_version="probe@1",
        preregistration_for=resolve,
    )

    assert taken is not None
    assert seen == [job.job_id]
