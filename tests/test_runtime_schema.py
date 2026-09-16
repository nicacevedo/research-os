"""The schema exists, migrates once, and agrees with the Python enums.

The agreement test is the important one. Every status column in the runtime
schema is ``text`` with a ``check`` constraint, and the matching Python
``StrEnum`` is what the code actually produces. Two copies of one list is a
drift hazard, so this reads the constraints out of the live catalog and compares
them. A value Python can emit that the database rejects would surface as a crash
in whichever code path was trying to record a failure -- the worst possible
place to find out.
"""

from __future__ import annotations

import re

import pytest

from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.findings import FindingKind, RuntimeFinding
from research_os.runtime.migrations import current_version, discover, migrate, pending
from research_os.runtime.models import ENUM_CONSTRAINTS
from research_os.runtime.store import RuntimeStore

pytestmark = pytest.mark.usefixtures("runtime_db")


def test_the_migration_files_are_discoverable_and_ordered() -> None:
    found = discover()
    assert found, "no migration files found"
    assert [m.version for m in found] == sorted(m.version for m in found)


def test_migrating_twice_applies_nothing_the_second_time(runtime_db: Database) -> None:
    # The session fixture already migrated. A second call is what every process
    # start does, and it must be a no-op rather than an error.
    assert migrate(runtime_db) == ()
    assert pending(runtime_db) == ()
    assert current_version(runtime_db) == max(m.version for m in discover())


def test_an_edited_applied_migration_is_refused(throwaway_dsn: str) -> None:
    """An applied migration must never be edited: two machines would diverge.

    On a throwaway database, because tampering with ``schema_migrations`` on the
    shared one leaks into every later test that calls ``migrate()`` -- the
    fixture truncates the runtime tables and deliberately not that one. It broke
    five CLI tests, visible only once the suites ran in a different order.
    """

    with Database(throwaway_dsn) as db:
        migrate(db)
        with db.tx() as conn:
            conn.execute(
                "update schema_migrations set checksum = 'tampered' where version = %s",
                (max(m.version for m in discover()),),
            )
        with pytest.raises(RuntimeDatabaseError, match="different checksum"):
            migrate(db)


def _constraint_values(runtime_db: Database, name: str) -> frozenset[str]:
    with runtime_db.tx() as conn:
        row = conn.execute(
            "select pg_get_constraintdef(oid) as src from pg_constraint where conname = %s",
            (name,),
        ).fetchone()
    assert row is not None, f"constraint {name} is missing from the schema"
    return frozenset(re.findall(r"'([^']+)'::text", str(row["src"])))


@pytest.mark.parametrize(("name", "expected"), sorted(ENUM_CONSTRAINTS.items()))
def test_each_status_constraint_matches_its_python_enum(
    runtime_db: Database, name: str, expected: frozenset[str]
) -> None:
    assert _constraint_values(runtime_db, name) == expected


def test_every_value_list_constraint_in_the_database_is_mirrored(
    runtime_db: Database,
) -> None:
    """The other direction, which is the one that lets a constraint escape.

    The parametrized test above proves that each constraint *named* in
    ``ENUM_CONSTRAINTS`` matches its enum. It says nothing about a constraint
    that is in the schema and not in the dictionary, so a migration adding
    ``check (kind in ('a','b'))`` with three string literals in Python and no
    enum passed a green suite. An adversarial review found two that had:
    ``runtime_finding_refs_kind_ck`` and
    ``runtime_proposal_reservations_status_ck``, both added by
    ``0007_runtime_findings.sql``.

    So this asks the live database what value-list constraints it has, and
    requires every one of them to be mirrored. A new one fails here until
    somebody writes the enum -- which is the point, because the enum is what
    stops Python and PostgreSQL disagreeing about a closed set.
    """

    with runtime_db.tx() as conn:
        rows = conn.execute(
            """
            select conname as name, pg_get_constraintdef(oid) as src
            from pg_constraint
            where contype = 'c' and conname like %s
            """,
            ("%_ck",),
        ).fetchall()
    # A value-list constraint is one whose definition enumerates string
    # literals. `check (source_cycle >= 0)` and the lease-shape constraints do
    # not, and there is no enum for them to mirror.
    found = {
        str(row["name"])
        for row in rows
        if re.search(r"= ANY \(ARRAY\[", str(row["src"]))
        or re.search(r" IN \('", str(row["src"]))
    }
    unmirrored = found - set(ENUM_CONSTRAINTS)
    assert unmirrored == set(), (
        f"these check constraints enumerate values with no Python enum "
        f"mirroring them: {sorted(unmirrored)}. Add the enum and the "
        f"ENUM_CONSTRAINTS entry, so the two definitions cannot drift."
    )
    # And the dictionary must not name a constraint the database does not have,
    # which is how an entry survives the table it described being dropped.
    assert set(ENUM_CONSTRAINTS) - found == set()


