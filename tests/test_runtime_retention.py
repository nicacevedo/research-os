"""Deleting a run is an operational act. It must not be a scientific one.

A `research_run` is a unit of *work*: a queue entry, a lease, a checkpoint, a
place to hang a budget. An operator reclaiming space by deleting old ones is
doing housekeeping. What hung off those rows, though, was not housekeeping --
the idempotency ledger that says an externally visible effect happened, the
model-call rows that say what it cost, and the links that say which
content-addressed bytes belong to which project. All three cascaded from the
run.

The consequential one was `artifact_links`. The preregistration guard -- the
mechanism that establishes that the criteria a result was read against were
fixed before the result existed -- reached the project *through the run*. So
pruning a run deleted the only link naming its preregistration and the guard
refused that experiment permanently, with the document sitting intact in the
content-addressed store and nothing able to find it.

`0015` gives the three tables their own `project_id` and stops the run from
taking them with it. These tests are the four properties §4.3 of the brief
names, plus the two ways the fix could have been wrong: a delete that fails,
and an erasure that stops erasing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.db import Database
from research_os.runtime.migrations import discover, migrate, pending
from research_os.runtime.models import ModelCallStatus
from research_os.runtime.store import RuntimeStore


def prune(db: Database, run_id: str) -> None:
    """What an operator reclaiming space does. There is no gentler verb."""

    with db.tx() as conn:
        conn.execute("delete from research_runs where run_id = %s", (run_id,))


@pytest.fixture
def pruned(runtime_db: Database, tmp_path: Any) -> dict[str, Any]:
    """One project, one run, and one of each effect the run caused."""

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="retained", repo_path="/tmp/retained")
    run = store.create_run(project_id="retained", objective="measure the thing")

    artifacts = FilesystemArtifactStore(tmp_path / "artifacts", store=store)
    prereg = artifacts.put_text(
        '{"spec_digest": "c0ffee", "primary_endpoint": "wall time"}',
        media_type="application/json",
        role="preregistration:c0ffee",
        producer="test",
    )
    artifacts.link(prereg, role="preregistration:c0ffee", run_id=run.run_id)

    call = store.record_model_call(
        provider="fake",
        role="planner",
        status=ModelCallStatus.OK,
        run_id=run.run_id,
        cost_usd=Decimal("1.25"),
        prompt_version="planner@3",
    )
    return {
        "db": runtime_db,
        "store": store,
        "artifacts": artifacts,
        "run": run,
        "prereg": prereg,
        "call": call,
    }


def rows(db: Database, sql: str, params: Any = ()) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


# -- the four properties the brief names --------------------------------------


def test_pruning_a_run_keeps_the_preregistration_findable(
    pruned: dict[str, Any], tmp_path: Any
) -> None:
    """The defect, as the mechanism that depends on it sees it.

    Asserted through `_preregistration_rows`, the production lookup, rather
    than through a query written here -- the bug was in the lookup's `join`,
    so a test with its own query would have passed throughout.
    """

    from research_os.runtime.actions.experiments import _preregistration_rows

    class _Ctx:
        db = pruned["db"]

    before = _preregistration_rows(_Ctx(), digest="c0ffee", project_id="retained")
    assert before == (pruned["prereg"].artifact_id,)

    prune(pruned["db"], pruned["run"].run_id)

    after = _preregistration_rows(_Ctx(), digest="c0ffee", project_id="retained")
    assert after == before, (
        "pruning a run made the preregistration unreachable, so the guard would "
        "refuse the experiment permanently while the document sat on disk"
    )


def test_pruning_a_run_keeps_the_artifact_link(pruned: dict[str, Any]) -> None:
    """Artifact provenance: which project these bytes belong to, and why."""

    prune(pruned["db"], pruned["run"].run_id)
    [link] = rows(
        pruned["db"],
        "select * from artifact_links where artifact_id = %s",
        (pruned["prereg"].artifact_id,),
    )
    assert link["project_id"] == "retained"
    assert link["role"] == "preregistration:c0ffee"
    assert link["run_id"] == pruned["run"].run_id, (
        "'RUN-x produced this and RUN-x has been pruned' is more provenance "
        "than 'some run, unknown'"
    )


def test_pruning_a_run_keeps_the_idempotency_ledger(
    pruned: dict[str, Any],
) -> None:
    """Side-effect auditability: the claim that an effect happened outlives the run."""

    from research_os.runtime.idempotency import InvocationLedger

    ledger = InvocationLedger(pruned["db"])
    outcome = ledger.run(
        key="probe:once",
        kind="probe",
        run_id=pruned["run"].run_id,
        perform=lambda: {"ok": True, "detail": "did the thing"},
    )
    assert outcome.result["detail"] == "did the thing"

    prune(pruned["db"], pruned["run"].run_id)

    [row] = rows(
        pruned["db"],
        "select * from tool_invocations where idempotency_key = %s",
        ("probe:once",),
    )
    assert row["status"] == "COMPLETED"
    assert row["project_id"] == "retained"
    assert row["run_id"] == pruned["run"].run_id
    # And the ledger still refuses to do it twice, which is the property the
    # table exists for. A pruned run must not become a way to replay an effect.
    again = InvocationLedger(pruned["db"]).run(
        key="probe:once",
        kind="probe",
        perform=lambda: pytest.fail("a completed effect was performed a second time"),
    )
    assert again.result["detail"] == "did the thing"


def test_pruning_a_run_keeps_the_cost_provenance(pruned: dict[str, Any]) -> None:
    """What the researcher was charged, and by whom, for which project."""

    prune(pruned["db"], pruned["run"].run_id)
    [row] = rows(
        pruned["db"],
        "select * from model_calls where call_id = %s",
        (pruned["call"].call_id,),
    )
    assert Decimal(row["cost_usd"]) == Decimal("1.25")
    assert row["project_id"] == "retained"
    assert row["provider"] == "fake"
    assert row["prompt_version"] == "planner@3"


# -- the two ways the fix could have been wrong -------------------------------


def test_pruning_two_runs_that_share_an_artifact_link_does_not_fail(
    runtime_db: Database, tmp_path: Any
) -> None:
    """Why `run_id` keeps its value instead of becoming null.

    `artifact_links_identity_idx` is unique over `(artifact_id,
    coalesce(run_id, ''), coalesce(work_id, ''), role)`. Content addressing
    makes two runs storing identical bytes under one role ordinary -- one
    reused prompt template is enough. `on delete set null` would collapse both
    rows onto the same identity and PostgreSQL would refuse the *second*
    delete with a unique violation, so an operator's prune would fail partway
    through with no obvious cause.
    """

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="shared", repo_path="/tmp/shared")
    first = store.create_run(project_id="shared", objective="one")
    second = store.create_run(project_id="shared", objective="two")

    artifacts = FilesystemArtifactStore(tmp_path / "artifacts", store=store)
    ref = artifacts.put_text("the same bytes", role="prompt", producer="test")
    artifacts.link(ref, role="prompt", run_id=first.run_id)
    artifacts.link(ref, role="prompt", run_id=second.run_id)

    prune(runtime_db, first.run_id)
    prune(runtime_db, second.run_id)

    surviving = rows(
        runtime_db,
        "select run_id from artifact_links where artifact_id = %s order by run_id",
        (ref.artifact_id,),
    )
    assert [row["run_id"] for row in surviving] == sorted([first.run_id, second.run_id])


def test_deleting_a_project_still_erases_all_three(
    runtime_db: Database, tmp_path: Any
) -> None:
    """The intended erasure must keep working.

    Giving these tables their own `project_id` is what makes it *possible* to
    keep them past a run; it must not become a way to keep them past the
    project. A cascading foreign key on the new column is the whole guard, and
    a column added without one would have left orphans nobody can attribute
    and nobody can remove.
    """

    from research_os.runtime.idempotency import InvocationLedger

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="erased", repo_path="/tmp/erased")
    run = store.create_run(project_id="erased", objective="go away")
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts", store=store)
    ref = artifacts.put_text("bytes", role="prompt", producer="test")
    artifacts.link(ref, role="prompt", run_id=run.run_id)
    InvocationLedger(runtime_db).run(
        key="erased:once",
        kind="probe",
        run_id=run.run_id,
        perform=lambda: {"ok": True, "detail": "done"},
    )
    store.record_model_call(
        provider="fake",
        role="planner",
        status=ModelCallStatus.OK,
        run_id=run.run_id,
        cost_usd=Decimal("0.5"),
    )

    with runtime_db.tx() as conn:
        conn.execute("delete from projects where project_id = %s", ("erased",))

    for table in ("artifact_links", "tool_invocations", "model_calls"):
        remaining = rows(
            runtime_db,
            f"select count(*) as n from {table} where project_id = %s",
            ("erased",),
        )
        assert remaining[0]["n"] == 0, f"{table} kept rows for a deleted project"


# -- the upgrade path ----------------------------------------------------------


def test_the_backfill_attributes_rows_written_before_the_column_existed(
    throwaway_dsn: str, tmp_path: Any
) -> None:
    """An existing deployment's rows have to be attributed, not just new ones.

    Applied as a real upgrade: everything up to 0014, rows inserted the way the
    old code inserted them, then 0015. A backfill that only worked on an empty
    table would pass every other test in this file.
    """

    files = discover()
    before = [m for m in files if m.version <= "0014"]
    after = [m for m in files if m.version > "0014"]
    assert after, "this test is pointless once nothing has been added after 0014"

    with Database(throwaway_dsn) as db:
        with db.tx() as conn:
            for migration in before:
                conn.execute(migration.sql)
                conn.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (migration.version, migration.checksum),
                )

        store = RuntimeStore(db)
        store.upsert_project(project_id="legacy", repo_path="/tmp/legacy")
        run = store.create_run(project_id="legacy", objective="written before 0015")
        with db.tx() as conn:
            conn.execute(
                "insert into artifacts (artifact_id, size_bytes, role) "
                "values (%s, %s, %s)",
                ("a" * 64, 3, "preregistration:beef"),
            )
            # Exactly the old insert: no project_id, because the column did not
            # exist when these rows were written.
            conn.execute(
                "insert into artifact_links (artifact_id, run_id, role) "
                "values (%s, %s, %s)",
                ("a" * 64, run.run_id, "preregistration:beef"),
            )
            conn.execute(
                "insert into tool_invocations "
                "(invocation_id, idempotency_key, run_id, kind) "
                "values (%s, %s, %s, %s)",
                ("INV-legacy", "legacy:once", run.run_id, "probe"),
            )
            conn.execute(
                "insert into model_calls (call_id, run_id, provider, role, cost_usd) "
                "values (%s, %s, %s, %s, %s)",
                ("MC-legacy", run.run_id, "fake", "planner", Decimal("2.5")),
            )

        applied = migrate(db)
        assert applied == tuple(m.version for m in after)
        assert pending(db) == ()

        for table, column in (
            ("artifact_links", "artifact_id"),
            ("tool_invocations", "invocation_id"),
            ("model_calls", "call_id"),
        ):
            with db.tx() as conn:
                found = conn.execute(
                    f"select project_id from {table} where {column} is not null"
                ).fetchall()
            assert [row["project_id"] for row in found] == ["legacy"], (
                f"{table} was not backfilled from its run"
            )

        # And the pruning property holds on the upgraded database, which is the
        # only reason the backfill matters.
        prune(db, run.run_id)
        with db.tx() as conn:
            link = conn.execute(
                "select project_id from artifact_links where artifact_id = %s",
                ("a" * 64,),
            ).fetchone()
        assert link is not None and link["project_id"] == "legacy"
