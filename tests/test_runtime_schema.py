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
from research_os.runtime.migrations import current_version, discover, migrate, pending
from research_os.runtime.models import ENUM_CONSTRAINTS

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


def test_an_edited_applied_migration_is_refused(runtime_db: Database) -> None:
    with runtime_db.tx() as conn:
        conn.execute(
            "update schema_migrations set checksum = 'tampered' where version = %s",
            (max(m.version for m in discover()),),
        )
    with pytest.raises(RuntimeDatabaseError, match="different checksum"):
        migrate(runtime_db)


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


def test_a_leased_row_must_have_an_owner_and_a_deadline(runtime_db: Database) -> None:
    """Half a lease is how double execution starts, so the schema forbids it."""

    from research_os.runtime.store import RuntimeStore

    RuntimeStore(runtime_db).upsert_project(project_id="p", repo_path="/tmp/p")
    with pytest.raises(RuntimeDatabaseError), runtime_db.tx() as conn:
        conn.execute(
            "insert into work_items (work_id, project_id, kind, status) "
            "values ('WORK-20260101T000000Z-aaaaaaaa', 'p', 'k', 'LEASED')"
        )