def test_a_leased_row_must_have_an_owner_and_a_deadline(runtime_db: Database) -> None:
    """Half a lease is how double execution starts, so the schema forbids it."""

    from research_os.runtime.store import RuntimeStore

    RuntimeStore(runtime_db).upsert_project(project_id="p", repo_path="/tmp/p")
    with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
        conn.execute(
            "insert into work_items (work_id, project_id, kind, status) "
            "values ('WORK-20260101T000000Z-aaaaaaaa', 'p', 'k', 'LEASED')"
        )


def test_an_upgrade_over_a_database_the_race_already_happened_in_says_what_to_do(
    throwaway_dsn: str,
) -> None:
    """0014 cannot build its index over rows that already violate it.

    The race `lock_run` failed to serialise was real, so an upgraded database
    may already hold two runs naming one parent. PostgreSQL's own message for
    that is `could not create unique index`, with a DETAIL naming one key and no
    instruction -- on a forward-only migration the operator cannot edit. The
    migration finds the duplicates first and names them.
    """

    from research_os.runtime.db import Database

    files = discover()
    before = [m for m in files if m.version < "0014"]
    with Database(throwaway_dsn) as db:
        with db.tx() as conn:
            conn.execute(
                "create table if not exists schema_migrations ("
                "version text primary key, checksum text not null, "
                "applied_at timestamptz not null default now())"
            )
            for migration in before:
                conn.execute(migration.sql)
                conn.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (migration.version, migration.checksum),
                )
        store = RuntimeStore(db)
        store.upsert_project(project_id="raced", repo_path="/tmp/raced")
        parent = store.create_run(project_id="raced", objective="advance me")
        # Two successors, which only the *absent* index made possible. Written
        # through the store, so this is the state the old code really produced.
        for _ in range(2):
            store.create_run(
                project_id="raced",
                objective="advance me",
                parent_run_id=parent.run_id,
                cycle_index=1,
            )

        with pytest.raises(RuntimeDatabaseError) as raised:
            migrate(db)

    message = str(raised.value)
    assert "more than one successor" in message
    assert parent.run_id in message, "the message must name the run to look at"
    assert "delete the" in message


def test_deleting_a_run_does_not_delete_the_interpretations_of_its_experiments(
    runtime_db: Database,
) -> None:
    """A run is a unit of work; an interpretation is a scientific reading.

    ``external_jobs.run_id`` cascaded, and ``experiment_interpretations.job_id``
    cascades from ``external_jobs``, so pruning one old run destroyed the record
    that its experiments had been interpreted at all. See
    ``sql/0013_external_job_run_link.sql``.
    """

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="pruned", repo_path="/tmp/pruned")
    run = store.create_run(project_id="pruned", objective="prune me")
    job = store.create_external_job(
        project_id="pruned",
        run_id=run.run_id,
        executor="local",
        spec_digest="b" * 64,
        run_dir="/tmp/pruned/run",
    )
    claim, _created = store.claim_interpretation(
        job_id=job.job_id,
        project_id="pruned",
        spec_digest=job.spec_digest,
        interpreter_version="probe@1",
        run_id=run.run_id,
    )

    with runtime_db.tx() as conn:
        conn.execute("delete from research_runs where run_id = %s", (run.run_id,))

    survived = store.get_interpretation(
        job_id=job.job_id, interpreter_version="probe@1"
    )
    assert survived is not None, "deleting a run destroyed an interpretation record"
    assert survived.interpretation_id == claim.interpretation_id
    kept = store.get_external_job(job.job_id)
    assert kept is not None and kept.run_id is None


def test_the_declared_schema_version_matches_the_highest_migration() -> None:
    """A constant that drifts from the files it describes is worse than no constant."""

    from research_os.runtime import RUNTIME_SCHEMA_VERSION

    assert RUNTIME_SCHEMA_VERSION == max(m.version for m in discover())


def test_migrations_compose_from_an_empty_database(throwaway_dsn: str) -> None:
    """Every migration applied in order, on a database that has seen none.

    The session fixture migrates once and every later test inherits that, so
    without this the later migrations would only ever be exercised as upgrades.
    A fresh clone is the more common case.
    """

    with Database(throwaway_dsn) as probe:
        applied = migrate(probe)
        assert applied == tuple(m.version for m in discover())
        assert pending(probe) == ()
        with probe.tx() as conn:
            indexes = conn.execute(
                "select indexname from pg_indexes where tablename = 'artifact_links'"
            ).fetchall()
        assert any(row["indexname"] == "artifact_links_identity_idx" for row in indexes)


