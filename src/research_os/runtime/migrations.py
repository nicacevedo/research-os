"""Forward-only schema migrations, applied under an advisory lock.

**Why not Alembic.** Alembic is the conventional answer and it was the first
one tried. It requires SQLAlchemy, which is larger than every other dependency
this runtime adds put together, and it would be carried by a project whose
stated condition for adopting a technology is that it "brought no dependency of
any size". Its autogeneration works from SQLAlchemy models, and this runtime has
none: the schema is hand-written SQL because the interesting parts of it --
partial indexes, the lease check constraint, ``for update skip locked`` --
are things an ORM abstracts away and this runtime depends on precisely.

What is left once autogeneration is gone is a numbered list of SQL files applied
in order and recorded in a table. That is roughly what Flyway is, it is eighty
lines, and it is written out here rather than depended upon.

**What it guarantees.** One migration at a time across every process, by
``pg_advisory_xact_lock`` taken before anything is read. *All* pending files run
inside one transaction -- not one each, as an earlier version of this paragraph
claimed -- so a failure in the third leaves the database at the version it
started from rather than part-way through. The trade is that one long DDL
transaction holds the lock for the duration of every pending migration, which
for this deployment is a second.

Each applied file's checksum is stored, so editing a migration that has already
run is an error at startup rather than a difference between two machines nobody
notices.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from research_os.runtime.db import Database, RuntimeDatabaseError

LOG = logging.getLogger("research_os.runtime.migrations")

#: Namespace for every advisory lock this runtime takes. Chosen once, never
#: computed, so two different lock purposes cannot collide by hashing alike.
ADVISORY_NAMESPACE = 0x52_4F_53_00  # "ROS\0"
MIGRATION_LOCK_ID = 1


def sql_dir() -> Path:
    return Path(__file__).parent / "sql"


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover() -> tuple[Migration, ...]:
    """Return every migration on disk, in version order."""

    found: list[Migration] = []
    for path in sorted(sql_dir().glob("*.sql")):
        version, _, name = path.stem.partition("_")
        if not version.isdigit():
            raise RuntimeDatabaseError(
                f"{path.name}: migration files must be named <digits>_<name>.sql"
            )
        found.append(
            Migration(
                version=version,
                name=name or path.stem,
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise RuntimeDatabaseError(f"duplicate migration versions: {sorted(versions)}")
    return tuple(found)


def _applied(conn: object) -> dict[str, str]:
    cur = conn.execute(  # type: ignore[attr-defined]
        # Search-path relative, not hardcoded to `public`. Under a different
        # search_path the qualified form returns NULL forever, so every startup
        # would retry every migration and fail on the primary key.
        "select to_regclass('schema_migrations') as present"
    ).fetchone()
    if not cur or cur["present"] is None:
        return {}
    rows = conn.execute(  # type: ignore[attr-defined]
        "select version, checksum from schema_migrations"
    ).fetchall()
    return {row["version"]: row["checksum"] for row in rows}


def current_version(db: Database) -> str | None:
    """Return the highest applied migration version, or ``None``."""

    with db.tx() as conn:
        applied = _applied(conn)
    return max(applied) if applied else None


def pending(db: Database) -> tuple[Migration, ...]:
    """Return the migrations that have not been applied yet."""

    with db.tx() as conn:
        applied = _applied(conn)
    return tuple(m for m in discover() if m.version not in applied)


def migrate(db: Database) -> tuple[str, ...]:
    """Apply every pending migration. Returns the versions applied, in order.

    Safe to call from every process at every startup: the advisory lock makes
    concurrent callers serialise, and the second one finds nothing to do.
    """

    applied_now: list[str] = []
    migrations = discover()
    with db.tx() as conn:
        conn.execute(
            "select pg_advisory_xact_lock(%s, %s)",
            (ADVISORY_NAMESPACE, MIGRATION_LOCK_ID),
        )
        already = _applied(conn)
        for migration in migrations:
            stored = already.get(migration.version)
            if stored is not None:
                if stored != migration.checksum:
                    raise RuntimeDatabaseError(
                        f"migration {migration.version} ({migration.name}) was applied "
                        f"with a different checksum. An applied migration must never be "
                        f"edited; add a new one instead."
                    )
                continue
            LOG.info("applying migration %s (%s)", migration.version, migration.name)
            conn.execute(migration.sql)
            conn.execute(
                "insert into schema_migrations (version, checksum) values (%s, %s)",
                (migration.version, migration.checksum),
            )
            applied_now.append(migration.version)
    return tuple(applied_now)