def test_the_new_migrations_apply_as_an_upgrade_over_the_previous_release(
    throwaway_dsn: str,
) -> None:
    """The case an existing deployment actually hits.

    ``test_migrations_compose_from_an_empty_database`` covers a fresh clone, and
    the session fixture applies everything at once — so without this, a
    migration added after a release is only ever exercised on a database that
    has seen nothing. An upgrade is different: the tables the new files
    reference already exist, already have rows, and already have constraints.

    So this applies the *previous* release's schema, puts rows in it, and then
    migrates the rest. A new foreign key that cannot be satisfied by existing
    data, or a new unique index that existing rows violate, fails here and
    nowhere else.
    """

    from research_os.runtime.db import Database
    from research_os.runtime.migrations import discover, sql_dir

    files = discover()
    previous = [m for m in files if m.version <= "0005"]
    added = [m for m in files if m.version > "0005"]
    assert previous, "the previous release's migrations are missing"
    assert added, "this test is pointless once nothing has been added"

    with Database(throwaway_dsn) as db:
        # The previous release, applied the way it was applied.
        with db.tx() as conn:
            for migration in previous:
                conn.execute(migration.sql)
                conn.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (migration.version, migration.checksum),
                )

        # Rows, because an empty table satisfies any constraint.
        store = RuntimeStore(db)
        store.upsert_project(project_id="upgraded", repo_path="/tmp/upgraded")
        run = store.create_run(project_id="upgraded", objective="before the upgrade")
        job = store.create_external_job(
            project_id="upgraded",
            run_id=run.run_id,
            executor="local",
            spec_digest="a" * 64,
            run_dir="/tmp/upgraded/run",
        )

        # Now the upgrade.
        applied = migrate(db)
        assert applied == tuple(m.version for m in added), (
            f"expected to apply {[m.version for m in added]}, applied {applied}"
        )
        assert pending(db) == ()

        # And the new relations work against the pre-existing rows, which is
        # the whole point: a foreign key to `external_jobs` that the upgrade
        # path cannot satisfy would pass every other test in this file.
        interpretation, created = store.claim_interpretation(
            job_id=job.job_id,
            project_id="upgraded",
            spec_digest=job.spec_digest,
            interpreter_version="upgrade-probe@1",
            run_id=run.run_id,
        )
        assert created is True
        assert interpretation.job_id == job.job_id

        finding, created = store.record_finding(
            RuntimeFinding(
                project_id="upgraded",
                kind=FindingKind.FRONTIER,
                summary="a finding recorded after the upgrade",
                source_run_id=run.run_id,
                experiment_job_id=job.job_id,
            )
        )
        assert created is True
        store.link_proposal_findings(
            proposal_id="PROP-19700101T000000Z-aaaaaaaa",
            finding_ids=(finding.finding_id,),
            run_id=run.run_id,
        )
        store.link_nomination_findings(
            nomination_id="NOM-19700101T000000Z-aaaaaaaa",
            finding_ids=(finding.finding_id,),
            project_id="upgraded",
            run_id=run.run_id,
        )
        changed, _previous_capsule, _previous_frontier = store.observe_capsule(
            project_id="upgraded", capsule_digest="d" * 64, frontier_digest="e" * 64
        )
        assert changed is False, "a first observation is not a change"
    del sql_dir


def test_deleting_a_project_takes_its_derived_rows_with_it(
    runtime_db: Database,
) -> None:
    """Operational state is disposable; it must not outlive what it describes.

    A finding, an interpretation or an observation belonging to a project that
    no longer exists is a row nothing can render and nothing can trace. Each of
    the new tables therefore cascades from ``projects``, and this asserts it
    rather than trusting the DDL was written the way it reads.
    """

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="doomed", repo_path="/tmp/doomed")
    run = store.create_run(project_id="doomed", objective="o")
    job = store.create_external_job(
        project_id="doomed",
        run_id=run.run_id,
        executor="local",
        spec_digest="a" * 64,
        run_dir="/tmp/doomed/run",
    )
    store.claim_interpretation(
        job_id=job.job_id,
        project_id="doomed",
        spec_digest=job.spec_digest,
        interpreter_version="probe@1",
    )
    finding, _created = store.record_finding(
        RuntimeFinding(
            project_id="doomed",
            kind=FindingKind.FRONTIER,
            summary="about a project that is about to go",
        )
    )
    store.link_nomination_findings(
        nomination_id="NOM-19700101T000000Z-bbbbbbbb",
        finding_ids=(finding.finding_id,),
        project_id="doomed",
    )
    store.observe_capsule(
        project_id="doomed", capsule_digest="d" * 64, frontier_digest="e" * 64
    )

    with runtime_db.tx() as conn:
        conn.execute("delete from projects where project_id = %s", ("doomed",))
        for table in (
            "runtime_findings",
            "runtime_nomination_links",
            "experiment_interpretations",
            "capsule_observations",
        ):
            remaining = conn.execute(
                f"select count(*) as n from {table} where project_id = %s", ("doomed",)
            ).fetchone()
            assert remaining is not None and remaining["n"] == 0, (
                f"{table} kept rows for a deleted project"
            )
